import os
import asyncio
from datetime import datetime, timezone, timedelta
import aiosqlite
from aiohttp import web, ClientSession
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ================================
# 設定・定数
# ================================
TOKEN = os.getenv("DISCORD_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 8080))
DB_NAME = "reminders.db"

# 日本標準時 (JST)
JST = timezone(timedelta(hours=9))

# 理由返信が不要なリアクション絵文字
VALID_REACTIONS = {"⭕"}

DEFAULT_DM_MESSAGE = (
    "【リマインド】\n"
    "定例会の出席確認へのリアクションによる出欠調査がされていません。ご確認ください。\n"
    "※欠席・保留等の場合は、指定のリアクションをした上でスレッド内に理由の返信をお願いします。"
)

# Discord Intents
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ================================
# 1. データベース操作
# ================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                channel_id INTEGER,
                message_id INTEGER,
                role_id INTEGER,
                due_timestamp REAL,
                custom_message TEXT,
                report_user_id INTEGER
            )
        """)
        await db.commit()

async def add_reminder(guild_id, channel_id, message_id, role_id, due_ts, msg, report_user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO reminders 
            (guild_id, channel_id, message_id, role_id, due_timestamp, custom_message, report_user_id) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (guild_id, channel_id, message_id, role_id, due_ts, msg, report_user_id)
        )
        await db.commit()

async def remove_reminder(reminder_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        await db.commit()

# ================================
# 2. 定期チェックスケジューラー
# ================================
@tasks.loop(minutes=1)
async def check_reminders():
    now_ts = datetime.now(timezone.utc).timestamp()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, guild_id, channel_id, message_id, role_id, custom_message, report_user_id FROM reminders WHERE due_timestamp <= ?",
            (now_ts,)
        ) as cursor:
            due_tasks = await cursor.fetchall()

    for r_id, guild_id, channel_id, msg_id, role_id, custom_msg, report_user_id in due_tasks:
        guild = bot.get_guild(guild_id)
        if not guild:
            await remove_reminder(r_id)
            continue

        channel = guild.get_channel(channel_id)
        if not channel:
            await remove_reminder(r_id)
            continue

        try:
            target_message = await channel.fetch_message(msg_id)
        except (discord.NotFound, discord.Forbidden):
            await remove_reminder(r_id)
            continue

        # 判定A: リアクション状況の集計
        valid_reacted_user_ids = set()
        other_reacted_user_ids = set()

        for reaction in target_message.reactions:
            emoji_str = str(reaction.emoji)
            async for user in reaction.users():
                if user.bot:
                    continue
                if emoji_str in VALID_REACTIONS:
                    valid_reacted_user_ids.add(user.id)
                else:
                    other_reacted_user_ids.add(user.id)

        # 判定B: スレッド内の返信者集計
        thread_replied_user_ids = set()
        thread = target_message.thread
        if not thread:
            for active_thread in channel.threads:
                if active_thread.id == target_message.id:
                    thread = active_thread
                    break

        if thread:
            try:
                async for thread_msg in thread.history(limit=500):
                    if not thread_msg.author.bot:
                        thread_replied_user_ids.add(thread_msg.author.id)
            except discord.Forbidden:
                pass

        # 判定C: 催促対象メンバーの抽出
        if role_id:
            role = guild.get_role(role_id)
            target_members = [m for m in role.members if not m.bot] if role else []
        else:
            target_members = [m for m in guild.members if not m.bot]

        unreacted_members = []
        for member in target_members:
            if member.id in valid_reacted_user_ids:
                continue
            if member.id in other_reacted_user_ids and member.id in thread_replied_user_ids:
                continue
            unreacted_members.append(member)

        # 判定D: DM送信（全体通知は一切行わない）
        success_count = 0
        failed_names = []
        dm_content = f"{custom_msg}\n\n対象メッセージ: {target_message.jump_url}"

        for member in unreacted_members:
            try:
                await member.send(dm_content)
                success_count += 1
                await asyncio.sleep(0.5)
            except discord.Forbidden:
                failed_names.append(member.display_name)

        # 判定E: 指定者のDMにのみ最終レポートを送信
        report_user = bot.get_user(report_user_id) or await bot.fetch_user(report_user_id)
        if report_user:
            report_lines = [
                "📋 **【出欠リマインド 実行結果レポート】**",
                f"- **対象メッセージ**: {target_message.jump_url}",
                f"- **催促対象人数**: {len(unreacted_members)}人",
                f"- **DM送信成功**: {success_count}人",
                f"- **DM送信失敗（DM拒否）**: {len(failed_names)}人"
            ]
            if failed_names:
                report_lines.append(f"- **未達者一覧**: {', '.join(failed_names)}")

            try:
                await report_user.send("\n".join(report_lines))
            except discord.Forbidden:
                print(f"レポート受信先ユーザー（ID: {report_user_id}）へのDM送信に失敗しました。")

        await remove_reminder(r_id)

# ================================
# 3. Render スリープ防止 (Self-Ping)
# ================================
async def handle_health_check(request):
    return web.Response(text="Bot is running!")

@tasks.loop(minutes=10)
async def keep_alive_ping():
    if not RENDER_EXTERNAL_URL:
        return
    try:
        async with ClientSession() as session:
            async with session.get(RENDER_EXTERNAL_URL) as resp:
                print(f"[{datetime.now(JST).strftime('%H:%M:%S')}] Self-ping: Status {resp.status}")
    except Exception as e:
        print(f"Self-ping failed: {e}")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server ready on port {PORT}")

# ================================
# 4. イベント & スラッシュコマンド
# ================================
@bot.event
async def on_ready():
    await init_db()
    if not check_reminders.is_running():
        check_reminders.start()
    if not keep_alive_ping.is_running():
        keep_alive_ping.start()
    await bot.tree.sync()
    print(f"起動完了: {bot.user.name}")

# リマインダー登録コマンド
@bot.tree.command(name="set_reminder", description="指定日時に未リアクション・未返信者へDMで催促します")
@app_commands.describe(
    message_id="対象メッセージのID",
    due_datetime="期限の日時（例: 2026-09-10 18:00）",
    role="催促対象のロール（省略時は全員）",
    report_recipient="最終結果レポートをDMで受け取る人（省略時はコマンド実行者本人）",
    custom_message="催促時のテキスト（省略時はデフォルト定型文）"
)
async def set_reminder(
    interaction: discord.Interaction,
    message_id: str,
    due_datetime: str,
    role: discord.Role = None,
    report_recipient: discord.User = None,
    custom_message: str = DEFAULT_DM_MESSAGE
):
    try:
        msg_id = int(message_id)
    except ValueError:
        await interaction.response.send_message("メッセージIDは半角数字で指定してください。", ephemeral=True)
        return

    try:
        naive_dt = datetime.strptime(due_datetime, "%Y-%m-%d %H:%M")
        target_jst = naive_dt.replace(tzinfo=JST)
        now_jst = datetime.now(JST)

        if target_jst <= now_jst:
            await interaction.response.send_message("現在時刻より未来の日時を指定してください。", ephemeral=True)
            return
    except ValueError:
        await interaction.response.send_message(
            "日時の形式が正しくありません。\n例: `2026-09-10 18:00` のように入力してください。",
            ephemeral=True
        )
        return

    try:
        msg = await interaction.channel.fetch_message(msg_id)
    except (discord.NotFound, discord.Forbidden):
        await interaction.response.send_message(
            "このチャンネル内で指定のメッセージが見つかりませんでした。",
            ephemeral=True
        )
        return

    target_report_user = report_recipient if report_recipient else interaction.user
    role_id = role.id if role else None

    await add_reminder(
        interaction.guild_id,
        interaction.channel_id,
        msg.id,
        role_id,
        target_jst.astimezone(timezone.utc).timestamp(),
        custom_message,
        target_report_user.id
    )

    await interaction.response.send_message(
        f"リマインダーをセットしました。\n"
        f"- **期限**: {target_jst.strftime('%Y年%m月%d日 %H:%M')} (JST)\n"
        f"- **対象ロール**: {role.mention if role else '全員'}\n"
        f"- **レポート送信先**: {target_report_user.mention} のDM\n"
        f"- **対象メッセージ**: {msg.jump_url}",
        ephemeral=True
    )

# 登録中リマインダー一覧確認コマンド
@bot.tree.command(name="list_reminders", description="現在登録されているリマインダーの一覧を表示します")
async def list_reminders(interaction: discord.Interaction):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, channel_id, message_id, due_timestamp, custom_message FROM reminders WHERE guild_id = ? ORDER BY due_timestamp ASC",
            (interaction.guild_id,)
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        await interaction.response.send_message("現在登録されているリマインダーはありません。", ephemeral=True)
        return

    text_lines = ["📋 **【現在登録中のリマインダー一覧】**"]
    for r_id, ch_id, msg_id, due_ts, msg in rows:
        due_dt = datetime.fromtimestamp(due_ts, tz=timezone.utc).astimezone(JST)
        due_str = due_dt.strftime("%Y/%m/%d %H:%M")
        jump_url = f"https://discord.com/channels/{interaction.guild_id}/{ch_id}/{msg_id}"
        text_lines.append(
            f"**[ID: {r_id}]** 期限: **{due_str}**\n"
            f"- メッセージ: {jump_url}\n"
            f"- 内容: {msg[:30]}..."
        )

    await interaction.response.send_message("\n\n".join(text_lines), ephemeral=True)

# 登録済みリマインダー取り消しコマンド
@bot.tree.command(name="cancel_reminder", description="登録済みのリマインダーを取り消します")
@app_commands.describe(reminder_id="取り消したいリマインダーのID（list_remindersで確認可能）")
async def cancel_reminder(interaction: discord.Interaction, reminder_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id FROM reminders WHERE id = ? AND guild_id = ?",
            (reminder_id, interaction.guild_id)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message(f"ID: `{reminder_id}` のリマインダーは見つかりませんでした。", ephemeral=True)
            return

        await db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        await db.commit()

    await interaction.response.send_message(f"リマインダー（ID: `{reminder_id}`）を取り消しました。", ephemeral=True)

# ================================
# 起動
# ================================
async def main():
    async with bot:
        await start_web_server()
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

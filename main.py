import asyncio
import os
import time
import discord
from discord.ext import commands
from TikTokLive import TikTokLiveClient
from TikTokLive.events import GiftEvent, ConnectEvent, DisconnectEvent
from TikTokLive.client.errors import UserOfflineError
from keep_alive import keep_alive

from db import init_db, save_gift_data, get_discord_id_from_tiktok, get_db_connection

# Start Flask Server
keep_alive()

# Configuration (1 Akun per Repl)
TARGET_TIKTOK_USER = "clarissadreams"
DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
ANNOUNCEMENT_CHANNEL_ID = int(os.environ.get('ANNOUNCEMENT_CHANNEL_ID', "0"))

GUILD_IDS = [762924058655457291, 987654321098765432]
ROLE_TIERS = {
    762924058655457291: [
        (100, 1554883561759187105),
        (500, 1554883677031370902),
        (1000, 1554883726989594725),
    ],
    987654321098765432: [
        (100, 555555555555555555),
        (500, 666666666666666666),
        (1000, 777777777777777777),
    ]
}
STACK_ROLES = False

# Global Trackers
STARTUP_TIME = time.time()
LAST_GIFT_TIMESTAMP = {TARGET_TIKTOK_USER.lower(): STARTUP_TIME}
IS_LIVE_NOTIFIED = {TARGET_TIKTOK_USER.lower(): False}

# Discord Bot Setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Automatic Role Assignment Helper
async def auto_assign_role(tiktok_user: str, total_coins: int):
    discord_id = get_discord_id_from_tiktok(tiktok_user)
    if not discord_id:
        return

    for guild_id in GUILD_IDS:
        guild = bot.get_guild(guild_id)
        if not guild or guild_id not in ROLE_TIERS:
            continue

        try:
            member = await guild.fetch_member(discord_id)
        except discord.NotFound:
            continue

        tiers = ROLE_TIERS[guild_id]
        roles_to_add = []
        roles_to_remove = []

        if STACK_ROLES:
            for min_coins, role_id in tiers:
                role = guild.get_role(role_id)
                if role and total_coins >= min_coins and role not in member.roles:
                    roles_to_add.append(role)
        else:
            highest_eligible_role = None
            all_tier_roles = []

            for min_coins, role_id in tiers:
                role = guild.get_role(role_id)
                if role:
                    all_tier_roles.append(role)
                    if total_coins >= min_coins:
                        highest_eligible_role = role

            if highest_eligible_role:
                if highest_eligible_role not in member.roles:
                    roles_to_add.append(highest_eligible_role)

                for role in all_tier_roles:
                    if role != highest_eligible_role and role in member.roles:
                        roles_to_remove.append(role)

        if roles_to_add:
            await member.add_roles(*roles_to_add)
            role_names = ", ".join([r.name for r in roles_to_add])
            print(f"[Discord] Assigned role {role_names} to {member.display_name} in '{guild.name}'!")

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
            role_names = ", ".join([r.name for r in roles_to_remove])
            print(f"[Discord] Removed previous tier role ({role_names}) from {member.display_name} in '{guild.name}'.")

# Live-share / Announcement Helpers
async def send_live_announcement(username: str):
    if not ANNOUNCEMENT_CHANNEL_ID:
        return
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not channel:
        return

    live_url = f"https://www.tiktok.com/@{username}/live"
    try:
        msg = await channel.send(f"🔴 **@{username}** sedang LIVE di TikTok!\nTonton di sini: {live_url}")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO active_live_announcements (tiktok_username, channel_id, message_id) VALUES (?, ?, ?)",
            (username.lower(), channel.id, msg.id)
        )
        conn.commit()
    except Exception as e:
        print(f"[Discord Error] Failed to send announcement: {e}")

async def delete_live_announcement(username: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT channel_id, message_id FROM active_live_announcements WHERE tiktok_username = ?", (username.lower(),))
    row = cursor.fetchone()

    if row:
        channel_id, message_id = row[0], row[1]
        try:
            channel = bot.get_channel(channel_id)
            if channel:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
        except Exception:
            pass
        cursor.execute("DELETE FROM active_live_announcements WHERE tiktok_username = ?", (username.lower(),))
        conn.commit()

# TikTok Client Setup
def create_tiktok_client(username: str) -> TikTokLiveClient:
    client = TikTokLiveClient(unique_id=username.lower())

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        user_key = username.lower()
        print(f"[@{username}] Connected to TikTok Live!")
        if not IS_LIVE_NOTIFIED.get(user_key, False):
            await send_live_announcement(username)
            IS_LIVE_NOTIFIED[user_key] = True

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        user_key = username.lower()
        print(f"[@{username}] Disconnected from TikTok Live!")
        IS_LIVE_NOTIFIED[user_key] = False
        await delete_live_announcement(username)

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        is_streakable = getattr(event.gift, "streakable", False)
        is_repeat_end = getattr(event, "repeat_end", True)

        if not is_streakable or is_repeat_end:
            user_name = event.user.unique_id.lower()
            gift_name = getattr(event.gift, "name", "gift")

            diamond_count = getattr(event.gift, "diamond_count", 0)
            repeat_count = getattr(event, "repeat_count", 1)
            coins = diamond_count * repeat_count

            if coins > 0:
                LAST_GIFT_TIMESTAMP[username.lower()] = time.time()
                total_coins = save_gift_data(user_name, coins)
                print(f"[gift - @{client.unique_id}] {user_name} sent {gift_name} ({coins} c); total : {total_coins}")
                await auto_assign_role(user_name, total_coins)

    return client

async def run_tiktok_client_with_standby(username: str):
    user_key = username.lower()
    retry_delay = 180  # Cooldown 3 menit jika offline

    while True:
        client = create_tiktok_client(username)
        try:
            print(f"[@{username}] Checking live status...")
            is_currently_live = await client.is_live()

            if not is_currently_live:
                IS_LIVE_NOTIFIED[user_key] = False
                await delete_live_announcement(username)
                print(f"[@{username}] Streamer is OFFLINE. Standby for 3 minutes...")
                await asyncio.sleep(retry_delay)
                continue

            print(f"[@{username}] Streamer is LIVE! Connecting...")
            await client.start()

        except UserOfflineError:
            IS_LIVE_NOTIFIED[user_key] = False
            await delete_live_announcement(username)
            print(f"[@{username}] Streamer is offline (UserOfflineError). Retry in 3 minutes...")
            await asyncio.sleep(retry_delay)

        except Exception as e:
            error_msg = str(e).lower()
            try:
                await client.disconnect()
            except Exception:
                pass

            last_gift = LAST_GIFT_TIMESTAMP.get(user_key, time.time())
            seconds_since_last_gift = time.time() - last_gift
            is_high_traffic = seconds_since_last_gift <= 180

            if "rate_limit" in error_msg or "too many connections" in error_msg or "429" in error_msg:
                cooldown = 120 if is_high_traffic else 300
                print(f"[@{username}] Rate limit detected! Cooldown {cooldown}s...")
                await asyncio.sleep(cooldown)
            elif "offline" in error_msg or "not live" in error_msg:
                IS_LIVE_NOTIFIED[user_key] = False
                await delete_live_announcement(username)
                print(f"[@{username}] Streamer offline. Cooldown 3 minutes...")
                await asyncio.sleep(retry_delay)
            else:
                cooldown = 30 if is_high_traffic else 60
                print(f"[@{username}] Disconnected: {e} (retry in {cooldown}s)")
                await asyncio.sleep(cooldown)

# Discord Commands
@bot.event
async def on_ready():
    print(f"[Discord] Bot logged in as {bot.user}")

@bot.command(name="claim")
async def claim(ctx, tiktok_user: str):
    tiktok_user_clean = tiktok_user.lstrip("@").lower()
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
    existing_discord_claim = cursor.fetchone()
    if existing_discord_claim:
        await ctx.send(f"❌ You have already claimed TikTok account `@{existing_discord_claim[0]}`.")
        return

    cursor.execute("SELECT discord_id FROM user_mappings WHERE tiktok_user = ?", (tiktok_user_clean,))
    existing_tiktok_claim = cursor.fetchone()
    if existing_tiktok_claim:
        await ctx.send(f"❌ TikTok username `@{tiktok_user_clean}` has already been claimed.")
        return

    cursor.execute("INSERT INTO user_mappings (discord_id, tiktok_user) VALUES (?, ?)", (ctx.author.id, tiktok_user_clean))
    conn.commit()

    cursor.execute("SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (tiktok_user_clean,))
    row = cursor.fetchone()
    total_coins = row[0] if row else 0

    await ctx.send(
        f"✅ Linked Discord account {ctx.author.mention} to TikTok `@{tiktok_user_clean}`.\n"
        f"Current recorded total: **{total_coins} c**."
    )
    await auto_assign_role(tiktok_user_clean, total_coins)

@bot.command(name="mycoins", aliases=["check", "coins"])
async def mycoins(ctx):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
    mapping = cursor.fetchone()

    if not mapping:
        await ctx.send(f"❌ {ctx.author.mention}, you haven't linked a TikTok account yet! Use `!claim @your_tiktok_username` first.")
        return

    tiktok_user = mapping[0]
    cursor.execute("SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (tiktok_user,))
    gift_data = cursor.fetchone()
    total_coins = gift_data[0] if gift_data else 0

    await ctx.send(
        f"📊 **Coin Balance for {ctx.author.display_name}:**\n"
        f"• TikTok Account: `@{tiktok_user}`\n"
        f"• Total Coins: **{total_coins} c**"
    )

@bot.command(name="topdonator")
async def topdonator(ctx):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT tiktok_user, total_coins FROM tiktok_gifts ORDER BY total_coins DESC LIMIT 5")
    rows = cursor.fetchall()

    if not rows:
        await ctx.send("No gift data recorded yet.")
        return

    response = "**🏆 Top 5 TikTok Live Donators:**\n"
    for idx, (user, coins) in enumerate(rows, start=1):
        response += f"{idx}. `@{user}` - {coins} c\n"

    await ctx.send(response)

async def main():
    init_db()
    asyncio.create_task(run_tiktok_client_with_standby(TARGET_TIKTOK_USER))
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
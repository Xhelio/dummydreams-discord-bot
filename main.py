import asyncio
import sqlite3
import os
import discord
from discord.ext import commands
from TikTokLive import TikTokLiveClient
from TikTokLive.events import GiftEvent
from keep_alive import keep_alive

# Start Flask Web Server for UptimeRobot
keep_alive()

# ==========================================
# 1. CONFIGURATION: BOT, TIKTOK, & ROLE TIERS
# ==========================================
TIKTOK_USERNAMES = [
    "clarissadreams",
    "georgevilora"
]

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')

# Allowed Discord Server IDs
GUILD_IDS = [
    762924058655457291,  # Test Server
    987654321098765432   # Main Server
]

# ROLE TIER CONFIGURATION PER GUILD ID
ROLE_TIERS = {
    762924058655457291: [
        (100, 1554883561759187105),   # Tier 1 (Min 100 coins)
        (500, 1554883677031370902),   # Tier 2 (Min 500 coins)
        (1000, 1554883726989594725),  # Tier 3 (Min 1000 coins)
    ],
    987654321098765432: [
        (100, 555555555555555555),
        (500, 666666666666666666),
        (1000, 777777777777777777),
    ]
}

STACK_ROLES = False

# Discord Bot Setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# 2. DATABASE INITIALIZATION (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect("tiktok_donations.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tiktok_gifts (
            tiktok_user TEXT PRIMARY KEY,
            total_coins INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_mappings (
            discord_id INTEGER PRIMARY KEY,
            tiktok_user TEXT UNIQUE
        )
    """)
    conn.commit()
    conn.close()

init_db()

def save_gift_data(username: str, coins: int) -> int:
    conn = sqlite3.connect("tiktok_donations.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO tiktok_gifts (tiktok_user, total_coins)
        VALUES (?, ?)
        ON CONFLICT(tiktok_user) DO UPDATE SET
        total_coins = total_coins + excluded.total_coins
    """, (username, coins))
    conn.commit()

    cursor.execute("SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (username,))
    total = cursor.fetchone()[0]
    conn.close()
    return total

def get_discord_id_from_tiktok(tiktok_user: str):
    conn = sqlite3.connect("tiktok_donations.db")
    cursor = conn.cursor()
    cursor.execute("SELECT discord_id FROM user_mappings WHERE tiktok_user = ?", (tiktok_user.lower(),))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

# ==========================================
# 3. HELPER: AUTOMATIC ROLE ASSIGNMENT
# ==========================================
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

# ==========================================
# 4. TIKTOK LIVE MONITORING (AUTO STANDBY)
# ==========================================
def create_tiktok_client(username: str) -> TikTokLiveClient:
    """Create a new TikTokLiveClient instance with English logging format."""
    client = TikTokLiveClient(unique_id=username.lower())

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
                total_coins = save_gift_data(user_name, coins)
                # Formatted English Gift Log
                print(f"[gift - @{client.unique_id}] {user_name} sent {gift_name} ({coins} c); total : {total_coins}")
                await auto_assign_role(user_name, total_coins)

    return client

async def run_tiktok_client_with_standby(username: str):
    """Monitoring loop dengan penanganan jeda rate limit yang lebih aman."""
    retry_delay = 120  # Standby 2 menit saat akun offline

    while True:
        client = create_tiktok_client(username)
        try:
            print(f"[@{username}] connecting...")
            await client.start()
            print(f"[@{username}] connected")
        except Exception as e:
            error_msg = str(e).lower()

            # Pastikan koneksi lama ditutup dengan bersih
            try:
                await client.disconnect()
            except Exception:
                pass

            if "rate_limit" in error_msg or "too many connections" in error_msg or "429" in error_msg:
                print(f"[@{username}] rate limit detected (cooldown 5 minutes)")
                await asyncio.sleep(300)  # Naikkan jeda jadi 5 menit jika kena rate limit
            elif "offline" in error_msg or "not live" in error_msg or "user_offline" in error_msg:
                print(f"[@{username}] offline (cooldown 2 minutes)")
                await asyncio.sleep(retry_delay)
            else:
                # KRUSIAL: Beri jeda 15-30 detik saat disconnect biasa agar tidak spam handshake
                print(f"[@{username}] disconnected/error: {e} (cooldown 20 seconds)")
                await asyncio.sleep(20)

# ==========================================
# 5. DISCORD BOT COMMANDS (ENGLISH RESPONSE)
# ==========================================
@bot.event
async def on_ready():
    print(f"[Discord] Bot logged in as {bot.user}")

@bot.command(name="claim")
async def claim(ctx, tiktok_user: str):
    tiktok_user_clean = tiktok_user.lstrip("@").lower()

    conn = sqlite3.connect("tiktok_donations.db")
    cursor = conn.cursor()

    cursor.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
    existing_discord_claim = cursor.fetchone()
    if existing_discord_claim:
        await ctx.send(f"❌ You have already claimed TikTok account `@{existing_discord_claim[0]}`. Contact an Administrator to unbind it.")
        conn.close()
        return

    cursor.execute("SELECT discord_id FROM user_mappings WHERE tiktok_user = ?", (tiktok_user_clean,))
    existing_tiktok_claim = cursor.fetchone()
    if existing_tiktok_claim:
        await ctx.send(f"❌ TikTok username `@{tiktok_user_clean}` has already been claimed by another Discord account.")
        conn.close()
        return

    cursor.execute("""
        INSERT INTO user_mappings (discord_id, tiktok_user)
        VALUES (?, ?)
    """, (ctx.author.id, tiktok_user_clean))
    conn.commit()

    cursor.execute("SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (tiktok_user_clean,))
    row = cursor.fetchone()
    conn.close()

    total_coins = row[0] if row else 0

    await ctx.send(
        f"✅ Linked Discord account {ctx.author.mention} to TikTok `@{tiktok_user_clean}`.\n"
        f"Current recorded total: **{total_coins} c**."
    )

    await auto_assign_role(tiktok_user_clean, total_coins)

@bot.command(name="mycoins", aliases=["check", "coins"])
async def mycoins(ctx):
    """Check total recorded TikTok coins for the user."""
    conn = sqlite3.connect("tiktok_donations.db")
    cursor = conn.cursor()

    cursor.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
    mapping = cursor.fetchone()

    if not mapping:
        await ctx.send(
            f"❌ {ctx.author.mention}, you haven't linked a TikTok account yet!\n"
            f"Use `!claim @your_tiktok_username` first."
        )
        conn.close()
        return

    tiktok_user = mapping[0]

    cursor.execute("SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (tiktok_user,))
    gift_data = cursor.fetchone()
    conn.close()

    total_coins = gift_data[0] if gift_data else 0

    await ctx.send(
        f"📊 **Coin Balance for {ctx.author.display_name}:**\n"
        f"• TikTok Account: `@{tiktok_user}`\n"
        f"• Total Coins: **{total_coins} c**"
    )

@bot.command(name="unbind")
@commands.has_permissions(administrator=True)
async def unbind(ctx, target: str):
    conn = sqlite3.connect("tiktok_donations.db")
    cursor = conn.cursor()

    target_clean = target.lstrip("@").lower()

    if target_clean.isdigit():
        cursor.execute("DELETE FROM user_mappings WHERE discord_id = ?", (int(target_clean),))
    else:
        cursor.execute("DELETE FROM user_mappings WHERE tiktok_user = ?", (target_clean,))

    conn.commit()
    deleted_rows = cursor.rowcount
    conn.close()

    if deleted_rows > 0:
        await ctx.send(f"✅ Unbound claim mapping for `{target}` successfully.")
    else:
        await ctx.send(f"⚠ No mapping found for `{target}` in database.")

@bot.command(name="topdonator")
async def topdonator(ctx):
    conn = sqlite3.connect("tiktok_donations.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tiktok_user, total_coins FROM tiktok_gifts ORDER BY total_coins DESC LIMIT 5")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await ctx.send("No gift data recorded yet.")
        return

    response = "**🏆 Top 5 TikTok Live Donators:**\n"
    for idx, (user, coins) in enumerate(rows, start=1):
        response += f"{idx}. `@{user}` - {coins} c\n"

    await ctx.send(response)

# ==========================================
# 6. RUN PARALLEL SERVICES
# ==========================================
async def main():
    for username in TIKTOK_USERNAMES:
        asyncio.create_task(run_tiktok_client_with_standby(username))

    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
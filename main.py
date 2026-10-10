import asyncio
import os
import discord
from discord.ext import commands

from db import (
    init_db,
    get_discord_id_from_tiktok,
    get_db_connection,
    get_daily_top_supporters,
    save_gift_data,
    save_daily_gift_data
)
from utils import get_schedule_info, load_role_config, load_summary_config
from server import keep_alive
from observer import run_all_observers

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
ANNOUNCEMENT_CHANNEL_ID = int(os.environ.get('ANNOUNCEMENT_CHANNEL_ID', "0"))

NOTIFICATION_LOCK = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

def handle_api_announcement(username: str, is_live: bool):
    try:
        loop = bot.loop if bot.is_ready() else asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(
            process_live_announcement(username, is_live),
            loop
        )
    except Exception as e:
        print(f"[API Error] Failed to schedule announcement for @{username}: {e}")

async def auto_assign_role(tiktok_user: str, total_coins: int):
    discord_id = get_discord_id_from_tiktok(tiktok_user)
    if not discord_id:
        return

    role_tiers, stack_roles = load_role_config()
    if not role_tiers:
        return

    for guild_id, tiers in role_tiers.items():
        guild = bot.get_guild(guild_id)
        if not guild:
            continue

        try:
            member = await guild.fetch_member(discord_id)
        except discord.NotFound:
            continue

        roles_to_add = []
        roles_to_remove = []

        if stack_roles:
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

async def process_live_announcement(username: str, is_live: bool):
    user_key = username.lower()
    async with NOTIFICATION_LOCK:
        if not ANNOUNCEMENT_CHANNEL_ID:
            print("[Discord] Notification failed: ANNOUNCEMENT_CHANNEL_ID is not set.")
            return

        await bot.wait_until_ready()
        channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if not channel:
            try:
                channel = await bot.fetch_channel(ANNOUNCEMENT_CHANNEL_ID)
            except Exception as e:
                print(f"[Discord] Notification failed: Channel ID {ANNOUNCEMENT_CHANNEL_ID} not found ({e}).")
                return

        host_name, account_name = get_schedule_info(user_key)
        live_url = f"https://www.tiktok.com/@{user_key}/live"
        live_content = (
            f"🔴 **{host_name} just live!**\n"
            f"Currently streaming on **{account_name}** (@{user_key})\n\n"
            f"Watch here:\n{live_url}"
        )
        offline_content = f"⚪ **@{user_key}** has ended the stream / disconnected from TikTok Live."

        conn = get_db_connection()
        try:
            res = conn.execute(
                "SELECT channel_id, message_id FROM active_live_announcements WHERE tiktok_username = ?",
                (user_key,)
            )
            row = res.fetchone()

            old_msg = None
            if row:
                old_message_id = int(row[1])
                try:
                    old_msg = await channel.fetch_message(old_message_id)
                except Exception:
                    old_msg = None

            if old_msg:
                is_announcement_msg = "🔴" in old_msg.content or "just live!" in old_msg.content
                is_offline_msg = "⚪" in old_msg.content or "ended the stream" in old_msg.content

                if is_live:
                    if is_announcement_msg:
                        if host_name in old_msg.content:
                            return
                        else:
                            try:
                                await old_msg.delete()
                            except Exception:
                                pass
                            new_msg = await channel.send(live_content)
                            conn.execute(
                                "INSERT OR REPLACE INTO active_live_announcements (tiktok_username, channel_id, message_id) VALUES (?, ?, ?)",
                                (user_key, str(channel.id), str(new_msg.id))
                            )
                            conn.commit()
                            return
                    elif is_offline_msg:
                        try:
                            await old_msg.delete()
                        except Exception:
                            pass
                        new_msg = await channel.send(live_content)
                        conn.execute(
                            "INSERT OR REPLACE INTO active_live_announcements (tiktok_username, channel_id, message_id) VALUES (?, ?, ?)",
                            (user_key, str(channel.id), str(new_msg.id))
                        )
                        conn.commit()
                        return
                else:
                    if is_offline_msg:
                        return
                    else:
                        try:
                            edited_msg = await old_msg.edit(content=offline_content)
                            conn.execute(
                                "INSERT OR REPLACE INTO active_live_announcements (tiktok_username, channel_id, message_id) VALUES (?, ?, ?)",
                                (user_key, str(channel.id), str(edited_msg.id))
                            )
                            conn.commit()
                        except Exception as e:
                            print(f"[Discord] Failed to edit message for @{user_key}: {e}")
                        return
            else:
                target_content = live_content if is_live else offline_content
                new_msg = await channel.send(target_content)
                conn.execute(
                    "INSERT OR REPLACE INTO active_live_announcements (tiktok_username, channel_id, message_id) VALUES (?, ?, ?)",
                    (user_key, str(channel.id), str(new_msg.id))
                )
                conn.commit()

        except Exception as e:
            print(f"[Discord Error] Failed to process announcement for @{user_key}: {e}")
        finally:
            conn.close()

# --- Discord Commands ---
@bot.event
async def on_ready():
    print(f"[Discord] Bot logged in as {bot.user}")

@bot.command(name="claim")
async def claim(ctx, tiktok_user: str):
    tiktok_user_clean = tiktok_user.lstrip("@").lower()
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
        if res.fetchone():
            await ctx.send("❌ You have already claimed a TikTok account.")
            return

        res = conn.execute("SELECT discord_id FROM user_mappings WHERE tiktok_user = ?", (tiktok_user_clean,))
        if res.fetchone():
            await ctx.send(f"❌ TikTok username `@{tiktok_user_clean}` has already been claimed.")
            return

        conn.execute("INSERT INTO user_mappings (discord_id, tiktok_user) VALUES (?, ?)", (ctx.author.id, tiktok_user_clean))
        conn.commit()

        res = conn.execute("SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (tiktok_user_clean,))
        row = res.fetchone()
        total_coins = row[0] if row else 0

        await ctx.send(f"✅ Linked Discord account {ctx.author.mention} to TikTok `@{tiktok_user_clean}`. Total: **{total_coins} c**.")
    finally:
        conn.close()

@bot.command(name="mycoins", aliases=["check", "coins"])
async def mycoins(ctx):
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
        mapping = res.fetchone()
        if not mapping:
            await ctx.send(f"❌ {ctx.author.mention}, you haven't linked a TikTok account yet! Use `!claim @username`.")
            return

        tiktok_user = mapping[0]
        res = conn.execute("SELECT display_name, total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (tiktok_user,))
        gift_data = res.fetchone()
        display_name = gift_data[0] if (gift_data and gift_data[0]) else tiktok_user
        total_coins = gift_data[1] if gift_data else 0

        await ctx.send(f"📊 **Coin Balance for {ctx.author.display_name}:** @{tiktok_user} | **{total_coins} c**")
    finally:
        conn.close()

@bot.command(name="myhistory")
async def myhistory(ctx):
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
        mapping = res.fetchone()
        if not mapping:
            await ctx.send(f"❌ {ctx.author.mention}, kamu belum link akun! Gunakan `!claim @username`.")
            return

        tiktok_user = mapping[0]
        res = conn.execute("""
            SELECT tiktok_username, gift_name, gift_count, coins, created_at 
            FROM gift_logs 
            WHERE viewer_username = ? 
            ORDER BY created_at DESC LIMIT 5
        """, (tiktok_user,))
        rows = res.fetchall()

        if not rows:
            await ctx.send("📜 Belum ada riwayat gift yang tercatat.")
            return

        msg = f"📜 **Riwayat Gift Terbaru untuk @{tiktok_user}:**\n"
        for host, g_name, g_count, coins, dt in rows:
            msg += f"• `{dt}` - **{g_count}x {g_name}** ({coins}c) ke host **@{host}**\n"

        await ctx.send(msg)
    finally:
        conn.close()

@bot.command(name="topsupporter", aliases=["leaderboard", "koin", "topdonator"])
async def topsupporter(ctx):
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user, display_name, total_coins FROM tiktok_gifts ORDER BY total_coins DESC LIMIT 10")
        rows = res.fetchall()
        if not rows:
            await ctx.send("❌ No gift data recorded yet.")
            return

        table = "```text\n"
        table += f"{'NO':<4} | {'NAME / USERNAME':<24} | {'TOTAL COINS':<10}\n"
        table += "-" * 46 + "\n"
        for idx, (username, display_name, coins) in enumerate(rows, start=1):
            name_str = display_name if display_name else f"@{username}"
            if len(name_str) > 23:
                name_str = name_str[:20] + "..."
            table += f"{idx:<4} | {name_str:<24} | {coins:<10} c\n"
        table += "```"
        await ctx.send(f"🏆 **TIKTOK LIVE TOP SUPPORTERS LEADERBOARD**\n{table}")
    finally:
        conn.close()

@bot.command(name="dailyrank", aliases=["daily", "harini"])
async def dailyrank(ctx):
    rows = get_daily_top_supporters()
    if not rows:
        await ctx.send("❌ Belum ada data koin yang tercatat untuk hari ini.")
        return

    table = "```text\n"
    table += f"{'NO':<4} | {'NAME / USERNAME':<24} | {'TODAY COINS':<10}\n"
    table += "-" * 46 + "\n"
    for idx, (username, display_name, coins) in enumerate(rows, start=1):
        name_str = display_name if display_name else f"@{username}"
        if len(name_str) > 23:
            name_str = name_str[:20] + "..."
        table += f"{idx:<4} | {name_str:<24} | {coins:<10} c\n"
    table += "```"
    await ctx.send(f"🏆 **TIKTOK LIVE DAILY RANKING (HARI INI)**\n{table}")

@bot.command(name="livesessions", aliases=["sessions"])
async def livesessions(ctx, host_username: str = None):
    conn = get_db_connection()
    try:
        if host_username:
            u_clean = host_username.lstrip("@").lower()
            res = conn.execute("""
                SELECT session_id, tiktok_username, start_time, duration_seconds, total_coins 
                FROM live_sessions WHERE tiktok_username = ? ORDER BY start_time DESC LIMIT 5
            """, (u_clean,))
        else:
            res = conn.execute("""
                SELECT session_id, tiktok_username, start_time, duration_seconds, total_coins 
                FROM live_sessions ORDER BY start_time DESC LIMIT 5
            """)
        
        rows = res.fetchall()
        if not rows:
            await ctx.send("❌ Belum ada riwayat sesi live yang tercatat.")
            return

        msg = "📹 **Riwayat Sesi Live Terbaru:**\n"
        for s_id, host, s_time, dur_sec, coins in rows:
            hrs = dur_sec // 3600
            mins = (dur_sec % 3600) // 60
            secs = dur_sec % 60
            dur_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if dur_sec > 0 else "Masih Live / Disconnect"
            msg += f"• Sesi `#{s_id}` (@{host}) | Mulai: `{s_time}` | Durasi: `{dur_str}` | Total: **{coins} c**\n"

        await ctx.send(msg)
    finally:
        conn.close()

async def main():
    init_db()
    keep_alive(handle_api_announcement)

    # Jalankan observer secara dinamis dengan membaca daftar dari Database
    asyncio.create_task(run_all_observers(process_live_announcement, auto_assign_role))

    await bot.start(DISCORD_TOKEN)

if

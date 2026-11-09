import asyncio
import os
import json
import re
import discord
from discord.ext import commands

from db import (
    init_db,
    get_discord_id_from_tiktok,
    get_db_connection,
    get_daily_top_supporters,
    save_gift_data,
    save_daily_gift_data,
    get_setting,
    set_setting,
    get_active_live_announcement,
    get_all_active_live_announcements,
    save_active_live_announcement,
    update_active_live_announcement_host,
    delete_active_live_announcement,
    is_tiktok_user_in_db,
    is_user_already_claimed,
    get_user_mapping,
    bind_user_mapping,
    create_pending_verification,
    get_pending_verification,
    find_pending_live_verification,
    find_pending_live_greeting,
    cancel_pending_verification,
    log_activity_event,
    get_bool_setting,
    get_int_list_setting,
    get_announcement_channel_ids,
    get_active_live_announcements_for_account,
    get_role_tiers,
    get_tier_settings,
    is_feature_enabled,
    tier_reset_due,
    mark_tier_reset_done,
    close_all_open_live_sessions
)
from utils import get_schedule_info, load_summary_config
from server import keep_alive
from observer import run_all_observers, get_current_live_hosts, is_any_host_live, probe_room_live
from verification import async_verify_tiktok_bio
from routes.settings import set_verification_refresh_handler
import runtime_status

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
ANNOUNCEMENT_CHANNEL_ID = int(os.environ.get('ANNOUNCEMENT_CHANNEL_ID', "0"))

NOTIFICATION_LOCK = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def is_admin_member(member) -> bool:
    """Admin = configured admin role, or server administrator when no role is set."""
    if not isinstance(member, discord.Member):
        return False
    role_id = get_setting("admin_role_id", "") or ""
    if role_id.isdigit():
        return any(r.id == int(role_id) for r in member.roles)
    return bool(member.guild_permissions.administrator)


def is_admin(ctx) -> bool:
    return is_admin_member(ctx.author)


@bot.check
async def global_bot_check(ctx):
    if not get_bool_setting("master_enabled", True):
        raise commands.CheckFailure("bot_disabled")
    if ctx.guild and get_bool_setting("allowed_channels_enabled", False):
        if not is_admin_member(ctx.author):
            allowed = get_int_list_setting("allowed_channel_ids")
            if ctx.channel.id not in allowed:
                raise commands.CheckFailure("channel_not_allowed")
    return True

def get_verified_role(guild: discord.Guild) -> discord.Role:
    """Mengambil object Role verifikasi dari setting, env, roles.json, atau nama role."""
    if not guild:
        return None
    # 1. Cek dari bot_settings di database
    role_id_str = get_setting("verified_role_id")
    if role_id_str and role_id_str.isdigit():
        role = guild.get_role(int(role_id_str))
        if role:
            return role
    # 2. Cek dari environment variable
    env_id = os.environ.get("VERIFIED_ROLE_ID")
    if env_id and env_id.isdigit():
        role = guild.get_role(int(env_id))
        if role:
            return role
    # 3. Cek dari config/roles.json
    try:
        if os.path.exists("config/roles.json"):
            with open("config/roles.json", "r") as f:
                data = json.load(f)
                val = data.get("verified_role_id")
                if val:
                    role = guild.get_role(int(val))
                    if role:
                        return role
    except Exception:
        pass
    # 4. Cari otomatis berdasarkan nama role standar
    for r in guild.roles:
        if r.name.lower() in ["verified", "terverifikasi", "member"]:
            return r
    return None

async def grant_verified_access(member: discord.Member, tiktok_user: str, total_coins: int) -> tuple:
    """Memberikan role verifikasi dan role coin tier yang sesuai kepada member Discord."""
    roles_added_names = []
    verified_role = get_verified_role(member.guild)
    if verified_role and verified_role not in member.roles:
        try:
            await member.add_roles(verified_role)
            roles_added_names.append(verified_role.name)
        except Exception as e:
            print(f"[Role Error] Failed to add verified role: {e}")

    # Berikan juga tier roles sesuai total koin yang ada
    await auto_assign_role(tiktok_user, total_coins)
    
    role_info = f" Role **{', '.join(roles_added_names)}** has been granted." if roles_added_names else " Role has been activated."
    return True, role_info

def handle_api_announcement(username: str, is_live: bool, host_name: str = None):
    try:
        loop = bot.loop if bot.is_ready() else asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(
            process_live_announcement(username, is_live, host_name),
            loop
        )
    except Exception as e:
        print(f"[API Error] Failed to schedule announcement for @{username}: {e}")

async def _announce_tier_up(member: discord.Member, role: discord.Role, tiktok_user: str, total_coins: int):
    tier_cfg = get_tier_settings()
    if not tier_cfg["announce"] or not is_feature_enabled("role_tiers"):
        return
    channel_id = str(tier_cfg.get("announce_channel_id") or "").strip()
    if not channel_id.isdigit():
        return
    try:
        channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        if not channel:
            return
        embed = discord.Embed(
            title="🎉 Tier Up!",
            description=(
                f"{member.mention} just reached **{role.name}**!\n\n"
                f"🎵 TikTok account: **@{tiktok_user}**\n"
                f"💰 Total coins: **{total_coins:,} c**"
            ),
            color=discord.Color.gold()
        )
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[Discord] Failed to announce tier up: {e}")


async def auto_assign_role(tiktok_user: str, total_coins: int):
    """Assign reset and permanent tier roles based on the admin panel settings."""
    if not is_feature_enabled("role_tiers"):
        return

    discord_id = get_discord_id_from_tiktok(tiktok_user)
    if not discord_id:
        return

    tier_cfg = get_tier_settings()
    groups = []
    if tier_cfg["enabled"]:
        groups.append("reset")
    if tier_cfg["permanent_enabled"]:
        groups.append("permanent")
    if not groups:
        return

    stacked = str(tier_cfg.get("mode", "replace")).lower() == "stacked"

    for guild in bot.guilds:
        try:
            member = await guild.fetch_member(discord_id)
        except (discord.NotFound, discord.HTTPException):
            continue

        roles_to_add = []
        roles_to_remove = []
        added_tiers = []

        for group_key in groups:
            tiers = get_role_tiers(group_key)
            resolved = []
            for tier in tiers:
                try:
                    role = guild.get_role(int(tier["role_id"]))
                except (TypeError, ValueError):
                    role = None
                if role:
                    resolved.append((tier, role))

            if not resolved:
                continue

            eligible = [(t, r) for t, r in resolved if total_coins >= t["min_coins"]]
            if stacked:
                for tier, role in eligible:
                    if role not in member.roles and role not in roles_to_add:
                        roles_to_add.append(role)
                        added_tiers.append((tier, role))
            else:
                highest = max(eligible, key=lambda x: x[0]["min_coins"], default=None)
                if highest:
                    tier, role = highest
                    if role not in member.roles and role not in roles_to_add:
                        roles_to_add.append(role)
                        added_tiers.append((tier, role))
                for tier, role in resolved:
                    if highest and role == highest[1]:
                        continue
                    if role in member.roles and role not in roles_to_remove:
                        roles_to_remove.append(role)

        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="Coin tier update")
                role_names = ", ".join([r.name for r in roles_to_add])
                print(f"[Discord] Assigned tier role(s) {role_names} to {member.display_name} in '{guild.name}'!")
            except Exception as e:
                print(f"[Discord] Error assigning tier roles: {e}")

        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove, reason="Coin tier replacement")
                role_names = ", ".join([r.name for r in roles_to_remove])
                print(f"[Discord] Removed previous tier role(s) ({role_names}) from {member.display_name} in '{guild.name}'.")
            except Exception as e:
                print(f"[Discord] Error removing previous tier roles: {e}")

        if added_tiers:
            best_tier, best_role = max(added_tiers, key=lambda x: x[0]["min_coins"])
            await _announce_tier_up(member, best_role, tiktok_user, total_coins)

async def _resolve_announcement_channel(announcement_session: dict = None):
    """Menentukan channel Discord tempat sesi pengumuman tersimpan (fallback ke env)."""
    channel_id = None
    if announcement_session and announcement_session.get("channel_id"):
        try:
            channel_id = int(announcement_session["channel_id"])
        except (TypeError, ValueError):
            channel_id = None
    if not channel_id:
        channel_id = ANNOUNCEMENT_CHANNEL_ID
    if not channel_id:
        return None

    channel = bot.get_channel(channel_id)
    if channel:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except Exception as e:
        print(f"[Discord] Notification failed: Channel ID {channel_id} not found ({e}).")
        return None

async def _fetch_announcement_message(channel, message_id):
    """Mengambil pesan pengumuman lama dari sesi yang tersimpan."""
    if not channel or not message_id:
        return None
    try:
        return await channel.fetch_message(int(message_id))
    except Exception as e:
        print(f"[Discord] Stored announcement message {message_id} is no longer available ({e}).")
        return None

async def process_live_announcement(username: str, is_live: bool, host_name: str = None):
    """Maintain one live announcement message per target account per channel.

    - Start / still live: send once per configured channel, edit on host changes.
    - Offline: delete every stored message for the account and clear sessions.
    - Channels removed from the admin panel are cleaned up automatically.
    """
    user_key = username.lower().lstrip("@")
    async with NOTIFICATION_LOCK:
        if not bot.is_ready():
            await bot.wait_until_ready()

        sessions = get_active_live_announcements_for_account(user_key)

        # --- OFFLINE: remove all stored announcement messages ---
        if not is_live:
            for session in sessions:
                channel = await _resolve_announcement_channel(session)
                old_msg = await _fetch_announcement_message(
                    channel, session.get("message_id")
                )
                if old_msg:
                    try:
                        await old_msg.delete()
                        print(f"[Discord] Deleted live announcement for @{user_key} in channel {session.get('channel_id')}.")
                    except Exception as e:
                        print(f"[Discord] Failed to delete message for @{user_key}: {e}")
                delete_active_live_announcement(user_key, session.get("channel_id"))
            if sessions:
                print(f"[Discord] Announcement sessions cleared for @{user_key}.")
            return

        if not is_feature_enabled("announcements"):
            return

        configured_channels = get_announcement_channel_ids()
        if not configured_channels:
            print("[Discord] Notification failed: no announcement channel configured.")
            return

        sched_host, account_name = get_schedule_info(user_key)
        final_host_name = host_name or sched_host or account_name or "Host"
        live_url = f"https://www.tiktok.com/@{user_key}/live"
        live_content = (
            f"🔴 **{final_host_name} just live!**\n"
            f"Currently streaming on **{account_name}** (@{user_key})\n\n"
            f"Watch here:\n{live_url}"
        )

        # Remove sessions for channels that are no longer configured.
        for session in sessions:
            try:
                stored_channel = int(session.get("channel_id"))
            except (TypeError, ValueError):
                stored_channel = None
            if stored_channel not in configured_channels:
                channel = await _resolve_announcement_channel(session)
                old_msg = await _fetch_announcement_message(channel, session.get("message_id"))
                if old_msg:
                    try:
                        await old_msg.delete()
                    except Exception:
                        pass
                delete_active_live_announcement(user_key, session.get("channel_id"))

        for channel_id in configured_channels:
            session = get_active_live_announcement(user_key, channel_id)
            channel = await _resolve_announcement_channel({"channel_id": channel_id})
            if not channel:
                continue

            if session:
                old_msg = await _fetch_announcement_message(channel, session.get("message_id"))
                if old_msg:
                    if old_msg.content != live_content:
                        try:
                            await old_msg.edit(content=live_content)
                            print(f"[Discord] Updated live announcement for @{user_key} in {channel_id}.")
                        except Exception as e:
                            print(f"[Discord] Failed to edit announcement for @{user_key}: {e}")
                    # Session exists and message is valid: never send a duplicate.
                    continue
                print(f"[Discord] Stored announcement message missing for @{user_key} in {channel_id}; sending a replacement.")

            try:
                new_msg = await channel.send(live_content)
            except Exception as e:
                print(f"[Discord] Failed to send announcement for @{user_key} in {channel_id}: {e}")
                continue

            try:
                saved = save_active_live_announcement(user_key, channel.id, new_msg.id, final_host_name)
                if not saved:
                    print(f"[Discord] Announcement sent but session was not saved for @{user_key} in {channel.id} (Msg ID: {new_msg.id}).")
                else:
                    print(f"[Discord] Sent live announcement for @{user_key} in {channel.id} (Msg ID: {new_msg.id}).")
            except Exception as e:
                # If we sent a message but failed to save its ID, duplicates can happen on next trigger.
                print(f"[Discord] CRITICAL: sent announcement but failed to save session for @{user_key} in {channel.id} (Msg ID: {new_msg.id}): {e}")

        update_active_live_announcement_host(user_key, final_host_name)

async def reconcile_announcement_sessions(delay_seconds: int = 180):
    """Clean stale announcement sessions after restart without deleting active lives.

    Runtime status can be empty shortly after restart, so we probe before deleting.
    """
    await asyncio.sleep(delay_seconds)
    try:
        sessions = get_all_active_live_announcements()
        if not sessions:
            return
        live_now = set(get_current_live_hosts())
        checked = {}
        for item in sessions:
            username = (item.get("tiktok_username") or "").lower()
            if not username or username in live_now:
                continue
            if username not in checked:
                try:
                    checked[username] = await probe_room_live(username)
                except Exception as e:
                    print(f"[Discord] Reconcile probe failed for @{username}: {e}")
                    checked[username] = None
            probe = checked[username]
            if probe is True:
                print(f"[Discord] Reconcile kept @{username}: probe reports live.")
                # Do not send a duplicate here; the observer/probe fallback handles live announcements.
                continue
            if probe is False:
                print(f"[Discord] Stale announcement session for @{username} found, cleaning up.")
                await process_live_announcement(username, False)
            else:
                print(f"[Discord] Reconcile skipped @{username}: live status unknown.")
    except Exception as e:
        print(f"[Discord] Announcement reconciliation failed: {e}")

# --- Leaderboard, tier reset, verification panel tasks ---

def build_leaderboard_embed(title: str, rows, value_label: str = "Total Coins") -> discord.Embed:
    embed = discord.Embed(
        title=title,
        color=discord.Color.gold(),
    )
    if not rows:
        embed.description = "No coin data recorded yet."
        return embed
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for idx, row in enumerate(rows, start=1):
        username, display_name, coins = row[0], row[1], row[2]
        name = display_name or f"@{username}"
        prefix = medals.get(idx, f"`#{idx}`")
        lines.append(f"{prefix} **{name}** — `{int(coins or 0):,} c`")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Dummy Dream Bot • {value_label} • updated automatically")
    return embed


async def update_leaderboard_message():
    if not is_feature_enabled("leaderboard"):
        return
    channel_id = str(get_setting("leaderboard_channel_id", "") or "").strip()
    if not channel_id.isdigit():
        return
    channel = bot.get_channel(int(channel_id))
    if not channel:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception:
            return

    conn = get_db_connection()
    try:
        all_time = conn.execute("""
            SELECT tiktok_user, display_name, total_coins
            FROM tiktok_gifts ORDER BY total_coins DESC LIMIT 10
        """).fetchall()
    finally:
        conn.close()

    embed = build_leaderboard_embed("🏆 All-Time Top Supporters", all_time)
    daily = get_daily_top_supporters()
    if daily:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        daily_lines = []
        for idx, row in enumerate(daily[:5], start=1):
            name = row[1] or f"@{row[0]}"
            daily_lines.append(f"{medals.get(idx, f'`#{idx}`')} **{name}** — `{int(row[2] or 0):,} c`")
        embed.add_field(name="📅 Today's Top Supporters", value="\n".join(daily_lines), inline=False)

    old_message_id = str(get_setting("leaderboard_message_id", "") or "")
    old_channel_id = str(get_setting("leaderboard_message_channel_id", "") or "")

    if old_message_id and old_channel_id and old_channel_id != str(channel.id):
        try:
            old_channel = bot.get_channel(int(old_channel_id)) or await bot.fetch_channel(int(old_channel_id))
            old_msg = await old_channel.fetch_message(int(old_message_id))
            await old_msg.delete()
        except Exception:
            pass
        old_message_id = ""

    if old_message_id and old_channel_id == str(channel.id):
        try:
            msg = await channel.fetch_message(int(old_message_id))
            await msg.edit(embed=embed)
            return
        except Exception:
            pass

    msg = await channel.send(embed=embed)
    set_setting("leaderboard_message_id", str(msg.id))
    set_setting("leaderboard_message_channel_id", str(channel.id))


async def leaderboard_loop():
    await asyncio.sleep(15)
    while True:
        try:
            await update_leaderboard_message()
        except Exception as e:
            print(f"[Leaderboard] Update failed: {e}")
        await asyncio.sleep(180)


async def reset_tier_roles():
    reset_tiers = get_role_tiers("reset")
    role_ids = set()
    for tier in reset_tiers:
        try:
            role_ids.add(int(tier["role_id"]))
        except (TypeError, ValueError):
            continue
    if not role_ids:
        mark_tier_reset_done()
        return

    removed = 0
    for guild in bot.guilds:
        for member in guild.members:
            roles = [r for r in member.roles if r.id in role_ids]
            if roles:
                try:
                    await member.remove_roles(*roles, reason="Scheduled role tier reset")
                    removed += 1
                except Exception as e:
                    print(f"[Tier Reset] Failed for {member.display_name}: {e}")

    mark_tier_reset_done()
    log_activity_event("tier_reset", f"Scheduled tier reset completed; roles removed from {removed} member(s).")
    print(f"[Tier Reset] Removed reset tier roles from {removed} member(s).")

    tier_cfg = get_tier_settings()
    channel_id = str(tier_cfg.get("announce_channel_id") or "").strip()
    if tier_cfg["announce"] and channel_id.isdigit():
        try:
            channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
            if channel:
                await channel.send(embed=discord.Embed(
                    title="🔄 Role Tier Reset",
                    description=f"The scheduled tier reset has completed. Roles were removed from **{removed}** member(s).",
                    color=discord.Color.orange()
                ))
        except Exception as e:
            print(f"[Tier Reset] Could not announce reset: {e}")


async def tier_reset_loop():
    await asyncio.sleep(30)
    while True:
        try:
            if tier_reset_due() and is_feature_enabled("role_tiers"):
                await reset_tier_roles()
        except Exception as e:
            print(f"[Tier Reset] Loop error: {e}")
        await asyncio.sleep(1800)


def build_verification_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛡️ TIKTOK ACCOUNT VERIFICATION",
        description=(
            "Welcome to the server!\n\n"
            "To get your role and unlock access to every server channel, "
            "you must first **verify your TikTok account**.\n\n"
            "📌 **How to verify:**\n"
            "1. Click the **[ 🔑 Verify TikTok Account ]** button below.\n"
            "2. Enter your TikTok username in the form.\n"
            "3. The conversation is **100% private (only you and the bot can see it)**.\n"
            "4. Choose a verification method (**Live Greeting**, **Live Chat Code**, or **Bio**).\n"
            "5. Once confirmed, your TikTok account is permanently linked and your server access role activates automatically!"
        ),
        color=discord.Color.from_rgb(0, 168, 255)
    )
    embed.set_footer(text="Dummy Dream Bot • Verification & Security System")
    return embed


async def send_verification_panel(channel):
    msg = await channel.send(embed=build_verification_panel_embed(), view=VerificationChannelView())
    set_setting("verification_panel_channel_id", str(channel.id))
    set_setting("verification_panel_message_id", str(msg.id))


async def move_verification_panel(old_channel_id, new_channel_id):
    await bot.wait_until_ready()
    # Delete the previous panel when the channel changes.
    old_msg_id = str(get_setting("verification_panel_message_id", "") or "")
    old_channel_id = str(old_channel_id or get_setting("verification_panel_channel_id", "") or "")
    if old_msg_id.isdigit() and old_channel_id.isdigit():
        try:
            old_channel = bot.get_channel(int(old_channel_id)) or await bot.fetch_channel(int(old_channel_id))
            old_msg = await old_channel.fetch_message(int(old_msg_id))
            await old_msg.delete()
        except Exception as e:
            print(f"[Verification] Could not delete old panel message: {e}")
    try:
        channel = bot.get_channel(int(new_channel_id)) or await bot.fetch_channel(int(new_channel_id))
        if channel:
            await send_verification_panel(channel)
            print(f"[Verification] Verification panel moved to channel {new_channel_id}.")
    except Exception as e:
        print(f"[Verification] Could not create panel in {new_channel_id}: {e}")


def schedule_verification_panel_refresh(old_channel_id, new_channel_id):
    try:
        asyncio.run_coroutine_threadsafe(
            move_verification_panel(old_channel_id, new_channel_id),
            bot.loop
        )
    except Exception as e:
        print(f"[Verification] Could not schedule panel move: {e}")



MIN_COINS_FOR_STANDARD_VERIFICATION = 10

WELCOME_GREETINGS = {
    "hi", "hii", "hiii", "hiiii", "hey", "heyy", "hello", "helo", "hallo",
    "yo", "greetings", "welcome",
}

WELCOME_PHRASES = (
    "good morning", "good afternoon", "good evening", "good night",
    "nice to meet you", "how are you",
)

def is_welcome_message(text: str) -> bool:
    """Return True when a live chat comment is an English welcome/greeting."""
    if not text:
        return False
    cleaned = re.sub(r"[^a-z\s]", " ", text.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return False
    if cleaned.split(" ", 1)[0] in WELCOME_GREETINGS:
        return True
    return cleaned.startswith(WELCOME_PHRASES)

def build_bio_verification_embed(tiktok_user: str, code: str) -> discord.Embed:
    """Embed with the exact bio text the user must save before verifying."""
    return discord.Embed(
        title="📝 Verify via TikTok Bio",
        description=(
            f"TikTok account: **@{tiktok_user}**\n\n"
            f"🔑 **Set your TikTok bio to this verification text:**\n"
            f"```text\n{code}\n```\n"
            f"**Steps:**\n"
            f"1. Open the TikTok app.\n"
            f"2. Go to **Profile** -> **Edit profile** -> **Bio**.\n"
            f"3. Put the verification text above in your bio and save it.\n"
            f"4. Click **[ 🔍 Verify Bio ]** below.\n\n"
            f"⏱️ *The code expires in 15 minutes. You can remove it from your bio after verification.*"
        ),
        color=discord.Color.gold()
    )

def build_verification_start(discord_user_id: int, tiktok_user: str, total_coins: int, display_name: str):
    """Pick the verification flow based on the account's coin activity.

    Accounts with at least 10 coins use the standard code-based flow.
    Accounts that never transacted (or have less than 10 coins) get two
    simpler options: a live greeting or a bio text.
    """
    low_activity = int(total_coins or 0) < MIN_COINS_FOR_STANDARD_VERIFICATION

    if low_activity:
        live_hosts = get_current_live_hosts()
        if not live_hosts:
            # No host is live: send the bio instructions right away.
            code = create_pending_verification(discord_user_id, tiktok_user, method='bio')
            embed = build_bio_verification_embed(tiktok_user, code)
            embed.description = (
                f"⚪ **No host is live right now**, so please verify using your TikTok bio.\n\n"
                + embed.description
            )
            return embed, BioVerificationView(tiktok_user, code, total_coins, low_activity=True)

        embed = discord.Embed(
            title="🔐 Verify Your TikTok Account",
            description=(
                f"TikTok account **@{tiktok_user}** (`{display_name or tiktok_user}`) is not in our "
                f"supporter database yet or has less than **{MIN_COINS_FOR_STANDARD_VERIFICATION} coins**.\n\n"
                f"You can still verify your account with either method below:"
            ),
            color=discord.Color.blue()
        )
        embed.add_field(
            name="🔴 Option 1 — Live greeting",
            value=(
                "Join an active live and post a welcome message such as `hello`, `hello host`, or `hiii` "
                "from your TikTok account. The bot will detect your comment and verify you automatically."
            ),
            inline=False
        )
        embed.add_field(
            name="📝 Option 2 — TikTok bio",
            value=(
                "The bot will give you a verification text to put in your TikTok bio. "
                "Save it, then click **Verify Bio**."
            ),
            inline=False
        )
        return embed, LowActivityVerificationView(tiktok_user, total_coins)

    # Standard flow for accounts with at least 10 coins
    code = create_pending_verification(discord_user_id, tiktok_user, method='choice')
    embed = discord.Embed(
        title="✅ TikTok Account Found",
        description=(
            f"TikTok account **@{tiktok_user}** (`{display_name or tiktok_user}`) is registered in our database!\n"
            f"💰 **Recorded coins:** `{total_coins} c`\n\n"
            f"🔒 **Choose a verification method:**\n"
            f"Prove that you own this account using one of the **2 methods** below:"
        ),
        color=discord.Color.blue()
    )
    embed.add_field(
        name="📝 Option 1 — Profile bio",
        value="Add the verification code to your TikTok bio, then click the confirm button. *(No live stream required.)*",
        inline=False
    )
    embed.add_field(
        name="🔴 Option 2 — Live chat code",
        value="Type the verification code in the live chat while one of our hosts is live. The bot detects it automatically.",
        inline=False
    )
    return embed, VerificationMethodChoiceView(tiktok_user, code, total_coins)

class VerificationChannelView(discord.ui.View):
    """Persistent view with the TikTok verification button."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify TikTok Account",
        style=discord.ButtonStyle.primary,
        emoji="🔑",
        custom_id="btn_tiktok_verify_persistent"
    )
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TikTokUsernameModal())

class TikTokUsernameModal(discord.ui.Modal, title="Verify TikTok Account"):
    username_input = discord.ui.TextInput(
        label="Your TikTok Username",
        placeholder="Example: clarissadreams (without @)",
        min_length=2,
        max_length=32,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Defer before DB lookups to avoid Discord "didn't respond in time".
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.InteractionResponded:
            pass
        except Exception:
            return

        if not is_feature_enabled("verification"):
            await interaction.followup.send(
                "⛔ Verification is currently disabled. Please try again later.",
                ephemeral=True
            )
            return
        u_clean = self.username_input.value.strip().lstrip('@').lower()

        # 1. Discord user already linked?
        existing_tiktok = get_user_mapping(interaction.user.id)
        if existing_tiktok:
            await interaction.followup.send(
                f"ℹ️ Your Discord account is already verified with TikTok **@{existing_tiktok}**.\n"
                f"You do not need to verify again.",
                ephemeral=True
            )
            return

        # 2. TikTok account claimed by someone else?
        is_claimed, claimed_discord_id = is_user_already_claimed(u_clean)
        if is_claimed:
            await interaction.followup.send(
                f"❌ TikTok account **@{u_clean}** is already linked to another Discord account.\n"
                f"One TikTok account can only be claimed by one Discord account. "
                f"If this is yours, please contact an admin.",
                ephemeral=True
            )
            return

        # 3. Look up the account (may not exist yet / may have less than 10 coins)
        exists, total_coins, display_name = is_tiktok_user_in_db(u_clean)
        embed, view = build_verification_start(
            interaction.user.id, u_clean, total_coins, display_name
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

class VerificationMethodChoiceView(discord.ui.View):
    """Standard verification (10+ coins): bio code or live chat code."""
    def __init__(self, tiktok_user: str, code: str, total_coins: int):
        super().__init__(timeout=None)
        self.tiktok_user = tiktok_user
        self.code = code
        self.total_coins = total_coins

    @discord.ui.button(label="Verify via Bio", style=discord.ButtonStyle.primary, emoji="📝")
    async def via_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.code = create_pending_verification(interaction.user.id, self.tiktok_user, method='bio')
        await interaction.response.edit_message(
            embed=build_bio_verification_embed(self.tiktok_user, self.code),
            view=BioVerificationView(self.tiktok_user, self.code, self.total_coins)
        )

    @discord.ui.button(label="Verify via Live Chat", style=discord.ButtonStyle.danger, emoji="🔴")
    async def via_live(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.code = create_pending_verification(interaction.user.id, self.tiktok_user, method='live_code')
        live_hosts = get_current_live_hosts()
        if live_hosts:
            host_text = "🟢 **Hosts live right now:**\n" + "\n".join(
                [f"• `@{h}` (https://www.tiktok.com/@{h}/live)" for h in live_hosts]
            )
        else:
            host_text = (
                "⚪ **No host is live right now.**\n"
                "💡 *Use the bio verification method if you do not want to wait.*"
            )

        embed = discord.Embed(
            title="🔴 Verify via Live Chat",
            description=(
                f"TikTok account: **@{self.tiktok_user}**\n\n"
                f"🔑 **Your verification code:**\n"
                f"```text\n{self.code}\n```\n"
                f"{host_text}\n\n"
                f"**Steps:**\n"
                f"1. Join one of our live streams.\n"
                f"2. Post a comment containing `{self.code}` from TikTok account **@{self.tiktok_user}**.\n"
                f"3. The bot detects it automatically and gives you the role.\n\n"
                f"⏱️ *The bot is listening to live chat... (code expires in 15 minutes)*"
            ),
            color=discord.Color.red()
        )
        await interaction.response.edit_message(
            embed=embed,
            view=LiveVerificationView(self.tiktok_user, self.code, self.total_coins)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_pending_verification(interaction.user.id)
        await interaction.response.edit_message(content="❌ Verification cancelled.", embed=None, view=None)

class LowActivityVerificationView(discord.ui.View):
    """Verification for accounts with fewer than 10 coins (or no transactions)."""
    def __init__(self, tiktok_user: str, total_coins: int):
        super().__init__(timeout=None)
        self.tiktok_user = tiktok_user
        self.total_coins = total_coins

    @discord.ui.button(label="Verify via Live Greeting", style=discord.ButtonStyle.danger, emoji="🔴")
    async def via_live_greeting(self, interaction: discord.Interaction, button: discord.ui.Button):
        live_hosts = get_current_live_hosts()
        if not live_hosts:
            code = create_pending_verification(interaction.user.id, self.tiktok_user, method='bio')
            embed = build_bio_verification_embed(self.tiktok_user, code)
            embed.description = (
                f"⚪ **No host is live right now.**\n\n" + embed.description
            )
            await interaction.response.edit_message(
                embed=embed,
                view=BioVerificationView(self.tiktok_user, code, self.total_coins, low_activity=True)
            )
            return

        create_pending_verification(interaction.user.id, self.tiktok_user, method='live_greeting')
        await interaction.response.edit_message(
            embed=build_live_greeting_embed(self.tiktok_user, live_hosts),
            view=LiveGreetingVerificationView(self.tiktok_user, self.total_coins)
        )

    @discord.ui.button(label="Verify via Bio", style=discord.ButtonStyle.success, emoji="📝")
    async def via_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        code = create_pending_verification(interaction.user.id, self.tiktok_user, method='bio')
        await interaction.response.edit_message(
            embed=build_bio_verification_embed(self.tiktok_user, code),
            view=BioVerificationView(self.tiktok_user, code, self.total_coins, low_activity=True)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_pending_verification(interaction.user.id)
        await interaction.response.edit_message(content="❌ Verification cancelled.", embed=None, view=None)

def build_live_greeting_embed(tiktok_user: str, live_hosts: list) -> discord.Embed:
    host_lines = "\n".join(
        [f"• `@{h}` — https://www.tiktok.com/@{h}/live" for h in live_hosts]
    )
    return discord.Embed(
        title="🔴 Verify via Live Greeting",
        description=(
            f"TikTok account: **@{tiktok_user}**\n\n"
            f"🟢 **Hosts live right now:**\n{host_lines}\n\n"
            f"**What to do:**\n"
            f"1. Join one of the live streams above using TikTok account **@{tiktok_user}**.\n"
            f"2. Post an English welcome message, for example:\n"
            f"   `hello`, `hello host`, `hiii`, `hey`, or `good evening`.\n"
            f"3. The bot will detect your account and your message automatically and verify you.\n\n"
            f"⏱️ *This verification request is valid for 15 minutes.*"
        ),
        color=discord.Color.red()
    )

class BioVerificationView(discord.ui.View):
    """Persistent bio verification: the button stays clickable and can be retried."""

    def __init__(self, tiktok_user: str = None, code: str = None, total_coins: int = 0, low_activity: bool = False):
        super().__init__(timeout=None)
        self.tiktok_user = tiktok_user
        self.code = code
        self.total_coins = total_coins
        self.low_activity = low_activity

    def _resolve_pending(self, discord_id: int):
        pending = get_pending_verification(discord_id)
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        if not tiktok_user:
            return None, None
        code = (pending or {}).get("code")
        if not pending or pending.get("method") != "bio" or \
                (pending.get("tiktok_user") or "").lower() != tiktok_user.lower():
            code = create_pending_verification(discord_id, tiktok_user, method="bio")
        self.tiktok_user = tiktok_user
        self.code = code
        return tiktok_user, code

    @discord.ui.button(
        label="Verify Bio",
        style=discord.ButtonStyle.success,
        emoji="🔍",
        custom_id="bio_verify_persistent"
    )
    async def check_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Defer immediately: DB/Turso calls and TikTok fetch can exceed Discord's
        # 3-second interaction response deadline.
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        except Exception:
            return

        if not is_feature_enabled("verification"):
            await interaction.followup.send(
                "⛔ Verification is currently disabled. Please try again later.",
                ephemeral=True
            )
            return

        tiktok_user, code = self._resolve_pending(interaction.user.id)
        if not tiktok_user:
            await interaction.followup.send(
                "⌛ Your verification session expired. Open the verification panel and start again.",
                ephemeral=True
            )
            return

        is_valid, current_bio = await async_verify_tiktok_bio(tiktok_user, code)

        if is_valid:
            bind_user_mapping(interaction.user.id, tiktok_user)
            cancel_pending_verification(interaction.user.id)
            _, total_coins, _ = is_tiktok_user_in_db(tiktok_user)
            log_activity_event(
                "verification",
                f"[BIO] @{tiktok_user} verified via TikTok bio.",
                tiktok_user
            )

            if isinstance(interaction.user, discord.Member):
                _, role_info = await grant_verified_access(
                    interaction.user, tiktok_user, total_coins
                )
            else:
                role_info = " Server access has been unlocked."

            embed = discord.Embed(
                title="🎉 Verification Successful!",
                description=(
                    f"Congratulations {interaction.user.mention}!\n\n"
                    f"✅ TikTok account **@{tiktok_user}** has been linked to your Discord account.\n"
                    f"🔒 The account is locked and cannot be claimed by anyone else.\n"
                    f"🌟{role_info}\n\n"
                    f"*You can now remove `{code}` from your TikTok bio. Have fun!*"
                ),
                color=discord.Color.green()
            )
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            wib = pytz.timezone("Asia/Jakarta")
            checked_at = datetime.now(wib).strftime("%H:%M:%S WIB")
            shown_bio = current_bio if current_bio not in (None, "") else "(empty bio)"
            embed = discord.Embed(
                title="⚠️ Verification Text Not Found in Bio",
                description=(
                    f"The text **`{code}`** was not found in the TikTok bio of **@{tiktok_user}**.\n\n"
                    f"📄 **Bio currently read (last checked {checked_at}):**\n"
                    f"> *\"{shown_bio}\"*\n\n"
                    f"💡 **Tips:**\n"
                    f"1. Make sure you pressed **Save** in your TikTok profile.\n"
                    f"2. Wait 5–15 seconds after saving so TikTok updates the data.\n"
                    f"3. Make sure the full text `{code}` is in your bio.\n"
                    f"4. If your bio is private or unavailable, try another method.\n\n"
                    f"🔁 *You can click **Verify Bio** again as many times as you need.*"
                ),
                color=discord.Color.orange()
            )
            await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(
        label="Change Method",
        style=discord.ButtonStyle.secondary,
        emoji="🔙",
        custom_id="bio_change_method_persistent"
    )
    async def change_method(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass
        except Exception:
            return

        pending = get_pending_verification(interaction.user.id)
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        if not tiktok_user:
            await interaction.followup.send(
                "⌛ Your verification session expired. Open the verification panel and start again.",
                ephemeral=True
            )
            return
        _, total_coins, _ = is_tiktok_user_in_db(tiktok_user)
        self.tiktok_user = tiktok_user
        self.total_coins = total_coins

        if total_coins < MIN_COINS_FOR_STANDARD_VERIFICATION:
            embed = discord.Embed(
                title="🔒 Choose Verification Method",
                description=f"Choose a verification method for TikTok account **@{tiktok_user}**:",
                color=discord.Color.blue()
            )
            view = LowActivityVerificationView(tiktok_user, total_coins)
        else:
            code = create_pending_verification(interaction.user.id, tiktok_user, method='choice')
            embed = discord.Embed(
                title="🔒 Choose Verification Method",
                description=f"Choose a verification method for TikTok account **@{tiktok_user}**:",
                color=discord.Color.blue()
            )
            view = VerificationMethodChoiceView(tiktok_user, code, total_coins)
        await interaction.edit_original_response(embed=embed, view=view, content=None)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        emoji="❌",
        custom_id="bio_cancel_persistent"
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass
        except Exception:
            return
        cancel_pending_verification(interaction.user.id)
        await interaction.edit_original_response(content="❌ Verification cancelled.", embed=None, view=None)

class LiveVerificationView(discord.ui.View):
    """Standard live chat code verification (10+ coins)."""
    def __init__(self, tiktok_user: str, code: str, total_coins: int):
        super().__init__(timeout=None)
        self.tiktok_user = tiktok_user
        self.code = code
        self.total_coins = total_coins

    @discord.ui.button(label="Check Verification Status", style=discord.ButtonStyle.primary, emoji="🔄")
    async def check_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_mapped = get_user_mapping(interaction.user.id)
        if current_mapped and current_mapped.lower() == self.tiktok_user.lower():
            if isinstance(interaction.user, discord.Member):
                _, role_info = await grant_verified_access(
                    interaction.user, self.tiktok_user, self.total_coins
                )
            else:
                role_info = " Server access has been unlocked."

            embed = discord.Embed(
                title="🎉 Live Verification Successful!",
                description=(
                    f"Congratulations {interaction.user.mention}!\n\n"
                    f"✅ Your comment was detected in the live chat!\n"
                    f"TikTok account **@{self.tiktok_user}** has been linked to your Discord account.\n"
                    f"🌟{role_info}"
                ),
                color=discord.Color.green()
            )
            await interaction.response.edit_message(embed=embed, view=None)
            return

        live_hosts = get_current_live_hosts()
        if live_hosts:
            host_text = "🟢 **Hosts live right now:**\n" + "\n".join(
                [f"• `@{h}`" for h in live_hosts]
            )
        else:
            host_text = "⚪ **No host is live right now.**"

        embed = discord.Embed(
            title="⏳ Waiting for Your Live Chat Comment",
            description=(
                f"TikTok account: **@{self.tiktok_user}**\n"
                f"Code: **`{self.code}`**\n\n"
                f"{host_text}\n\n"
                f"Post a comment containing **`{self.code}`** in one of our live chats.\n"
                f"Once your comment arrives, verification completes automatically!"
            ),
            color=discord.Color.yellow()
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Switch to Bio", style=discord.ButtonStyle.secondary, emoji="📝")
    async def switch_to_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.code = create_pending_verification(interaction.user.id, self.tiktok_user, method='bio')
        await interaction.response.edit_message(
            embed=build_bio_verification_embed(self.tiktok_user, self.code),
            view=BioVerificationView(self.tiktok_user, self.code, self.total_coins)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_pending_verification(interaction.user.id)
        await interaction.response.edit_message(content="❌ Verification cancelled.", embed=None, view=None)

class LiveGreetingVerificationView(discord.ui.View):
    """Live greeting verification for accounts with fewer than 10 coins."""
    def __init__(self, tiktok_user: str, total_coins: int):
        super().__init__(timeout=None)
        self.tiktok_user = tiktok_user
        self.total_coins = total_coins

    @discord.ui.button(label="Check Verification Status", style=discord.ButtonStyle.primary, emoji="🔄")
    async def check_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        current_mapped = get_user_mapping(interaction.user.id)
        if current_mapped and current_mapped.lower() == self.tiktok_user.lower():
            if isinstance(interaction.user, discord.Member):
                _, role_info = await grant_verified_access(
                    interaction.user, self.tiktok_user, self.total_coins
                )
            else:
                role_info = " Server access has been unlocked."

            embed = discord.Embed(
                title="🎉 Live Greeting Verification Successful!",
                description=(
                    f"Congratulations {interaction.user.mention}!\n\n"
                    f"✅ Your welcome message was detected in the live chat!\n"
                    f"TikTok account **@{self.tiktok_user}** has been linked to your Discord account.\n"
                    f"🌟{role_info}"
                ),
                color=discord.Color.green()
            )
            await interaction.response.edit_message(embed=embed, view=None)
            return

        live_hosts = get_current_live_hosts()
        if live_hosts:
            embed = build_live_greeting_embed(self.tiktok_user, live_hosts)
        else:
            embed = discord.Embed(
                title="⏳ Waiting for a Live Stream",
                description=(
                    f"No host is live right now.\n\n"
                    f"TikTok account **@{self.tiktok_user}** will be verified automatically "
                    f"as soon as you post a welcome message in one of our live streams.\n\n"
                    f"💡 *You can also switch to bio verification if you do not want to wait.*"
                ),
                color=discord.Color.yellow()
            )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Switch to Bio", style=discord.ButtonStyle.secondary, emoji="📝")
    async def switch_to_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        code = create_pending_verification(interaction.user.id, self.tiktok_user, method='bio')
        await interaction.response.edit_message(
            embed=build_bio_verification_embed(self.tiktok_user, code),
            view=BioVerificationView(self.tiktok_user, code, self.total_coins, low_activity=True)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cancel_pending_verification(interaction.user.id)
        await interaction.response.edit_message(content="❌ Verification cancelled.", embed=None, view=None)

async def handle_live_comment_verification(tiktok_user: str, comment_text: str, target_host: str):
    """Verify a pending request when the matching TikTok account comments in a live stream.

    Supports both the standard verification code and the English welcome-message
    flow used for accounts with fewer than 10 coins.
    """
    if not is_feature_enabled("verification"):
        return
    pending = find_pending_live_verification(tiktok_user, comment_text)
    greeting_used = False

    if not pending and is_welcome_message(comment_text):
        pending = find_pending_live_greeting(tiktok_user)
        greeting_used = bool(pending)

    if not pending:
        return

    discord_id = pending['discord_id']
    u_clean = tiktok_user.lower()

    # Link the TikTok account to the Discord user
    bind_user_mapping(discord_id, u_clean)
    cancel_pending_verification(discord_id)

    if greeting_used:
        log_activity_event(
            "verification",
            f"[LIVE GREETING] @{u_clean} verified via welcome message \"{comment_text}\" in @{target_host}'s live.",
            target_host
        )
        print(f"[Verification] Live greeting verified for @{u_clean} (Discord ID: {discord_id})")
    else:
        log_activity_event(
            "verification",
            f"[LIVE CODE] @{u_clean} verified via live chat code in @{target_host}'s live.",
            target_host
        )
        print(f"[Verification] Live comment verified for @{u_clean} (Discord ID: {discord_id})")

    # Get the coin balance
    _, total_coins, _ = is_tiktok_user_in_db(u_clean)

    # Grant the verified role in every guild the member belongs to
    for guild in bot.guilds:
        member = guild.get_member(discord_id)
        if member:
            try:
                await grant_verified_access(member, u_clean, total_coins)
                print(f"[Verification] Granted verified role to {member.display_name} in {guild.name}")
            except Exception as e:
                print(f"[Verification] Error granting role in {guild.name}: {e}")

    # Send a DM notification when the user's DMs are open
    try:
        user_obj = await bot.fetch_user(discord_id)
        if user_obj:
            trigger = (
                f"Your welcome message in **@{target_host}**'s live chat was detected!"
                if greeting_used
                else f"Your verification code in **@{target_host}**'s live chat was detected!"
            )
            embed = discord.Embed(
                title="🎉 TikTok Verification Successful!",
                description=(
                    f"Hello {user_obj.name}!\n\n"
                    f"{trigger}\n"
                    f"TikTok account **@{u_clean}** has been linked to your Discord account.\n"
                    f"✅ The verified role has been granted and server access is now open!"
                ),
                color=discord.Color.green()
            )
            await user_obj.send(embed=embed)
    except Exception:
        pass

# --- Discord Events & Commands ---

@bot.event
async def on_ready():
    print(f"[Discord] Bot logged in as {bot.user}")
    runtime_status.set_bot_ready(str(bot.user))
    # Register persistent views so verification buttons keep working after restarts
    bot.add_view(VerificationChannelView())
    bot.add_view(BioVerificationView())

    debug_mode = str(os.environ.get("DEBUG_MODE", "")).strip().lower() in ("1", "true", "yes", "on")
    presence_name = "DEBUG MODE | !help" if debug_mode else "!help | TikTok live monitor"
    try:
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name=presence_name)
        )
    except Exception as e:
        print(f"[Discord] Failed to set presence: {e}")

    if not getattr(bot, "_background_tasks_started", False):
        bot._background_tasks_started = True
        asyncio.create_task(reconcile_announcement_sessions())
        asyncio.create_task(leaderboard_loop())
        asyncio.create_task(tier_reset_loop())


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        key = str(error)
        if "bot_disabled" in key:
            message = "⛔ The bot is currently disabled by an administrator."
        elif "channel_not_allowed" in key:
            message = "⛔ This channel is not allowed to use bot commands."
        else:
            message = "🛡️ This command is restricted to administrators."
        try:
            await ctx.send(message, delete_after=10)
        except Exception:
            pass
        return
    if isinstance(error, commands.MissingRequiredArgument):
        try:
            await ctx.send(f"❌ Missing argument: `{error.param.name}`.", delete_after=10)
        except Exception:
            pass
        return
    print(f"[Command Error] {ctx.command}: {error}")

@bot.event
async def on_member_join(member: discord.Member):
    # Restore roles if the member has verified before
    tt_user = get_user_mapping(member.id)
    if tt_user:
        _, total_coins, _ = is_tiktok_user_in_db(tt_user)
        await grant_verified_access(member, tt_user, total_coins)

@bot.command(name="setup_verification")
@commands.check(is_admin)
async def setup_verification(ctx):
    """Admin command: send the TikTok verification panel to this channel."""
    old_channel_id = get_setting("verification_panel_channel_id", "") or get_setting("verification_channel_id", "") or ""
    old_msg_id = get_setting("verification_panel_message_id", "") or ""
    if old_msg_id.isdigit() and old_channel_id.isdigit() and str(ctx.channel.id) != old_channel_id:
        try:
            old_channel = bot.get_channel(int(old_channel_id)) or await bot.fetch_channel(int(old_channel_id))
            old_msg = await old_channel.fetch_message(int(old_msg_id))
            await old_msg.delete()
        except Exception:
            pass
    set_setting("verification_channel_id", str(ctx.channel.id))
    await send_verification_panel(ctx.channel)
    try:
        await ctx.message.delete()
    except Exception:
        pass

@bot.command(name="set_verified_role")
@commands.check(is_admin)
async def set_verified_role_cmd(ctx, role: discord.Role):
    """Admin command: set the role granted after successful verification."""
    set_setting("verified_role_id", str(role.id))
    await ctx.send(f"✅ Verified role set to: **{role.name}** (`{role.id}`)")

@bot.command(name="unclaim")
@commands.check(is_admin)
async def unclaim(ctx, member: discord.Member):
    """Admin command: remove a member's linked TikTok account."""
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (member.id,)).fetchone()
        if not res:
            await ctx.send(f"❌ {member.mention} has no linked TikTok account.")
            return
        tt_user = res[0]
        conn.execute("DELETE FROM user_mappings WHERE discord_id = ?", (member.id,))
        conn.commit()
        await ctx.send(f"✅ Removed the link to TikTok account `@{tt_user}` from {member.mention}.")
    finally:
        conn.close()

@bot.command(name="mycoins", aliases=["check", "coins"])
async def mycoins(ctx):
    """Show the coin balance of your linked TikTok account."""
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
        mapping = res.fetchone()
        if not mapping:
            await ctx.send(
                f"❌ {ctx.author.mention}, you have not verified a TikTok account yet! "
                f"Use the button in the verification channel."
            )
            return

        tiktok_user = mapping[0]
        res = conn.execute("SELECT display_name, total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (tiktok_user,))
        gift_data = res.fetchone()
        display_name = gift_data[0] if (gift_data and gift_data[0]) else tiktok_user
        total_coins = gift_data[1] if gift_data else 0

        await ctx.send(f"📊 **Coin balance for {ctx.author.display_name}:** @{tiktok_user} | **{total_coins} c**")
    finally:
        conn.close()

@bot.command(name="myhistory")
async def myhistory(ctx):
    """Show your 5 most recent gift transactions."""
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (ctx.author.id,))
        mapping = res.fetchone()
        if not mapping:
            await ctx.send(
                f"❌ {ctx.author.mention}, you have not linked an account yet! "
                f"Use the button in the verification channel."
            )
            return

        tiktok_user = mapping[0]
        res = conn.execute("""
            SELECT target_username, gift_name, gift_count, coins, created_at
            FROM gift_logs
            WHERE viewer_username = ?
            ORDER BY created_at DESC LIMIT 5
        """, (tiktok_user,))
        rows = res.fetchall()

        if not rows:
            await ctx.send("📜 No gift history recorded yet.")
            return

        msg = f"📜 **Recent gifts for @{tiktok_user}:**\n"
        for host, g_name, g_count, coins, dt in rows:
            msg += f"• `{dt}` - **{g_count}x {g_name}** ({coins}c) to host **@{host}**\n"

        await ctx.send(msg)
    finally:
        conn.close()

@bot.command(name="topsupporter", aliases=["leaderboard", "topdonator"])
async def topsupporter(ctx):
    """Show the all-time top supporters leaderboard."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT tiktok_user, display_name, total_coins FROM tiktok_gifts ORDER BY total_coins DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        await ctx.send("❌ No gift data recorded yet.")
        return
    await ctx.send(embed=build_leaderboard_embed("🏆 All-Time Top Supporters", rows, "All-Time Coins"))

@bot.command(name="dailyrank", aliases=["daily", "today"])
async def dailyrank(ctx):
    """Show today's coin ranking."""
    rows = get_daily_top_supporters()
    if not rows:
        await ctx.send("❌ No coin data recorded for today yet.")
        return
    await ctx.send(embed=build_leaderboard_embed("🏆 Today's Coin Ranking", rows, "Today's Coins"))

@bot.command(name="livesessions", aliases=["sessions"])
async def livesessions(ctx, host_username: str = None):
    """Show the most recent live sessions."""
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
            await ctx.send("❌ No live session history recorded yet.")
            return

        msg = "📹 **Most recent live sessions:**\n"
        for s_id, host, s_time, dur_sec, coins in rows:
            hrs = dur_sec // 3600
            mins = (dur_sec % 3600) // 60
            secs = dur_sec % 60
            dur_str = f"{hrs:02d}:{mins:02d}:{secs:02d}" if dur_sec > 0 else "Still live / disconnected"
            msg += f"• Session `#{s_id}` (@{host}) | Started: `{s_time}` | Duration: `{dur_str}` | Total: **{coins} c**\n"

        await ctx.send(msg)
    finally:
        conn.close()

@bot.command(name="help", aliases=["commands"])
async def help_command(ctx):
    """Show all available bot commands."""
    embed = discord.Embed(
        title="📖 Dummy Dream Bot Help",
        description=(
            f"Hello {ctx.author.mention}! Here are the available commands.\n"
            f"Prefix: `!` • Example: `!mycoins`"
        ),
        color=discord.Color.from_rgb(0, 168, 255)
    )
    embed.add_field(
        name="🔑 Verification & Account",
        value=(
            "Use the **Verify TikTok Account** button in the verification channel.\n"
            "`!mycoins` — show your total coins (`!check`, `!coins`)\n"
            "`!myhistory` — your 5 most recent gift transactions"
        ),
        inline=False
    )
    embed.add_field(
        name="🏆 Stats & Live",
        value=(
            "`!topsupporter` — supporter leaderboard (`!leaderboard`, `!topdonator`)\n"
            "`!dailyrank` — today's coin ranking (`!daily`, `!today`)\n"
            "`!livesessions` — most recent live sessions (`!sessions`)\n"
            "`!livesessions <username>` — live sessions for a specific account\n"
            "`!help` — show this help message"
        ),
        inline=False
    )

    admin_viewer = is_admin_member(ctx.author)
    if admin_viewer:
        embed.add_field(
            name="🛡️ Admin",
            value=(
                "`!setup_verification` — send the verification panel to this channel\n"
                "`!set_verified_role @role` — set the role granted after verification\n"
                "`!unclaim @member` — remove a member's linked TikTok account"
            ),
            inline=False
        )

    live_hosts = get_current_live_hosts()
    if live_hosts:
        embed.add_field(
            name="🟢 Live Now",
            value="\n".join(
                [f"• @{h} — https://www.tiktok.com/@{h}/live" for h in live_hosts]
            ),
            inline=False
        )

    embed.set_footer(text="Dummy Dream Bot • Type !help anytime")
    await ctx.send(embed=embed)

async def main():
    init_db()
    close_all_open_live_sessions()
    set_verification_refresh_handler(schedule_verification_panel_refresh)
    keep_alive(handle_api_announcement)

    # Start observers with announcement, role, and live chat verification callbacks
    asyncio.create_task(run_all_observers(
        announcement_callback=process_live_announcement,
        role_callback=auto_assign_role,
        comment_callback=handle_live_comment_verification
    ))

    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        runtime_status.set_bot_offline()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Bot] Stopped successfully.")

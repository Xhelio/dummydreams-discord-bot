import asyncio
import os
import json
import re
import sys
import discord
from discord import app_commands
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
    suspend_open_live_sessions,
    save_verification_status,
    delete_verification_status
)
from utils import get_schedule_info, load_summary_config
from server import keep_alive
from observer import run_all_observers, get_current_live_hosts, is_any_host_live, probe_room_live
from verification import async_verify_tiktok_bio
from routes.settings import set_verification_refresh_handler, set_presence_refresh_handler
import runtime_status
import chatbot

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
ANNOUNCEMENT_CHANNEL_ID = int(os.environ.get('ANNOUNCEMENT_CHANNEL_ID', "0"))

NOTIFICATION_LOCK = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
runtime_status.register_bot(bot)

# Saat main.py dijalankan sebagai script (Render: `python main.py`), modulnya
# bernama "__main__". Daftarkan alias "main" supaya Flask (routes/admin.py)
# mengakses instance bot yang SAMA, bukan meng-import ulang main.py dan
# membuat bot kedua yang tidak pernah connect.
if __name__ == "__main__":
    sys.modules.setdefault("main", sys.modules[__name__])


def is_admin_member(member) -> bool:
    """Admin = configured admin role, or server administrator when no role is set."""
    if member is None:
        return False
    role_id = get_setting("admin_role_id", "") or ""
    if role_id.isdigit() and isinstance(member, discord.Member):
        return any(r.id == int(role_id) for r in member.roles)
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and perms.administrator)


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
    problems = []
    verified_role = get_verified_role(member.guild) if member and member.guild else None

    if verified_role is None:
        problems.append("the verification role is not configured in Bot Settings")
    elif verified_role in member.roles:
        pass
    else:
        try:
            me = member.guild.me
            if me is None and bot.user is not None:
                me = await member.guild.fetch_member(bot.user.id)
            if me is not None and verified_role >= me.top_role:
                problems.append(
                    f"the bot's role ({me.top_role.name}) is below the verification role ({verified_role.name})"
                )
            elif me is not None and not me.guild_permissions.manage_roles:
                problems.append("the bot does not have Manage Roles permission")
            else:
                await member.add_roles(
                    verified_role,
                    reason=f"Verification: TikTok @{tiktok_user}",
                )
                roles_added_names.append(verified_role.name)
        except Exception as e:
            problems.append(f"Discord rejected the role assignment: {e}")

    if problems:
        problem_text = "; ".join(problems)
        print(f"[Role Error] Failed to add verified role for @{tiktok_user}: {problem_text}")
        try:
            log_activity_event(
                "system",
                f"[Verify] Failed to grant verified role to {member} for @{tiktok_user}: {problem_text}.",
                tiktok_user,
            )
        except Exception:
            pass
        role_info = f" ⚠️ The role could not be granted automatically ({problem_text}). Please contact an admin."
    else:
        role_info = (
            f" Role **{', '.join(roles_added_names)}** has been granted."
            if roles_added_names else " The role is already active."
        )

    # Berikan juga tier roles sesuai total koin yang ada.
    try:
        await auto_assign_role(tiktok_user, total_coins)
    except Exception as e:
        print(f"[Role Error] Failed to assign coin tier for @{tiktok_user}: {e}")

    return True, role_info


async def grant_verified_access_for_discord_id(discord_id, tiktok_user: str, total_coins: int) -> tuple:
    """Grant verified (and tier) role across all guilds for a Discord user ID.

    Works even when the interaction happens in DMs or the member is not cached.
    Returns (granted_any: bool, role_info: str).
    """
    try:
        did = int(str(discord_id).strip())
    except (TypeError, ValueError):
        return False, " ⚠️ Invalid Discord ID for role assignment."

    found = 0
    granted = 0
    already = 0
    problems = []
    for guild in list(getattr(bot, "guilds", []) or []):
        member = guild.get_member(did)
        if member is None:
            try:
                member = await guild.fetch_member(did)
            except Exception:
                member = None
        if member is None:
            continue
        found += 1
        try:
            ok, info = await grant_verified_access(member, tiktok_user, total_coins)
            if "could not be granted automatically" in (info or "") or "⚠️" in (info or ""):
                problems.append(f"{guild.name}: {info.strip()}")
            elif "has been granted" in (info or ""):
                granted += 1
            else:
                already += 1
        except Exception as e:
            problems.append(f"{guild.name}: {e}")

    if found == 0:
        return False, " ⚠️ Discord account not found in any server the bot is in. Make sure you have joined the server, then try re-verifying or ask an admin to grant the role."
    if problems:
        # If some succeeded or already ok, be explicit but still report issues.
        summary = "; ".join(problems[:2])
        return granted > 0 or already > 0, f" ⚠️ Role could not be granted automatically in some servers ({summary})."
    if granted > 0:
        return True, " The verification role has been granted."
    return True, " The role is already active."

async def revoke_verification_roles_for_discord_id(discord_id) -> tuple:
    """Remove the verified role AND every coin tier role for a Discord user.

    Used when a link is removed (admin "Unclaim" or /unclaim) so Discord never
    keeps a role that the database no longer justifies.
    Returns (ok: bool, detail: str).
    """
    try:
        did = int(str(discord_id).strip())
    except (TypeError, ValueError):
        return False, "Invalid Discord ID."

    tier_role_ids = set()
    try:
        for tier in get_role_tiers(None) or []:
            rid = str(tier.get("role_id") or "").strip()
            if rid.isdigit():
                tier_role_ids.add(int(rid))
    except Exception as e:
        print(f"[Role Error] Could not load tier roles for revoke: {e}")

    found = 0
    removed_total = 0
    problems = []

    for guild in list(getattr(bot, "guilds", []) or []):
        member = guild.get_member(did)
        if member is None:
            try:
                member = await guild.fetch_member(did)
            except Exception:
                member = None
        if member is None:
            continue
        found += 1

        roles_to_remove = []
        try:
            verified_role = get_verified_role(guild)
        except Exception:
            verified_role = None
        if verified_role is not None and verified_role in member.roles:
            roles_to_remove.append(verified_role)
        for role in member.roles:
            if role.id in tier_role_ids and role not in roles_to_remove:
                roles_to_remove.append(role)

        if not roles_to_remove:
            continue
        try:
            await member.remove_roles(*roles_to_remove, reason="Unclaim: revoke verification and tier roles")
            removed_total += len(roles_to_remove)
            print(
                f"[Discord] Revoked {len(roles_to_remove)} role(s) from {member} in '{guild.name}': "
                f"{', '.join(r.name for r in roles_to_remove)}"
            )
        except Exception as e:
            problems.append(f"{guild.name}: {e}")
            print(f"[Role Error] Failed to revoke roles for {did} in '{guild.name}': {e}")

    if found == 0:
        return False, "Discord account not found in any server the bot is in."
    if problems:
        return removed_total > 0, "Could not revoke in: " + "; ".join(problems[:2])
    return True, f"Removed {removed_total} role(s)."


def revoke_verification_roles(discord_id) -> tuple:
    """Existing event-loop-safe wrapper for Flask routes (runs on the bot loop)."""
    try:
        loop = getattr(bot, "loop", None)
    except Exception:
        loop = None
    if loop is None or not loop.is_running():
        return False, "Bot event loop is not running."
    try:
        future = asyncio.run_coroutine_threadsafe(
            revoke_verification_roles_for_discord_id(discord_id), loop
        )
        return future.result(timeout=20)
    except Exception as e:
        return False, f"Revoke failed: {e}"


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
            member = await guild.fetch_member(int(discord_id))
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
    """Fetch a stored announcement message.

    Returns (message, missing):
      - (Message, False) when found
      - (None, True) when the message is confirmed deleted/missing
      - (None, False) on transient/unknown fetch errors (do NOT treat as missing)
    """
    if not channel or not message_id:
        return None, True
    try:
        msg = await channel.fetch_message(int(message_id))
        return msg, False
    except discord.NotFound:
        print(f"[Discord] Stored announcement message {message_id} not found (deleted).")
        return None, True
    except Exception as e:
        print(f"[Discord] Could not fetch announcement message {message_id} (transient?): {e}")
        return None, False

async def process_live_announcement(username: str, is_live: bool, host_name: str = None):
    """Maintain one live announcement message per target account per channel.

    - Start / still live: send once per configured channel, edit on host changes.
    - Offline: delete every stored message for the account and clear sessions.
    - Channels removed from the admin panel are cleaned up automatically.
    """
    user_key = username.lower().lstrip("@")
    try:
        async with NOTIFICATION_LOCK:
            if not bot.is_ready():
                await bot.wait_until_ready()

            sessions = get_active_live_announcements_for_account(user_key)

            # --- OFFLINE: remove all stored announcement messages ---
            if not is_live:
                for session in sessions:
                    try:
                        channel = await _resolve_announcement_channel(session)
                        old_msg, missing = await _fetch_announcement_message(
                            channel, session.get("message_id")
                        )
                        if old_msg:
                            try:
                                await old_msg.delete()
                                print(f"[Discord] Deleted live announcement for @{user_key} in channel {session.get('channel_id')}.")
                            except Exception as e:
                                print(f"[Discord] Failed to delete announcement message for @{user_key} in {session.get('channel_id')}: {e}")
                        elif missing:
                            print(f"[Discord] Announcement message already missing for @{user_key} in {session.get('channel_id')}; clearing session.")
                        else:
                            print(f"[Discord] Could not verify announcement message for @{user_key} in {session.get('channel_id')} (transient fetch error); clearing session record but leaving message untouched to avoid duplicates.")
                    except Exception as e:
                        print(f"[Discord] Error while cleaning announcement for @{user_key} in {session.get('channel_id')}: {e}")
                    finally:
                        delete_active_live_announcement(user_key, session.get("channel_id"))
                if sessions:
                    print(f"[Discord] Announcement sessions cleared for @{user_key}.")
                else:
                    print(f"[Discord] No stored announcement sessions to clear for @{user_key}.")
                return

            if not is_feature_enabled("announcements"):
                return

            configured_channels = get_announcement_channel_ids()
            if not configured_channels:
                print("[Discord] Notification failed: no announcement channel configured.")
                return
            # Dedupe while preserving order to avoid duplicate sends on misconfig.
            seen_channels = set()
            deduped_channels = []
            for cid in configured_channels:
                try:
                    key = int(cid)
                except (TypeError, ValueError):
                    key = cid
                if key in seen_channels:
                    continue
                seen_channels.add(key)
                deduped_channels.append(cid)
            configured_channels = deduped_channels

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
                    old_msg, missing = await _fetch_announcement_message(channel, session.get("message_id"))
                    if old_msg:
                        try:
                            await old_msg.delete()
                        except Exception:
                            pass
                    # Only drop the session record when we are sure it's gone or deleted.
                    # If fetch failed transiently, keep the record to avoid duplicates later.
                    if old_msg or missing:
                        delete_active_live_announcement(user_key, session.get("channel_id"))

            for channel_id in configured_channels:
                session = get_active_live_announcement(user_key, channel_id)
                channel = await _resolve_announcement_channel({"channel_id": channel_id})
                if not channel:
                    continue

                if session:
                    old_msg, missing = await _fetch_announcement_message(channel, session.get("message_id"))
                    if old_msg:
                        if old_msg.content != live_content:
                            try:
                                await old_msg.edit(content=live_content)
                                print(f"[Discord] Updated live announcement for @{user_key} in {channel_id}.")
                            except Exception as e:
                                print(f"[Discord] Failed to edit announcement for @{user_key}: {e}")
                        # Session exists and message is valid: never send a duplicate.
                        continue
                    if not missing:
                        # Transient fetch failure: do NOT send a replacement (would duplicate).
                        print(f"[Discord] Skipping announcement send for @{user_key} in {channel_id}: existing message could not be verified (will retry next trigger).")
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
                        print(f"[Discord] Announcement sent but session was not saved for @{user_key} in {channel.id} (Msg ID: {new_msg.id}); deleting message to avoid duplicate.")
                        try:
                            await new_msg.delete()
                        except Exception:
                            pass
                    else:
                        print(f"[Discord] Sent live announcement for @{user_key} in {channel.id} (Msg ID: {new_msg.id}).")
                except Exception as e:
                    # If we sent a message but failed to save its ID, duplicates can happen on next trigger.
                    print(f"[Discord] CRITICAL: sent announcement but failed to save session for @{user_key} in {channel.id} (Msg ID: {new_msg.id}): {e}; deleting message to avoid duplicate.")
                    try:
                        await new_msg.delete()
                    except Exception:
                        pass

            update_active_live_announcement_host(user_key, final_host_name)
    except Exception as e:
        print(f"[Discord] process_live_announcement error for @{user_key} (is_live={is_live}): {e}")

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
            "3. Verification continues in your **Direct Messages** (so it won't disappear).\n"
            "4. Choose a verification method (**Live Greeting**, **Live Chat Code**, or **Bio**).\n"
            "5. Once confirmed, your TikTok account is permanently linked and your server access role activates automatically!"
        ),
        color=discord.Color.from_rgb(0, 168, 255)
    )
    embed.set_footer(text="Dummy Dream Bot • Verification & Security System")
    return embed


def build_rules_welcome_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📜 Welcome to the Server!",
        description=(
            "Welcome! We’re glad to have you here.\n"
            "Please take a moment to read the rules below to help keep this community safe, friendly, and enjoyable for everyone."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Server Rules (1/2)",
        value=(
            "**1. Be respectful.** Treat everyone with kindness. Harassment, hate speech, discrimination, bullying, or personal attacks are not allowed.\n"
            "**2. Keep it appropriate.** No NSFW, explicit, or disturbing content. Keep discussions and media suitable for the community.\n"
            "**3. No spam or self-promotion.** Avoid flooding chats, excessive mentions, repeated messages, or unsolicited advertising/links without permission.\n"
            "**4. Use the right channels.** Post content in the appropriate channels and follow any pinned guidelines or channel topics."
        ),
        inline=False,
    )
    embed.add_field(
        name="Server Rules (2/2)",
        value=(
            "**5. No cheating, scams, or malicious activity.** Do not share malware, phishing links, exploits, or attempt to deceive/scam members.\n"
            "**6. Protect privacy and safety.** Do not share personal information (yours or others’) or engage in doxxing.\n"
            "**7. Follow Discord’s Terms & Guidelines.** Comply with [Discord’s Terms of Service](https://discord.com/terms) and [Community Guidelines](https://discord.com/guidelines).\n"
            "**8. Listen to staff.** Moderators and admins may take action to maintain a safe environment. Contact staff respectfully if you need help."
        ),
        inline=False,
    )
    embed.add_field(
        name="Getting Started",
        value=(
            "• **Read & follow the rules** to keep access to the server.\n"
            "• **Verify your account** (if required) in the designated verification channel to unlock full access/roles.\n"
            "• **Introduce yourself** and join the conversation in the appropriate channels.\n"
            "• If you need help, reach out to a moderator or admin."
        ),
        inline=False,
    )
    embed.set_footer(text="By participating in this server, you agree to follow these rules.")
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


def get_push_code() -> str:
    push_code = (
        os.environ.get("PUSH_CODE")
        or os.environ.get("BOT_VERSION")
        or os.environ.get("RENDER_GIT_COMMIT", "")[:7]
    )
    if not push_code:
        try:
            import subprocess
            push_code = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=os.path.dirname(__file__) or ".",
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            ).strip()
        except Exception:
            push_code = "unknown"
    return push_code or "unknown"


def get_bot_env() -> str:
    """Return the deployment environment label: 'stable' (default), 'test', or custom."""
    raw = str(os.environ.get("BOT_ENV") or os.environ.get("APP_ENV") or "stable").strip().lower()
    if raw in ("prod", "production", "stable", "live"):
        return "stable"
    if raw in ("test", "testing", "staging", "dev", "development"):
        return "test"
    return raw or "stable"


async def apply_bot_presence():
    if not bot.is_ready():
        return
    env_debug = str(os.environ.get("DEBUG_MODE", "")).strip().lower() in ("1", "true", "yes", "on")
    debug_mode = get_bool_setting("debug_mode", False) or env_debug
    bot_env = get_bot_env()
    push_code = get_push_code()
    parts = []
    if bot_env != "stable":
        parts.append(bot_env.upper())
    if debug_mode:
        parts.append(f"DEBUG {push_code}")
    parts.append("!help / /help | TikTok live monitor")
    presence_name = " | ".join(parts)
    print(f"[Discord] Presence updated: {presence_name} (env={bot_env}, debug={debug_mode})")
    try:
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name=presence_name)
        )
    except Exception as e:
        print(f"[Discord] Failed to set presence: {e}")


def schedule_presence_refresh():
    try:
        asyncio.run_coroutine_threadsafe(apply_bot_presence(), bot.loop)
    except Exception as e:
        print(f"[Discord] Could not schedule presence refresh: {e}")



MIN_COINS_FOR_STANDARD_VERIFICATION = 10


def get_coin_only_verification_config() -> dict:
    """Coin-only verification ("role murni"): grant access with no ownership proof.

    The claimed TikTok account only has to reach ``coin_only_min_coins`` total
    coins. Admins control it from Bot Settings.
    """
    try:
        enabled = get_bool_setting("coin_only_verification_enabled", False)
    except Exception:
        enabled = False
    try:
        min_coins = int(get_setting("coin_only_min_coins", "100") or 100)
    except (TypeError, ValueError):
        min_coins = 100
    if min_coins < 0:
        min_coins = 0
    return {"enabled": enabled, "min_coins": min_coins}


def build_coin_only_success_embed(tiktok_user: str, total_coins: int, min_coins: int,
                                  role_info: str) -> discord.Embed:
    """Confirmation embed for a coin-only (no proof) grant."""
    return discord.Embed(
        title="✅ Verification Complete (Coin-only)",
        description=(
            f"Your Discord account is now linked to TikTok **@{tiktok_user}**.\n\n"
            f"💰 Total coins: **{total_coins:,} c** "
            f"(minimum for automatic access: **{min_coins:,} c**)\n"
            f"🎭 Role: {role_info or 'granted'}\n\n"
            f"⚠️ This access was granted **without ownership proof** because the "
            f"account already reached the coin threshold. If this account is not "
            f"yours, contact an admin immediately — claiming someone else's account "
            f"can get your Discord account removed from the server."
        ),
        color=discord.Color.green()
    )


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
            f"Use the **📋 Copy Code** button below to copy the code easily.\n\n"
            f"**Steps:**\n"
            f"1. Open the TikTok app.\n"
            f"2. Go to **Profile** -> **Edit profile** -> **Bio**.\n"
            f"3. Paste the verification text above into your bio and save it.\n"
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

        # 3b. Coin-only mode: grant access without ownership proof once the
        # claimed account has reached the admin-configured coin threshold.
        coin_cfg = get_coin_only_verification_config()
        if coin_cfg["enabled"] and int(total_coins or 0) >= coin_cfg["min_coins"]:
            if not bind_user_mapping(interaction.user.id, u_clean):
                await interaction.followup.send(
                    "❌ I could not link your account right now. Please try again.",
                    ephemeral=True
                )
                return

            log_activity_event(
                "verification",
                f"[Coin-only] {interaction.user} (ID {interaction.user.id}) linked @{u_clean} "
                f"with {total_coins} coins (no proof, threshold {coin_cfg['min_coins']}).",
                u_clean
            )
            granted, role_info = await grant_verified_access_for_discord_id(
                interaction.user.id, u_clean, total_coins
            )
            try:
                save_verification_status(
                    interaction.user.id,
                    discord_name=str(interaction.user),
                    discord_handle=getattr(interaction.user, "name", "") or "",
                    guild_name=getattr(interaction.guild, "name", "") if interaction.guild else "",
                    has_role=True if granted else None,
                    role_name=role_info or "",
                    error="" if granted else (role_info or "Role could not be granted"),
                )
            except Exception as e:
                print(f"[Verification] Failed to save coin-only status: {e}")

            if granted:
                embed = build_coin_only_success_embed(
                    u_clean, total_coins, coin_cfg["min_coins"], role_info
                )
            else:
                embed = discord.Embed(
                    title="⚠️ Account linked, but the role could not be granted",
                    description=(
                        f"Your Discord account is now linked to TikTok **@{u_clean}** "
                        f"({total_coins:,} c).\n\n"
                        f"**Reason:** {role_info}\n\n"
                        f"Please contact an admin so they can grant the role manually."
                    ),
                    color=discord.Color.orange()
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
            print(
                f"[Verification] Coin-only grant for {interaction.user} -> "
                f"@{u_clean} ({total_coins} c) | {role_info}"
            )
            return

        embed, view = build_verification_start(
            interaction.user.id, u_clean, total_coins, display_name
        )
        try:
            await interaction.user.send(embed=embed, view=view)
            await interaction.followup.send(
                "📬 Verification steps were sent to your **Direct Messages**. Please continue there (DMs won't disappear like ephemeral messages).",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ I couldn't DM you. Please enable DMs from this server, or continue here (this message may disappear if dismissed).",
                ephemeral=True
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            print(f"[Verification] Failed to DM verification flow: {e}")
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

    def _resolve_pending(self, discord_id):
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
        # 3-second interaction response deadline. Defer on the message so we can
        # update the verification panel in place (not an ephemeral-only response).
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass
        except Exception:
            return

        if not is_feature_enabled("verification"):
            await interaction.edit_original_response(
                content="⛔ Verification is currently disabled. Please try again later.",
                embed=None,
                view=None
            )
            return

        tiktok_user, code = self._resolve_pending(interaction.user.id)
        if not tiktok_user:
            await interaction.edit_original_response(
                content="⌛ Your verification session expired. Open the verification panel and start again.",
                embed=None,
                view=None
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

            # Grant verified/tier roles across all guilds (works in DM and in-server).
            try:
                _, role_info = await grant_verified_access_for_discord_id(
                    interaction.user.id, tiktok_user, total_coins
                )
            except Exception as e:
                print(f"[Verification] Auto-grant after bio verify failed for {interaction.user.id}: {e}")
                role_info = f" ⚠️ Verification succeeded, but the role could not be granted automatically: {e}. Please contact an admin."

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
            raw_bio = current_bio if current_bio is not None else ""
            shown_bio = raw_bio if str(raw_bio).strip() != "" else "(bio is empty / could not be read / profile may be private)"
            shown_bio = str(shown_bio)
            if len(shown_bio) > 1200:
                shown_bio = shown_bio[:1200] + "\n…(truncated)"
            # Escape triple backticks to keep code block intact
            shown_bio_safe = shown_bio.replace("```", "'''")
            embed = discord.Embed(
                title="⚠️ Verification Text Not Found in Bio",
                description=(
                    f"The text **`{code}`** was not found in the TikTok bio of **@{tiktok_user}**.\n\n"
                    f"📄 **Bio read by the bot (last checked {checked_at}):**\n"
                    f"```text\n{shown_bio_safe}\n```\n"
                    f"💡 **Tips:**\n"
                    f"1. Make sure you pressed **Save** in your TikTok profile.\n"
                    f"2. Wait 5–15 seconds after saving so TikTok updates the data, then try again.\n"
                    f"3. Make sure the full text **`{code}`** is in your bio (use the **📋 Copy Code** button to avoid typos).\n"
                    f"4. If your bio is private or cannot be read, try another verification method.\n\n"
                    f"🔁 *You can click **Verify Bio** again anytime.*"
                ),
                color=discord.Color.orange()
            )
            await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(
        label="Copy Code",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
        custom_id="bio_copy_code_persistent"
    )
    async def copy_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        except Exception:
            return

        pending = get_pending_verification(interaction.user.id)
        code = (pending or {}).get("code") or self.code
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        if not code:
            if tiktok_user:
                code = create_pending_verification(interaction.user.id, tiktok_user, method="bio")
                self.code = code
            else:
                await interaction.followup.send(
                    "⌛ Session expired. Please open the verification panel and start again.",
                    ephemeral=True
                )
                return
        self.tiktok_user = tiktok_user or self.tiktok_user
        self.code = code
        embed = discord.Embed(
            title="📋 Verification Code",
            description=(
                f"TikTok: **@{tiktok_user or 'your TikTok'}**\n\n"
                f"**Code (tap and hold to copy):**\n"
                f"```text\n{code}\n```\n"
                f"Paste this code into your TikTok bio, then click **Verify Bio**."
            ),
            color=discord.Color.blurple(),
        )
        # Also include plain code for easy selection/copy on clients that show content.
        await interaction.followup.send(content=f"`{code}`", embed=embed, ephemeral=True)

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
            await interaction.edit_original_response(
                content="⌛ Your verification session expired. Open the verification panel and start again.",
                embed=None,
                view=None
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
                _, role_info = await grant_verified_access_for_discord_id(
                    interaction.user.id, self.tiktok_user, self.total_coins
                )

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
                _, role_info = await grant_verified_access_for_discord_id(
                    interaction.user.id, self.tiktok_user, self.total_coins
                )

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

    # Grant the verified role in every guild the member belongs to (fetch if not cached)
    try:
        granted_any, role_info = await grant_verified_access_for_discord_id(discord_id, u_clean, total_coins)
        if granted_any:
            print(f"[Verification] Auto-granted verified role for @{u_clean} (Discord {discord_id}). {role_info.strip()}")
        else:
            print(f"[Verification] Auto-grant role issue for @{u_clean} (Discord {discord_id}): {role_info.strip()}")
    except Exception as e:
        print(f"[Verification] Error granting verified role for @{u_clean} (Discord {discord_id}): {e}")

    # Send a DM notification when the user's DMs are open
    try:
        user_obj = await bot.fetch_user(int(discord_id))
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
async def on_message(message):
    # Chatbot: reply when mentioned / replied to (and in DMs), then still process commands.
    try:
        if await chatbot.should_respond(message, bot.user):
            async with message.channel.typing():
                reply, err = await chatbot.generate_reply(message, bot.user)
            if reply:
                await message.reply(reply, mention_author=False)
            elif err and str(os.environ.get("CHATBOT_DEBUG", "")).strip().lower() in ("1", "true", "yes", "on"):
                print(f"[Chatbot] error: {err}")
    except Exception as e:
        print(f"[Chatbot] handler error: {e}")
    await bot.process_commands(message)


@bot.event
async def on_ready():
    bot_env = get_bot_env()
    print(f"[Discord] Bot logged in as {bot.user} (env={bot_env}, version={get_push_code()})")
    if bot_env == "test":
        print("[Discord] TEST environment active: use a separate bot token, database, and server from stable.")
    if chatbot.is_chatbot_enabled():
        cfg = chatbot.get_chatbot_config()
        print(f"[Chatbot] enabled (model={cfg.get('model')}, base_url={cfg.get('base_url')})")
    else:
        print("[Chatbot] disabled (set OLLAMA_BASE_URL and OLLAMA_MODEL to enable)")

    # Optional safety guard: ensure this deployment is connected to the intended guild.
    expected_guild_id = str(os.environ.get("EXPECTED_GUILD_ID", "") or "").strip()
    if expected_guild_id.isdigit():
        current_ids = {str(g.id) for g in (bot.guilds or [])}
        if expected_guild_id not in current_ids:
            print(
                f"[Discord] WARNING: connected guilds {sorted(current_ids)} do not include "
                f"EXPECTED_GUILD_ID={expected_guild_id}. Check BOT_ENV/token/config before writing data."
            )
        else:
            print(f"[Discord] Guild check OK: connected to expected guild {expected_guild_id}.")

    runtime_status.set_bot_ready(str(bot.user))
    # Register persistent views so verification buttons keep working after restarts
    bot.add_view(VerificationChannelView())
    bot.add_view(BioVerificationView())

    await apply_bot_presence()

    if not getattr(bot, "_background_tasks_started", False):
        bot._background_tasks_started = True
        try:
            synced = await bot.tree.sync()
            print(f"[Discord] Synced {len(synced)} slash command(s).")
        except Exception as e:
            print(f"[Discord] Slash command sync failed: {e}")
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

async def _unclaim_discord_member(member: discord.Member):
    """Remove a member's TikTok link plus the roles that link justified.

    Returns (ok: bool, message: str).
    """
    conn = get_db_connection()
    try:
        res = conn.execute(
            "SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (str(member.id),)
        ).fetchone()
    finally:
        conn.close()
    if not res:
        return False, f"❌ {member.mention} has no linked TikTok account."

    tt_user = res[0]
    roles_ok, role_detail = await revoke_verification_roles_for_discord_id(member.id)

    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM user_mappings WHERE discord_id = ?", (str(member.id),))
        conn.commit()
    finally:
        conn.close()
    try:
        delete_verification_status(member.id)
    except Exception as e:
        print(f"[Unclaim] Failed to delete verification status for {member.id}: {e}")

    log_activity_event(
        "verification",
        f"[Unclaim] Link @{tt_user} removed from {member} (ID {member.id}) | roles: {role_detail}",
        tt_user,
    )
    print(f"[Unclaim] {member} (ID {member.id}) unlinked from @{tt_user} | {role_detail}")

    if roles_ok:
        return True, (
            f"✅ Removed the link to TikTok account `@{tt_user}` from {member.mention}.\n"
            f"Discord roles: {role_detail}"
        )
    return True, (
        f"⚠️ Removed the link to TikTok account `@{tt_user}` from {member.mention}, "
        f"but the Discord roles could not be fully revoked.\n"
        f"Details: {role_detail}"
    )


@bot.command(name="unclaim")
@commands.check(is_admin)
async def unclaim(ctx, member: discord.Member):
    """Admin command: remove a member's linked TikTok account and its roles."""
    ok, message = await _unclaim_discord_member(member)
    await ctx.send(message)

@bot.command(name="mycoins", aliases=["check", "coins"])
async def mycoins(ctx):
    """Show the coin balance of your linked TikTok account."""
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (str(ctx.author.id),))
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
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (str(ctx.author.id),))
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
@commands.check(is_admin)
async def livesessions(ctx, host_username: str = None):
    """Admin command: show the most recent live sessions, optionally filtered by host account."""
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

def build_help_embed(user) -> discord.Embed:
    embed = discord.Embed(
        title="📖 Dummy Dream Bot Help",
        description=(
            f"Hello {getattr(user, 'mention', user)}! Here are the available commands.\n"
            f"Text prefix: `!` • Slash commands: `/` (Discord menu)"
        ),
        color=discord.Color.from_rgb(0, 168, 255)
    )
    embed.add_field(
        name="🔑 Verification & Account",
        value=(
            "Use the **Verify TikTok Account** button in the verification channel.\n"
            "`!mycoins` or `/mycoins` — show your total coins\n"
            "`!myhistory` or `/myhistory` — your 5 most recent gift transactions"
        ),
        inline=False
    )
    embed.add_field(
        name="🏆 Stats & Live",
        value=(
            "`!topsupporter` or `/topsupporter` — supporter leaderboard\n"
            "`!dailyrank` or `/dailyrank` — today's coin ranking\n"
            "`!help` or `/help` — show this help message"
        ),
        inline=False
    )

    admin_viewer = is_admin_member(user)
    if admin_viewer:
        embed.add_field(
            name="🛡️ Admin",
            value=(
                "`!setup_verification` or `/setup_verification` — send the verification panel\n"
                "`!set_verified_role` or `/set_verified_role` — set the verified role\n"
                "`!unclaim` or `/unclaim` — remove a member's linked TikTok account\n"
                "`!livesessions` or `/livesessions` — recent live sessions (admin only)"
            ),
            inline=False
        )

    live_hosts = get_current_live_hosts()
    if live_hosts:
        embed.add_field(
            name="🟢 Live Now",
            value="\n".join([f"• @{h} — https://www.tiktok.com/@{h}/live" for h in live_hosts]),
            inline=False
        )
    embed.set_footer(text="Dummy Dream Bot • !help or /help")
    return embed


@bot.command(name="help", aliases=["commands"])
async def help_command(ctx):
    """Show all available bot commands."""
    await ctx.send(embed=build_help_embed(ctx.author))

# --- Discord Slash Commands (hybrid with ! text commands) ---

@bot.tree.command(name="help", description="Show all available bot commands")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_help_embed(interaction.user), ephemeral=True)


@bot.tree.command(name="mycoins", description="Show the coin balance of your linked TikTok account")
async def slash_mycoins(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (str(interaction.user.id),))
        mapping = res.fetchone()
        if not mapping:
            await interaction.followup.send("❌ Your account is not verified yet. Use the button in the verification channel.", ephemeral=True)
            return
        tt_user = mapping[0]
        res = conn.execute("SELECT display_name, total_coins FROM tiktok_gifts WHERE LOWER(tiktok_user) = ?", (tt_user.lower(),))
        row = res.fetchone()
        if not row:
            await interaction.followup.send(f"ℹ️ TikTok account **@{tt_user}** is linked, but no coin data was found yet.", ephemeral=True)
            return
        display_name, total_coins = row
        msg = (
            f"👤 **Account Info**\n"
            f"• Discord: {interaction.user.mention}\n"
            f"• TikTok: **@{tt_user}** ({display_name or 'No name'})\n"
            f"• **Total Coins: {int(total_coins or 0):,} c**"
        )
        await interaction.followup.send(msg, ephemeral=True)
    finally:
        conn.close()


@bot.tree.command(name="myhistory", description="Show your 5 most recent gift transactions")
async def slash_myhistory(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (str(interaction.user.id),))
        mapping = res.fetchone()
        if not mapping:
            await interaction.followup.send("❌ Your account is not verified yet. Use the button in the verification channel.", ephemeral=True)
            return
        tt_user = mapping[0]
        res = conn.execute("""
            SELECT target_username, gift_name, gift_count, coins, log_datetime
            FROM gift_logs
            WHERE LOWER(viewer_username) = ?
            ORDER BY log_id DESC LIMIT 5
        """, (tt_user.lower(),))
        rows = res.fetchall()
        if not rows:
            await interaction.followup.send(f"❌ No gift history found for **@{tt_user}** yet.", ephemeral=True)
            return
        msg = f"📜 **Last 5 Gift Transactions (@{tt_user})**\n\n"
        for target, gift, count, coins, dt in rows:
            msg += f"• `{dt}`: **{gift}** x{count} ({coins} c) on @{target}\n"
        await interaction.followup.send(msg, ephemeral=True)
    finally:
        conn.close()


@bot.tree.command(name="topsupporter", description="Show the all-time top supporters leaderboard")
async def slash_topsupporter(interaction: discord.Interaction):
    await interaction.response.defer()
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT tiktok_user, display_name, total_coins FROM tiktok_gifts ORDER BY total_coins DESC LIMIT 10"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        await interaction.followup.send("❌ No gift data recorded yet.")
        return
    await interaction.followup.send(embed=build_leaderboard_embed("🏆 All-Time Top Supporters", rows, "All-Time Coins"))


@bot.tree.command(name="dailyrank", description="Show today's coin ranking")
async def slash_dailyrank(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = get_daily_top_supporters()
    if not rows:
        await interaction.followup.send("❌ No coin data recorded for today yet.")
        return
    await interaction.followup.send(embed=build_leaderboard_embed("🏆 Today's Coin Ranking", rows, "Today's Coins"))


@bot.tree.command(name="livesessions", description="Admin: show recent live sessions")
@app_commands.describe(host_username="Optional TikTok host username filter")
async def slash_livesessions(interaction: discord.Interaction, host_username: str = None):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_member(interaction.user):
        await interaction.followup.send("🛡️ This command is restricted to administrators.", ephemeral=True)
        return
    conn = get_db_connection()
    try:
        if host_username:
            u_clean = host_username.lstrip("@").lower()
            rows = conn.execute("""
                SELECT tiktok_username, start_time, end_time, duration_seconds, total_coins, scheduled_host
                FROM live_sessions
                WHERE LOWER(tiktok_username) = ?
                ORDER BY start_time DESC LIMIT 5
            """, (u_clean,)).fetchall()
            title = f"@{u_clean}"
        else:
            rows = conn.execute("""
                SELECT tiktok_username, start_time, end_time, duration_seconds, total_coins, scheduled_host
                FROM live_sessions
                ORDER BY start_time DESC LIMIT 10
            """).fetchall()
            title = "All Hosts"
        if not rows:
            await interaction.followup.send("❌ No live session data found yet.", ephemeral=True)
            return
        msg = f"📹 **Recent Live Sessions ({title})**\n\n"
        for user, start, end, dur, coins, host in rows:
            end_str = end if end else "🟢 Live now"
            dur_str = f"{dur // 3600}h {(dur % 3600) // 60}m" if dur else "-"
            msg += f"• **@{user}** [{start} - {end_str}] ({dur_str})\n  Host: {host} | {coins} c\n\n"
        await interaction.followup.send(msg, ephemeral=True)
    finally:
        conn.close()


@bot.tree.command(name="setup_verification", description="Admin: send the TikTok verification panel to this channel")
async def slash_setup_verification(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_member(interaction.user) or not interaction.guild or not interaction.channel:
        await interaction.followup.send("🛡️ This command is restricted to administrators in a server channel.", ephemeral=True)
        return
    old_channel_id = get_setting("verification_panel_channel_id", "") or get_setting("verification_channel_id", "") or ""
    old_msg_id = get_setting("verification_panel_message_id", "") or ""
    if old_msg_id.isdigit() and old_channel_id.isdigit() and str(interaction.channel.id) != old_channel_id:
        try:
            old_channel = bot.get_channel(int(old_channel_id)) or await bot.fetch_channel(int(old_channel_id))
            old_msg = await old_channel.fetch_message(int(old_msg_id))
            await old_msg.delete()
        except Exception:
            pass
    set_setting("verification_channel_id", str(interaction.channel.id))
    await send_verification_panel(interaction.channel)
    await interaction.followup.send("✅ Verification panel sent to this channel.", ephemeral=True)


@bot.tree.command(name="set_verified_role", description="Admin: set the role granted after verification")
@app_commands.describe(role="Role to grant after successful verification")
async def slash_set_verified_role(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_member(interaction.user):
        await interaction.followup.send("🛡️ This command is restricted to administrators.", ephemeral=True)
        return
    set_setting("verified_role_id", str(role.id))
    await interaction.followup.send(f"✅ Verified role set to: **{role.name}** (`{role.id}`)", ephemeral=True)


verification_group = app_commands.Group(name="verification", description="Verification setup commands")


@verification_group.command(name="setup", description="Admin: send the welcome/rules message to this channel")
async def verification_setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_member(interaction.user) or not interaction.guild or not interaction.channel:
        await interaction.followup.send(
            "🛡️ This command is restricted to administrators in a server channel.",
            ephemeral=True,
        )
        return
    await interaction.channel.send(embed=build_rules_welcome_embed())
    await interaction.followup.send("✅ Welcome/rules message sent to this channel.", ephemeral=True)


bot.tree.add_command(verification_group)


@bot.tree.command(name="welcome-setup", description="Admin: send the welcome/rules message to this channel")
async def slash_welcome_setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_member(interaction.user) or not interaction.guild or not interaction.channel:
        await interaction.followup.send(
            "🛡️ This command is restricted to administrators in a server channel.",
            ephemeral=True,
        )
        return
    await interaction.channel.send(embed=build_rules_welcome_embed())
    await interaction.followup.send("✅ Welcome/rules message sent to this channel.", ephemeral=True)


@bot.tree.command(name="unclaim", description="Admin: remove a member's linked TikTok account")
@app_commands.describe(member="Member to unclaim")
async def slash_unclaim(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_member(interaction.user):
        await interaction.followup.send("🛡️ This command is restricted to administrators.", ephemeral=True)
        return
    ok, message = await _unclaim_discord_member(member)
    await interaction.followup.send(message, ephemeral=True)


async def main():
    init_db()
    suspend_open_live_sessions()
    set_verification_refresh_handler(schedule_verification_panel_refresh)
    set_presence_refresh_handler(schedule_presence_refresh)
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

import asyncio
import concurrent.futures
import io
import logging
import os
import json
import re
import sys
import time
from datetime import datetime

import pytz
import discord
from discord import app_commands
from discord.ext import commands

from db import (
    init_db,
    warm_settings_cache,
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
    get_pending_live_usernames,
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
    delete_verification_status,
    record_verification_method,
    get_last_gift_for_tiktok
)
from utils import get_schedule_info, load_summary_config
from server import keep_alive
from observer import run_all_observers, get_current_live_hosts, is_any_host_live, probe_room_live
from verification import async_verify_tiktok_bio
from routes.settings import set_verification_refresh_handler, set_presence_refresh_handler
import runtime_status
import chatbot
import log_setup

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN')
ANNOUNCEMENT_CHANNEL_ID = int(os.environ.get('ANNOUNCEMENT_CHANNEL_ID', "0"))

NOTIFICATION_LOCK = asyncio.Lock()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


def _describe_user(user) -> str:
    """Readable 'name (display name)' label for notification context."""
    try:
        base = str(user)
        display = getattr(user, "display_name", None)
        handle = getattr(user, "name", None)
        if display and display != handle and display not in base:
            return f"{base} ({display})"
        return base
    except Exception:
        return "unknown"


def _bind_interaction_context(interaction, command: str = None, custom_id: str = None) -> None:
    """Attach who/where/what to every log record (and notification) of a task."""
    try:
        data = interaction.data or {}
        user = interaction.user
        guild = getattr(interaction, "guild", None)
        channel = getattr(interaction, "channel", None)
        if command is None and interaction.type is discord.InteractionType.application_command:
            command = str(data.get("name") or "")
        if custom_id is None:
            custom_id = str(data.get("custom_id") or "")
        if channel is None:
            channel_label = None
        elif getattr(channel, "name", None):
            channel_label = f"#{channel.name}" if guild is not None else "DM"
        elif getattr(channel, "guild", None) is None:
            channel_label = "DM"
        else:
            channel_label = str(getattr(channel, "id", None) or "-")
        log_setup.bind_log_context(
            actor=_describe_user(user),
            user_id=getattr(user, "id", None),
            guild=getattr(guild, "name", None) or "DM",
            channel=channel_label,
            command=(f"/{command}" if command else None),
            custom_id=custom_id or None,
        )
    except Exception:
        pass


class _LoggingCommandTree(app_commands.CommandTree):
    """Command tree that records slash-command context for notifications."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        _bind_interaction_context(interaction)
        return True


bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
    tree_cls=_LoggingCommandTree,
    http_trace=log_setup.build_http_trace(),
)
runtime_status.register_bot(bot)
log = log_setup.get_logger("bot.main")

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

def _note_interaction_lag(interaction: discord.Interaction, label: str):
    """Log gateway -> handler delay.

    Discord invalidates an interaction when the handler has not acknowledged it
    within ~3 seconds. If this prints >= 1.5s the event loop was blocked before
    the callback ran, which is the cause of "didn't respond in time".
    """
    try:
        lag = time.time() - interaction.created_at.timestamp()
    except Exception:
        return
    if lag >= 1.5:
        log.warning(
            "[Interaction Lag] %s: handled after %.2fs (user=%s)",
            label,
            lag,
            getattr(interaction.user, "id", "?"),
        )


async def _safe_defer(interaction: discord.Interaction, label: str, *,
                      ephemeral: bool = False, thinking: bool = False) -> bool:
    """Acknowledge an interaction as early as possible, with clear logging.

    Discord shows "didn't respond in time" when the interaction is not
    acknowledged within ~3 seconds. This helper records how long the ACK took
    (so a blocked event loop is visible in the log) and tolerates a response
    that was already acknowledged elsewhere.
    """
    started = time.perf_counter()
    _bind_interaction_context(interaction)
    actor = _describe_user(interaction.user)
    try:
        if interaction.response.is_done():
            log.info(
                "[Verify] %s: already acknowledged by another handler "
                "(custom_id=%s user=%s (%s))",
                label,
                str((interaction.data or {}).get("custom_id") or "-"),
                getattr(interaction.user, "id", "?"),
                actor,
            )
            return True
        await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        log.info(
            "[Verify] %s: acknowledged in %.2fs (custom_id=%s user=%s (%s))",
            label,
            time.perf_counter() - started,
            str((interaction.data or {}).get("custom_id") or "-"),
            getattr(interaction.user, "id", "?"),
            actor,
        )
        return True
    except discord.InteractionResponded:
        return True
    except discord.HTTPException as e:
        # 10062 = Unknown interaction (already expired), 40060 = already
        # acknowledged. These mean Discord's 3s window was missed, which is
        # expected during a freeze; log it without paging the error channel.
        if getattr(e, "code", None) in (10062, 40060):
            log.info(
                "[Verify] %s: interaction expired before ACK (code=%s, custom_id=%s user=%s)",
                label,
                getattr(e, "code", None),
                str((interaction.data or {}).get("custom_id") or "-"),
                getattr(interaction.user, "id", "?"),
            )
        else:
            log.warning(
                "[Verify] %s: could not acknowledge interaction: %s: %s",
                label,
                type(e).__name__,
                e,
            )
        return False
    except Exception as e:
        log.warning(
            "[Verify] %s: could not acknowledge interaction: %s: %s",
            label,
            type(e).__name__,
            e,
        )
        return False


def get_verification_click_timeout() -> float:
    """Seconds a verification click may spend on TikTok/DB work.

    Configurable in the admin panel (``verification_click_timeout_seconds``,
    default 45 s, clamped to 5-120 s). Kept well below Discord's 15-minute
    interaction-token lifetime so the user always gets a clear answer.
    """
    try:
        value = float(get_setting("verification_click_timeout_seconds", "45") or 45)
    except (TypeError, ValueError):
        value = 45.0
    return max(5.0, min(value, 120.0))


def build_verification_timeout_embed(action: str) -> discord.Embed:
    """Explain that a verification click hit its time limit."""
    seconds = int(get_verification_click_timeout())
    return discord.Embed(
        title="⏱️ Verification Timed Out",
        description=(
            f"The **{action}** check took longer than **{seconds} seconds**, "
            f"so the bot stopped waiting.\n\n"
            f"**What to do:**\n"
            f"1. Wait a few seconds, then click the button again.\n"
            f"2. Make sure your TikTok profile and bio are public.\n"
            f"3. If it keeps happening, try the other verification method or ask an admin.\n\n"
            f"⏱️ *The current limit is {seconds}s; admins can change it in the admin panel.*"
        ),
        color=discord.Color.orange(),
    )


async def _run_verify_step(step, label: str):
    """Await one verification step with the configured timeout.

    Returns ``(True, result)`` when it finished, ``(False, None)`` on timeout.
    The caller edits the deferred response so the user is never left waiting.
    """
    timeout = get_verification_click_timeout()
    started = time.perf_counter()
    try:
        return True, await asyncio.wait_for(step, timeout=timeout)
    except asyncio.TimeoutError:
        log.warning(
            "[Verify] %s timed out after %.1fs (limit %.0fs); click aborted cleanly.",
            label,
            time.perf_counter() - started,
            timeout,
        )
        return False, None


class VerificationView(discord.ui.View):
    """Base for verification views: never leave a click unanswered.

    Without ``on_error`` an exception inside a button callback makes Discord
    show "didn't respond in time", which hides the real error from the member.
    """

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        _bind_interaction_context(interaction)
        log.exception(
            "[Verify] %s failed on %s: %s: %s",
            getattr(item, "custom_id", "-"),
            interaction.type.name,
            type(error).__name__,
            error,
        )
        embed = discord.Embed(
            title="❌ Verification Error",
            description=(
                "Something went wrong while handling that click, so nothing changed.\n\n"
                "Please try again. If it keeps happening, screenshot this message and ask an admin "
                "to check the bot log."
            ),
            color=discord.Color.red(),
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass


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
            await asyncio.to_thread(
                log_activity_event,
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

    discord_id = await asyncio.to_thread(get_discord_id_from_tiktok, tiktok_user)
    if not discord_id:
        return

    tier_cfg = await asyncio.to_thread(get_tier_settings)
    groups = []
    if tier_cfg["enabled"]:
        groups.append("reset")
    if tier_cfg["permanent_enabled"]:
        groups.append("permanent")
    if not groups:
        return

    stacked = str(tier_cfg.get("mode", "replace")).lower() == "stacked"

    # Fetch each tier list once instead of per guild (each read opens a new
    # Turso connection; this runs after every gift event).
    tiers_by_group = {}
    for group_key in groups:
        tiers_by_group[group_key] = await asyncio.to_thread(get_role_tiers, group_key)

    for guild in bot.guilds:
        try:
            member = await guild.fetch_member(int(discord_id))
        except (discord.NotFound, discord.HTTPException):
            continue

        roles_to_add = []
        roles_to_remove = []
        added_tiers = []

        for group_key in groups:
            tiers = tiers_by_group.get(group_key, [])
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

            sessions = await asyncio.to_thread(get_active_live_announcements_for_account, user_key)

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
                        await asyncio.to_thread(delete_active_live_announcement, user_key, session.get("channel_id"))
                if sessions:
                    print(f"[Discord] Announcement sessions cleared for @{user_key}.")
                else:
                    print(f"[Discord] No stored announcement sessions to clear for @{user_key}.")
                return

            if not is_feature_enabled("announcements"):
                return

            configured_channels = await asyncio.to_thread(get_announcement_channel_ids)
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
                        await asyncio.to_thread(delete_active_live_announcement, user_key, session.get("channel_id"))

            for channel_id in configured_channels:
                session = await asyncio.to_thread(get_active_live_announcement, user_key, channel_id)
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
                    saved = await asyncio.to_thread(
                        save_active_live_announcement, user_key, channel.id, new_msg.id, final_host_name
                    )
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

            await asyncio.to_thread(update_active_live_announcement_host, user_key, final_host_name)
    except Exception as e:
        print(f"[Discord] process_live_announcement error for @{user_key} (is_live={is_live}): {e}")

async def reconcile_announcement_sessions(delay_seconds: int = 180):
    """Clean stale announcement sessions after restart without deleting active lives.

    Runtime status can be empty shortly after restart, so we probe before deleting.
    """
    await asyncio.sleep(delay_seconds)
    try:
        sessions = await asyncio.to_thread(get_all_active_live_announcements)
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

    def _load_leaderboard_rows():
        conn = get_db_connection()
        try:
            all_time = conn.execute("""
                SELECT tiktok_user, display_name, total_coins
                FROM tiktok_gifts ORDER BY total_coins DESC LIMIT 10
            """).fetchall()
        finally:
            conn.close()
        return all_time, get_daily_top_supporters()

    all_time, daily = await asyncio.to_thread(_load_leaderboard_rows)

    embed = build_leaderboard_embed("🏆 All-Time Top Supporters", all_time)
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
    await asyncio.to_thread(set_setting, "leaderboard_message_id", str(msg.id))
    await asyncio.to_thread(set_setting, "leaderboard_message_channel_id", str(channel.id))


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
        await asyncio.to_thread(mark_tier_reset_done)
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
            "4. Choose a verification method (**Live Greeting** or **Bio**).\n"
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
    await asyncio.to_thread(set_setting, "verification_panel_channel_id", str(channel.id))
    await asyncio.to_thread(set_setting, "verification_panel_message_id", str(msg.id))


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


# In-memory guard: usernames that currently have a pending live verification.
# Without it every live-chat comment opened a Turso connection just to discover
# that nobody was verifying, which could starve the event loop.
_LIVE_PENDING_CACHE = {}  # tiktok username -> expiry epoch seconds
_LIVE_PENDING_TTL = 15 * 60


def _cache_live_pending(tiktok_user: str, expires_at: str = None):
    u = str(tiktok_user or "").lower().lstrip("@")
    if not u:
        return
    expires_epoch = time.time() + _LIVE_PENDING_TTL
    if expires_at:
        try:
            wib = pytz.timezone("Asia/Jakarta")
            expires_epoch = datetime.strptime(
                str(expires_at), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=wib).timestamp()
        except Exception:
            pass
    _LIVE_PENDING_CACHE[u] = expires_epoch


def _drop_live_pending(tiktok_user: str):
    _LIVE_PENDING_CACHE.pop(str(tiktok_user or "").lower().lstrip("@"), None)


def _has_live_pending(tiktok_user: str) -> bool:
    u = str(tiktok_user or "").lower().lstrip("@")
    expires = _LIVE_PENDING_CACHE.get(u)
    if not expires:
        return False
    if expires < time.time():
        _LIVE_PENDING_CACHE.pop(u, None)
        return False
    return True


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
        name="🔴 Option 2 — Live greeting",
        value=(
            "Join one of our live streams and post a normal welcome message such as "
            "`hello`, `hello host`, or `hiii` from your TikTok account. "
            "The bot detects it automatically."
        ),
        inline=False
    )
    return embed, VerificationMethodChoiceView(tiktok_user, code, total_coins)

class VerificationChannelView(VerificationView):
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

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        _bind_interaction_context(interaction, custom_id="modal_tiktok_username")
        log.exception(
            "[Verify] modal submit failed: %s: %s", type(error).__name__, error
        )
        embed = discord.Embed(
            title="❌ Verification Error",
            description=(
                "Something went wrong while reading your TikTok username.\n\n"
                "Please try again. If it keeps happening, screenshot this message and ask an admin."
            ),
            color=discord.Color.red(),
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass

    async def on_submit(self, interaction: discord.Interaction):
        _bind_interaction_context(interaction, custom_id="modal_tiktok_username")
        _note_interaction_lag(interaction, "verification modal submit")
        # Defer before DB lookups to avoid Discord "didn't respond in time".
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.InteractionResponded:
            pass
        except Exception as e:
            print(f"[Verification] Could not ACK modal submit: {type(e).__name__}: {e}")
            return

        if not is_feature_enabled("verification"):
            await interaction.followup.send(
                "⛔ Verification is currently disabled. Please try again later.",
                ephemeral=True
            )
            return
        u_clean = self.username_input.value.strip().lstrip('@').lower()
        log_setup.bind_log_context(tiktok=f"@{u_clean}")
        actor = f"{interaction.user} (ID {interaction.user.id})"

        # Run the log + three pre-checks in a single worker task. Separate
        # asyncio.to_thread calls queue separately behind the shared DB
        # executor; during a busy live that can add seconds before the user
        # gets any feedback.
        def _precheck():
            log_activity_event(
                "verification",
                f"[VERIFY ATTEMPT] {actor} requested verification for @{u_clean}.",
                u_clean
            )
            existing = get_user_mapping(interaction.user.id)
            if existing:
                return "linked", existing
            claimed, claimed_id = is_user_already_claimed(u_clean)
            if claimed:
                return "claimed", claimed_id
            return "ok", is_tiktok_user_in_db(u_clean)

        ok, precheck = await _run_verify_step(
            asyncio.to_thread(_precheck), "verification precheck"
        )
        if not ok:
            await interaction.followup.send(
                embed=build_verification_timeout_embed("verification precheck"),
                ephemeral=True,
            )
            return
        precheck_status, precheck_payload = precheck

        # 1. Discord user already linked?
        if precheck_status == "linked":
            existing_tiktok = precheck_payload
            await asyncio.to_thread(
                log_activity_event,
                "verification",
                f"[VERIFY ATTEMPT] {actor} blocked: Discord account already linked to @{existing_tiktok}.",
                u_clean
            )
            await interaction.followup.send(
                f"ℹ️ Your Discord account is already verified with TikTok **@{existing_tiktok}**.\n"
                f"You do not need to verify again.",
                ephemeral=True
            )
            return

        # 2. TikTok account claimed by someone else?
        if precheck_status == "claimed":
            claimed_discord_id = precheck_payload
            await asyncio.to_thread(
                log_activity_event,
                "verification",
                f"[VERIFY ATTEMPT] {actor} blocked: @{u_clean} is already claimed by Discord ID {claimed_discord_id}.",
                u_clean
            )
            await interaction.followup.send(
                f"❌ TikTok account **@{u_clean}** is already linked to another Discord account.\n"
                f"One TikTok account can only be claimed by one Discord account. "
                f"If this is yours, please contact an admin.",
                ephemeral=True
            )
            return

        # 3. Account lookup completed inside the same worker task above.
        exists, total_coins, display_name = precheck_payload

        # 3b. Coin-only mode: grant access without ownership proof once the
        # claimed account has reached the admin-configured coin threshold.
        coin_cfg = get_coin_only_verification_config()
        if coin_cfg["enabled"] and int(total_coins or 0) >= coin_cfg["min_coins"]:
            linked = await asyncio.to_thread(bind_user_mapping, interaction.user.id, u_clean)
            if not linked:
                await interaction.followup.send(
                    "❌ I could not link your account right now. Please try again.",
                    ephemeral=True
                )
                return

            try:
                await asyncio.to_thread(
                    record_verification_method,
                    interaction.user.id,
                    "coin_only",
                    f"{int(total_coins or 0):,} coins (min {coin_cfg['min_coins']:,})"
                )
            except Exception as e:
                print(f"[Verification] Failed to record coin-only method: {e}")

            await asyncio.to_thread(
                log_activity_event,
                "verification",
                f"[Coin-only] {interaction.user} (ID {interaction.user.id}) linked @{u_clean} "
                f"with {total_coins} coins (no proof, threshold {coin_cfg['min_coins']}).",
                u_clean
            )
            granted, role_info = await grant_verified_access_for_discord_id(
                interaction.user.id, u_clean, total_coins
            )
            try:
                await asyncio.to_thread(
                    save_verification_status,
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

        flow_name = (
            "low-activity"
            if int(total_coins or 0) < MIN_COINS_FOR_STANDARD_VERIFICATION
            else "standard"
        )
        await asyncio.to_thread(
            log_activity_event,
            "verification",
            f"[VERIFY ATTEMPT] {actor} started {flow_name} verification for @{u_clean} "
            f"({int(total_coins or 0):,} c).",
            u_clean
        )

        embed, view = await asyncio.to_thread(
            build_verification_start,
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

class VerificationMethodChoiceView(VerificationView):
    """Standard verification (10+ coins): bio code or live greeting."""
    def __init__(self, tiktok_user: str = None, code: str = None, total_coins: int = 0):
        super().__init__(timeout=None)
        self.tiktok_user = tiktok_user
        self.code = code
        self.total_coins = total_coins

    async def _resolve(self, interaction: discord.Interaction):
        pending = await asyncio.to_thread(get_pending_verification, interaction.user.id)
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        if not tiktok_user:
            return None, 0
        log_setup.bind_log_context(tiktok=f"@{tiktok_user}")
        _, total_coins, _ = await asyncio.to_thread(is_tiktok_user_in_db, tiktok_user)
        return tiktok_user, int(total_coins or 0)

    @discord.ui.button(label="Verify via Bio", style=discord.ButtonStyle.primary, emoji="📝", custom_id="verify_choice_bio")
    async def via_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        _note_interaction_lag(interaction, "verification method: bio")
        if not await _safe_defer(interaction, "verification method: bio"):
            return
        ok, resolved = await _run_verify_step(
            self._resolve(interaction), "resolve verification request"
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed("verification session"), view=self
            )
            return
        tiktok_user, total_coins = resolved
        if not tiktok_user:
            await interaction.edit_original_response(content="❌ This verification request expired. Please start again from the verification panel.", embed=None, view=None)
            return
        await asyncio.to_thread(
            log_activity_event,
            "verification",
            f"[VERIFY METHOD] {interaction.user} (ID {interaction.user.id}) chose bio for @{tiktok_user}.",
            tiktok_user
        )
        code = await asyncio.to_thread(
            create_pending_verification, interaction.user.id, tiktok_user, 'bio'
        )
        await interaction.edit_original_response(
            embed=build_bio_verification_embed(tiktok_user, code),
            view=BioVerificationView(tiktok_user, code, total_coins)
        )

    @discord.ui.button(label="Verify via Live Greeting", style=discord.ButtonStyle.danger, emoji="🔴", custom_id="verify_choice_live_greeting")
    async def via_live(self, interaction: discord.Interaction, button: discord.ui.Button):
        _note_interaction_lag(interaction, "verification method: live greeting")
        if not await _safe_defer(interaction, "verification method: live greeting"):
            return
        ok, resolved = await _run_verify_step(
            self._resolve(interaction), "resolve verification request"
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed("verification session"), view=self
            )
            return
        tiktok_user, total_coins = resolved
        if not tiktok_user:
            await interaction.edit_original_response(content="❌ This verification request expired. Please start again from the verification panel.", embed=None, view=None)
            return
        # Live verification uses a normal welcome message, not a secret code.
        await asyncio.to_thread(
            create_pending_verification, interaction.user.id, tiktok_user, 'live_greeting'
        )
        _cache_live_pending(tiktok_user)
        await asyncio.to_thread(
            log_activity_event,
            "verification",
            f"[VERIFY METHOD] {interaction.user} (ID {interaction.user.id}) chose live greeting for @{tiktok_user}.",
            tiktok_user
        )
        live_hosts = get_current_live_hosts()
        if live_hosts:
            embed = build_live_greeting_embed(tiktok_user, live_hosts)
        else:
            embed = discord.Embed(
                title="⏳ Waiting for a Live Stream",
                description=(
                    f"No host is live right now.\n\n"
                    f"TikTok account **@{tiktok_user}** will be verified automatically "
                    f"as soon as you post a welcome message (for example `hello` or `hiii`) "
                    f"in one of our live streams.\n\n"
                    f"💡 *You can switch to bio verification if you do not want to wait.*"
                ),
                color=discord.Color.yellow()
            )
        await interaction.edit_original_response(
            embed=embed,
            view=LiveGreetingVerificationView(tiktok_user, total_coins)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌", custom_id="verify_choice_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        pending = await asyncio.to_thread(get_pending_verification, interaction.user.id)
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        await asyncio.to_thread(cancel_pending_verification, interaction.user.id)
        if tiktok_user:
            _drop_live_pending(tiktok_user)
        await interaction.edit_original_response(content="❌ Verification cancelled.", embed=None, view=None)

class LowActivityVerificationView(VerificationView):
    """Verification for accounts with fewer than 10 coins (or no transactions)."""
    def __init__(self, tiktok_user: str = None, total_coins: int = 0):
        super().__init__(timeout=None)
        self.tiktok_user = tiktok_user
        self.total_coins = total_coins

    async def _resolve(self, interaction: discord.Interaction):
        pending = await asyncio.to_thread(get_pending_verification, interaction.user.id)
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        if not tiktok_user:
            return None, 0
        log_setup.bind_log_context(tiktok=f"@{tiktok_user}")
        _, total_coins, _ = await asyncio.to_thread(is_tiktok_user_in_db, tiktok_user)
        return tiktok_user, int(total_coins or 0)

    @discord.ui.button(label="Verify via Live Greeting", style=discord.ButtonStyle.danger, emoji="🔴", custom_id="verify_low_live_greeting")
    async def via_live_greeting(self, interaction: discord.Interaction, button: discord.ui.Button):
        _note_interaction_lag(interaction, "low-activity method: live greeting")
        if not await _safe_defer(interaction, "low-activity method: live greeting"):
            return
        ok, resolved = await _run_verify_step(
            self._resolve(interaction), "resolve verification request"
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed("verification session"), view=self
            )
            return
        tiktok_user, total_coins = resolved
        if not tiktok_user:
            await interaction.edit_original_response(content="❌ This verification request expired. Please start again from the verification panel.", embed=None, view=None)
            return
        await asyncio.to_thread(
            log_activity_event,
            "verification",
            f"[VERIFY METHOD] {interaction.user} (ID {interaction.user.id}) chose live greeting for @{tiktok_user}.",
            tiktok_user
        )
        live_hosts = get_current_live_hosts()
        if not live_hosts:
            code = await asyncio.to_thread(
                create_pending_verification, interaction.user.id, tiktok_user, 'bio'
            )
            embed = build_bio_verification_embed(tiktok_user, code)
            embed.description = (
                f"⚪ **No host is live right now.**\n\n" + embed.description
            )
            await interaction.edit_original_response(
                embed=embed,
                view=BioVerificationView(tiktok_user, code, total_coins, low_activity=True)
            )
            return

        await asyncio.to_thread(
            create_pending_verification, interaction.user.id, tiktok_user, 'live_greeting'
        )
        _cache_live_pending(tiktok_user)
        await interaction.edit_original_response(
            embed=build_live_greeting_embed(tiktok_user, live_hosts),
            view=LiveGreetingVerificationView(tiktok_user, total_coins)
        )

    @discord.ui.button(label="Verify via Bio", style=discord.ButtonStyle.success, emoji="📝", custom_id="verify_low_bio")
    async def via_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        _note_interaction_lag(interaction, "low-activity method: bio")
        if not await _safe_defer(interaction, "low-activity method: bio"):
            return
        ok, resolved = await _run_verify_step(
            self._resolve(interaction), "resolve verification request"
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed("verification session"), view=self
            )
            return
        tiktok_user, total_coins = resolved
        if not tiktok_user:
            await interaction.edit_original_response(content="❌ This verification request expired. Please start again from the verification panel.", embed=None, view=None)
            return
        await asyncio.to_thread(
            log_activity_event,
            "verification",
            f"[VERIFY METHOD] {interaction.user} (ID {interaction.user.id}) chose bio for @{tiktok_user}.",
            tiktok_user
        )
        code = await asyncio.to_thread(
            create_pending_verification, interaction.user.id, tiktok_user, 'bio'
        )
        await interaction.edit_original_response(
            embed=build_bio_verification_embed(tiktok_user, code),
            view=BioVerificationView(tiktok_user, code, total_coins, low_activity=True)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌", custom_id="verify_low_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        pending = await asyncio.to_thread(get_pending_verification, interaction.user.id)
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        await asyncio.to_thread(cancel_pending_verification, interaction.user.id)
        if tiktok_user:
            _drop_live_pending(tiktok_user)
        await interaction.edit_original_response(content="❌ Verification cancelled.", embed=None, view=None)

def build_live_greeting_embed(tiktok_user: str, live_hosts: list) -> discord.Embed:
    host_lines = "\n".join(
        [f"• `@{h}` — https://www.tiktok.com/@{h}/live" for h in live_hosts]
    )
    return discord.Embed(
        title="🔴 Verify via Live Greeting",
        description=(
            f"TikTok account: **@{tiktok_user}**\n"
            f"No verification code needed — just greet normally.\n\n"
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

class BioVerificationView(VerificationView):
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
        _note_interaction_lag(interaction, "bio verify")
        _bind_interaction_context(interaction)
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

        ok, resolved = await _run_verify_step(
            asyncio.to_thread(self._resolve_pending, interaction.user.id),
            "resolve pending bio request",
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed("verification session"), view=self
            )
            return
        tiktok_user, code = resolved
        if not tiktok_user:
            await interaction.edit_original_response(
                content="⌛ Your verification session expired. Open the verification panel and start again.",
                embed=None,
                view=None
            )
            return

        ok, bio_result = await _run_verify_step(
            async_verify_tiktok_bio(tiktok_user, code),
            f"TikTok bio check for @{tiktok_user}",
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed(f"TikTok bio for @{tiktok_user}"),
                view=self,
            )
            return
        is_valid, current_bio = bio_result

        if is_valid:
            await asyncio.to_thread(bind_user_mapping, interaction.user.id, tiktok_user)
            await asyncio.to_thread(cancel_pending_verification, interaction.user.id)
            _drop_live_pending(tiktok_user)
            try:
                await asyncio.to_thread(
                    record_verification_method, interaction.user.id, "bio", f"code: {code}"
                )
            except Exception as e:
                print(f"[Verification] Failed to record bio method: {e}")
            _, total_coins, _ = await asyncio.to_thread(is_tiktok_user_in_db, tiktok_user)
            await asyncio.to_thread(
                log_activity_event,
                "verification",
                f"[BIO] {interaction.user} (ID {interaction.user.id}) verified @{tiktok_user} via TikTok bio.",
                tiktok_user,
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
            await asyncio.to_thread(
                log_activity_event,
                "verification",
                f"[BIO] {interaction.user} (ID {interaction.user.id}) failed bio verification for "
                f"@{tiktok_user}: code `{code}` not found in the profile bio.",
                tiktok_user,
            )
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

        ok, pending = await _run_verify_step(
            asyncio.to_thread(get_pending_verification, interaction.user.id),
            "load bio verification code",
        )
        if not ok:
            await interaction.followup.send(
                embed=build_verification_timeout_embed("bio verification code"),
                ephemeral=True,
            )
            return
        code = (pending or {}).get("code") or self.code
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        if not code:
            if tiktok_user:
                ok, code = await _run_verify_step(
                    asyncio.to_thread(
                        create_pending_verification, interaction.user.id, tiktok_user, "bio"
                    ),
                    "create bio verification code",
                )
                if not ok:
                    await interaction.followup.send(
                        embed=build_verification_timeout_embed("bio verification code"),
                        ephemeral=True,
                    )
                    return
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

        def _load_method_target():
            pending = get_pending_verification(interaction.user.id)
            target = (pending or {}).get("tiktok_user") or self.tiktok_user
            if not target:
                return None, 0
            _, coins, _ = is_tiktok_user_in_db(target)
            return target, int(coins or 0)

        ok, resolved = await _run_verify_step(
            asyncio.to_thread(_load_method_target), "load verification method target"
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed("verification method"), view=self
            )
            return
        tiktok_user, total_coins = resolved
        if not tiktok_user:
            await interaction.edit_original_response(
                content="⌛ Your verification session expired. Open the verification panel and start again.",
                embed=None,
                view=None
            )
            return
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
            code = await asyncio.to_thread(
                create_pending_verification, interaction.user.id, tiktok_user, 'choice'
            )
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
        await asyncio.to_thread(cancel_pending_verification, interaction.user.id)
        _drop_live_pending(self.tiktok_user)
        await interaction.edit_original_response(content="❌ Verification cancelled.", embed=None, view=None)

class LiveVerificationView(VerificationView):
    """Legacy live chat code verification (kept for old pending requests)."""
    def __init__(self, tiktok_user: str = None, code: str = None, total_coins: int = 0):
        super().__init__(timeout=None)
        self.tiktok_user = tiktok_user
        self.code = code
        self.total_coins = total_coins

    async def _resolve(self, interaction: discord.Interaction):
        pending = await asyncio.to_thread(get_pending_verification, interaction.user.id)
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        code = (pending or {}).get("code") or self.code
        if not tiktok_user:
            mapped = await asyncio.to_thread(get_user_mapping, interaction.user.id)
            tiktok_user = mapped
        if not tiktok_user:
            return None, code, 0
        log_setup.bind_log_context(tiktok=f"@{tiktok_user}")
        _, total_coins, _ = await asyncio.to_thread(is_tiktok_user_in_db, tiktok_user)
        return tiktok_user, code, int(total_coins or 0)

    @discord.ui.button(label="Check Verification Status", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="verify_live_code_check")
    async def check_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        _note_interaction_lag(interaction, "live code check")
        if not await _safe_defer(interaction, "live code check"):
            return
        ok, resolved = await _run_verify_step(
            self._resolve(interaction), "resolve live verification"
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed("verification session"), view=self
            )
            return
        tiktok_user, code, total_coins = resolved
        if not tiktok_user:
            await interaction.edit_original_response(content="❌ This verification request expired. Please start again from the verification panel.", embed=None, view=None)
            return
        current_mapped = await asyncio.to_thread(get_user_mapping, interaction.user.id)
        if current_mapped and current_mapped.lower() == tiktok_user.lower():
            if isinstance(interaction.user, discord.Member):
                _, role_info = await grant_verified_access(interaction.user, tiktok_user, total_coins)
            else:
                _, role_info = await grant_verified_access_for_discord_id(interaction.user.id, tiktok_user, total_coins)
            embed = discord.Embed(
                title="🎉 Live Verification Successful!",
                description=(
                    f"Congratulations {interaction.user.mention}!\n\n"
                    f"✅ Your comment was detected in the live chat!\n"
                    f"TikTok account **@{tiktok_user}** has been linked to your Discord account.\n"
                    f"🌟{role_info}"
                ),
                color=discord.Color.green()
            )
            await interaction.edit_original_response(embed=embed, view=None)
            return

        live_hosts = get_current_live_hosts()
        host_text = ("🟢 **Hosts live right now:**\n" + "\n".join([f"• `@{h}`" for h in live_hosts])) if live_hosts else "⚪ **No host is live right now.**"
        embed = discord.Embed(
            title="⏳ Waiting for Your Live Chat Comment",
            description=(
                f"TikTok account: **@{tiktok_user}**\n"
                f"Code: **`{code or '-'}`**\n\n"
                f"{host_text}\n\n"
                f"Post a comment containing **`{code or '-'}`** in one of our live chats.\n"
                f"Once your comment arrives, verification completes automatically!"
            ),
            color=discord.Color.yellow()
        )
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Switch to Bio", style=discord.ButtonStyle.secondary, emoji="📝", custom_id="verify_live_code_switch_bio")
    async def switch_to_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok, resolved = await _run_verify_step(
            self._resolve(interaction), "resolve live verification"
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed("verification session"), view=self
            )
            return
        tiktok_user, _, total_coins = resolved
        if not tiktok_user:
            await interaction.edit_original_response(content="❌ This verification request expired. Please start again from the verification panel.", embed=None, view=None)
            return
        code = await asyncio.to_thread(create_pending_verification, interaction.user.id, tiktok_user, 'bio')
        _drop_live_pending(tiktok_user)
        await interaction.edit_original_response(
            embed=build_bio_verification_embed(tiktok_user, code),
            view=BioVerificationView(tiktok_user, code, total_coins)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌", custom_id="verify_live_code_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        pending = await asyncio.to_thread(get_pending_verification, interaction.user.id)
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        await asyncio.to_thread(cancel_pending_verification, interaction.user.id)
        if tiktok_user:
            _drop_live_pending(tiktok_user)
        await interaction.edit_original_response(content="❌ Verification cancelled.", embed=None, view=None)

class LiveGreetingVerificationView(VerificationView):
    """Live greeting verification."""
    def __init__(self, tiktok_user: str = None, total_coins: int = 0):
        super().__init__(timeout=None)
        self.tiktok_user = tiktok_user
        self.total_coins = total_coins

    async def _resolve(self, interaction: discord.Interaction):
        pending = await asyncio.to_thread(get_pending_verification, interaction.user.id)
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        if not tiktok_user:
            tiktok_user = await asyncio.to_thread(get_user_mapping, interaction.user.id)
        if not tiktok_user:
            return None, 0
        log_setup.bind_log_context(tiktok=f"@{tiktok_user}")
        _, total_coins, _ = await asyncio.to_thread(is_tiktok_user_in_db, tiktok_user)
        return tiktok_user, int(total_coins or 0)

    @discord.ui.button(label="Check Verification Status", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="verify_live_greeting_check")
    async def check_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        _note_interaction_lag(interaction, "live greeting check")
        if not await _safe_defer(interaction, "live greeting check"):
            return
        ok, resolved = await _run_verify_step(
            self._resolve(interaction), "resolve verification request"
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed("verification session"), view=self
            )
            return
        tiktok_user, total_coins = resolved
        if not tiktok_user:
            await interaction.edit_original_response(content="❌ This verification request expired. Please start again from the verification panel.", embed=None, view=None)
            return
        current_mapped = await asyncio.to_thread(get_user_mapping, interaction.user.id)
        if current_mapped and current_mapped.lower() == tiktok_user.lower():
            if isinstance(interaction.user, discord.Member):
                _, role_info = await grant_verified_access(interaction.user, tiktok_user, total_coins)
            else:
                _, role_info = await grant_verified_access_for_discord_id(interaction.user.id, tiktok_user, total_coins)

            embed = discord.Embed(
                title="🎉 Live Greeting Verification Successful!",
                description=(
                    f"Congratulations {interaction.user.mention}!\n\n"
                    f"✅ Your welcome message was detected in the live chat!\n"
                    f"TikTok account **@{tiktok_user}** has been linked to your Discord account.\n"
                    f"🌟{role_info}"
                ),
                color=discord.Color.green()
            )
            await interaction.edit_original_response(embed=embed, view=None)
            return

        await asyncio.to_thread(
            log_activity_event,
            "verification",
            f"[VERIFY CHECK] {interaction.user} (ID {interaction.user.id}) checked status for "
            f"@{tiktok_user}: not verified yet.",
            tiktok_user
        )
        live_hosts = get_current_live_hosts()
        if live_hosts:
            embed = build_live_greeting_embed(tiktok_user, live_hosts)
        else:
            embed = discord.Embed(
                title="⏳ Waiting for a Live Stream",
                description=(
                    f"No host is live right now.\n\n"
                    f"TikTok account **@{tiktok_user}** will be verified automatically "
                    f"as soon as you post a welcome message in one of our live streams.\n\n"
                    f"💡 *You can also switch to bio verification if you do not want to wait.*"
                ),
                color=discord.Color.yellow()
            )
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Switch to Bio", style=discord.ButtonStyle.secondary, emoji="📝", custom_id="verify_live_greeting_switch_bio")
    async def switch_to_bio(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        ok, resolved = await _run_verify_step(
            self._resolve(interaction), "resolve verification request"
        )
        if not ok:
            await interaction.edit_original_response(
                embed=build_verification_timeout_embed("verification session"), view=self
            )
            return
        tiktok_user, total_coins = resolved
        if not tiktok_user:
            await interaction.edit_original_response(content="❌ This verification request expired. Please start again from the verification panel.", embed=None, view=None)
            return
        code = await asyncio.to_thread(create_pending_verification, interaction.user.id, tiktok_user, 'bio')
        _drop_live_pending(tiktok_user)
        low_activity = int(total_coins or 0) < MIN_COINS_FOR_STANDARD_VERIFICATION
        await interaction.edit_original_response(
            embed=build_bio_verification_embed(tiktok_user, code),
            view=BioVerificationView(tiktok_user, code, total_coins, low_activity=low_activity)
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌", custom_id="verify_live_greeting_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        pending = await asyncio.to_thread(get_pending_verification, interaction.user.id)
        tiktok_user = (pending or {}).get("tiktok_user") or self.tiktok_user
        await asyncio.to_thread(cancel_pending_verification, interaction.user.id)
        if tiktok_user:
            _drop_live_pending(tiktok_user)
        await interaction.edit_original_response(content="❌ Verification cancelled.", embed=None, view=None)

async def handle_live_comment_verification(tiktok_user: str, comment_text: str, target_host: str):
    """Verify a pending request when the matching TikTok account comments in a live stream.

    Greeting messages (``hello``, ``hiii``, ...) are the normal path for both the
    standard and the low-activity flows. Matching a legacy verification code is
    still supported so old pending requests finish normally.

    All database work runs in a worker thread: this callback fires for every
    live-chat comment, and blocking Turso reads here used to freeze the Discord
    event loop (interactions then failed with "did not respond").
    """
    u_clean = tiktok_user.lower().lstrip("@")
    log_setup.bind_log_context(tiktok=f"@{u_clean}", host=f"@{target_host}")

    # Fast path: comments are frequent, so skip the database entirely unless
    # this account actually has a live verification request in flight.
    if not _has_live_pending(u_clean):
        return

    if not is_feature_enabled("verification"):
        return

    pending = await asyncio.to_thread(
        find_pending_live_verification, tiktok_user, comment_text
    )
    greeting_used = False

    if not pending and is_welcome_message(comment_text):
        pending = await asyncio.to_thread(find_pending_live_greeting, tiktok_user)
        greeting_used = bool(pending)

    if not pending:
        return

    discord_id = pending['discord_id']
    try:
        discord_user = bot.get_user(int(discord_id))
    except Exception:
        discord_user = None
    actor_label = (
        f"{_describe_user(discord_user)} (ID {discord_id})"
        if discord_user else f"Discord ID {discord_id}"
    )
    if discord_user:
        log_setup.bind_log_context(actor=_describe_user(discord_user), user_id=discord_id)

    # Link the TikTok account to the Discord user
    await asyncio.to_thread(bind_user_mapping, discord_id, u_clean)
    await asyncio.to_thread(cancel_pending_verification, discord_id)
    _drop_live_pending(u_clean)

    try:
        if greeting_used:
            await asyncio.to_thread(
                record_verification_method,
                discord_id, "live_greeting", f'message: "{comment_text}"'
            )
        else:
            await asyncio.to_thread(
                record_verification_method,
                discord_id, "live_code", f"code: {(pending or {}).get('code') or ''}"
            )
    except Exception as e:
        print(f"[Verification] Failed to record live method for {discord_id}: {e}")

    if greeting_used:
        await asyncio.to_thread(
            log_activity_event,
            "verification",
            f"[LIVE GREETING] {actor_label} verified @{u_clean} via welcome message "
            f"\"{comment_text}\" in @{target_host}'s live.",
            target_host,
        )
        print(f"[Verification] Live greeting verified for @{u_clean} (Discord ID: {discord_id})")
    else:
        await asyncio.to_thread(
            log_activity_event,
            "verification",
            f"[LIVE CODE] {actor_label} verified @{u_clean} via live chat code "
            f"in @{target_host}'s live.",
            target_host,
        )
        print(f"[Verification] Live comment verified for @{u_clean} (Discord ID: {discord_id})")

    # Get the coin balance
    _, total_coins, _ = await asyncio.to_thread(is_tiktok_user_in_db, u_clean)

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

def _register_persistent_views() -> None:
    """Register verification views before login.

    Registering in ``on_ready`` meant a click during startup/reconnect could be
    delivered before the view existed, which Discord reports as "didn't respond
    in time". The ViewStore accepts registrations as soon as the client exists.
    """
    for view in (
        VerificationChannelView(),
        BioVerificationView(),
        VerificationMethodChoiceView(),
        LowActivityVerificationView(),
        LiveGreetingVerificationView(),
        LiveVerificationView(),
    ):
        try:
            bot.add_view(view)
        except Exception as exc:
            log.warning(
                "[Verify] Could not register persistent view %s: %s",
                type(view).__name__,
                exc,
            )


_register_persistent_views()


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
        log.warning("[Chatbot] handler error: %s", e)
    await bot.process_commands(message)


@bot.event
async def on_ready():
    bot_env = get_bot_env()
    print(f"[Discord] Bot logged in as {bot.user} (env={bot_env}, version={get_push_code()})")
    if bot_env == "test":
        print("[Discord] TEST environment active: use a separate bot token, database, and server from stable.")
    try:
        endpoint = getattr(bot.application, "interactions_endpoint_url", None)
        if endpoint:
            print(
                "[Discord] CRITICAL: this application has an Interactions Endpoint URL set "
                f"({endpoint}). Discord will NOT deliver slash commands, buttons, or modals to "
                "this gateway bot. Remove the Interactions Endpoint URL in the Discord Developer "
                "Portal > General Information, then restart."
            )
    except Exception as e:
        print(f"[Discord] Could not check interactions endpoint URL: {e}")
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
    log.info(
        "[Discord] Ready: user=%s id=%s env=%s version=%s latency=%.0fms guilds=%s intents=%s",
        bot.user,
        getattr(bot.user, "id", "?"),
        bot_env,
        get_push_code(),
        (bot.latency or 0) * 1000,
        [f"{g.name}({g.id})" for g in (bot.guilds or [])],
        intents,
    )
    # Register persistent views so verification buttons keep working after restarts
    # (they are already registered at import time; this is a safety net for reloads).
    try:
        _register_persistent_views()
    except Exception as e:
        log.warning("[Verify] Persistent view re-registration failed: %s", e)

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
async def on_connect():
    print("[Gateway] Connected to Discord gateway.")


@bot.event
async def on_disconnect():
    print("[Gateway] Disconnected from Discord; reconnecting...")


@bot.event
async def on_resumed():
    print("[Gateway] Resumed Discord session.")


_LOOP_BEAT = {"at": time.monotonic(), "reported": 0.0}


def _start_loop_stack_watchdog() -> None:
    """Dump the main thread's Python stack when the event loop is stalled.

    The task-name list from ``_loop_lag_monitor`` cannot say which call blocked
    the loop. A separate OS thread keeps ticking while the loop is frozen and
    can capture the exact stack, which is what turns "blocked ~7s" into a fix.
    """
    import threading
    import traceback

    main_ident = threading.main_thread().ident
    if not main_ident:
        return

    def _watch() -> None:
        while True:
            time.sleep(1.0)
            stalled = time.monotonic() - _LOOP_BEAT["at"]
            if stalled < 2.0:
                continue
            now = time.monotonic()
            if now - _LOOP_BEAT["reported"] < 10.0:
                continue
            _LOOP_BEAT["reported"] = now
            frame = sys._current_frames().get(main_ident)
            if frame is not None:
                stack = "".join(traceback.format_stack(frame)[-14:])
            else:
                stack = "(main thread frame unavailable)"
            log.warning(
                "[EventLoop Stack] main thread blocked ~%.1fs; innermost Python frames:\n%s",
                stalled,
                stack,
            )

    threading.Thread(
        target=_watch, name="loop-stack-watchdog", daemon=True
    ).start()


async def _loop_lag_monitor() -> None:
    """Detect synchronous work blocking the Discord event loop.

    A healthy loop wakes every ~1s. Overshoot means some coroutine performed
    blocking I/O or heavy CPU work without yielding -- the usual reason an
    interaction is not acknowledged within Discord's 3-second window.
    """
    interval = 1.0
    while True:
        started = time.perf_counter()
        await asyncio.sleep(interval)
        _LOOP_BEAT["at"] = time.monotonic()
        lag = (time.perf_counter() - started) - interval
        if lag >= 1.0:
            current = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
            names = sorted({getattr(t.get_coro(), "__qualname__", "?") for t in tasks})[:12]
            # Only lag near Discord's 3s ACK deadline is page-worthy; smaller
            # stalls stay at INFO so the error channel is not flooded.
            level = logging.WARNING if lag >= 2.5 else logging.INFO
            log.log(
                level,
                "[EventLoop] blocked ~%.2fs; active_tasks=%d names=%s",
                lag,
                len(tasks),
                ", ".join(names),
            )


async def _keep_awake_loop() -> None:
    """Keep a free Render service from sleeping on its own.

    Render free web services shut down after ~15 minutes without inbound HTTP
    traffic. The Discord gateway is outbound, so the bot disappears and every
    click shows "didn't respond in time" until something wakes the service.
    ``RENDER_EXTERNAL_URL`` is injected by Render; without it this is a no-op
    (use an external uptime monitor on /health instead).
    """
    base = (os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if not base:
        log.info("[KeepAlive] RENDER_EXTERNAL_URL not set; self-ping disabled.")
        return
    url = f"{base}/health"
    try:
        import aiohttp
    except Exception as exc:
        log.info("[KeepAlive] aiohttp unavailable (%s); self-ping disabled.", exc)
        return
    await asyncio.sleep(90)
    log.info("[KeepAlive] Pinging %s every 10 minutes.", url)
    while True:
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    await response.read()
                    log.debug("[KeepAlive] GET %s -> %s", url, response.status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug("[KeepAlive] ping failed: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(600)


_AUTO_ACK_IDS = {
    # custom_id -> ephemeral?  Only long-running verification buttons whose
    # handlers tolerate an already-acknowledged interaction.
    "verify_choice_bio": False,
    "verify_choice_live_greeting": False,
    "verify_low_bio": False,
    "verify_low_live_greeting": False,
    "bio_verify_persistent": False,
    "bio_change_method_persistent": False,
    "bio_copy_code_persistent": True,
    "verify_live_code_check": False,
    "verify_live_greeting_check": False,
}


async def _watch_unacknowledged_interaction(interaction: discord.Interaction) -> None:
    """Warn when an interaction is still not acknowledged near Discord's 3s limit.

    Waits until ~2.2s after Discord created the interaction, leaving ~0.8s for
    the HTTP defer to arrive, then answers on behalf of a stuck handler.
    """
    try:
        try:
            elapsed = time.time() - interaction.created_at.timestamp()
        except Exception:
            elapsed = 0.0
        await asyncio.sleep(max(0.0, 2.2 - elapsed))
        if interaction.response.is_done():
            return
        try:
            received_lag = time.time() - interaction.created_at.timestamp()
        except Exception:
            received_lag = -1
        log.warning(
            "[Interaction Timeout Risk] type=%s command=%s custom_id=%s user=%s (%s) guild=%s (%s) "
            "was not acknowledged after ~2.2s (gateway_lag=%.2fs; Discord shows \"didn't respond in time\" at ~3s).",
            interaction.type.name,
            str((interaction.data or {}).get("name") or "-"),
            str((interaction.data or {}).get("custom_id") or "-"),
            getattr(interaction.user, "id", "?"),
            _describe_user(interaction.user),
            getattr(getattr(interaction, "guild", None), "id", "-"),
            getattr(getattr(interaction, "guild", None), "name", None) or "DM",
            received_lag,
        )
        custom_id = str((interaction.data or {}).get("custom_id") or "")
        if (
            custom_id in _AUTO_ACK_IDS
            and interaction.type is discord.InteractionType.component
        ):
            try:
                await interaction.response.defer(ephemeral=_AUTO_ACK_IDS[custom_id])
                log.warning(
                    "[Interaction Rescue] %s was not acknowledged in time; sent a defer at %.2fs "
                    "so the member does not see \"didn't respond in time\".",
                    custom_id,
                    time.time() - interaction.created_at.timestamp(),
                )
            except Exception as exc:
                log.debug("[Interaction Rescue] %s: could not defer: %s", custom_id, exc)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


@bot.event
async def on_interaction(interaction: discord.Interaction):
    """Log receipt and provide a fallback for outdated components/modals.

    Older verification DMs used random custom_ids that cannot be registered
    persistently. Instead of letting Discord show "didn't respond in time",
    answer with a clear instruction if nothing acknowledged the interaction.
    """
    interaction_type = interaction.type.name
    custom_id = str((interaction.data or {}).get("custom_id") or "")
    command_name = ""
    if interaction.type is discord.InteractionType.application_command:
        command_name = str((interaction.data or {}).get("name") or "")
    _bind_interaction_context(interaction, command=command_name or None, custom_id=custom_id or None)
    try:
        lag = time.time() - interaction.created_at.timestamp()
    except Exception:
        lag = -1
    guild_id = getattr(interaction.guild, "id", None)
    guild_name = getattr(interaction.guild, "name", None) or "DM"
    channel = getattr(interaction, "channel", None)
    channel_label = (
        f"#{getattr(channel, 'name', None)}" if getattr(channel, "name", None)
        else (str(getattr(channel, "id", None) or "-"))
    )
    log.info(
        "[Gateway] %s received command=%s custom_id=%s lag=%.2fs user=%s (%s) guild=%s (%s) channel=%s",
        interaction_type,
        command_name or "-",
        custom_id or "-",
        lag,
        getattr(interaction.user, "id", "?"),
        _describe_user(interaction.user),
        guild_id or "-",
        guild_name,
        channel_label,
    )
    asyncio.create_task(_watch_unacknowledged_interaction(interaction))
    if interaction.type not in (
        discord.InteractionType.component,
        discord.InteractionType.modal_submit,
    ):
        return
    if not re.fullmatch(r"[0-9a-f]{32}", custom_id):
        return

    async def _fallback():
        await asyncio.sleep(1.8)
        if interaction.response.is_done():
            return
        print(
            f"[Interaction Fallback] Unhandled legacy component "
            f"custom_id={custom_id} type={interaction.type.name} user={interaction.user.id}"
        )
        try:
            await interaction.response.send_message(
                "⚠️ This verification message is outdated, so I sent you a fresh menu.",
                ephemeral=True,
            )
        except Exception as e:
            print(f"[Interaction Fallback] Could not reply: {e}")
            return

        # Rebuild the current step from the pending verification and DM it, so
        # users clicking an old message do not have to start from the panel.
        if interaction.type is not discord.InteractionType.component:
            return
        try:
            pending = await asyncio.to_thread(get_pending_verification, interaction.user.id)
            tiktok_user = (pending or {}).get("tiktok_user")
            if not tiktok_user:
                return
            _, total_coins, display_name = await asyncio.to_thread(is_tiktok_user_in_db, tiktok_user)
            embed, view = await asyncio.to_thread(
                build_verification_start,
                interaction.user.id,
                tiktok_user,
                total_coins,
                display_name,
            )
            await interaction.user.send(embed=embed, view=view)
            print(f"[Interaction Fallback] Re-sent verification menu to {interaction.user.id} for @{tiktok_user}.")
        except Exception as e:
            print(f"[Interaction Fallback] Could not resend verification menu: {e}")

    asyncio.create_task(_fallback())


@bot.event
async def on_command_error(ctx, error):
    try:
        log_setup.bind_log_context(
            actor=_describe_user(ctx.author),
            user_id=getattr(ctx.author, "id", None),
            guild=getattr(getattr(ctx, "guild", None), "name", None) or "DM",
            channel=getattr(getattr(ctx, "channel", None), "name", None)
            or getattr(getattr(ctx, "channel", None), "id", None),
            command=f"!{getattr(ctx.command, 'qualified_name', getattr(ctx, 'invoked_with', '?'))}",
        )
    except Exception:
        pass
    if isinstance(error, commands.CommandNotFound):
        log.debug(
            "[Command] unknown !%s user=%s guild=%s",
            getattr(ctx, "invoked_with", "?"),
            getattr(ctx.author, "id", "?"),
            getattr(getattr(ctx, "guild", None), "id", "-"),
        )
        return
    if isinstance(error, commands.CheckFailure):
        key = str(error)
        log.info(
            "[Command] denied !%s user=%s(%s) guild=%s reason=%s",
            getattr(ctx.command, "qualified_name", ctx.command),
            getattr(ctx.author, "id", "?"),
            getattr(ctx.author, "name", "?"),
            getattr(getattr(ctx, "guild", None), "id", "-"),
            key or type(error).__name__,
        )
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
        log.info(
            "[Command] missing argument !%s user=%s param=%s",
            getattr(ctx.command, "qualified_name", ctx.command),
            getattr(ctx.author, "id", "?"),
            error.param.name,
        )
        try:
            await ctx.send(f"❌ Missing argument: `{error.param.name}`.", delete_after=10)
        except Exception:
            pass
        return
    if isinstance(error, commands.UserInputError):
        log.info(
            "[Command] bad input !%s user=%s: %s",
            getattr(ctx.command, "qualified_name", ctx.command),
            getattr(ctx.author, "id", "?"),
            error,
        )
        try:
            await ctx.send(f"❌ Invalid input: {error}", delete_after=10)
        except Exception:
            pass
        return
    original = getattr(error, "original", error)
    log.error(
        "[Command] FAILED !%s user=%s(%s) guild=%s: %s",
        getattr(ctx.command, "qualified_name", ctx.command),
        getattr(ctx.author, "id", "?"),
        getattr(ctx.author, "name", "?"),
        getattr(getattr(ctx, "guild", None), "id", "-"),
        error,
        exc_info=(type(original), original, original.__traceback__),
    )
    try:
        await ctx.send("❌ Something went wrong. The error was logged for the admin.", delete_after=15)
    except Exception:
        pass


@bot.before_invoke
async def _log_prefix_command_start(ctx):
    ctx._log_started_at = time.perf_counter()
    try:
        log_setup.bind_log_context(
            actor=_describe_user(ctx.author),
            user_id=getattr(ctx.author, "id", None),
            guild=getattr(getattr(ctx, "guild", None), "name", None) or "DM",
            channel=getattr(getattr(ctx, "channel", None), "name", None)
            or getattr(getattr(ctx, "channel", None), "id", None),
            command=f"!{getattr(ctx.command, 'qualified_name', ctx.command)}",
        )
    except Exception:
        pass
    log.info(
        "[Command] -> !%s user=%s(%s) guild=%s channel=%s content=%r",
        getattr(ctx.command, "qualified_name", ctx.command),
        getattr(ctx.author, "id", "?"),
        getattr(ctx.author, "name", "?"),
        getattr(getattr(ctx, "guild", None), "id", "-"),
        getattr(getattr(ctx, "channel", None), "id", "-"),
        log_setup.redact((getattr(ctx.message, "content", "") or "")[:300]),
    )


@bot.after_invoke
async def _log_prefix_command_end(ctx):
    started = getattr(ctx, "_log_started_at", None)
    duration = (time.perf_counter() - started) * 1000 if started else -1
    log.info(
        "[Command] <- !%s user=%s duration=%.0fms",
        getattr(ctx.command, "qualified_name", ctx.command),
        getattr(ctx.author, "id", "?"),
        duration,
    )


async def _on_app_command_error(interaction: discord.Interaction, error: Exception):
    _bind_interaction_context(interaction)
    original = getattr(error, "original", getattr(error, "orig", error))
    command_name = getattr(getattr(interaction, "command", None), "qualified_name", None) or str(
        (interaction.data or {}).get("name") or "?"
    )
    if isinstance(error, app_commands.CheckFailure):
        log.info(
            "[Slash] denied /%s user=%s(%s) guild=%s reason=%s",
            command_name,
            getattr(interaction.user, "id", "?"),
            getattr(interaction.user, "name", "?"),
            getattr(getattr(interaction, "guild", None), "id", "-"),
            error,
        )
        message = "🛡️ This command is restricted to administrators."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass
        return
    if isinstance(error, app_commands.CommandInvokeError):
        log.error(
            "[Slash] FAILED /%s user=%s(%s) guild=%s: %s",
            command_name,
            getattr(interaction.user, "id", "?"),
            getattr(interaction.user, "name", "?"),
            getattr(getattr(interaction, "guild", None), "id", "-"),
            original,
            exc_info=(type(original), original, original.__traceback__),
        )
    else:
        log.error(
            "[Slash] FAILED /%s user=%s(%s) guild=%s: %s",
            command_name,
            getattr(interaction.user, "id", "?"),
            getattr(interaction.user, "name", "?"),
            getattr(getattr(interaction, "guild", None), "id", "-"),
            error,
        )
    if interaction.response.is_done():
        try:
            await interaction.followup.send("❌ Something went wrong. The error was logged.", ephemeral=True)
        except Exception:
            pass
    else:
        try:
            await interaction.response.send_message(
                "❌ Something went wrong. The error was logged.", ephemeral=True
            )
        except Exception:
            pass


bot.tree.on_error = _on_app_command_error


@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command):
    log.info(
        "[Slash] <- /%s user=%s(%s) guild=%s channel=%s",
        getattr(command, "qualified_name", command),
        getattr(interaction.user, "id", "?"),
        getattr(interaction.user, "name", "?"),
        getattr(getattr(interaction, "guild", None), "id", "-"),
        getattr(getattr(interaction, "channel", None), "id", "-"),
    )


@bot.event
async def on_error(event_method, *args, **kwargs):
    log.exception(
        "[Event] Unhandled exception in %s (args=%r)",
        event_method,
        log_setup.redact(repr(args)[:400]),
    )

@bot.event
async def on_member_join(member: discord.Member):
    # Restore roles if the member has verified before
    def _lookup():
        tt_user = get_user_mapping(member.id)
        if not tt_user:
            return None, 0
        _, total_coins, _ = is_tiktok_user_in_db(tt_user)
        return tt_user, total_coins

    tt_user, total_coins = await asyncio.to_thread(_lookup)
    if tt_user:
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
    await asyncio.to_thread(set_setting, "verification_channel_id", str(ctx.channel.id))
    await send_verification_panel(ctx.channel)
    try:
        await ctx.message.delete()
    except Exception:
        pass

@bot.command(name="set_verified_role")
@commands.check(is_admin)
async def set_verified_role_cmd(ctx, role: discord.Role):
    """Admin command: set the role granted after successful verification."""
    await asyncio.to_thread(set_setting, "verified_role_id", str(role.id))
    await ctx.send(f"✅ Verified role set to: **{role.name}** (`{role.id}`)")

async def _unclaim_discord_member(member: discord.Member):
    """Remove a member's TikTok link plus the roles that link justified.

    Returns (ok: bool, message: str).
    """
    def _lookup_link():
        conn = get_db_connection()
        try:
            return conn.execute(
                "SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (str(member.id),)
            ).fetchone()
        finally:
            conn.close()

    res = await asyncio.to_thread(_lookup_link)
    if not res:
        return False, f"❌ {member.mention} has no linked TikTok account."

    tt_user = res[0]
    roles_ok, role_detail = await revoke_verification_roles_for_discord_id(member.id)

    def _delete_link():
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

    await asyncio.to_thread(_delete_link)

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

def build_member_coin_embed(member: discord.Member) -> discord.Embed:
    """Detailed coin report for one Discord member (admin lookup)."""
    tiktok_user = get_user_mapping(member.id)
    if not tiktok_user:
        return discord.Embed(
            title="❌ Not Linked",
            description=(
                f"{member.mention} (`{member.id}`) has not verified a TikTok account yet, "
                f"so there are no coins to show."
            ),
            color=discord.Color.red()
        )

    _, total_coins, display_name = is_tiktok_user_in_db(tiktok_user)
    last = get_last_gift_for_tiktok(tiktok_user)

    embed = discord.Embed(
        title=f"🪙 Coin Report — {member.display_name}",
        color=discord.Color.gold()
    )
    try:
        embed.set_thumbnail(url=member.display_avatar.url)
    except Exception:
        pass
    embed.add_field(name="Discord", value=f"{member.mention}\n`{member.id}`", inline=True)
    tiktok_value = f"**@{tiktok_user}**"
    if display_name and display_name.lower() != tiktok_user.lower():
        tiktok_value += f"\n{display_name}"
    embed.add_field(name="TikTok", value=tiktok_value, inline=True)
    embed.add_field(name="Total Coins", value=f"**{int(total_coins or 0):,} c**", inline=True)

    if last:
        gift_line = f"**{last['gift_name']}**"
        if last["gift_count"] > 1:
            gift_line += f" × {last['gift_count']}"
        gift_line += f" — **{last['coins']:,} c**"
        embed.add_field(name="Last Gift", value=gift_line, inline=False)

        when = last["log_datetime"] or f"{last['log_date']} {last['log_time']}".strip()
        where = f"to **@{last['target_username']}**" if last["target_username"] else ""
        host = (last["host_name"] or "").strip()
        if host and host.lower() != (last["target_username"] or "").lower():
            where += f" (host: {host})"
        embed.add_field(
            name="Last Gift Date",
            value=(f"`{when} WIB`\n{where}" if where else f"`{when} WIB`"),
            inline=False
        )
    else:
        embed.add_field(name="Last Gift", value="⚠️ No gift recorded yet.", inline=False)

    embed.set_footer(text="!coin/!coins @user • /coin/coins member")
    return embed


@bot.command(name="mycoins", aliases=["check", "coins", "coin"])
async def mycoins(ctx, member: discord.Member = None):
    """Show the coin balance of your linked TikTok account.

    Admins can pass @member to get that member's coins plus the date and type of
    their most recent recorded gift.
    """
    if member is not None and member.id != ctx.author.id:
        if not is_admin_member(ctx.author):
            await ctx.send("🛡️ Only administrators can check another member's coins.")
            return
        await ctx.send(embed=build_member_coin_embed(member))
        return

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
            "`!mycoins` / `!coins` or `/mycoins` / `/coins` — show your total coins\n"
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
                "`!coin @user` / `!coins @user` or `/coin member` / `/coins member` — coins, last gift date and gift type\n"
                "`!setup_verification` or `/setup_verification` — send the verification panel\n"
                "`!set_verified_role` or `/set_verified_role` — set the verified role\n"
                "`!unclaim` or `/unclaim` — remove a member's linked TikTok account\n"
                "`!livesessions` or `/livesessions` — recent live sessions (admin only)\n"
                "`/log` — download recent bot logs for debugging (admin only)"
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
    # Defer first: build_help_embed reads settings (Turso round-trips) and must
    # not eat into Discord's 3-second acknowledgement window.
    await interaction.response.defer(ephemeral=True)
    embed = await asyncio.to_thread(build_help_embed, interaction.user)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="mycoins", description="Show the coin balance of your linked TikTok account")
async def slash_mycoins(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    def _fetch_mycoins():
        conn = get_db_connection()
        try:
            mapping = conn.execute(
                "SELECT tiktok_user FROM user_mappings WHERE discord_id = ?",
                (str(interaction.user.id),)
            ).fetchone()
            if not mapping:
                return None, None, None
            tt_user = mapping[0]
            row = conn.execute(
                "SELECT display_name, total_coins FROM tiktok_gifts WHERE LOWER(tiktok_user) = ?",
                (tt_user.lower(),)
            ).fetchone()
            return tt_user, row, None
        finally:
            conn.close()

    tt_user, row, _ = await asyncio.to_thread(_fetch_mycoins)
    if not tt_user:
        await interaction.followup.send("❌ Your account is not verified yet. Use the button in the verification channel.", ephemeral=True)
        return
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


async def _send_coin_report(interaction: discord.Interaction, member: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    target = member or interaction.user
    if not isinstance(target, discord.Member):
        await interaction.followup.send("This command only works inside the server.", ephemeral=True)
        return
    if target.id != interaction.user.id and not is_admin_member(interaction.user):
        await interaction.followup.send("🛡️ Only administrators can check another member's coins.", ephemeral=True)
        return
    embed = await asyncio.to_thread(build_member_coin_embed, target)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="coins", description="Show a member's coins and their last recorded gift")
@app_commands.describe(member="Member to check (administrators can check other members)")
async def slash_coins(interaction: discord.Interaction, member: discord.Member = None):
    await _send_coin_report(interaction, member)


@bot.tree.command(name="coin", description="Show a member's coins and their last recorded gift")
@app_commands.describe(member="Member to check (administrators can check other members)")
async def slash_coin(interaction: discord.Interaction, member: discord.Member = None):
    await _send_coin_report(interaction, member)


@bot.tree.command(name="myhistory", description="Show your 5 most recent gift transactions")
async def slash_myhistory(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    def _fetch_history():
        conn = get_db_connection()
        try:
            mapping = conn.execute(
                "SELECT tiktok_user FROM user_mappings WHERE discord_id = ?",
                (str(interaction.user.id),)
            ).fetchone()
            if not mapping:
                return None, None
            tt_user = mapping[0]
            rows = conn.execute("""
                SELECT target_username, gift_name, gift_count, coins, log_datetime
                FROM gift_logs
                WHERE LOWER(viewer_username) = ?
                ORDER BY log_id DESC LIMIT 5
            """, (tt_user.lower(),)).fetchall()
            return tt_user, rows
        finally:
            conn.close()

    tt_user, rows = await asyncio.to_thread(_fetch_history)
    if not tt_user:
        await interaction.followup.send("❌ Your account is not verified yet. Use the button in the verification channel.", ephemeral=True)
        return
    if not rows:
        await interaction.followup.send(f"❌ No gift history found for **@{tt_user}** yet.", ephemeral=True)
        return
    msg = f"📜 **Last 5 Gift Transactions (@{tt_user})**\n\n"
    for target, gift, count, coins, dt in rows:
        msg += f"• `{dt}`: **{gift}** x{count} ({coins} c) on @{target}\n"
    await interaction.followup.send(msg, ephemeral=True)


@bot.tree.command(name="topsupporter", description="Show the all-time top supporters leaderboard")
async def slash_topsupporter(interaction: discord.Interaction):
    await interaction.response.defer()

    def _fetch_top():
        conn = get_db_connection()
        try:
            return conn.execute(
                "SELECT tiktok_user, display_name, total_coins FROM tiktok_gifts ORDER BY total_coins DESC LIMIT 10"
            ).fetchall()
        finally:
            conn.close()

    rows = await asyncio.to_thread(_fetch_top)
    if not rows:
        await interaction.followup.send("❌ No gift data recorded yet.")
        return
    await interaction.followup.send(embed=build_leaderboard_embed("🏆 All-Time Top Supporters", rows, "All-Time Coins"))


@bot.tree.command(name="dailyrank", description="Show today's coin ranking")
async def slash_dailyrank(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = await asyncio.to_thread(get_daily_top_supporters)
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
    await asyncio.to_thread(set_setting, "verification_channel_id", str(interaction.channel.id))
    await send_verification_panel(interaction.channel)
    await interaction.followup.send("✅ Verification panel sent to this channel.", ephemeral=True)


@bot.tree.command(name="set_verified_role", description="Admin: set the role granted after verification")
@app_commands.describe(role="Role to grant after successful verification")
async def slash_set_verified_role(interaction: discord.Interaction, role: discord.Role):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_member(interaction.user):
        await interaction.followup.send("🛡️ This command is restricted to administrators.", ephemeral=True)
        return
    await asyncio.to_thread(set_setting, "verified_role_id", str(role.id))
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


@bot.tree.command(name="log", description="Admin: download recent bot logs for debugging")
@app_commands.describe(
    lines="How many recent lines to include (1-3000, default 300)",
    level="Filter: all, info, warning, error",
    search="Only include lines containing this text",
)
async def slash_log(
    interaction: discord.Interaction,
    lines: int = 300,
    level: str = "all",
    search: str = None,
):
    await interaction.response.defer(ephemeral=True)
    if not is_admin_member(interaction.user):
        await interaction.followup.send("🛡️ This command is restricted to administrators.", ephemeral=True)
        return

    line_count = max(1, min(int(lines or 300), 3000))
    text, matched = await asyncio.to_thread(log_setup.tail_log, line_count, level, search)
    info = log_setup.get_build_info()
    log.info(
        "[Log] /log requested by %s(%s) lines=%s level=%s search=%r matched=%s",
        interaction.user.id,
        interaction.user.name,
        line_count,
        level,
        search,
        matched,
    )

    if not (text or "").strip():
        await interaction.followup.send(
            f"No log lines matched (level={level}, search={search!r}).", ephemeral=True
        )
        return

    header = (
        f"🧾 **Bot log** — env=`{info['env']}` version=`{info['version']}` "
        f"· {matched} matched, showing up to {line_count} lines"
    )
    if len(text) <= 1800 and line_count <= 250:
        await interaction.followup.send(f"{header}\n```\n{text}\n```", ephemeral=True)
        return

    safe_version = re.sub(r"[^A-Za-z0-9._-]+", "_", str(info["version"]))[:24]
    filename = (
        f"bot-log-{info['env']}-{safe_version}-"
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    )
    payload = io.BytesIO(text.encode("utf-8", "replace"))
    await interaction.followup.send(
        header + "\nDownload the attached file (it is visible only to you).",
        file=discord.File(payload, filename=filename),
        ephemeral=True,
    )


def _log_startup_snapshot() -> None:
    """Write the effective configuration (no secrets) to the log."""
    from db import HAS_LIBSQL, TURSO_URL, TURSO_TOKEN

    keys = (
        "master_enabled",
        "debug_mode",
        "verification_enabled",
        "coin_only_enabled",
        "coin_only_min_coins",
        "verification_click_timeout_seconds",
        "live_tracking_enabled",
        "leaderboard_enabled",
        "allowed_channels_enabled",
        "admin_role_id",
        "verified_role_id",
        "live_verification_enabled",
        "live_greeting_enabled",
    )
    snapshot = {}
    for key in keys:
        try:
            snapshot[key] = log_setup.redact(str(get_setting(key, "") or ""))
        except Exception as exc:
            snapshot[key] = f"<error: {exc}>"
    try:
        snapshot["announcement_channels"] = len(get_announcement_channel_ids())
    except Exception:
        snapshot["announcement_channels"] = "?"
    try:
        snapshot["allowed_channels"] = len(get_int_list_setting("allowed_channel_ids"))
    except Exception:
        snapshot["allowed_channels"] = "?"
    try:
        snapshot["role_tiers"] = len(get_role_tiers() or [])
    except Exception:
        snapshot["role_tiers"] = "?"
    snapshot["db_backend"] = "turso" if (HAS_LIBSQL and TURSO_URL) else "local-sqlite"
    snapshot["db_host"] = log_setup.redact(
        (TURSO_URL or "").split("?")[0].replace("libsql://", "").split("/")[0]
    ) if TURSO_URL else "-"
    snapshot["turso_token_set"] = bool(TURSO_TOKEN)
    snapshot["expected_guild_id"] = os.environ.get("EXPECTED_GUILD_ID", "") or "-"
    snapshot["admin_auth_enabled"] = bool(
        os.environ.get("ADMIN_USERNAME") and os.environ.get("ADMIN_PASSWORD")
    )
    snapshot["error_channel_id"] = log_setup.LOG_ERROR_CHANNEL_ID or "-"
    log.info("[Startup] Config snapshot: %s", json.dumps(snapshot, sort_keys=True))


async def main():
    log_setup.set_build_info(env=get_bot_env(), version=get_push_code())
    log_setup.setup_logging()
    loop = asyncio.get_running_loop()
    log_setup.install_asyncio_exception_handler(loop)
    log.info(
        "[Startup] main() starting: env=%s version=%s python=%s pid=%s",
        get_bot_env(),
        get_push_code(),
        sys.version.split()[0],
        os.getpid(),
    )

    # Turso/libSQL calls run in worker threads via asyncio.to_thread. The default
    # executor only has ~5 workers on small instances, so a burst of gift events
    # could starve interaction handlers. These tasks are network-bound, so extra
    # threads are cheap and keep slash commands responsive during busy lives.
    try:
        loop.set_default_executor(
            concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="db")
        )
    except Exception as e:
        log.exception("[Startup] Could not enlarge default executor: %s", e)

    init_db()
    log.info("[Startup] Database initialised.")

    # Load bot_settings into memory before any interaction arrives. With the
    # stale-while-revalidate cache this means a settings read can never block
    # the Discord event loop, even on a cold cache.
    try:
        await asyncio.to_thread(warm_settings_cache)
        print("[Startup] Settings cache warmed.")
    except Exception as e:
        print(f"[Startup] Could not warm settings cache: {e}")

    # Rebuild the in-memory live-verification guard so pending requests created
    # before this start still match comments without hitting the database for
    # every other live-chat message.
    try:
        pending_live = await asyncio.to_thread(get_pending_live_usernames)
        for pending_user, expires_at in pending_live:
            _cache_live_pending(pending_user, expires_at)
        if pending_live:
            print(f"[Verification] Restored {len(pending_live)} pending live request(s) into memory cache.")
    except Exception as e:
        print(f"[Verification] Could not rebuild live pending cache: {e}")

    await asyncio.to_thread(suspend_open_live_sessions)
    set_verification_refresh_handler(schedule_verification_panel_refresh)
    set_presence_refresh_handler(schedule_presence_refresh)
    keep_alive(handle_api_announcement)

    # Log a secret-free snapshot of the runtime configuration. This is the first
    # thing to read when a deployment misbehaves (wrong DB, wrong guild, wrong mode).
    try:
        await asyncio.to_thread(_log_startup_snapshot)
    except Exception as e:
        log.exception("[Startup] Could not log config snapshot: %s", e)

    # Deliver queued warnings/errors to the Discord error channel.
    asyncio.create_task(log_setup.notify_loop(bot))
    asyncio.create_task(_loop_lag_monitor())
    _start_loop_stack_watchdog()
    asyncio.create_task(_keep_awake_loop())
    log.info(
        "[Startup] Error notifications -> channel %s (queue=%s).",
        log_setup.LOG_ERROR_CHANNEL_ID,
        log_setup.notification_queue_size(),
    )

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
    log_setup.set_build_info(env=get_bot_env(), version=get_push_code())
    log_setup.setup_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("\n[Bot] Stopped successfully.")
    except Exception:
        log.exception("[Bot] Fatal crash in main()")
        raise

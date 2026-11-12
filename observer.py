import asyncio
import os
import time
from datetime import datetime, timedelta
import pytz
from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    ConnectEvent, 
    DisconnectEvent, 
    GiftEvent, 
    CommentEvent
)

# Optional events (beberapa event mungkin tidak tersedia di versi TikTokLive tertentu, misal v7+)
try:
    from TikTokLive.events import GreetingEvent
except ImportError:
    GreetingEvent = None

try:
    from TikTokLive.events import LiveIntroEvent
except ImportError:
    LiveIntroEvent = None

try:
    from TikTokLive.events import NoticeEvent
except ImportError:
    NoticeEvent = None

try:
    from TikTokLive.events import RoomPinEvent
except ImportError:
    RoomPinEvent = None

try:
    from TikTokLive.client.web.web_client import FetchRoomIdAPIRoute
except Exception:
    FetchRoomIdAPIRoute = None
from db import (
    save_gift_data, 
    save_daily_gift_data, 
    start_live_session, 
    end_live_session, 
    log_gift_transaction,
    get_active_hosts,
    get_observable_hosts,
    get_db_connection,
    log_activity_event,
    get_user_channel_spent,
    get_user_today_spent,
    get_user_total_spent,
    detect_host_from_welcome_message,
    detect_host_match_from_welcome_message,
    get_current_host_for_account,
    set_current_host_for_account,
    update_live_session_host,
    add_live_session_paused_seconds,
    get_channel_display_name,
    get_host_aliases,
    get_host_connect_mode,
    is_feature_enabled,
    get_setting,
    set_setting
)
import runtime_status

# Grace period setelah disconnect sebelum live benar-benar dinyatakan berakhir.
LIVE_DISCONNECT_GRACE_SECONDS = 600

# Set global untuk melacak host yang saat ini sedang LIVE
CURRENT_LIVE_HOSTS = set()

# Active observer clients and reconnect requests (for admin-triggered reconnects)
import threading

_ACTIVE_CLIENTS = {}
_ACTIVE_CLIENTS_LOCK = threading.Lock()
_RECONNECT_REQUESTED = set()
_RECONNECT_REQUESTED_LOCK = threading.Lock()
_HOST_RECONFIRM_REQUESTED = set()
_HOST_RECONFIRM_REQUESTED_LOCK = threading.Lock()

# Cached Live Intro text. This is only ever populated from LiveIntroEvent (or
# intro-named fields in room info), so the fallback stays tied to the intro
# source and never uses arbitrary chat / room titles.
_LAST_INTRO_TEXT = {}
_LAST_INTRO_ANALYZED = {}
_INTRO_CACHE_TTL_SECONDS = 6 * 3600


def _safe_preview(value, limit: int = 180) -> str:
    try:
        text = str(value).replace("\n", " ").strip()
    except Exception:
        return ""
    if len(text) > limit:
        text = text[: max(0, limit - 3)] + "..."
    return text


def _text_from_value(value) -> str:
    """Normalise a scalar / Text-proto / nested dict into intro text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace").strip()
        except Exception:
            return ""
    if isinstance(value, dict):
        for key in ("default_pattern", "content", "text", "message", "value"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        return ""
    # TikTokLive's Text protobuf exposes the rendered text on default_pattern.
    for attr in ("default_pattern", "content", "text", "message"):
        try:
            nested = getattr(value, attr, None)
        except Exception:
            nested = None
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _extract_live_intro_text(event):
    """Extract intro text from LiveIntroEvent across TikTokLive versions.

    Returns (text, field_name, candidate_fields). The event itself is the only
    source here: we never read room titles or comments.
    """
    candidates = []
    seen = set()

    def add_candidate(name, value):
        text = _text_from_value(value)
        if text and (name, text) not in seen:
            seen.add((name, text))
            candidates.append((str(name), text))

    for name in (
        "content",
        "description",
        "intro",
        "message",
        "text",
        "room_intro",
        "live_intro",
    ):
        try:
            add_candidate(name, getattr(event, name, None))
        except Exception:
            pass

    try:
        values = vars(event) if hasattr(event, "__dict__") else {}
    except Exception:
        values = {}
    if isinstance(event, dict):
        values = event
    for key, value in list(values.items())[:80]:
        if "intro" in str(key).lower():
            add_candidate(key, value)

    # Some protobuf fields are properties and do not appear in vars().
    try:
        names = [n for n in dir(event) if not n.startswith("_")]
    except Exception:
        names = []
    for name in names[:160]:
        if "intro" not in name.lower():
            continue
        try:
            add_candidate(name, getattr(event, name))
        except Exception:
            pass

    if candidates:
        return candidates[0][1], candidates[0][0], candidates
    return "", "", candidates


def _collect_intro_texts_from_room_info(value, path: str = "", depth: int = 0, out=None):
    """Collect only intro-named text fields from a room-info payload.

    This is deliberately strict: only keys containing "intro" are considered,
    so the fallback cannot accidentally use the room title, comments or other
    arbitrary text.
    """
    if out is None:
        out = []
    if depth > 6 or len(out) >= 5:
        return out
    if isinstance(value, dict):
        for key, item in value.items():
            key_l = str(key).lower()
            child_path = f"{path}.{key}" if path else str(key)
            if "intro" in key_l:
                text = _text_from_value(item)
                if text:
                    out.append((child_path, text))
                elif isinstance(item, (dict, list)):
                    _collect_intro_texts_from_room_info(item, child_path, depth + 1, out)
            elif isinstance(item, (dict, list)):
                _collect_intro_texts_from_room_info(item, child_path, depth + 1, out)
    elif isinstance(value, list):
        for idx, item in enumerate(value[:25]):
            _collect_intro_texts_from_room_info(item, f"{path}[{idx}]", depth + 1, out)
    return out


def _cache_live_intro_text(username: str, text: str):
    username = (username or "").lower().lstrip("@")
    text = (text or "").strip()
    if not username or not text:
        return
    _LAST_INTRO_TEXT[username] = text
    try:
        set_setting(f"last_live_intro_{username}", text)
        set_setting(f"last_live_intro_at_{username}", str(time.time()))
    except Exception as e:
        print(f"[{username}] Failed to persist last Live Intro text: {e}")


def _get_cached_live_intro_text(username: str) -> str:
    username = (username or "").lower().lstrip("@")
    if not username:
        return ""
    text = _LAST_INTRO_TEXT.get(username, "")
    if not text:
        try:
            text = (get_setting(f"last_live_intro_{username}") or "").strip()
        except Exception:
            text = ""
    if not text:
        return ""
    try:
        cached_at = float(get_setting(f"last_live_intro_at_{username}") or 0)
        if cached_at and (time.time() - cached_at) > _INTRO_CACHE_TTL_SECONDS:
            return ""
    except Exception:
        pass
    return text


def get_current_live_hosts() -> list:
    """Mengembalikan daftar username host yang saat ini sedang terhubung dan live."""
    return runtime_status.get_live_usernames()

def is_any_host_live() -> bool:
    """Mengecek apakah ada minimal 1 host yang sedang live saat ini."""
    return len(runtime_status.get_live_usernames()) > 0

async def _trigger_announcement(announcement_callback, username: str, is_live: bool, host_name: str = None):
    if not announcement_callback:
        return
    try:
        if asyncio.iscoroutinefunction(announcement_callback):
            try:
                await announcement_callback(username, is_live, host_name)
            except TypeError:
                await announcement_callback(username, is_live)
        else:
            try:
                announcement_callback(username, is_live, host_name)
            except TypeError:
                announcement_callback(username, is_live)
    except Exception as e:
        print(f"[{username}] Error triggering announcement callback: {e}")


async def probe_room_live(username: str):
    """Best-effort probe for TikTok live status. Returns True/False/None."""
    username = (username or "").lower().lstrip("@")
    if not username or FetchRoomIdAPIRoute is None:
        return None
    try:
        client = TikTokLiveClient(unique_id=username)
        web = getattr(client, "web", None)
        if web is None:
            return None
        fetch = getattr(FetchRoomIdAPIRoute, "fetch_user_room_data", None)
        if fetch is None:
            return None
        data = await fetch(web, username)
        payload = data or {}
        root = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(root, dict):
            root = payload if isinstance(payload, dict) else {}

        user = root.get("user") if isinstance(root.get("user"), dict) else {}
        room = root.get("liveRoom") or root.get("live_room") or user.get("liveRoom") or user.get("live_room") or {}
        if not isinstance(room, dict):
            room = {}

        status = user.get("status")
        if status in (2, "2"):
            return True
        for key in ("roomId", "room_id", "roomIdStr", "id_str", "id"):
            value = room.get(key) or root.get(key)
            if value and str(value) not in ("0", "", "None"):
                # A room ID alone is not always proof of live; combine with status hints when present.
                if room.get("status") in (2, "2") or root.get("status") in (2, "2"):
                    return True
        if room.get("status") in (2, "2"):
            return True
        # Conservative default when structure is recognized but no live signal found.
        if user or room or root:
            return False
    except Exception as e:
        print(f"[{username}] Live probe error: {e}")
        return None
    return None

def clear_active_live_status(username: str):
    """Menghapus status live akun dari database."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM active_live_announcements WHERE tiktok_username = ?", (username,))
        conn.commit()
    except Exception as e:
        print(f"[{username}] Failed to clear active live status: {e}")
    finally:
        conn.close()

# --- Sign-server (EulerStream) API key -------------------------------------------
# Rate limits are per account/day on the free tier. An API key raises them.
# Priority: SIGN_API_KEY / TIKTOK_SIGN_API_KEY env var, then DB setting "sign_api_key".
_SIGN_KEY_LOGGED = {"value": None}


def _apply_sign_api_key() -> bool:
    key = (os.environ.get("SIGN_API_KEY") or os.environ.get("TIKTOK_SIGN_API_KEY") or "").strip()
    if not key:
        try:
            key = (get_setting("sign_api_key", "") or "").strip()
        except Exception:
            key = ""
    if not key:
        return False
    try:
        from TikTokLive.client.web.web_settings import WebDefaults
        WebDefaults.tiktok_sign_api_key = key
    except Exception as e:
        print(f"[Signer] Could not apply EulerStream API key: {e}")
        return False
    if _SIGN_KEY_LOGGED["value"] != key[:6]:
        _SIGN_KEY_LOGGED["value"] = key[:6]
        print("[Signer] EulerStream API key applied (higher rate limits).")
        log_activity_event("system", "[Debug] EulerStream sign API key detected; using it for websocket signing.", "")
    return True


# --- Rate limit cooldown helpers -------------------------------------------------
# EulerStream/TikTokLive rate limits are per account (and often per day). Persist
# the cooldown so a redeploy/restart does not immediately burn more attempts.
_LAST_OFFLINE_LOG = {}
_OFFLINE_LOG_INTERVAL_SECONDS = 600


def _is_reconnect_requested(username: str) -> bool:
    with _RECONNECT_REQUESTED_LOCK:
        return username in _RECONNECT_REQUESTED


def _clear_rate_limit_state(username: str) -> None:
    """Reset persisted rate-limit strikes/cooldown for an account (cheap no-op if clean)."""
    try:
        if str(get_setting(f"rl_strikes:{username}", "0") or "0") == "0":
            return
        set_setting(f"rl_strikes:{username}", "0")
        set_setting(f"rl_until:{username}", "0")
    except Exception:
        pass


def _rate_limit_cooldown_seconds(username: str, err_text: str) -> int:
    """Escalating, persisted cooldown for sign-server/account rate limits."""
    key = f"rl_strikes:{username}"
    try:
        strikes = int(get_setting(key, "0") or 0) + 1
    except Exception:
        strikes = 1
    try:
        set_setting(key, str(strikes))
    except Exception:
        pass
    cooldown = {1: 1800, 2: 3600, 3: 7200}.get(strikes, 10800)
    text = (err_text or "").lower()
    if "account_day" in text or "rate limit" in text:
        # Daily per-account quota exhausted: wait for the next UTC day reset.
        now = datetime.now(pytz.UTC)
        next_reset = (now + timedelta(days=1)).replace(hour=0, minute=10, second=0, microsecond=0)
        until_reset = max(1800, int((next_reset - now).total_seconds()))
        cooldown = max(cooldown, min(until_reset, 6 * 3600))
    try:
        set_setting(f"rl_until:{username}", str(int(time.time()) + cooldown))
    except Exception:
        pass
    return cooldown


async def start_single_observer(tiktok_username: str, announcement_callback, role_callback, comment_callback=None):
    username_clean = tiktok_username.lower().lstrip("@")
    current_session_id = None
    is_connected = False
    disconnect_timer_task = None
    # Menyimpan sesi & waktu disconnect terakhir selama grace period, supaya
    # observer yang dihentikan saat grace tetap menutup sesi di waktu disconnect.
    disconnect_meta = {"session_id": None, "at": None}
    current_host = get_current_host_for_account(username_clean) or "Host"

    async def check_and_update_host_from_text(text: str, sender_username: str = None, source: str = "message", origin: str = "event"):
        """Detect host from Live Intro data only (event or strict intro fallback)."""
        nonlocal current_host, current_session_id
        allowed_sources = ("live_intro", "live_intro_fallback")
        sender = (sender_username or "").lower().lstrip("@")
        # Only accept host detection from Live Intro data for the target account.
        if source not in allowed_sources or sender != username_clean:
            return

        msg_preview = _safe_preview(text, 200)
        if msg_preview and _LAST_INTRO_ANALYZED.get(sender) == msg_preview:
            print(f"[{username_clean}] [HostDetect:{source}] intro text unchanged; skipping re-analysis (current: {current_host}).")
            return
        if not msg_preview:
            print(f"[{username_clean}] [HostDetect:{source}] received via {origin} but the intro text is empty.")
            log_activity_event(
                "host_check",
                f"Live Intro received on @{username_clean} via {origin} but no text was extracted.",
                username_clean,
            )
            return

        _LAST_INTRO_ANALYZED[sender] = msg_preview
        try:
            runtime_status.update_observer_diag(
                username_clean,
                last_intro_source=source,
                last_intro_origin=origin,
                last_intro_preview=msg_preview,
                last_intro_at=datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y-%m-%d %H:%M:%S'),
            )
        except Exception:
            pass

        detected, matched_alias = detect_host_match_from_welcome_message(text)
        if detected:
            if detected != current_host:
                conclusion = f"host changed: {current_host} -> {detected} (alias match: {matched_alias!r})"
            else:
                conclusion = f"host unchanged: {detected} (alias match: {matched_alias!r})"
        else:
            try:
                alias_keys = sorted(get_host_aliases().keys())
            except Exception:
                alias_keys = []
            sample = ", ".join(alias_keys[:12]) + ("..." if len(alias_keys) > 12 else "")
            conclusion = (
                f"no alias match in database host_aliases (current: {current_host}). "
                f"Configured aliases ({len(alias_keys)}): {sample or 'none'}. "
                f"If this intro text names the host, add that alias in Settings."
            )

        print(f"[{username_clean}] [HostDetect:{source}] origin={origin} sender=@{sender} message={msg_preview!r} => {conclusion}")

        if detected and detected != current_host:
            old_host = current_host
            current_host = detected
            runtime_status.set_live(username_clean, detected, source=source)
            if current_session_id:
                update_live_session_host(current_session_id, detected)
            set_current_host_for_account(username_clean, detected)
            log_activity_event(
                "host_detected",
                f"Alias match in database: '{matched_alias}' -> {detected}. Host changed {old_host} -> {detected} on @{username_clean} from {origin}: \"{msg_preview}\"",
                username_clean,
            )

            # Update/edit pesan pengumuman yang sudah ada di Discord
            await _trigger_announcement(announcement_callback, username_clean, True, host_name=detected)
        elif detected:
            log_activity_event(
                "host_check",
                f"Alias match in database: '{matched_alias}' -> {detected}. Host unchanged ({detected}) on @{username_clean} from {origin}: \"{msg_preview}\"",
                username_clean,
            )
        else:
            log_activity_event(
                "host_check",
                f"No alias match in database (host_aliases) on @{username_clean} from {origin}: \"{msg_preview}\" (current: {current_host})",
                username_clean,
            )

    async def try_live_intro_fallback(client, reason: str, log_missing: bool = True, allow_when_not_connected: bool = False):
        """Apply previously captured LiveIntroEvent output (cached) if available.

        Host detection comes from LiveIntroEvent (WebcastLiveIntroMessage) output
        text; we keep listening for that event on online/connected accounts until
        output is received. Cached text is a prior LiveIntroEvent output only.
        """
        online = is_connected or probed_live or (username_clean in get_current_live_hosts())
        if not online:
            if log_missing:
                print(f"[{username_clean}] [HostDetect:live_intro_fallback] skipped ({reason}): account not online.")
            return False
        if not is_connected and not allow_when_not_connected:
            if log_missing:
                print(f"[{username_clean}] [HostDetect:live_intro_fallback] skipped ({reason}): websocket not connected.")
            return False

        cached = _get_cached_live_intro_text(username_clean)
        if cached:
            if log_missing:
                log_activity_event(
                    "host_check",
                    f"Using cached LiveIntroEvent output for @{username_clean} ({reason}): {_safe_preview(cached)}",
                    username_clean,
                )
            print(
                f"[{username_clean}] [HostDetect:live_intro_fallback] using cached LiveIntroEvent output "
                f"({reason}): {_safe_preview(cached)!r}"
            )
            await check_and_update_host_from_text(
                cached,
                sender_username=username_clean,
                source="live_intro_fallback",
                origin=f"cached_live_intro_event_output ({reason})",
            )
            return True

        if log_missing:
            if is_connected:
                log_activity_event(
                    "host_check",
                    f"No cached LiveIntroEvent output for @{username_clean} ({reason}); still waiting for WebcastLiveIntroMessage output on connected account.",
                    username_clean,
                )
                print(
                    f"[{username_clean}] [HostDetect:live_intro_fallback] no cached LiveIntroEvent output "
                    f"for {reason}; waiting for WebcastLiveIntroMessage."
                )
            else:
                log_activity_event(
                    "host_check",
                    f"No cached LiveIntroEvent output for @{username_clean} ({reason}); account online but websocket not connected yet, will keep trying to connect for LiveIntroEvent.",
                    username_clean,
                )
                print(
                    f"[{username_clean}] [HostDetect:live_intro_fallback] no cached LiveIntroEvent output "
                    f"for {reason}; websocket not connected."
                )
        return False

    consecutive_failures = 0
    probed_live = False

    async def delayed_disconnect_handler(session_to_close, disconnected_at: str):
        """Menunggu grace period 10 menit sebelum benar-benar menyatakan live berakhir di Discord.

        ``disconnected_at`` adalah waktu disconnect terakhir: sesi live ditutup
        pada waktu itu (bukan waktu timer grace selesai), sehingga durasi live
        tidak kelebihan waktu tunggu dan disconnect terakhir menjadi end live.
        """
        try:
            await asyncio.sleep(LIVE_DISCONNECT_GRACE_SECONDS)
            nonlocal is_connected, current_session_id, probed_live
            runtime_status.clear_live(username_clean)
            probed_live = False
            if session_to_close:
                end_live_session(session_to_close, end_time=disconnected_at)
            # Saat offline, bot Discord akan MENGHAPUS pesan pengumuman lewat sesi tersimpan.
            # JANGAN hapus sesi DB lebih dulu, karena message_id dibutuhkan untuk menghapus pesan Discord.
            if announcement_callback:
                await _trigger_announcement(announcement_callback, username_clean, False)
            else:
                # Fallback only when no callback exists; this cannot delete Discord messages.
                clear_active_live_status(username_clean)
                print(f"[{username_clean}] No announcement callback; cleared announcement DB status only.")
            print(f"[{username_clean}] Live confirmed offline after grace period.")
        except asyncio.CancelledError:
            # Reconnected within grace period
            pass

    def resolve_grace_reconnect() -> bool:
        """Batalkan grace timer dan catat jeda offline sebagai paused time.

        Jeda saat live pending/terputus tidak boleh dihitung sebagai durasi
        live. Saat live tersambung lagi dalam grace period, selisih waktu
        disconnect -> reconnect ditambahkan ke paused_seconds sesi.
        """
        nonlocal disconnect_timer_task
        if not (disconnect_timer_task and not disconnect_timer_task.done()):
            return False
        disconnect_timer_task.cancel()
        disconnect_timer_task = None
        session_id = disconnect_meta.get("session_id")
        disconnected_at = disconnect_meta.get("at")
        disconnect_meta["session_id"] = None
        disconnect_meta["at"] = None
        if session_id and disconnected_at:
            try:
                wib = pytz.timezone('Asia/Jakarta')
                disc_dt = wib.localize(
                    datetime.strptime(str(disconnected_at)[:19], '%Y-%m-%d %H:%M:%S')
                )
                gap = max(0, int((datetime.now(wib) - disc_dt).total_seconds()))
                if gap > 0:
                    add_live_session_paused_seconds(session_id, gap)
                    print(
                        f"[{username_clean}] Grace gap {gap}s excluded from live duration "
                        f"(session {session_id})."
                    )
            except Exception as e:
                print(f"[{username_clean}] Failed to record grace paused time: {e}")
        return True

    while True:
        connect_mode = (get_host_connect_mode(username_clean) or "normal").lower()
        if connect_mode == "off":
            runtime_status.update_observer_diag(username_clean, status="non_priority_off", connect_mode="off")
            await asyncio.sleep(60)
            continue
        # Respect persisted rate-limit cooldown so restarts/redeploys don't burn
        # more daily-quota attempts. Manual Reconnect/Reset overrides it.
        try:
            rl_until = int(float(get_setting(f"rl_until:{username_clean}", "0") or 0))
        except Exception:
            rl_until = 0
        remaining_cooldown = rl_until - int(time.time())
        if remaining_cooldown > 0 and not _is_reconnect_requested(username_clean):
            runtime_status.update_observer_diag(
                username_clean,
                status="rate_limited_cooldown",
                wait_seconds=remaining_cooldown,
                rate_limited=True,
            )
            log_activity_event(
                "system",
                f"[Debug @{username_clean}] Rate-limit cooldown active; waiting {max(1, remaining_cooldown // 60)}m before retry "
                f"(EulerStream daily quota per account). Manual Reconnect/Reset overrides.",
                username_clean,
            )
            for _ in range(remaining_cooldown):
                if _is_reconnect_requested(username_clean):
                    break
                await asyncio.sleep(1)
            continue
        try:
            slow_seconds = max(60, int(get_setting("non_priority_slow_seconds", "300") or 300))
        except Exception:
            slow_seconds = 300
        try:
            _apply_sign_api_key()
        except Exception:
            pass
        client = TikTokLiveClient(unique_id=username_clean)
        connected_this_cycle = False
        reconnect_state = {"triggered": False}
        with _ACTIVE_CLIENTS_LOCK:
            _ACTIVE_CLIENTS[username_clean] = client
        runtime_status.update_observer_diag(username_clean, connect_mode=connect_mode)

        async def _watch_reconnect_request(active_client):
            while True:
                await asyncio.sleep(2)
                if active_client is not client:
                    return
                requested = False
                with _RECONNECT_REQUESTED_LOCK:
                    if username_clean in _RECONNECT_REQUESTED:
                        _RECONNECT_REQUESTED.discard(username_clean)
                        requested = True
                if requested:
                    reconnect_state["triggered"] = True
                    print(f"[{username_clean}] Reconnect requested by admin; stopping current client.")
                    try:
                        stopper = getattr(active_client, "stop", None) or getattr(active_client, "disconnect", None)
                        if stopper:
                            res = stopper()
                            if asyncio.iscoroutine(res):
                                await res
                    except Exception as e:
                        print(f"[{username_clean}] Failed to stop client for reconnect: {e}")
                    return

        reconnect_watch_task = asyncio.create_task(_watch_reconnect_request(client))
        intro_watch_task = None

        async def _watch_live_intro_until_detected(active_client):
            # Only for accounts detected as active (connected). Repeat until a host is detected.
            # TikTok only sends the intro once per connection, so this is mostly a
            # visibility/diagnostic loop: back off progressively and log sparsely.
            waits = [30, 60, 120, 240, 300]
            checks = 0
            while True:
                await asyncio.sleep(waits[min(checks, len(waits) - 1)])
                checks += 1
                if active_client is not client or not is_connected:
                    return
                if current_host and current_host != "Host":
                    return
                cached = _get_cached_live_intro_text(username_clean)
                if checks == 1 or checks % 4 == 0:
                    next_wait = waits[min(checks, len(waits) - 1)]
                    log_activity_event(
                        "host_check",
                        f"[Debug @{username_clean}] Still waiting for LiveIntroEvent (WebcastLiveIntroMessage) output "
                        f"| checks={checks} | online_connected={is_connected} | current_host={current_host} "
                        f"| cached_intro={'yes' if cached else 'no'} | next_check_in={next_wait}s",
                        username_clean,
                    )
                print(f"[{username_clean}] Waiting for LiveIntroEvent output (connected={is_connected}, host={current_host})...")
                try:
                    await try_live_intro_fallback(active_client, "retry_connected", log_missing=False)
                except Exception as e:
                    print(f"[{username_clean}] Intro retry error: {e}")

        def _start_intro_watch_if_needed():
            nonlocal intro_watch_task
            if not is_connected or (current_host and current_host != "Host"):
                return
            if intro_watch_task and not intro_watch_task.done():
                return
            intro_watch_task = asyncio.create_task(_watch_live_intro_until_detected(client))

        @client.on(ConnectEvent)
        async def on_connect(event: ConnectEvent):
            nonlocal current_session_id, is_connected, disconnect_timer_task, current_host, connected_this_cycle, consecutive_failures, probed_live
            try:
                reconnected_in_grace = resolve_grace_reconnect()
                if reconnected_in_grace:
                    print(f"[{username_clean}] Reconnected within grace period. Live status maintained.")

                is_connected = True
                connected_this_cycle = True
                consecutive_failures = 0
                probed_live = False

                # On manual refresh/reconnect request, reconfirm host instead of trusting stale stored value.
                force_reconfirm_host = False
                with _HOST_RECONFIRM_REQUESTED_LOCK:
                    if username_clean in _HOST_RECONFIRM_REQUESTED:
                        _HOST_RECONFIRM_REQUESTED.discard(username_clean)
                        force_reconfirm_host = True

                if force_reconfirm_host:
                    print(f"[{username_clean}] Reconfirming host on reconnect (clearing stale host state).")
                    try:
                        set_setting(f"current_host_{username_clean}", "")
                    except Exception:
                        pass
                    # Use neutral placeholder until a real host is detected from LiveIntroEvent.
                    current_host = "Host"
                    _LAST_INTRO_ANALYZED.pop(username_clean, None)
                else:
                    # Refresh host/session context on (re)connect so host detection can continue/update.
                    stored_host = get_current_host_for_account(username_clean)
                    if stored_host and stored_host != "Host":
                        current_host = stored_host

                if not current_session_id:
                    current_session_id = start_live_session(username_clean, initial_host=current_host)
                elif force_reconfirm_host:
                    try:
                        update_live_session_host(current_session_id, "Host")
                    except Exception:
                        pass

                runtime_status.set_live(username_clean, current_host, source="event")
                runtime_status.update_observer_diag(
                    username_clean,
                    status="reconnected" if reconnected_in_grace else "connected",
                    consecutive_failures=0,
                    last_error=None,
                )
                print(f"[{username_clean}] {'Reconnected to' if reconnected_in_grace else 'Connected to'} live stream (Host: {current_host}). Host detection active.")

                # Host detection source is LiveIntroEvent only. If the event has
                # not arrived yet, try the strict Live Intro fallback (intro-named
                # room-info fields or the last received intro text). Never use
                # the room title, comments or other arbitrary text.
                room_info = getattr(client, "room_info", None)
                if isinstance(room_info, dict):
                    print(
                        f"[{username_clean}] room_info available for Live Intro fallback "
                        f"(keys={sorted(room_info.keys())[:30]})."
                    )
                else:
                    print(
                        f"[{username_clean}] room_info unavailable "
                        f"(type={type(room_info).__name__}); Live Intro fallback will use cached intro if any."
                    )
                if current_host == "Host":
                    await try_live_intro_fallback(
                        client,
                        "reconnect" if reconnected_in_grace else "connect",
                    )

                connect_msg = f"{'Reconnected to' if reconnected_in_grace else 'Connected to'} @{username_clean}'s live (Host: {current_host})"
                log_activity_event("connect", connect_msg, username_clean)
                if LiveIntroEvent:
                    attach_msg = f"[Debug @{username_clean}] LiveIntroEvent (WebcastLiveIntroMessage) listener attached after connect (online/connected). current_host={current_host}"
                    print(f"[{username_clean}] {attach_msg}")
                    log_activity_event("system", attach_msg, username_clean)
                    try:
                        runtime_status.update_observer_diag(username_clean, live_intro_listener_attached=True)
                    except Exception:
                        pass
                log_activity_event(
                    "system",
                    f"[Debug @{username_clean}] Connected online (websocket). Listening for LiveIntroEvent (WebcastLiveIntroMessage) output until host detected | current_host={current_host} | source=event",
                    username_clean,
                )
                print(f"[{username_clean}] Connected (online/websocket); listening for LiveIntroEvent output until host detected (current_host={current_host})...")

                # Kirim/update pengumuman agar status tetap sinkron setelah (re)connect.
                await _trigger_announcement(announcement_callback, username_clean, True, host_name=current_host)
                _start_intro_watch_if_needed()
            except Exception as e:
                # Keep runtime live status even if DB/announcement side effects fail.
                runtime_status.set_live(username_clean, current_host, source="event")
                runtime_status.update_observer_diag(username_clean, status="connected_with_handler_error", last_error=str(e))
                print(f"[{username_clean}] Connect handler error (live status preserved): {e}")

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            nonlocal current_session_id, is_connected, disconnect_timer_task, intro_watch_task
            print(f"[{username_clean}] Disconnected from live stream.")

            if is_connected:
                is_connected = False
                if intro_watch_task and not intro_watch_task.done():
                    intro_watch_task.cancel()
                disconnect_msg = f"Disconnected from @{username_clean}'s live"
                log_activity_event("disconnect", disconnect_msg, username_clean)

                # Jalankan grace period agar tidak spam hapus/buat pengumuman jika hanya reconnect singkat
                if current_session_id and (not disconnect_timer_task or disconnect_timer_task.done()):
                    disconnected_at = datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y-%m-%d %H:%M:%S')
                    disconnect_meta["session_id"] = current_session_id
                    disconnect_meta["at"] = disconnected_at
                    disconnect_timer_task = asyncio.create_task(delayed_disconnect_handler(current_session_id, disconnected_at))
                    current_session_id = None

        @client.on(CommentEvent)
        async def on_comment(event: CommentEvent):
            try:
                user = event.user.unique_id.lower()
                comment_text = event.comment
                runtime_status.touch_live(username_clean)
                
                # 1. Cek verifikasi live chat akun Discord
                if comment_callback:
                    if asyncio.iscoroutinefunction(comment_callback):
                        await comment_callback(user, comment_text, username_clean)
                    else:
                        comment_callback(user, comment_text, username_clean)

                # Host detection uses LiveIntroEvent only.
                # (comment text ignored for host detection)
            except Exception as e:
                print(f"[{username_clean}] Error in on_comment handler: {e}")

        # Host detection source: LiveIntroEvent only.
        if GreetingEvent:
            @client.on(GreetingEvent)
            async def on_greeting(event):
                try:
                    _ = getattr(event, 'greeting_text', '') or getattr(event, 'guide_text', '')
                except Exception as e:
                    print(f"[{username_clean}] Error in on_greeting handler: {e}")

        if LiveIntroEvent:
            @client.on(LiveIntroEvent)
            async def on_live_intro(event):
                try:
                    # Only process LiveIntroEvent for accounts detected as active/online (connected).
                    if not is_connected:
                        return
                    text, field_name, candidates = _extract_live_intro_text(event)
                    try:
                        attr_names = [n for n in dir(event) if not n.startswith("_")][:40]
                    except Exception:
                        attr_names = []
                    candidate_names = [name for name, _ in candidates]
                    msg_preview_raw = _safe_preview(text, 200)
                    debug_line = (
                        f"[Debug @{username_clean}] LiveIntroEvent (WebcastLiveIntroMessage) received | online_connected={is_connected} | "
                        f"current_host={current_host} | field={field_name or 'none'} | candidates={candidate_names} | "
                        f"attrs={attr_names} | text={msg_preview_raw or '(empty)'}"
                    )
                    print(f"[{username_clean}] [HostDetect:live_intro] {debug_line}")
                    log_activity_event("host_check", debug_line, username_clean)
                    if not text:
                        log_activity_event(
                            "host_check",
                            f"LiveIntroEvent (WebcastLiveIntroMessage) received on @{username_clean} but no intro text was extracted "
                            f"(fields: {attr_names}).",
                            username_clean,
                        )
                        try:
                            runtime_status.update_observer_diag(
                                username_clean,
                                last_intro_source="live_intro",
                                last_intro_origin="event_no_text",
                                last_intro_preview="",
                                last_intro_at=datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y-%m-%d %H:%M:%S'),
                            )
                        except Exception:
                            pass
                        return
                    _cache_live_intro_text(username_clean, text)
                    await check_and_update_host_from_text(
                        text,
                        sender_username=username_clean,
                        source="live_intro",
                        origin=f"live_intro_event:{field_name or 'content'}",
                    )
                except Exception as e:
                    print(f"[{username_clean}] Error in on_live_intro handler: {e}")
                    log_activity_event("error", f"LiveIntroEvent (WebcastLiveIntroMessage) handler error on @{username_clean}: {e}", username_clean)
        else:
            # LiveIntroEvent unavailable in library; fallback (only when connected) will be used.
            pass

        if NoticeEvent:
            @client.on(NoticeEvent)
            async def on_notice(event):
                try:
                    _ = getattr(event, 'content', '') or getattr(event, 'notice_content', '')
                except Exception as e:
                    print(f"[{username_clean}] Error in on_notice handler: {e}")

        if RoomPinEvent:
            @client.on(RoomPinEvent)
            async def on_room_pin(event):
                try:
                    chat_msg = getattr(event, 'chat_message', '')
                    if hasattr(chat_msg, 'content'):
                        chat_msg = chat_msg.content
                    _ = str(chat_msg or '')
                except Exception as e:
                    print(f"[{username_clean}] Error in on_room_pin handler: {e}")

        @client.on(GiftEvent)
        async def on_gift(event: GiftEvent):
            try:
                if not is_feature_enabled("live_tracking"):
                    return
                runtime_status.touch_live(username_clean)
                # TikTokLive v7 moved ``streaking`` onto the event itself; older
                # versions exposed it on event.gift. Support both without raising.
                streaking = bool(getattr(event, "streaking", False) or getattr(getattr(event, "gift", None), "streaking", False))
                streakable = bool(getattr(getattr(event, "gift", None), "streakable", False))
                if streakable and streaking:
                    return

                gift_obj = getattr(event, "gift", None)
                user_obj = getattr(event, "user", None)
                user = (getattr(user_obj, "unique_id", None) or getattr(user_obj, "display_id", "") or "").lower().lstrip("@")
                if not user:
                    return
                display_name = getattr(user_obj, "nickname", None) or user

                # ``repeat_count`` is the full combo count on the final
                # (non-streaking) streak event and the batch size otherwise.
                try:
                    repeat = int(getattr(event, "repeat_count", 1) or 1)
                except Exception:
                    repeat = 1
                if repeat < 1 or repeat > 100000:
                    repeat = 1

                diamond = 0
                for candidate in (
                    getattr(gift_obj, "diamond_count", 0),
                    getattr(getattr(gift_obj, "info", None), "diamond_count", 0),
                    getattr(getattr(gift_obj, "m_gift", None), "diamond_count", 0),
                ):
                    try:
                        if candidate:
                            diamond = int(candidate)
                            break
                    except Exception:
                        continue
                coins = max(0, diamond) * repeat
                gift_name = (
                    getattr(gift_obj, "name", None)
                    or getattr(gift_obj, "gift_name", None)
                    or "Gift"
                )

                # Unique id of the TikTok message: prevents double counting when
                # a gift is replayed after a reconnect or delivered twice. Only
                # the real message id is used; ``log_id`` is not always unique
                # and using it could silently drop legitimate gifts.
                common = getattr(event, "common", None)
                msg_id = str(getattr(common, "msg_id", "") or "").strip() or None

                if coins > 0:
                    nonlocal current_session_id
                    if not current_session_id:
                        current_session_id = start_live_session(username_clean, initial_host=current_host)

                    # Insert the detailed log first: if this message id was
                    # already stored, skip aggregate updates entirely.
                    log_status = log_gift_transaction(
                        session_id=current_session_id,
                        target_user=username_clean,
                        viewer_user=user,
                        viewer_display=display_name,
                        gift_name=gift_name,
                        gift_count=repeat,
                        coins=coins,
                        msg_id=msg_id,
                        host_name=current_host,
                    )
                    if log_status is False:
                        print(
                            f"[{username_clean}] Duplicate gift message ignored "
                            f"(msg_id={msg_id or 'n/a'}, @{user}, {gift_name}, {coins}c)."
                        )
                        return
                    if log_status is None:
                        print(
                            f"[{username_clean}] WARNING: gift log write failed for @{user} "
                            f"({gift_name}, {coins}c); coin totals will still be updated."
                        )

                    total_coins = save_gift_data(user, display_name, coins)
                    save_daily_gift_data(user, display_name, coins)

                    # Format Waktu WIB
                    wib = pytz.timezone('Asia/Jakarta')
                    now_wib = datetime.now(wib)
                    full_datetime = now_wib.strftime('%Y-%m-%d %H:%M:%S')
                    today_str = now_wib.strftime('%Y-%m-%d')

                    channel_spent = get_user_channel_spent(user, username_clean)
                    today_spent = get_user_today_spent(user, today_str)
                    total_all = get_user_total_spent(user)
                    channel_name = get_channel_display_name(username_clean)

                    # Log activity gift lengkap: tanggal, nama akun, user, nama gift, jumlah koin, total koin saat ini
                    gift_msg = (
                        f"[{full_datetime}] [{channel_name} (@{username_clean})] "
                        f"Host: {current_host} | "
                        f"User: @{user} - {gift_name} x{repeat} ({diamond} diamonds) = {coins} coins [{coins}c] - "
                        f"[channel {channel_spent} c] - [today {today_spent} c] - [current total: {total_all} c]"
                    )
                    log_activity_event("gift", gift_msg, username_clean)
                    print(f"[Activity] {gift_msg}")

                    if role_callback:
                        if asyncio.iscoroutinefunction(role_callback):
                            await role_callback(user, total_coins)
                        else:
                            role_callback(user, total_coins)
            except Exception as e:
                print(f"[{username_clean}] Error handling GiftEvent: {e}")

        cycle_error = None
        client_task = None
        try:
            # TikTokLive v7's start() is non-blocking: it returns the websocket
            # task immediately. Await the task so the observer does not create a
            # new client every cycle while old clients keep running.
            try:
                client_task = await client.start(fetch_room_info=True)
            except Exception as room_info_error:
                err_text = str(room_info_error).lower()
                if "age" in err_text and "restrict" in err_text:
                    print(f"[{username_clean}] Room info unavailable (age-restricted); connecting without it.")
                    client_task = await client.start(fetch_room_info=False)
                else:
                    raise
            await client_task
        except asyncio.CancelledError:
            # Observer stopped by supervisor (host paused/removed/restarted).
            if client_task and not client_task.done():
                client_task.cancel()
            if disconnect_timer_task and not disconnect_timer_task.done():
                disconnect_timer_task.cancel()
                # Tutup sesi yang sedang menunggu grace di waktu disconnect
                # terakhir, bukan membiarkannya terbuka.
                grace_session_id = disconnect_meta.get("session_id")
                grace_at = disconnect_meta.get("at")
                if grace_session_id:
                    try:
                        end_live_session(grace_session_id, end_time=grace_at)
                    except Exception:
                        pass
            if is_connected and current_session_id:
                try:
                    end_live_session(current_session_id)
                except Exception:
                    pass
            runtime_status.clear_live(username_clean)
            runtime_status.update_observer_diag(username_clean, status="stopped")
            raise
        except Exception as e:
            cycle_error = e
            err = str(e)
            err_detail = f"{type(e).__name__}: {err}"
            consecutive_failures += 1
            err_lower = err_detail.lower()
            is_rate_limited = ("rate_limit" in err_lower) or ("too many connections" in err_lower) or ("eulerstream" in err_lower)
            is_json_empty_error = ("jsondecodeerror" in err_lower) or ("expecting value" in err_lower)
            runtime_status.update_observer_diag(
                username_clean,
                status="rate_limited" if is_rate_limited else "error",
                consecutive_failures=consecutive_failures,
                last_error=err_detail[:300],
                rate_limited=is_rate_limited,
            )
            if is_rate_limited:
                print(f"[{username_clean}] Websocket rate-limited (failures={consecutive_failures}): {err_detail}")
                log_activity_event(
                    "error",
                    f"[Debug @{username_clean}] Websocket RATE LIMITED (failures={consecutive_failures}, connected_this_cycle={connected_this_cycle}, host={current_host}) - backing off: {err_detail}",
                    username_clean,
                )
            elif "offline" in err_lower or "not found" in err_lower or "useroffline" in err_lower:
                # Offline is a normal state, not an error: throttle logging so the
                # activity feed stays readable while we keep polling.
                last_log = _LAST_OFFLINE_LOG.get(username_clean, 0)
                if time.time() - last_log >= _OFFLINE_LOG_INTERVAL_SECONDS:
                    _LAST_OFFLINE_LOG[username_clean] = time.time()
                    print(f"[{username_clean}] Not live/offline check: {err_detail}")
                    log_activity_event(
                        "system",
                        f"[Debug @{username_clean}] Not live/offline (checks={consecutive_failures}, logged every {_OFFLINE_LOG_INTERVAL_SECONDS // 60}m): {err_detail}",
                        username_clean,
                    )
            else:
                print(f"[{username_clean}] Observer error while checking live (failures={consecutive_failures}): {err_detail}")
                log_activity_event(
                    "error",
                    f"[Debug @{username_clean}] Websocket observer error (failures={consecutive_failures}, connected_this_cycle={connected_this_cycle}, host={current_host}, probe={probe_result if 'probe_result' in locals() else 'n/a'}): {err_detail}",
                    username_clean,
                )
            # Treat sign-server empty/invalid JSON responses as backoff-worthy (often blocking/rate-limit side effects).
            if is_json_empty_error:
                runtime_status.update_observer_diag(username_clean, status="error_json_response")
        finally:
            if reconnect_watch_task and not reconnect_watch_task.done():
                reconnect_watch_task.cancel()
            if intro_watch_task and not intro_watch_task.done():
                intro_watch_task.cancel()
            with _ACTIVE_CLIENTS_LOCK:
                if _ACTIVE_CLIENTS.get(username_clean) is client:
                    _ACTIVE_CLIENTS.pop(username_clean, None)
            if is_connected:
                is_connected = False
                disconnect_msg = f"Disconnected from @{username_clean}'s live"
                log_activity_event("disconnect", disconnect_msg, username_clean)

                if current_session_id and (not disconnect_timer_task or disconnect_timer_task.done()):
                    disconnected_at = datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y-%m-%d %H:%M:%S')
                    disconnect_meta["session_id"] = current_session_id
                    disconnect_meta["at"] = disconnected_at
                    disconnect_timer_task = asyncio.create_task(delayed_disconnect_handler(current_session_id, disconnected_at))
                    current_session_id = None

            # Release this cycle's client. TikTokLive v7 starts non-blocking, so
            # an un-awaited task would otherwise leave sockets running forever.
            if client_task and not client_task.done():
                client_task.cancel()
                try:
                    await client_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            try:
                disconnector = getattr(client, "disconnect", None)
                if disconnector:
                    # NOTE: never pass close_client=True here. TikTokLive's close()
                    # calls loop.run_until_complete() internally, which raises
                    # "This event loop is already running" inside our async loop.
                    result = disconnector()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as e:
                print(f"[{username_clean}] Client disconnect cleanup error: {e}")
            try:
                # Close the HTTP session manually so connections are not leaked
                # between observer cycles (leaks contribute to rate limits).
                web = getattr(client, "web", None)
                closer = getattr(web, "close", None)
                if closer:
                    res = closer()
                    if asyncio.iscoroutine(res):
                        await res
            except Exception as e:
                print(f"[{username_clean}] Client HTTP cleanup error: {e}")

        # Secondary probe: if the event client didn't connect but TikTok reports live,
        # mark status/announcement so the dashboard is not stuck on "No Live Now".
        grace_pending = bool(disconnect_timer_task and not disconnect_timer_task.done())
        probe_result = None
        if not connected_this_cycle:
            probe_result = await probe_room_live(username_clean)
            runtime_status.update_observer_diag(
                username_clean,
                probe_live=probe_result,
                last_probe_at=datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y-%m-%d %H:%M:%S'),
            )
            if probe_result is True:
                if grace_pending:
                    if resolve_grace_reconnect():
                        print(f"[{username_clean}] Probe confirms live during grace period; keeping live status.")
                if not probed_live and username_clean not in get_current_live_hosts():
                    probed_live = True
                    runtime_status.set_live(username_clean, current_host, source="probe")
                    if not current_session_id:
                        current_session_id = start_live_session(username_clean, initial_host=current_host)
                    await _trigger_announcement(announcement_callback, username_clean, True, host_name=current_host)
                    ws_err = str(cycle_error) if cycle_error else "none"
                    log_activity_event(
                        "connect",
                        f"[Debug @{username_clean}] Connected by probe fallback | host={current_host} | websocket_connected=False | last_ws_error={ws_err}",
                        username_clean,
                    )
                    print(f"[{username_clean}] Live detected by probe fallback (websocket event client not connected). last_ws_error={ws_err}")
                    log_activity_event(
                        "system",
                        f"[Debug @{username_clean}] Online via probe but websocket not connected; retrying websocket to receive LiveIntroEvent (WebcastLiveIntroMessage) | last_ws_error={ws_err} | host={current_host}",
                        username_clean,
                    )
                    if current_host == "Host":
                        await try_live_intro_fallback(client, "probe", log_missing=True, allow_when_not_connected=True)
                else:
                    runtime_status.set_live(username_clean, current_host, source=runtime_status.get_live_snapshot().get(username_clean, {}).get("source", "probe"))
                    runtime_status.touch_live(username_clean)
                    if current_host == "Host":
                        await try_live_intro_fallback(client, "probe_retry", log_missing=False, allow_when_not_connected=True)
                # Reset backoff so we keep trying to establish websocket for LiveIntroEvent,
                # unless we are rate-limited (must back off to recover).
                last_err = str(cycle_error or "").lower()
                if not (("rate_limit" in last_err) or ("too many connections" in last_err) or ("eulerstream" in last_err)):
                    consecutive_failures = 0
            elif probe_result is False and not grace_pending:
                if probed_live:
                    runtime_status.clear_live(username_clean)
                    if current_session_id:
                        try:
                            end_live_session(current_session_id)
                        except Exception:
                            pass
                        current_session_id = None
                    await _trigger_announcement(announcement_callback, username_clean, False)
                    log_activity_event("disconnect", f"Disconnected from @{username_clean}'s live (probe confirmed offline)", username_clean)
                    probed_live = False
                if cycle_error is None:
                    runtime_status.update_observer_diag(username_clean, status="offline", consecutive_failures=0, last_error=None)
            elif probe_result is None and cycle_error is None and not grace_pending:
                runtime_status.update_observer_diag(username_clean, status="checking")
        else:
            consecutive_failures = 0
            probe_result = False if connected_this_cycle else None
            _clear_rate_limit_state(username_clean)
            _LAST_OFFLINE_LOG.pop(username_clean, None)

        # Adaptive retry: back off slightly on repeated failures, but keep checking.
        err_lower_cycle = str(cycle_error or "").lower()
        rate_limited_cycle = ("rate_limit" in err_lower_cycle) or ("too many connections" in err_lower_cycle) or ("eulerstream" in err_lower_cycle)
        json_empty_cycle = ("jsondecodeerror" in err_lower_cycle) or ("expecting value" in err_lower_cycle)
        if reconnect_state.get("triggered"):
            wait_seconds = 2
            runtime_status.update_observer_diag(username_clean, status="reconnecting")
        elif rate_limited_cycle:
            # Back off hard on sign-server/account rate limits to avoid prolonging lockout.
            wait_seconds = _rate_limit_cooldown_seconds(username_clean, str(cycle_error or ""))
            runtime_status.update_observer_diag(username_clean, status="rate_limited_backoff", wait_seconds=wait_seconds, rate_limited=True)
            log_activity_event(
                "system",
                f"[Debug @{username_clean}] Rate-limited websocket; backing off {wait_seconds}s (~{max(1, wait_seconds // 60)}m) before retry "
                f"(failures={consecutive_failures}, probe_online={probe_result is True}). "
                f"Persistent limit? Add an EulerStream API key: https://www.eulerstream.com/pricing",
                username_clean,
            )
        elif json_empty_cycle:
            # Empty/invalid JSON responses often indicate blocking/sign-server issues; back off.
            wait_seconds = 120
            if consecutive_failures >= 5:
                wait_seconds = 300
            if connect_mode == "slow":
                wait_seconds = max(wait_seconds, slow_seconds)
            runtime_status.update_observer_diag(username_clean, status="backoff_json_response", wait_seconds=wait_seconds)
            log_activity_event(
                "system",
                f"[Debug @{username_clean}] Websocket returned empty/invalid JSON response; backing off {wait_seconds}s before retry (failures={consecutive_failures}, probe_online={probe_result is True}, mode={connect_mode}).",
                username_clean,
            )
        elif probe_result is True and not connected_this_cycle:
            # Account is online (probe) but websocket event client not connected: retry to receive LiveIntroEvent.
            wait_seconds = 30 if connect_mode == "slow" else 10
            runtime_status.update_observer_diag(username_clean, status="online_waiting_websocket", wait_seconds=wait_seconds)
        else:
            wait_seconds = slow_seconds if connect_mode == "slow" else 30
            if not connected_this_cycle and consecutive_failures >= 5:
                wait_seconds = max(wait_seconds, 60)
            if not connected_this_cycle and consecutive_failures >= 10:
                wait_seconds = max(wait_seconds, 120)
        # If reconnect was requested while waiting, skip delay.
        for _ in range(wait_seconds):
            with _RECONNECT_REQUESTED_LOCK:
                requested = username_clean in _RECONNECT_REQUESTED
            if requested:
                break
            await asyncio.sleep(1)


def request_tiktok_reconnect(username: str = None):
    """Request observer reconnect for one host or all active hosts."""
    with _ACTIVE_CLIENTS_LOCK:
        targets = [username.lower().lstrip("@")] if username else list(_ACTIVE_CLIENTS.keys())
    if not targets:
        # No active clients registered; target active hosts so next cycle picks up quickly.
        try:
            targets = [u.lower().lstrip("@") for u in get_active_hosts() if u]
        except Exception:
            targets = []
    with _RECONNECT_REQUESTED_LOCK:
        for target in targets:
            if target:
                _RECONNECT_REQUESTED.add(target)
    # Manual reconnect is an explicit admin override: clear persisted rate-limit cooldown.
    for target in targets:
        if target:
            _clear_rate_limit_state(target)
            _LAST_INTRO_ANALYZED.pop(target, None)
    with _HOST_RECONFIRM_REQUESTED_LOCK:
        for target in targets:
            if target:
                _HOST_RECONFIRM_REQUESTED.add(target)
    try:
        label = ", ".join(f"@{t}" for t in targets if t) or "(no targets)"
        log_activity_event("reconnect", f"TikTok reconnect requested for {label}.", (targets[0] if len(targets) == 1 else ""))
    except Exception:
        pass
    return targets

async def run_all_observers(announcement_callback, role_callback, comment_callback=None):
    """Supervise one observer task per active host, refreshing from the DB.

    Hosts added, paused or removed in the admin panel are picked up within a
    minute without restarting the bot.
    """
    tasks = {}
    print("[Observer System] Starting supervised observers for target hosts in DB.")
    try:
        log_activity_event("system", "Observer supervisor started.", "")
    except Exception:
        pass
    if LiveIntroEvent is None:
        print(
            "[Observer System] LiveIntroEvent is unavailable in the installed TikTokLive; "
            "host detection will use the strict room-info intro fallback only."
        )
        try:
            log_activity_event(
                "system",
                "LiveIntroEvent is unavailable in the installed TikTokLive; strict intro fallback only.",
                "",
            )
        except Exception:
            pass
    else:
        print("[Observer System] LiveIntroEvent support enabled; host detection source is Live Intro only.")
    try:
        aliases = get_host_aliases()
        alias_preview = ", ".join(f"{a}->{h}" for a, h in aliases[:40])
        print(f"[Observer System] Host aliases loaded ({len(aliases)}): {alias_preview}")
        try:
            log_activity_event("system", f"Host aliases loaded ({len(aliases)}): {alias_preview}", "")
        except Exception:
            pass
    except Exception as e:
        print(f"[Observer System] Failed to load host aliases: {e}")

    while True:
        try:
            observable = get_observable_hosts() or []
        except Exception:
            observable = [{"username": u, "mode": "normal"} for u in (get_active_hosts() or [])]
        target_users = [u["username"].lower().lstrip("@") for u in observable if u.get("username")]
        target_set = set(target_users)
        target_modes = {u["username"].lower().lstrip("@"): (u.get("mode") or "normal") for u in observable if u.get("username")}

        # Stop observers for hosts that were paused, removed, or set to non-priority off.
        for username in list(tasks.keys()):
            task = tasks[username]
            if username not in target_set:
                if task and not task.done():
                    task.cancel()
                tasks.pop(username, None)
                runtime_status.clear_live(username)
                print(f"[Observer System] Stopped observer for @{username} (inactive or excluded by priority mode).")
                try:
                    log_activity_event("system", f"Stopped observer for @{username} (inactive or excluded by priority mode).", username)
                except Exception:
                    pass

        # Start observers for newly added or previously failed hosts.
        for username in target_users:
            task = tasks.get(username)
            if task is None or task.done():
                mode = target_modes.get(username, "normal")
                tasks[username] = asyncio.create_task(
                    start_single_observer(
                        username, announcement_callback, role_callback, comment_callback
                    )
                )
                print(f"[Observer System] Started observer for @{username} (mode={mode}).")
                try:
                    log_activity_event("system", f"Started observer task for @{username} (mode={mode}).", username)
                except Exception:
                    pass

        try:
            summary = ", ".join(f"@{u}:{target_modes.get(u,'normal')}" for u in target_users) or "(none)"
            runtime_status.update_observer_diag("_supervisor", status="running", targets=summary)
        except Exception:
            pass

        await asyncio.sleep(60)

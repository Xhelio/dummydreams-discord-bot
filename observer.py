import asyncio
from importlib import metadata
import os
import ssl
import sys
import time
from datetime import datetime, timedelta
import pytz
import log_setup
from tiktoklive_parser import NonBlockingTikTokLiveClient as TikTokLiveClient
from proto_performance import install_proto_type_hint_caches

# betterproto2 resolves typing annotations during nested protobuf parsing.
# Cache those immutable class hints to avoid repeating get_type_hints() per event.
_PROTO_TYPE_HINT_CACHES = install_proto_type_hint_caches()
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
    record_gift_event_transaction,
    get_active_hosts,
    get_observable_hosts,
    get_db_connection,
    log_activity_event,
    get_gift_event_summary,
    detect_host_from_welcome_message,
    detect_host_match_from_welcome_message,
    get_current_host_for_account,
    set_current_host_for_account,
    touch_current_host_detection,
    update_live_session_host,
    add_live_session_paused_seconds,
    touch_live_session,
    get_open_live_session_id,
    invalidate_settings_cache,
    get_host_aliases,
    get_host_connect_mode,
    is_feature_enabled,
    get_setting,
    set_setting,
    queue_setting
)
import runtime_status
from timed_executor import call_with_timing

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

# Slow GiftEvent stage diagnostics are throttled per host/stage to avoid log floods.
_GIFT_TIMING_LOG = log_setup.get_logger("observer.timing")
_GIFT_TIMING_LOCK = threading.Lock()
_GIFT_TIMING_LAST_LOG = {}
_GIFT_TIMING_THRESHOLD_SECONDS = 0.5
_GIFT_TIMING_COOLDOWN_SECONDS = 30.0


def _log_gift_timing(username: str, stage: str, elapsed: float, context: str = "host") -> bool:
    """Log a slow gift-processing stage at most once per host/stage per cooldown."""
    if elapsed < _GIFT_TIMING_THRESHOLD_SECONDS:
        return False
    now = time.monotonic()
    key = (username, stage)
    with _GIFT_TIMING_LOCK:
        if now - _GIFT_TIMING_LAST_LOG.get(key, 0.0) < _GIFT_TIMING_COOLDOWN_SECONDS:
            return False
        _GIFT_TIMING_LAST_LOG[key] = now
    _GIFT_TIMING_LOG.info(
        "[GiftEvent Timing] %s=%s stage=%s elapsed=%.3fs",
        context,
        username,
        stage,
        elapsed,
    )
    return True


# Cached Live Intro text. This is only ever populated from LiveIntroEvent (or
# intro-named fields in room info), so the fallback stays tied to the intro
# source and never uses arbitrary chat / room titles.
_LAST_INTRO_TEXT = {}
_LAST_INTRO_ANALYZED = {}
_INTRO_CACHE_TTL_SECONDS = 6 * 3600


def _extract_comment_username(event) -> str:
    """Return a normalized TikTok username across TikTokLive user model versions."""
    user_obj = event.get("user") if isinstance(event, dict) else getattr(event, "user", None)
    if user_obj is None:
        return ""
    for attribute in ("unique_id", "display_id"):
        try:
            value = user_obj.get(attribute) if isinstance(user_obj, dict) else getattr(user_obj, attribute, None)
        except Exception:
            value = None
        if value is not None:
            username = str(value).strip().lstrip("@").lower()
            if username:
                return username
    return ""


def _observer_init_retry_delay(failures: int) -> int:
    """Return bounded retry delay for repeated TikTok client initialization failures."""
    count = max(1, int(failures or 0))
    if count >= 10:
        return 120
    if count >= 5:
        return 60
    return 30


def _exception_chain_types(exc: BaseException, limit: int = 5) -> str:
    """Describe exception types across chained causes without exposing payloads."""
    types = []
    seen = set()
    current = exc
    while current is not None and id(current) not in seen and len(types) < limit:
        seen.add(id(current))
        types.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    return " -> ".join(types)


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
            if len(out) >= 5:
                break
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
    # Non-blocking: jalan di event loop Discord, jadi jangan menunggu Turso.
    try:
        queue_setting(f"last_live_intro_{username}", text)
        queue_setting(f"last_live_intro_at_{username}", str(time.time()))
    except Exception as e:
        print(f"[{username}] Failed to persist last Live Intro text: {e}")


def clear_cached_live_intro(username: str):
    """Hapus cache teks Live Intro (memori + DB) untuk satu akun.

    Teks intro dari live SEBELUMNYA tidak boleh dipakai sebagai fallback pada
    live berikutnya. Tanpa ini, host lama (mis. ZIZE) terus terdeteksi walau
    live sekarang dibuka host baru (mis. YOSA), karena websocket sering gagal
    connect sehingga fallback cache dipakai.
    """
    username = (username or "").lower().lstrip("@")
    if not username:
        return
    _LAST_INTRO_TEXT.pop(username, None)
    _LAST_INTRO_ANALYZED.pop(username, None)
    try:
        # queue_setting langsung memperbarui cache baca, jadi get_setting()
        # berikutnya sudah melihat nilai kosong tanpa menunggu writer Turso.
        queue_setting(f"last_live_intro_{username}", "")
        queue_setting(f"last_live_intro_at_{username}", "0")
    except Exception as e:
        print(f"[{username}] Failed to clear cached Live Intro text: {e}")


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


_PRIORITY_PROBE_TTL_SECONDS = 45
_priority_probe_cache = {"at": 0.0, "targets": (), "results": []}
_priority_probe_lock = None
_ROOM_ID_PROBE_SEMAPHORE = asyncio.Semaphore(1)
_ROOM_ID_PROBE_COOLDOWN_SECONDS = 300
_ROOM_ID_PROBE_COOLDOWN_UNTIL = 0.0


async def probe_priority_targets_once(force: bool = False):
    """Probe active priority targets once to verify live accessibility.

    Uses a short TTL cache and skips network probing for hosts already known live
    in runtime status to avoid unnecessary requests. Returns a list of
    {"username": str, "live": bool|None} in priority-target order.
    """
    global _priority_probe_lock
    if _priority_probe_lock is None:
        _priority_probe_lock = asyncio.Lock()

    try:
        targets = get_observable_hosts() or []
    except Exception:
        targets = []
    usernames = []
    seen = set()
    for target in targets:
        if not isinstance(target, dict) or not target.get("priority"):
            continue
        username = (target.get("username") or "").lower().lstrip("@")
        if not username or username in seen:
            continue
        seen.add(username)
        usernames.append(username)
    target_tuple = tuple(usernames)

    async with _priority_probe_lock:
        now = time.time()
        cache_ttl = max(
            _PRIORITY_PROBE_TTL_SECONDS,
            _ROOM_ID_PROBE_COOLDOWN_UNTIL - now,
        )
        if (
            not force
            and target_tuple == _priority_probe_cache.get("targets")
            and (now - float(_priority_probe_cache.get("at") or 0.0)) < cache_ttl
        ):
            cached = _priority_probe_cache.get("results") or []
            return [dict(item) for item in cached]

        if not usernames:
            _priority_probe_cache["at"] = now
            _priority_probe_cache["targets"] = target_tuple
            _priority_probe_cache["results"] = []
            return []

        try:
            live_now = set(get_current_live_hosts() or [])
        except Exception:
            live_now = set()

        out = [None] * len(usernames)
        to_probe = []
        probe_indices = []
        for idx, username in enumerate(usernames):
            if username in live_now:
                out[idx] = {"username": username, "live": True}
            else:
                to_probe.append(username)
                probe_indices.append(idx)

        if to_probe:
            results = await asyncio.gather(*(probe_room_live(u) for u in to_probe), return_exceptions=True)
            for idx, username, result in zip(probe_indices, to_probe, results):
                if isinstance(result, Exception):
                    print(f"[{username}] Priority probe exception: {result}")
                    out[idx] = {"username": username, "live": None}
                else:
                    out[idx] = {"username": username, "live": result}

        final = [item for item in out if item is not None]
        _priority_probe_cache["at"] = time.time()
        _priority_probe_cache["targets"] = target_tuple
        _priority_probe_cache["results"] = [dict(item) for item in final]
        return final

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
    """Best-effort TikTok live probe. Returns True/False/None.

    TikTok's room-ID API may temporarily return HTML/captcha instead of JSON.
    Serialize these low-priority probes and pause them after that response so
    a batch of priority targets cannot amplify a platform-side block.
    """
    global _ROOM_ID_PROBE_COOLDOWN_UNTIL
    username = (username or "").lower().lstrip("@")
    if not username or FetchRoomIdAPIRoute is None:
        return None

    client = None
    async with _ROOM_ID_PROBE_SEMAPHORE:
        # Check inside the semaphore: concurrent callers queued behind the
        # first failed request must observe the cooldown before making a call.
        if time.time() < _ROOM_ID_PROBE_COOLDOWN_UNTIL:
            return None
        try:
            # Construct off the loop: httpx loads the certifi CA bundle synchronously.
            client = await asyncio.to_thread(TikTokLiveClient, unique_id=username)
            web = getattr(client, "web", None)
            if web is None:
                return None
            fetch = getattr(FetchRoomIdAPIRoute, "fetch_user_room_data", None)
            if fetch is None:
                return None
            data = await fetch(web, username)
            payload = data or {}
            if isinstance(payload, dict) and "data" in payload:
                root = payload.get("data")
                # An explicitly present but malformed data envelope is not proof
                # that the account is offline; keep the probe result unknown.
                if not isinstance(root, dict):
                    return None
            else:
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
            has_known_shape = (
                isinstance(root.get("user"), dict)
                or isinstance(root.get("liveRoom"), dict)
                or isinstance(root.get("live_room"), dict)
                or "status" in root
            )
            if has_known_shape:
                return False
        except Exception as e:
            error_text = f"{type(e).__name__}: {e}".lower()
            if (
                "jsondecodeerror" in error_text
                or "expecting value" in error_text
                or "non-json response" in error_text
                or "failedparseroomiderror" in error_text
            ):
                _ROOM_ID_PROBE_COOLDOWN_UNTIL = time.time() + _ROOM_ID_PROBE_COOLDOWN_SECONDS
                print(
                    f"[{username}] TikTok room probe received a non-JSON/blocked response; "
                    f"pausing low-priority room probes for {_ROOM_ID_PROBE_COOLDOWN_SECONDS}s."
                )
            else:
                print(f"[{username}] Live probe error: {e}")
            return None
        finally:
            # Probe clients are short-lived and do not enter the observer's normal
            # lifecycle cleanup. Close their HTTP session on every return path.
            web = getattr(client, "web", None) if client is not None else None
            closer = getattr(web, "close", None)
            if closer:
                try:
                    result = closer()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as close_error:
                    print(f"[{username}] Live probe HTTP cleanup error: {close_error}")
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

# A single process-wide pacing gate prevents multiple account observers from
# requesting signed WebSocket URLs at once. This is intentionally independent
# of per-account backoff: a healthy account must not inherit another account's
# cooldown, but all sign requests share the provider's global request budget.
_SIGNER_START_LOCK = asyncio.Lock()
_SIGNER_LAST_START_MONOTONIC = 0.0
_SIGNER_DEFAULT_START_INTERVAL_SECONDS = 15.0


def _signer_start_interval_seconds() -> float:
    """Return a conservative global spacing between signer/start requests."""
    raw = os.environ.get("TIKTOK_SIGN_START_INTERVAL_SECONDS", "")
    try:
        interval = float(raw) if raw.strip() else _SIGNER_DEFAULT_START_INTERVAL_SECONDS
    except (TypeError, ValueError):
        interval = _SIGNER_DEFAULT_START_INTERVAL_SECONDS
    return max(0.0, min(interval, 300.0))


async def _start_client_with_signer_pacing(client, **kwargs):
    """Serialize TikTokLive start calls and pace them across all accounts."""
    global _SIGNER_LAST_START_MONOTONIC
    async with _SIGNER_START_LOCK:
        interval = _signer_start_interval_seconds()
        now = time.monotonic()
        elapsed = now - _SIGNER_LAST_START_MONOTONIC
        if _SIGNER_LAST_START_MONOTONIC and elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        _SIGNER_LAST_START_MONOTONIC = time.monotonic()
        return await client.start(**kwargs)


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
# Identical observer errors repeat every retry cycle; report each distinct
# signature once per window instead of paging the error channel every 30s.
_LAST_OBSERVER_ERROR_LOG = {}
_OBSERVER_ERROR_LOG_INTERVAL_SECONDS = 900
# TikTokLive raises UserNotFoundError for accounts that cannot go live at all
# (never live / not found). They are not hosts yet: poll slower, never alert.
_UNAVAILABLE_RETRY_SECONDS = 300


def _should_reset_observer_cycle_state(connected_this_cycle: bool, json_empty_cycle: bool) -> bool:
    """Only a successful websocket cycle may clear failure/backoff state."""
    return bool(connected_this_cycle and not json_empty_cycle)


def _next_consecutive_failures(
    current: int, *, is_unavailable: bool = False, is_offline_error: bool = False
) -> int:
    """Only unexpected observer errors should increase the failure streak."""
    if is_unavailable or is_offline_error:
        return 0
    return current + 1


def _is_json_response_error(error) -> bool:
    """Recognize TikTok empty/non-JSON response signatures consistently."""
    error_text = str(error or "").lower()
    return any(
        marker in error_text
        for marker in (
            "jsondecodeerror",
            "expecting value",
            "failedparseroomiderror",
            "non-json response",
        )
    )


def _is_reconnect_requested(username: str) -> bool:
    with _RECONNECT_REQUESTED_LOCK:
        return username in _RECONNECT_REQUESTED


def _consume_reconnect_request(username: str) -> bool:
    """Take a pending reconnect request so it cannot trigger a second cycle."""
    with _RECONNECT_REQUESTED_LOCK:
        if username in _RECONNECT_REQUESTED:
            _RECONNECT_REQUESTED.discard(username)
            return True
    return False


def _clear_rate_limit_state(username: str) -> None:
    """Reset persisted rate-limit strikes/cooldown for an account."""
    try:
        strikes = str(get_setting(f"rl_strikes:{username}", "0") or "0").strip()
        until = str(get_setting(f"rl_until:{username}", "0") or "0").strip()
        if strikes in ("0", "") and until in ("0", ""):
            return
        if strikes not in ("0", ""):
            queue_setting(f"rl_strikes:{username}", "0")
        if until not in ("0", ""):
            queue_setting(f"rl_until:{username}", "0")
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
        queue_setting(key, str(strikes))
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
        queue_setting(f"rl_until:{username}", str(int(time.time()) + cooldown))
    except Exception:
        pass
    return cooldown


def _record_gift_event_db(session_id, username_clean, user, display_name, gift_name,
                          repeat, coins, msg_id, diamond, current_host):
    """Semua pekerjaan database untuk satu GiftEvent (dijalankan di worker thread).

    Turso/libSQL butuh round-trip jaringan per query. Menjalankannya langsung di
    event loop Discord memblokir ACK interaksi (3 detik) sehingga muncul
    "The application did not respond".
    """
    stage_started = time.perf_counter()
    result = record_gift_event_transaction(
        session_id=session_id,
        target_user=username_clean,
        viewer_user=user,
        viewer_display=display_name,
        gift_name=gift_name,
        gift_count=repeat,
        coins=coins,
        msg_id=msg_id,
        host_name=current_host,
    )
    _log_gift_timing(
        username_clean,
        "database.gift_event_write",
        time.perf_counter() - stage_started,
    )
    if result["status"] == "duplicate":
        return {"status": "duplicate"}
    if result["status"] != "ok":
        print(
            f"[GiftLog] Atomic gift write failed for @{user} "
            f"({gift_name}, {coins}c, msg_id={msg_id or 'n/a'})."
        )
        return {"status": "log_failed"}
    total_coins = result["total_coins"]

    wib = pytz.timezone('Asia/Jakarta')
    now_wib = datetime.now(wib)
    full_datetime = now_wib.strftime('%Y-%m-%d %H:%M:%S')
    today_str = now_wib.strftime('%Y-%m-%d')

    stage_started = time.perf_counter()
    summary = get_gift_event_summary(user, username_clean, today_str)
    _log_gift_timing(
        username_clean,
        "database.gift_summary",
        time.perf_counter() - stage_started,
    )
    channel_spent = summary["channel_spent"]
    today_spent = summary["today_spent"]
    total_all = summary["total_spent"]
    channel_name = summary["channel_name"]

    gift_msg = (
        f"[{full_datetime}] [{channel_name} (@{username_clean})] "
        f"Host: {current_host} | "
        f"User: @{user} - {gift_name} x{repeat} ({diamond} diamonds) = {coins} coins [{coins}c] - "
        f"[channel {channel_spent} c] - [today {today_spent} c] - [current total: {total_all} c]"
    )
    log_activity_event("gift", gift_msg, username_clean)
    return {
        "status": "ok",
        "total_coins": total_coins,
        "message": gift_msg,
    }


def _has_fresh_host_detection(current_host: str, detected_this_connection: bool) -> bool:
    """Whether a LiveIntro on this connection must outrank stored session state."""
    return bool(
        detected_this_connection
        and current_host
        and current_host not in ("Host", "-")
    )


async def start_single_observer(tiktok_username: str, announcement_callback, role_callback, comment_callback=None):
    username_clean = tiktok_username.lower().lstrip("@")
    # Notifications from this observer task should always name the host.
    try:
        log_setup.bind_log_context(host=f"@{username_clean}")
    except Exception:
        pass
    current_session_id = None
    # A LiveIntro can arrive before ConnectEvent finishes restoring the session.
    # This flag prevents stale persisted host data from overwriting that detection.
    host_detected_this_connection = False
    is_connected = False
    disconnect_timer_task = None
    # Menyimpan sesi & waktu disconnect terakhir selama grace period, supaya
    # observer yang dihentikan saat grace tetap menutup sesi di waktu disconnect.
    disconnect_meta = {"session_id": None, "at": None}
    # Only trust a host detected during the live that is running now; a host
    # from the previous live must not leak into this one (fresh_only=True).
    current_host = await asyncio.to_thread(
        get_current_host_for_account, username_clean, True
    ) or "Host"

    async def check_and_update_host_from_text(text: str, sender_username: str = None, source: str = "message", origin: str = "event"):
        """Detect host from Live Intro data only (event or strict intro fallback)."""
        nonlocal current_host, current_session_id, host_detected_this_connection
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

        detected, matched_alias = detect_host_match_from_welcome_message(text, block=False)
        if detected:
            host_detected_this_connection = True
            if detected != current_host:
                conclusion = f"host changed: {current_host} -> {detected} (alias match: {matched_alias!r})"
            else:
                conclusion = f"host unchanged: {detected} (alias match: {matched_alias!r})"
        else:
            try:
                alias_keys = sorted({alias for alias, _host in get_host_aliases(block=False)})
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
                await asyncio.to_thread(update_live_session_host, current_session_id, detected)
            await asyncio.to_thread(set_current_host_for_account, username_clean, detected)
            log_activity_event(
                "host_detected",
                f"Alias match in database: '{matched_alias}' -> {detected}. Host changed {old_host} -> {detected} on @{username_clean} from {origin}: \"{msg_preview}\"",
                username_clean,
            )

            # Update/edit pesan pengumuman yang sudah ada di Discord
            await _trigger_announcement(announcement_callback, username_clean, True, host_name=detected)
        elif detected:
            # Same host as before: refresh the detection time so a long live
            # keeps its host instead of falling back to "Host".
            try:
                await asyncio.to_thread(touch_current_host_detection, username_clean)
            except Exception:
                pass
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
    # Consecutive cycles where live could not be confirmed by websocket or probe.
    # Safety net so a stale "Live" badge cannot stay forever when the probe keeps
    # failing (rate limits) and no DisconnectEvent arrived.
    live_unconfirmed_cycles = 0
    attempts_total = 0
    attempts_since_success = 0

    def _fmt_wait(seconds: int) -> str:
        try:
            s = max(0, int(seconds or 0))
        except Exception:
            s = 0
        if s >= 3600:
            h = s // 3600
            m = (s % 3600) // 60
            return f"{h}h {m}m" if m else f"{h}h"
        if s >= 60:
            m = s // 60
            sec = s % 60
            return f"{m}m {sec}s" if sec and m < 10 else f"{m}m"
        return f"{s}s"

    def _limit_progress(status: str, failures: int, strikes: int, json_empty: bool):
        """Rough progress toward next backoff/limit tier (not the external quota)."""
        s = (status or "").lower()
        f = max(0, int(failures or 0))
        st = max(0, int(strikes or 0))
        # explicit rate-limit cooldown tiers (strikes after hit): 1->30m,2->60m,3->120m,4+->180m
        if s.startswith("rate_limited") or st > 0:
            tier = min(st if st > 0 else 1, 4)
            return {"used": tier, "next": 4, "label": f"limit tier {tier}/4"}
        # json-empty backoff: 120s, becomes 300s at >=5 failures
        if json_empty or s == "backoff_json_response":
            y = 5
            x = min(f, y)
            return {"used": x, "next": y, "label": f"{x}/{y} gagal (backoff lebih lama >=5)"}
        # generic reconnect backoff: 30s, 60s at >=5, 120s at >=10
        if f > 0:
            y = 10 if f >= 5 else 5
            x = min(f, y)
            return {"used": x, "next": y, "label": f"{x}/{y} gagal beruntun"}
        return {"used": 0, "next": 0, "label": ""}

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
            # Live has ended: drop the intro text so the next live cannot reuse
            # this livestream's host via the cached fallback.
            clear_cached_live_intro(username_clean)
            unresolved_host = (current_host in (None, "", "Host", "-"))
            if unresolved_host:
                print(f"[{username_clean}] Host debt: live ended with unresolved host (session {session_to_close}).")
                try:
                    await asyncio.to_thread(
                        log_activity_event,
                        "host_check",
                        f"Host debt unresolved: session {session_to_close} ended without detected host on @{username_clean}. Resolve manually in Live Monitor if needed.",
                        username_clean,
                    )
                except Exception:
                    pass
            if session_to_close:
                await asyncio.to_thread(end_live_session, session_to_close, end_time=disconnected_at)
            # Saat offline, bot Discord akan MENGHAPUS pesan pengumuman lewat sesi tersimpan.
            # JANGAN hapus sesi DB lebih dulu, karena message_id dibutuhkan untuk menghapus pesan Discord.
            if announcement_callback:
                await _trigger_announcement(announcement_callback, username_clean, False)
            else:
                # Fallback only when no callback exists; this cannot delete Discord messages.
                clear_active_live_status(username_clean)
                print(f"[{username_clean}] No announcement callback; cleared announcement DB status only.")
            if unresolved_host:
                print(f"[{username_clean}] Live confirmed offline after grace period (host debt unresolved).")
            else:
                print(f"[{username_clean}] Live confirmed offline after grace period.")
        except asyncio.CancelledError:
            # Reconnected within grace period
            pass

    def schedule_disconnect(session_id=None) -> bool:
        """Jadwalkan penutupan sesi setelah grace period.

        Setiap disconnect baru memperbarui waktu disconnect dan me-reset timer,
        sehingga sesi ditutup pada disconnect TERAKHIR (bukan yang pertama).
        """
        nonlocal disconnect_timer_task, current_session_id
        sid = session_id or current_session_id
        if not sid:
            return False
        if disconnect_timer_task and not disconnect_timer_task.done():
            disconnect_timer_task.cancel()
        disconnected_at = datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y-%m-%d %H:%M:%S')
        disconnect_meta["session_id"] = sid
        disconnect_meta["at"] = disconnected_at
        disconnect_timer_task = asyncio.create_task(
            delayed_disconnect_handler(sid, disconnected_at)
        )
        if current_session_id == sid:
            current_session_id = None
        stop_session_heartbeat()
        return True

    session_heartbeat_task = None

    async def _session_heartbeat_loop():
        while True:
            await asyncio.sleep(30)
            sid = current_session_id
            if sid:
                try:
                    await asyncio.to_thread(touch_live_session, sid)
                except Exception:
                    pass

    def start_session_heartbeat():
        nonlocal session_heartbeat_task
        if session_heartbeat_task is None or session_heartbeat_task.done():
            session_heartbeat_task = asyncio.create_task(_session_heartbeat_loop())

    def stop_session_heartbeat():
        nonlocal session_heartbeat_task
        if session_heartbeat_task and not session_heartbeat_task.done():
            session_heartbeat_task.cancel()
        session_heartbeat_task = None

    async def resolve_grace_reconnect() -> bool:
        """Batalkan grace timer dan catat jeda offline sebagai paused time.

        paused_seconds disimpan sebagai informasi, tetapi durasi dilaporkan
        dengan policy wall-clock (sama seperti TikTok Live Studio), sehingga
        jeda reconnect tidak mengurangi durasi akhir.
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
                    # DB write: keep it off the Discord event loop.
                    await asyncio.to_thread(add_live_session_paused_seconds, session_id, gap)
                    print(
                        f"[{username_clean}] Grace gap {gap}s recorded in paused_seconds "
                        f"(session {session_id})."
                    )
            except Exception as e:
                print(f"[{username_clean}] Failed to record grace paused time: {e}")
        return True

    while True:
        host_detected_this_connection = False
        probe_result = None  # per-cycle: never log a previous cycle's probe result
        try:
            connect_mode = (await asyncio.to_thread(get_host_connect_mode, username_clean) or "normal").lower()
        except Exception:
            connect_mode = "normal"
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
        try:
            rl_strikes_val = int(float(get_setting(f"rl_strikes:{username_clean}", "0") or 0))
        except Exception:
            rl_strikes_val = 0
        remaining_cooldown = rl_until - int(time.time())
        if remaining_cooldown > 0 and not _is_reconnect_requested(username_clean):
            prog = _limit_progress("rate_limited_cooldown", consecutive_failures, rl_strikes_val, False)
            runtime_status.update_observer_diag(
                username_clean,
                status="rate_limited_cooldown",
                wait_seconds=remaining_cooldown,
                rate_limited=True,
                retry_at=rl_until,
                rl_strikes=rl_strikes_val,
                attempts_total=attempts_total,
                attempts_since_success=attempts_since_success,
                limit_progress=prog,
            )
            log_activity_event(
                "system",
                f"[Debug @{username_clean}] Rate-limit cooldown active; waiting {max(1, remaining_cooldown // 60)}m before retry "
                f"(EulerStream daily quota per account). Manual Reconnect/Reset overrides.",
                username_clean,
            )
            for _ in range(remaining_cooldown):
                if _consume_reconnect_request(username_clean):
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
        attempts_total += 1
        attempts_since_success += 1
        # See probe_room_live: TLS/CA setup must not run on the event loop.
        # Client construction can fail before the websocket-cycle try/except below;
        # contain that failure here so one transient init error does not terminate
        # this account's observer task and defer recovery to the supervisor.
        try:
            client = await asyncio.to_thread(TikTokLiveClient, unique_id=username_clean)
        except asyncio.CancelledError:
            raise
        except Exception as init_error:
            consecutive_failures = min(consecutive_failures + 1, 10)
            init_wait_seconds = _observer_init_retry_delay(consecutive_failures)
            init_error_detail = (
                f"{type(init_error).__name__}: {init_error} "
                f"[phase=client_initialization]"
            )
            runtime_status.update_observer_diag(
                username_clean,
                status="client_init_error",
                consecutive_failures=consecutive_failures,
                attempts_total=attempts_total,
                attempts_since_success=attempts_since_success,
                wait_seconds=init_wait_seconds,
                last_error=init_error_detail[:300],
                rate_limited=False,
                retry_at=None,
                limit_progress=_limit_progress(
                    "client_init_error", consecutive_failures, rl_strikes_val, False
                ),
            )
            print(
                f"[{username_clean}] TikTok client initialization failed "
                f"(failures={consecutive_failures}); retrying in "
                f"{init_wait_seconds}s: {init_error_detail}"
            )
            try:
                log_activity_event(
                    "error",
                    f"[Debug @{username_clean}] TikTok client initialization failed "
                    f"(failures={consecutive_failures}); retrying in "
                    f"{init_wait_seconds}s: {init_error_detail}",
                    username_clean,
                )
            except Exception:
                pass
            for _ in range(init_wait_seconds):
                if _consume_reconnect_request(username_clean):
                    break
                await asyncio.sleep(1)
            continue
        connected_this_cycle = False
        reconnect_state = {"triggered": False}
        with _ACTIVE_CLIENTS_LOCK:
            _ACTIVE_CLIENTS[username_clean] = client
        runtime_status.update_observer_diag(
            username_clean,
            connect_mode=connect_mode,
            attempts_total=attempts_total,
            attempts_since_success=attempts_since_success,
            rl_strikes=rl_strikes_val,
        )

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
            nonlocal current_session_id, is_connected, disconnect_timer_task, current_host, connected_this_cycle, consecutive_failures, probed_live, host_detected_this_connection, attempts_since_success
            try:
                reconnected_in_grace = await resolve_grace_reconnect()
                if reconnected_in_grace:
                    print(f"[{username_clean}] Reconnected within grace period. Live status maintained.")

                is_connected = True
                connected_this_cycle = True
                consecutive_failures = 0
                probed_live = False

                # Adopt the DB's currently open session. A webhook offline event
                # (or admin reset) can close/delete the session while this task
                # still holds its id; using it would write gifts to a dead row.
                try:
                    open_id = await asyncio.to_thread(get_open_live_session_id, username_clean)
                    if open_id != current_session_id:
                        current_session_id = open_id
                except Exception:
                    pass

                # On manual refresh/reconnect request, reconfirm host instead of trusting stale stored value.
                force_reconfirm_host = False
                with _HOST_RECONFIRM_REQUESTED_LOCK:
                    if username_clean in _HOST_RECONFIRM_REQUESTED:
                        _HOST_RECONFIRM_REQUESTED.discard(username_clean)
                        force_reconfirm_host = True

                if force_reconfirm_host and not host_detected_this_connection:
                    print(f"[{username_clean}] Reconfirming host on reconnect (clearing stale host state).")
                    try:
                        await asyncio.to_thread(set_setting, f"current_host_{username_clean}", "")
                        await asyncio.to_thread(invalidate_settings_cache)
                    except Exception:
                        pass
                    # Use neutral placeholder until a real host is detected from LiveIntroEvent.
                    current_host = "Host"
                    clear_cached_live_intro(username_clean)
                elif _has_fresh_host_detection(current_host, host_detected_this_connection):
                    # LiveIntro may beat ConnectEvent during startup/reconnect.
                    # Its fresh result is authoritative; never restore an older host.
                    print(f"[{username_clean}] Preserving host detected by LiveIntro before ConnectEvent: {current_host}.")
                else:
                    # Refresh host/session context on (re)connect so host detection can continue/update.
                    stored_host = await asyncio.to_thread(
                        get_current_host_for_account, username_clean, True
                    )
                    if stored_host and stored_host != "Host":
                        current_host = stored_host
                    elif current_host and current_host != "Host":
                        # Host is stale (previous live) or the session is gone:
                        # wait for this live's LiveIntro instead of showing a wrong name.
                        current_host = "Host"

                if host_detected_this_connection and current_session_id:
                    # The LiveIntro can arrive before the resumed session ID is adopted.
                    # Synchronize that session now so its old scheduled_host cannot reappear.
                    await asyncio.to_thread(update_live_session_host, current_session_id, current_host)

                if not current_session_id:
                    # Placeholder means "not detected yet"; let start_live_session
                    # preserve an existing host instead of overwriting it.
                    initial_host = None if current_host in (None, "", "Host", "-") else current_host
                    current_session_id = await asyncio.to_thread(
                        start_live_session, username_clean, initial_host=initial_host
                    )
                    # New live session: a repeated LiveIntro text must be analysed
                    # again, otherwise host detection stays "Host" all live.
                    clear_cached_live_intro(username_clean)
                elif force_reconfirm_host and current_host not in (None, "", "Host", "-"):
                    try:
                        await asyncio.to_thread(update_live_session_host, current_session_id, current_host)
                    except Exception:
                        pass
                # A resumed/still-open session may already know this live's host
                # (e.g. after redeploy). Never show the "Host" placeholder when
                # that confirmed name is available.
                if current_host in (None, "", "Host", "-") and current_session_id:
                    try:
                        def _open_session_host(sid):
                            conn = get_db_connection()
                            try:
                                row = conn.execute(
                                    "SELECT scheduled_host FROM live_sessions WHERE session_id = ?",
                                    (sid,),
                                ).fetchone()
                                return str((row or [None])[0] or "")
                            finally:
                                conn.close()
                        stored_session_host = await asyncio.to_thread(_open_session_host, current_session_id)
                        if stored_session_host and stored_session_host not in ("Host", "-"):
                            current_host = stored_session_host
                    except Exception:
                        pass
                await asyncio.to_thread(touch_live_session, current_session_id)
                start_session_heartbeat()

                attempts_since_success = 0
                # A successful websocket connection clears stale backoff/limit
                # diagnostics. Without this, the monitor kept showing an old
                # "10/10 gagal beruntun" estimate even after LiveIntro worked.
                _clear_rate_limit_state(username_clean)
                runtime_status.set_live(username_clean, current_host, source="event")
                runtime_status.update_observer_diag(
                    username_clean,
                    status="reconnected" if reconnected_in_grace else "connected",
                    consecutive_failures=0,
                    last_error=None,
                    attempts_total=attempts_total,
                    attempts_since_success=attempts_since_success,
                    wait_seconds=0,
                    retry_at=None,
                    rate_limited=False,
                    limit_progress={"used": 0, "next": 0, "label": ""},
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
                # A connect handler error must still leave a DB session behind,
                # otherwise the live badge has no backing row and no grace timer
                # can ever clear it on disconnect (stale "Live" forever).
                if not current_session_id:
                    try:
                        current_session_id = await asyncio.to_thread(
                            start_live_session, username_clean, initial_host=None
                        )
                    except Exception:
                        pass
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

                # Jalankan grace period agar tidak spam hapus/buat pengumuman jika hanya reconnect singkat.
                # Disconnect berulang me-reset timer ke disconnect terakhir.
                schedule_disconnect(current_session_id)

        @client.on(CommentEvent)
        async def on_comment(event: CommentEvent):
            try:
                user = _extract_comment_username(event)
                comment_text = getattr(event, "comment", "") or ""
                runtime_status.touch_live(username_clean)
                if current_session_id:
                    await asyncio.to_thread(touch_live_session, current_session_id)

                # Keep the live heartbeat even when TikTok omits a usable user ID.
                if not user:
                    return

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
            gift_started = time.perf_counter()
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
                    database_started = time.perf_counter()
                    if not current_session_id:
                        session_started = time.perf_counter()
                        current_session_id = await asyncio.to_thread(
                            start_live_session, username_clean, initial_host=current_host
                        )
                        _log_gift_timing(
                            username_clean, "database.session_start",
                            time.perf_counter() - session_started
                        )
                    touch_started = time.perf_counter()
                    await asyncio.to_thread(touch_live_session, current_session_id)
                    _log_gift_timing(
                        username_clean, "database.session_touch",
                        time.perf_counter() - touch_started
                    )
                    start_session_heartbeat()

                    result, worker_elapsed = await asyncio.to_thread(
                        call_with_timing,
                        _record_gift_event_db,
                        current_session_id, username_clean, user, display_name,
                        gift_name, repeat, coins, msg_id, diamond, current_host,
                    )
                    total_elapsed = time.perf_counter() - database_started
                    _log_gift_timing(
                        username_clean, "database.gift_event_worker", worker_elapsed
                    )
                    _log_gift_timing(
                        username_clean, "database.gift_event_dispatch",
                        max(0.0, total_elapsed - worker_elapsed)
                    )
                    _log_gift_timing(username_clean, "database", total_elapsed)
                    if result["status"] == "duplicate":
                        print(
                            f"[{username_clean}] Duplicate gift message ignored "
                            f"(msg_id={msg_id or 'n/a'}, @{user}, {gift_name}, {coins}c)."
                        )
                        return
                    if result["status"] == "log_failed":
                        print(
                            f"[{username_clean}] WARNING: gift log write failed for @{user} "
                            f"({gift_name}, {coins}c); coin totals were NOT updated to avoid drift."
                        )
                        return
                    if result["status"] == "aggregate_failed":
                        print(
                            f"[{username_clean}] WARNING: gift log stored but coin totals update failed "
                            f"for @{user} ({gift_name}, {coins}c); check the gift log vs stored total."
                        )
                        return

                    total_coins = result["total_coins"]
                    print(f"[Activity] {result['message']}")

                    if role_callback:
                        role_started = time.perf_counter()
                        if asyncio.iscoroutinefunction(role_callback):
                            await role_callback(user, total_coins)
                        else:
                            role_callback(user, total_coins)
                        _log_gift_timing(
                            username_clean, "role_callback", time.perf_counter() - role_started
                        )
            except Exception as e:
                print(f"[{username_clean}] Error handling GiftEvent: {e}")
            finally:
                _log_gift_timing(
                    username_clean, "total", time.perf_counter() - gift_started
                )

        cycle_error = None
        client_task = None
        try:
            # TikTokLive v7's start() is non-blocking: it returns the websocket
            # task immediately. Await the task so the observer does not create a
            # new client every cycle while old clients keep running.
            try:
                client_task = await _start_client_with_signer_pacing(client, fetch_room_info=True)
            except Exception as room_info_error:
                err_text = str(room_info_error).lower()
                if "age" in err_text and "restrict" in err_text:
                    print(f"[{username_clean}] Room info unavailable (age-restricted); connecting without it.")
                    client_task = await _start_client_with_signer_pacing(client, fetch_room_info=False)
                else:
                    raise
            await client_task
        except asyncio.CancelledError:
            # Observer stopped by supervisor (host paused/removed/restarted).
            stop_session_heartbeat()
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
                        await asyncio.to_thread(end_live_session, grace_session_id, end_time=grace_at)
                    except Exception:
                        pass
            if is_connected and current_session_id:
                try:
                    await asyncio.to_thread(end_live_session, current_session_id)
                except Exception:
                    pass
            # The sessions above are already closed. Clear connection state so
            # the finally block does not schedule another 10-minute grace timer
            # (which would fire a duplicate offline announcement after stop).
            is_connected = False
            current_session_id = None
            disconnect_meta["session_id"] = None
            disconnect_meta["at"] = None
            disconnect_timer_task = None
            runtime_status.clear_live(username_clean)
            runtime_status.update_observer_diag(username_clean, status="stopped")
            raise
        except Exception as e:
            cycle_error = e
            err = str(e)
            err_detail = f"{type(e).__name__}: {err} [exception_chain={_exception_chain_types(e)}]"
            err_lower = err_detail.lower()
            is_rate_limited = ("rate_limit" in err_lower) or ("too many connections" in err_lower) or ("eulerstream" in err_lower)
            is_json_empty_error = _is_json_response_error(err_detail)
            is_unavailable = (
                "usernotfounderror" in err_lower
                or "not capable of going live" in err_lower
                or "never gone live" in err_lower
                or "does not exist" in err_lower
            )
            is_offline_error = (not is_rate_limited) and (
                not is_unavailable
                and ("offline" in err_lower or "useroffline" in err_lower or "not found" in err_lower)
            )
            consecutive_failures = _next_consecutive_failures(
                consecutive_failures,
                is_unavailable=is_unavailable,
                is_offline_error=is_offline_error,
            )
            runtime_status.update_observer_diag(
                username_clean,
                status=(
                    "rate_limited" if is_rate_limited
                    else ("unavailable" if is_unavailable
                          else ("offline" if is_offline_error else "error"))
                ),
                consecutive_failures=consecutive_failures,
                last_error=err_detail[:300],
                rate_limited=is_rate_limited,
            )
            # Offline/not-live-capable is normal polling, not a failure streak.
            if is_unavailable or is_offline_error:
                runtime_status.update_observer_diag(
                    username_clean,
                    limit_progress={"used": 0, "next": 0, "label": ""},
                    wait_seconds=0,
                    retry_at=None,
                )
            if is_rate_limited:
                print(f"[{username_clean}] Websocket rate-limited (failures={consecutive_failures}): {err_detail}")
                log_activity_event(
                    "error",
                    f"[Debug @{username_clean}] Websocket RATE LIMITED (failures={consecutive_failures}, connected_this_cycle={connected_this_cycle}, host={current_host}) - backing off: {err_detail}",
                    username_clean,
                )
            elif is_unavailable:
                # Not an error: TikTok says this account cannot go live (never
                # live) or does not exist. Poll rarely and keep the channel clean.
                last_log = _LAST_OFFLINE_LOG.get(username_clean, 0)
                if time.time() - last_log >= _OFFLINE_LOG_INTERVAL_SECONDS:
                    _LAST_OFFLINE_LOG[username_clean] = time.time()
                    print(
                        f"[{username_clean}] Account is not live-capable "
                        f"(never live / not found): {err_detail}"
                    )
                    log_activity_event(
                        "system",
                        f"[Debug @{username_clean}] Account is not live-capable "
                        f"(never live / not found). Polling every "
                        f"{_UNAVAILABLE_RETRY_SECONDS // 60}m: {err_detail}",
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
                now = time.time()
                signature = (username_clean, err_detail[:160])
                if now - _LAST_OBSERVER_ERROR_LOG.get(signature, 0) >= _OBSERVER_ERROR_LOG_INTERVAL_SECONDS:
                    if len(_LAST_OBSERVER_ERROR_LOG) > 500:
                        _LAST_OBSERVER_ERROR_LOG.clear()
                    _LAST_OBSERVER_ERROR_LOG[signature] = now
                    log_activity_event(
                        "error",
                        f"[Debug @{username_clean}] Websocket observer error (failures={consecutive_failures}, connected_this_cycle={connected_this_cycle}, host={current_host}, probe={probe_result if 'probe_result' in locals() else 'n/a'}): {err_detail}",
                        username_clean,
                    )
                else:
                    log_activity_event(
                        "system",
                        f"[Debug @{username_clean}] Repeated observer error (reported once per "
                        f"{_OBSERVER_ERROR_LOG_INTERVAL_SECONDS // 60}m, failures={consecutive_failures}): {err_detail}",
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

                schedule_disconnect(current_session_id)

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
        # However, when the websocket handshake already received an empty/non-JSON
        # response, a second room API request in the same cycle only repeats the
        # blocked request and can worsen TikTok's rate limiting. Preserve an
        # unknown probe result and let the existing backoff handle the next retry.
        grace_pending = bool(disconnect_timer_task and not disconnect_timer_task.done())
        probe_result = None
        json_empty_cycle = _is_json_response_error(cycle_error)
        if not connected_this_cycle and not json_empty_cycle:
            probe_result = await probe_room_live(username_clean)
            runtime_status.update_observer_diag(
                username_clean,
                probe_live=probe_result,
                last_probe_at=datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y-%m-%d %H:%M:%S'),
            )
            if probe_result is True:
                if grace_pending:
                    if await resolve_grace_reconnect():
                        print(f"[{username_clean}] Probe confirms live during grace period; keeping live status.")
                if not probed_live and username_clean not in get_current_live_hosts():
                    probed_live = True
                    runtime_status.set_live(username_clean, current_host, source="probe")
                    if not current_session_id:
                        current_session_id = await asyncio.to_thread(
                            start_live_session, username_clean, initial_host=current_host
                        )
                    await asyncio.to_thread(touch_live_session, current_session_id)
                    start_session_heartbeat()
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
                # Probe confirmed offline. The live marker can come from the
                # websocket/LiveIntro (probed_live stays False), so never gate
                # this on the probe flag: that left a stale "Live" badge forever
                # when DisconnectEvent was missed (e.g. UserOfflineError).
                if current_session_id:
                    # Keep session/announcement grace, but make the badge honest
                    # immediately; a reconnect within grace restores it.
                    schedule_disconnect(current_session_id)
                    log_activity_event(
                        "disconnect",
                        f"Disconnected from @{username_clean}'s live (probe confirmed offline; "
                        f"grace {LIVE_DISCONNECT_GRACE_SECONDS // 60}m started)",
                        username_clean,
                    )
                runtime_status.clear_live(username_clean)
                probed_live = False
                live_unconfirmed_cycles = 0
                if cycle_error is None:
                    runtime_status.update_observer_diag(
                        username_clean, status="offline", consecutive_failures=0, last_error=None,
                        wait_seconds=0, retry_at=None, rate_limited=False,
                        limit_progress={"used": 0, "next": 0, "label": ""},
                    )
            elif probe_result is None and cycle_error is None and not grace_pending:
                runtime_status.update_observer_diag(username_clean, status="checking")
        elif _should_reset_observer_cycle_state(connected_this_cycle, json_empty_cycle):
            # Preserve consecutive failures and the persisted JSON-response
            # cooldown when TikTok returns an empty/non-JSON websocket response.
            consecutive_failures = 0
            probe_result = False
            _clear_rate_limit_state(username_clean)
            _LAST_OFFLINE_LOG.pop(username_clean, None)

        # Safety net: when neither the websocket nor the probe confirms live and
        # no grace timer is pending, a lingering live marker is stale (the probe
        # may be failing/rate-limited). Drop it after a few cycles so the
        # dashboard stops showing "Live" for an offline account.
        if connected_this_cycle or probe_result is True or grace_pending:
            live_unconfirmed_cycles = 0
        elif probe_result is None:
            live_unconfirmed_cycles += 1
            if live_unconfirmed_cycles >= 3:
                live_unconfirmed_cycles = 0
                if username_clean in runtime_status.get_live_usernames():
                    if current_session_id:
                        schedule_disconnect(current_session_id)
                    runtime_status.clear_live(username_clean)
                    probed_live = False
                    log_activity_event(
                        "system",
                        f"[Debug @{username_clean}] Live not confirmed for 3 cycles "
                        f"(websocket/probe unavailable); cleared stale live marker.",
                        username_clean,
                    )

        # Adaptive retry: back off slightly on repeated failures, but keep checking.
        err_lower_cycle = str(cycle_error or "").lower()
        rate_limited_cycle = ("rate_limit" in err_lower_cycle) or ("too many connections" in err_lower_cycle) or ("eulerstream" in err_lower_cycle)
        json_empty_cycle = _is_json_response_error(cycle_error)
        unavailable_cycle = (
            "usernotfounderror" in err_lower_cycle
            or "not capable of going live" in err_lower_cycle
            or "never gone live" in err_lower_cycle
            or "does not exist" in err_lower_cycle
        )
        if reconnect_state.get("triggered"):
            wait_seconds = 2
            runtime_status.update_observer_diag(username_clean, status="reconnecting")
        elif rate_limited_cycle:
            # Back off hard on sign-server/account rate limits to avoid prolonging lockout.
            wait_seconds = _rate_limit_cooldown_seconds(username_clean, str(cycle_error or ""))
            retry_at = int(time.time()) + wait_seconds
            try:
                rl_strikes_now = int(float(get_setting(f"rl_strikes:{username_clean}", "0") or 0))
            except Exception:
                rl_strikes_now = 1
            prog = _limit_progress("rate_limited_backoff", consecutive_failures, rl_strikes_now, False)
            runtime_status.update_observer_diag(
                username_clean,
                status="rate_limited_backoff",
                wait_seconds=wait_seconds,
                rate_limited=True,
                retry_at=retry_at,
                rl_strikes=rl_strikes_now,
                attempts_total=attempts_total,
                attempts_since_success=attempts_since_success,
                consecutive_failures=consecutive_failures,
                limit_progress=prog,
            )
            log_activity_event(
                "system",
                f"[Debug @{username_clean}] Rate-limited websocket; backing off {_fmt_wait(wait_seconds)} before retry "
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
            retry_at = int(time.time()) + wait_seconds
            try:
                queue_setting(f"rl_until:{username_clean}", str(retry_at))
            except Exception:
                pass
            try:
                rl_strikes_now = int(float(get_setting(f"rl_strikes:{username_clean}", "0") or 0))
            except Exception:
                rl_strikes_now = 0
            prog = _limit_progress("backoff_json_response", consecutive_failures, rl_strikes_now, True)
            runtime_status.update_observer_diag(
                username_clean,
                status="backoff_json_response",
                wait_seconds=wait_seconds,
                rate_limited=True,
                retry_at=retry_at,
                rl_strikes=rl_strikes_now,
                attempts_total=attempts_total,
                attempts_since_success=attempts_since_success,
                consecutive_failures=consecutive_failures,
                limit_progress=prog,
            )
            log_activity_event(
                "system",
                f"[Debug @{username_clean}] Websocket returned empty/invalid JSON response; backing off {_fmt_wait(wait_seconds)} before retry "
                f"(failures={consecutive_failures}, probe_online={probe_result is True}, mode={connect_mode}, "
                f"progress={prog.get('label','')}).",
                username_clean,
            )
        elif unavailable_cycle:
            # The account cannot go live (never live / not found). Keep a slow
            # watch in case it becomes live-capable later, without burning quota.
            wait_seconds = _UNAVAILABLE_RETRY_SECONDS
            runtime_status.update_observer_diag(
                username_clean, status="unavailable", wait_seconds=wait_seconds,
                attempts_total=attempts_total, attempts_since_success=attempts_since_success,
                consecutive_failures=consecutive_failures,
                limit_progress={"used": 0, "next": 0, "label": ""},
            )
        elif probe_result is True and not connected_this_cycle:
            # Account is online (probe) but websocket event client not connected: retry to receive LiveIntroEvent.
            wait_seconds = 30 if connect_mode == "slow" else 10
            prog = _limit_progress("online_waiting_websocket", consecutive_failures, 0, False)
            runtime_status.update_observer_diag(
                username_clean, status="online_waiting_websocket", wait_seconds=wait_seconds,
                attempts_total=attempts_total, attempts_since_success=attempts_since_success,
                consecutive_failures=consecutive_failures, limit_progress=prog,
                rate_limited=False, retry_at=None,
            )
        else:
            wait_seconds = slow_seconds if connect_mode == "slow" else 30
            if not connected_this_cycle and consecutive_failures >= 5:
                wait_seconds = max(wait_seconds, 60)
            if not connected_this_cycle and consecutive_failures >= 10:
                wait_seconds = max(wait_seconds, 120)
            # "User is offline" is a normal polling state, not a failure streak:
            # report it as offline with no limit estimate.
            offline_cycle = (
                "offline" in err_lower_cycle or "useroffline" in err_lower_cycle
                or "not found" in err_lower_cycle
            )
            if offline_cycle:
                prog = {"used": 0, "next": 0, "label": ""}
                diag_fields = {
                    "status": "offline",
                    "wait_seconds": wait_seconds,
                    "attempts_total": attempts_total,
                    "attempts_since_success": attempts_since_success,
                    "consecutive_failures": consecutive_failures,
                    "limit_progress": prog,
                    "rate_limited": False,
                    "retry_at": None,
                }
            else:
                prog = _limit_progress("retry", consecutive_failures, 0, False) if consecutive_failures > 0 else {"used": 0, "next": 0, "label": ""}
                diag_fields = {
                    "wait_seconds": wait_seconds,
                    "attempts_total": attempts_total,
                    "attempts_since_success": attempts_since_success,
                    "consecutive_failures": consecutive_failures,
                    "limit_progress": prog,
                }
            runtime_status.update_observer_diag(username_clean, **diag_fields)
        # If reconnect was requested while waiting, skip delay (consume the
        # flag so the fresh client is not stopped again by the watcher).
        for _ in range(wait_seconds):
            if _consume_reconnect_request(username_clean):
                break
            await asyncio.sleep(1)


def request_tiktok_reconnect(username: str = None, reconfirm_host: bool = True):
    """Request observer reconnect for one host or all active hosts.

    ``reconfirm_host=False`` is used by primary live webhooks: the account is
    already confirmed live, so forcing host re-detection would wipe the host
    that LiveIntro just recorded.
    """
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
            clear_cached_live_intro(target)
    if reconfirm_host:
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

def _report_observer_task_exit(username: str, task: asyncio.Task) -> None:
    """Retrieve and report unexpected observer task failures.

    The supervisor restarts completed tasks on its next refresh; consume the
    exception here so the reason is visible instead of silently disappearing.
    Normal cancellation and clean exits are intentionally quiet.
    """
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is None:
        return

    detail = f"{type(error).__name__}: {error}"[:300]
    message = f"[Observer System] Observer task for @{username} exited unexpectedly: {detail}"
    print(message)
    try:
        log_activity_event("error", message, username)
    except Exception:
        pass


def _transport_runtime_fingerprint() -> str:
    """Return non-secret runtime/dependency versions for SSL transport diagnostics."""
    packages = (
        "TikTokLive",
        "websockets",
        "websockets-proxy",
        "httpx",
        "httpcore",
        "python-socks",
    )
    versions = []
    for package in packages:
        try:
            version = metadata.version(package)
        except metadata.PackageNotFoundError:
            version = "missing"
        except Exception:
            version = "unknown"
        versions.append(f"{package}={version}")

    return (
        f"python={sys.version.split()[0]} "
        f"openssl={ssl.OPENSSL_VERSION} "
        + " ".join(versions)
    )


async def run_all_observers(announcement_callback, role_callback, comment_callback=None):
    """Supervise one observer task per active host, refreshing from the DB.

    Hosts added, paused or removed in the admin panel are picked up within a
    minute without restarting the bot.
    """
    tasks = {}
    print("[Observer System] Starting supervised observers for target hosts in DB.")
    print(f"[Observer System] Transport runtime: {_transport_runtime_fingerprint()}")
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
        aliases = await asyncio.to_thread(get_host_aliases)
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
            # DB reads must stay off the event loop: this loop runs every 60s and
            # a Turso round-trip here freezes interaction ACKs.
            observable = await asyncio.to_thread(get_observable_hosts) or []
        except Exception:
            try:
                active = await asyncio.to_thread(get_active_hosts) or []
            except Exception:
                active = []
            observable = [{"username": u, "mode": "normal"} for u in active]
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
                tasks[username].add_done_callback(
                    lambda completed, user=username: _report_observer_task_exit(user, completed)
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

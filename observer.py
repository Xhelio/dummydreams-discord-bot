import asyncio
import time
from datetime import datetime
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
    get_channel_display_name,
    get_host_aliases,
    is_feature_enabled,
    get_setting,
    set_setting
)
import runtime_status

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

async def start_single_observer(tiktok_username: str, announcement_callback, role_callback, comment_callback=None):
    username_clean = tiktok_username.lower().lstrip("@")
    current_session_id = None
    is_connected = False
    disconnect_timer_task = None
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
        if not msg_preview:
            print(f"[{username_clean}] [HostDetect:{source}] received via {origin} but the intro text is empty.")
            log_activity_event(
                "host_check",
                f"Live Intro received on @{username_clean} via {origin} but no text was extracted.",
                username_clean,
            )
            return

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
            conclusion = f"no alias match in database host_aliases (current: {current_host})"

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

    async def _get_fresh_intro_room_info(client):
        """Best-effort: return room_info dict that may contain intro fields."""
        try:
            room_info = getattr(client, "room_info", None)
            if isinstance(room_info, dict):
                return room_info
            web = getattr(client, "web", None)
            fetcher = getattr(web, "fetch_room_info", None)
            if web and fetcher:
                result = fetcher()
                if asyncio.iscoroutine(result):
                    result = await result
                if isinstance(result, dict):
                    return result
        except Exception as e:
            print(f"[{username_clean}] [HostDetect:live_intro_fallback] room_info refresh failed: {e}")
        return None

    async def try_live_intro_fallback(client, reason: str, log_missing: bool = True):
        """Strict Live Intro fallback: intro-named room-info fields, then last intro text.

        We never use room title, comments or other arbitrary text. The cached
        text is itself a previously received LiveIntroEvent, so a reconnect
        mid-live can still restore the host until a fresh event arrives.
        """
        if not is_connected:
            # Only act on accounts detected as active (connected websocket).
            if log_missing:
                print(f"[{username_clean}] [HostDetect:live_intro_fallback] skipped ({reason}): account not connected.")
            return False

        room_info = await _get_fresh_intro_room_info(client)
        texts = _collect_intro_texts_from_room_info(room_info) if room_info else []
        if log_missing:
            keys = sorted(room_info.keys())[:40] if isinstance(room_info, dict) else []
            log_activity_event(
                "host_check",
                f"Trying strict Live Intro fallback for @{username_clean} ({reason}); intro_fields={len(texts)}; room_info_keys={keys or 'unavailable'}.",
                username_clean,
            )
            print(
                f"[{username_clean}] [HostDetect:live_intro_fallback] trying ({reason}); "
                f"intro_fields={len(texts)} room_info_keys={keys}"
            )
        if texts:
            for path, text in texts[:3]:
                print(
                    f"[{username_clean}] [HostDetect:live_intro_fallback] room_info field={path} "
                    f"text={_safe_preview(text)!r}"
                )
                log_activity_event(
                    "host_check",
                    f"Live Intro fallback (room_info:{path}, {reason}) on @{username_clean}: {_safe_preview(text)}",
                    username_clean,
                )
                await check_and_update_host_from_text(
                    text,
                    sender_username=username_clean,
                    source="live_intro_fallback",
                    origin=f"room_info:{path} ({reason})",
                )
            return True

        cached = _get_cached_live_intro_text(username_clean)
        if cached:
            print(
                f"[{username_clean}] [HostDetect:live_intro_fallback] using cached Live Intro "
                f"({reason}): {_safe_preview(cached)!r}"
            )
            log_activity_event(
                "host_check",
                f"Live Intro fallback using cached Live Intro ({reason}) on @{username_clean}: {_safe_preview(cached)}",
                username_clean,
            )
            await check_and_update_host_from_text(
                cached,
                sender_username=username_clean,
                source="live_intro_fallback",
                origin=f"cached_intro ({reason})",
            )
            return True

        if log_missing:
            keys = sorted(room_info.keys())[:40] if isinstance(room_info, dict) else []
            log_activity_event(
                "host_check",
                f"Strict Live Intro fallback on @{username_clean} ({reason}) found no intro text "
                f"(room_info keys: {keys or 'unavailable'}).",
                username_clean,
            )
        return False

    consecutive_failures = 0
    probed_live = False

    async def delayed_disconnect_handler(session_to_close):
        """Menunggu grace period 30 detik sebelum benar-benar menyatakan live berakhir di Discord."""
        try:
            await asyncio.sleep(30)
            nonlocal is_connected, current_session_id, probed_live
            runtime_status.clear_live(username_clean)
            probed_live = False
            if session_to_close:
                end_live_session(session_to_close)
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

    while True:
        client = TikTokLiveClient(unique_id=username_clean)
        connected_this_cycle = False
        reconnect_state = {"triggered": False}
        with _ACTIVE_CLIENTS_LOCK:
            _ACTIVE_CLIENTS[username_clean] = client

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
            while True:
                await asyncio.sleep(20)
                if active_client is not client or not is_connected:
                    return
                if current_host and current_host != "Host":
                    return
                log_activity_event(
                    "host_check",
                    f"Waiting for LiveIntroEvent (WebcastLiveIntroMessage) on @{username_clean} (connected); retrying strict intro check...",
                    username_clean,
                )
                print(f"[{username_clean}] Still connected; waiting for LiveIntroEvent and retrying strict intro check...")
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
                reconnected_in_grace = bool(disconnect_timer_task and not disconnect_timer_task.done())
                # Jika sebelumnya ada pending disconnect timer, batalkan karena sudah terhubung kembali
                if reconnected_in_grace:
                    disconnect_timer_task.cancel()
                    disconnect_timer_task = None
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
                log_activity_event(
                    "system",
                    f"Connected to @{username_clean}; listening for LiveIntroEvent (WebcastLiveIntroMessage) for host detection.",
                    username_clean,
                )
                print(f"[{username_clean}] Listening for LiveIntroEvent (WebcastLiveIntroMessage) for host detection...")

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
                    disconnect_timer_task = asyncio.create_task(delayed_disconnect_handler(current_session_id))
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
                    print(
                        f"[{username_clean}] [HostDetect:live_intro] WebcastLiveIntroMessage received (online/connected); "
                        f"field={field_name or 'none'} candidates={candidate_names} attrs={attr_names} text={msg_preview_raw!r}"
                    )
                    log_activity_event(
                        "host_check",
                        f"LiveIntroEvent (WebcastLiveIntroMessage) received on @{username_clean} (online/connected) | field={field_name or 'unknown'} | text={msg_preview_raw or '(empty)'}",
                        username_clean,
                    )
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
                if event.gift.streakable and event.gift.streaking:
                    return

                user = event.user.unique_id
                display_name = event.user.nickname or user
                
                repeat = getattr(event, 'repeat_count', 1) or 1
                diamond = getattr(event.gift, 'diamond_count', 0)
                if not diamond and hasattr(event.gift, 'info'):
                    diamond = getattr(event.gift.info, 'diamond_count', 0)
                coins = int(diamond or 0) * int(repeat)
                gift_name = getattr(event.gift, 'name', 'Gift')

                if coins > 0:
                    nonlocal current_session_id
                    if not current_session_id:
                        current_session_id = start_live_session(username_clean, initial_host=current_host)

                    total_coins = save_gift_data(user, display_name, coins)
                    save_daily_gift_data(user, display_name, coins)
                    log_gift_transaction(
                        session_id=current_session_id,
                        target_user=username_clean,
                        viewer_user=user,
                        viewer_display=display_name,
                        gift_name=gift_name,
                        gift_count=repeat,
                        coins=coins
                    )

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
                        f"User: @{user} - {gift_name} - {coins} coins [{coins}c] - "
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
            consecutive_failures += 1
            runtime_status.update_observer_diag(
                username_clean,
                status="error",
                consecutive_failures=consecutive_failures,
                last_error=err[:300],
            )
            if "offline" in err.lower() or "not found" in err.lower():
                print(f"[{username_clean}] Not live/offline check: {e}")
            else:
                print(f"[{username_clean}] Observer error while checking live (failures={consecutive_failures}): {e}")
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
                    disconnect_timer_task = asyncio.create_task(delayed_disconnect_handler(current_session_id))
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
                    result = disconnector(close_client=True)
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as e:
                print(f"[{username_clean}] Client cleanup error: {e}")

        # Secondary probe: if the event client didn't connect but TikTok reports live,
        # mark status/announcement so the dashboard is not stuck on "No Live Now".
        grace_pending = bool(disconnect_timer_task and not disconnect_timer_task.done())
        if not connected_this_cycle:
            probe = await probe_room_live(username_clean)
            runtime_status.update_observer_diag(
                username_clean,
                probe_live=probe,
                last_probe_at=datetime.now(pytz.timezone('Asia/Jakarta')).strftime('%Y-%m-%d %H:%M:%S'),
            )
            if probe is True:
                if grace_pending and disconnect_timer_task and not disconnect_timer_task.done():
                    disconnect_timer_task.cancel()
                    disconnect_timer_task = None
                    print(f"[{username_clean}] Probe confirms live during grace period; keeping live status.")
                if not probed_live and username_clean not in get_current_live_hosts():
                    probed_live = True
                    if current_host == "Host":
                        await try_live_intro_fallback(client, "probe", log_missing=False)
                    runtime_status.set_live(username_clean, current_host, source="probe")
                    if not current_session_id:
                        current_session_id = start_live_session(username_clean, initial_host=current_host)
                    await _trigger_announcement(announcement_callback, username_clean, True, host_name=current_host)
                    log_activity_event("connect", f"Connected to @{username_clean}'s live by probe fallback (Host: {current_host})", username_clean)
                    print(f"[{username_clean}] Live detected by probe fallback (event client not connected).")
                else:
                    runtime_status.set_live(username_clean, current_host, source=runtime_status.get_live_snapshot().get(username_clean, {}).get("source", "probe"))
                    runtime_status.touch_live(username_clean)
                consecutive_failures = 0
            elif probe is False and not grace_pending:
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
            elif probe is None and cycle_error is None and not grace_pending:
                runtime_status.update_observer_diag(username_clean, status="checking")
        else:
            consecutive_failures = 0

        # Adaptive retry: back off slightly on repeated failures, but keep checking.
        if reconnect_state.get("triggered"):
            wait_seconds = 2
            runtime_status.update_observer_diag(username_clean, status="reconnecting")
        else:
            wait_seconds = 30
            if not connected_this_cycle and consecutive_failures >= 5:
                wait_seconds = 60
            if not connected_this_cycle and consecutive_failures >= 10:
                wait_seconds = 120
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
        target_users = [u.lower().lstrip("@") for u in get_active_hosts() if u]
        target_set = set(target_users)

        # Stop observers for hosts that were paused or deleted.
        for username in list(tasks.keys()):
            task = tasks[username]
            if username not in target_set:
                if task and not task.done():
                    task.cancel()
                tasks.pop(username, None)
                runtime_status.clear_live(username)
                print(f"[Observer System] Stopped observer for @{username} (no longer active).")
                try:
                    log_activity_event("system", f"Stopped observer for @{username} (no longer active).", username)
                except Exception:
                    pass

        # Start observers for newly added or previously failed hosts.
        for username in target_users:
            task = tasks.get(username)
            if task is None or task.done():
                tasks[username] = asyncio.create_task(
                    start_single_observer(
                        username, announcement_callback, role_callback, comment_callback
                    )
                )
                print(f"[Observer System] Started observer for @{username}.")
                try:
                    log_activity_event("system", f"Started observer task for @{username}.", username)
                except Exception:
                    pass

        await asyncio.sleep(60)

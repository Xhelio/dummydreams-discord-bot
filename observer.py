import asyncio
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
    get_current_host_for_account,
    set_current_host_for_account,
    update_live_session_host,
    get_channel_display_name,
    is_feature_enabled
)
import runtime_status

# Set global untuk melacak host yang saat ini sedang LIVE
CURRENT_LIVE_HOSTS = set()

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

    async def check_and_update_host_from_text(text: str, sender_username: str = None):
        """Detect host only from messages sent by the target account itself (with or without '#')."""
        nonlocal current_host, current_session_id
        if not text:
            return
        sender = (sender_username or "").lower().lstrip("@")
        # Only accept messages from the target account itself.
        if not sender or sender != username_clean:
            return

        msg_preview = str(text).replace("\n", " ").strip()
        if len(msg_preview) > 200:
            msg_preview = msg_preview[:197] + "..."

        detected = detect_host_from_welcome_message(text)
        if detected:
            if detected != current_host:
                conclusion = f"host changed: {current_host} -> {detected}"
            else:
                conclusion = f"host unchanged: {detected}"
        else:
            conclusion = f"no host match (current: {current_host})"

        print(f"[{username_clean}] [HostDetect] sender=@{sender} message={msg_preview!r} => {conclusion}")

        if detected and detected != current_host:
            old_host = current_host
            current_host = detected
            runtime_status.set_live(username_clean, detected)
            if current_session_id:
                update_live_session_host(current_session_id, detected)
            set_current_host_for_account(username_clean, detected)
            log_activity_event(
                "host_detected",
                f"Host changed {old_host} -> {detected} on @{username_clean} from @{sender}: \"{msg_preview}\"",
                username_clean,
            )

            # Update/edit pesan pengumuman yang sudah ada di Discord
            await _trigger_announcement(announcement_callback, username_clean, True, host_name=detected)
        elif detected:
            log_activity_event(
                "host_check",
                f"Host unchanged ({detected}) on @{username_clean} from @{sender}: \"{msg_preview}\"",
                username_clean,
            )
        else:
            log_activity_event(
                "host_check",
                f"No host match on @{username_clean} from @{sender}: \"{msg_preview}\" (current: {current_host})",
                username_clean,
            )

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

                # Refresh host/session context on (re)connect so host detection can continue/update.
                stored_host = get_current_host_for_account(username_clean)
                if stored_host and stored_host != "Host":
                    current_host = stored_host

                if not current_session_id:
                    current_session_id = start_live_session(username_clean, initial_host=current_host)

                runtime_status.set_live(username_clean, current_host, source="event")
                runtime_status.update_observer_diag(
                    username_clean,
                    status="reconnected" if reconnected_in_grace else "connected",
                    consecutive_failures=0,
                    last_error=None,
                )
                print(f"[{username_clean}] {'Reconnected to' if reconnected_in_grace else 'Connected to'} live stream (Host: {current_host}). Host detection active.")

                # Room title is owned by the target account; allow host detection from it.
                if hasattr(client, 'room_info') and client.room_info:
                    title = getattr(client.room_info, 'title', '')
                    if title:
                        await check_and_update_host_from_text(title, sender_username=username_clean)

                connect_msg = f"{'Reconnected to' if reconnected_in_grace else 'Connected to'} @{username_clean}'s live (Host: {current_host})"
                log_activity_event("connect", connect_msg, username_clean)

                # Kirim/update pengumuman agar status tetap sinkron setelah (re)connect.
                await _trigger_announcement(announcement_callback, username_clean, True, host_name=current_host)
            except Exception as e:
                # Keep runtime live status even if DB/announcement side effects fail.
                runtime_status.set_live(username_clean, current_host, source="event")
                runtime_status.update_observer_diag(username_clean, status="connected_with_handler_error", last_error=str(e))
                print(f"[{username_clean}] Connect handler error (live status preserved): {e}")

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            nonlocal current_session_id, is_connected, disconnect_timer_task
            print(f"[{username_clean}] Disconnected from live stream.")

            if is_connected:
                is_connected = False
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

                # 2. Host detection: only from messages sent by the target account itself (with or without '#').
                await check_and_update_host_from_text(comment_text, sender_username=user)
            except Exception as e:
                print(f"[{username_clean}] Error in on_comment handler: {e}")

        # Host detection rule: only messages sent by the target account itself are accepted.
        # System events (greeting/intro/notice/pin) are not treated as target-account chat messages.
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
                    _ = getattr(event, 'content', '')
                except Exception as e:
                    print(f"[{username_clean}] Error in on_live_intro handler: {e}")

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
        try:
            # Jalankan client koneksi TikTok Live
            await client.start()
        except asyncio.CancelledError:
            # Observer stopped by supervisor (host paused/removed/restarted).
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
            if is_connected:
                is_connected = False
                disconnect_msg = f"Disconnected from @{username_clean}'s live"
                log_activity_event("disconnect", disconnect_msg, username_clean)

                if current_session_id and (not disconnect_timer_task or disconnect_timer_task.done()):
                    disconnect_timer_task = asyncio.create_task(delayed_disconnect_handler(current_session_id))
                    current_session_id = None

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
                    runtime_status.set_live(username_clean, current_host, source="probe")
                    if not current_session_id:
                        current_session_id = start_live_session(username_clean, initial_host=current_host)
                    await _trigger_announcement(announcement_callback, username_clean, True, host_name=current_host)
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
                    probed_live = False
                if cycle_error is None:
                    runtime_status.update_observer_diag(username_clean, status="offline", consecutive_failures=0, last_error=None)
            elif probe is None and cycle_error is None and not grace_pending:
                runtime_status.update_observer_diag(username_clean, status="checking")
        else:
            consecutive_failures = 0

        # Adaptive retry: back off slightly on repeated failures, but keep checking.
        wait_seconds = 30
        if not connected_this_cycle and consecutive_failures >= 5:
            wait_seconds = 60
        if not connected_this_cycle and consecutive_failures >= 10:
            wait_seconds = 120
        await asyncio.sleep(wait_seconds)

async def run_all_observers(announcement_callback, role_callback, comment_callback=None):
    """Supervise one observer task per active host, refreshing from the DB.

    Hosts added, paused or removed in the admin panel are picked up within a
    minute without restarting the bot.
    """
    tasks = {}
    print("[Observer System] Starting supervised observers for target hosts in DB.")

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

        await asyncio.sleep(60)

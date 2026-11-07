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
    get_channel_display_name
)

# Set global untuk melacak host yang saat ini sedang LIVE
CURRENT_LIVE_HOSTS = set()

def get_current_live_hosts() -> list:
    """Mengembalikan daftar username host yang saat ini sedang terhubung dan live."""
    return list(CURRENT_LIVE_HOSTS)

def is_any_host_live() -> bool:
    """Mengecek apakah ada minimal 1 host yang sedang live saat ini."""
    return len(CURRENT_LIVE_HOSTS) > 0

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

    async def check_and_update_host_from_text(text: str):
        """Membaca teks welcome message / chat / intro, mendeteksi nama host, dan meng-edit postingan Discord jika berubah."""
        nonlocal current_host, current_session_id
        if not text:
            return
        detected = detect_host_from_welcome_message(text)
        if detected and detected != current_host:
            current_host = detected
            print(f"[{username_clean}] 🎯 Host detected from welcome message: {detected} (from text: {text!r})")
            if current_session_id:
                update_live_session_host(current_session_id, detected)
            set_current_host_for_account(username_clean, detected)
            log_activity_event("host_detected", f"Host terdeteksi: {detected} di @{username_clean} ('{text}')", username_clean)
            
            # Update/edit pesan pengumuman yang sudah ada di Discord
            await _trigger_announcement(announcement_callback, username_clean, True, host_name=detected)

    async def delayed_disconnect_handler(session_to_close):
        """Menunggu grace period 60 detik sebelum benar-benar menyatakan live berakhir di Discord."""
        try:
            await asyncio.sleep(60)
            nonlocal is_connected, current_session_id
            CURRENT_LIVE_HOSTS.discard(username_clean)
            if session_to_close:
                end_live_session(session_to_close)
            clear_active_live_status(username_clean)
            # Saat offline, bot Discord akan MENGHAPUS pesan pengumuman
            await _trigger_announcement(announcement_callback, username_clean, False)
            print(f"[{username_clean}] Live confirmed offline after grace period. Announcement message deleted.")
        except asyncio.CancelledError:
            # Reconnected within grace period
            pass

    while True:
        client = TikTokLiveClient(unique_id=username_clean)

        @client.on(ConnectEvent)
        async def on_connect(event: ConnectEvent):
            nonlocal current_session_id, is_connected, disconnect_timer_task, current_host
            
            # Jika sebelumnya ada pending disconnect timer, batalkan karena sudah terhubung kembali
            if disconnect_timer_task and not disconnect_timer_task.done():
                disconnect_timer_task.cancel()
                disconnect_timer_task = None
                print(f"[{username_clean}] Reconnected within grace period. Live status maintained.")

            is_connected = True
            CURRENT_LIVE_HOSTS.add(username_clean)
            print(f"[{username_clean}] Connected to live stream.")
            
            # Cek apakah judul / room_info memuat nama host dari welcome message
            if hasattr(client, 'room_info') and client.room_info:
                title = getattr(client.room_info, 'title', '')
                if title:
                    await check_and_update_host_from_text(title)

            current_session_id = start_live_session(username_clean, initial_host=current_host)
            
            connect_msg = f"terhubung ke akun @{username_clean} channel (Host: {current_host})"
            log_activity_event("connect", connect_msg, username_clean)
            
            # Kirim pengumuman (hanya 1 kali per sesi live)
            await _trigger_announcement(announcement_callback, username_clean, True, host_name=current_host)

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            nonlocal current_session_id, is_connected, disconnect_timer_task
            print(f"[{username_clean}] Disconnected from live stream.")
            
            if is_connected:
                is_connected = False
                disconnect_msg = f"terputus dari akun @{username_clean} channel"
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
                
                # 1. Cek verifikasi live chat akun Discord
                if comment_callback:
                    if asyncio.iscoroutinefunction(comment_callback):
                        await comment_callback(user, comment_text, username_clean)
                    else:
                        comment_callback(user, comment_text, username_clean)

                # 2. Cek apakah komentar memuat welcome message atau nama host (misal: "ZIZE's here", "welcome Mommy", dsb.)
                await check_and_update_host_from_text(comment_text)
            except Exception as e:
                print(f"[{username_clean}] Error in on_comment handler: {e}")

        if GreetingEvent:
            @client.on(GreetingEvent)
            async def on_greeting(event):
                try:
                    greeting_text = getattr(event, 'greeting_text', '') or getattr(event, 'guide_text', '')
                    await check_and_update_host_from_text(greeting_text)
                except Exception as e:
                    print(f"[{username_clean}] Error in on_greeting handler: {e}")

        if LiveIntroEvent:
            @client.on(LiveIntroEvent)
            async def on_live_intro(event):
                try:
                    content = getattr(event, 'content', '')
                    await check_and_update_host_from_text(content)
                except Exception as e:
                    print(f"[{username_clean}] Error in on_live_intro handler: {e}")

        if NoticeEvent:
            @client.on(NoticeEvent)
            async def on_notice(event):
                try:
                    notice_text = getattr(event, 'content', '') or getattr(event, 'notice_content', '')
                    await check_and_update_host_from_text(notice_text)
                except Exception as e:
                    print(f"[{username_clean}] Error in on_notice handler: {e}")

        if RoomPinEvent:
            @client.on(RoomPinEvent)
            async def on_room_pin(event):
                try:
                    chat_msg = getattr(event, 'chat_message', '')
                    if hasattr(chat_msg, 'content'):
                        chat_msg = chat_msg.content
                    await check_and_update_host_from_text(str(chat_msg or ''))
                except Exception as e:
                    print(f"[{username_clean}] Error in on_room_pin handler: {e}")

        @client.on(GiftEvent)
        async def on_gift(event: GiftEvent):
            try:
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
                        f"User: @{user} - {gift_name} - {coins} koin [{coins}c] - "
                        f"[channel {channel_spent} c] - [today {today_spent} c] - [total saat ini: {total_all} c]"
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

        try:
            # Jalankan client koneksi TikTok Live
            await client.start()
        except Exception as e:
            # Log santai jika stream offline
            print(f"[{username_clean}] Stream offline or reconnecting ({e}). Next check in 30s...")
        finally:
            if is_connected:
                is_connected = False
                disconnect_msg = f"terputus dari akun @{username_clean} channel"
                log_activity_event("disconnect", disconnect_msg, username_clean)

                if current_session_id and (not disconnect_timer_task or disconnect_timer_task.done()):
                    disconnect_timer_task = asyncio.create_task(delayed_disconnect_handler(current_session_id))
                    current_session_id = None

        # Interval jeda sebelum cek rutin ulang status live berikutnya
        await asyncio.sleep(30)

async def run_all_observers(announcement_callback, role_callback, comment_callback=None):
    """Membaca daftar host aktif dari DB dan menjalankan observer secara paralel."""
    target_users = get_active_hosts()
    print(f"[Observer System] Initializing observers for target hosts in DB: {target_users}")
    
    tasks = []
    for username in target_users:
        tasks.append(
            asyncio.create_task(
                start_single_observer(username, announcement_callback, role_callback, comment_callback)
            )
        )
    await asyncio.gather(*tasks)

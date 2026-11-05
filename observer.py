import asyncio
from datetime import datetime
import pytz
from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, DisconnectEvent, GiftEvent
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
    get_user_total_spent
)

async def _trigger_announcement(announcement_callback, username: str, is_live: bool):
    if not announcement_callback:
        return
    if asyncio.iscoroutinefunction(announcement_callback):
        await announcement_callback(username, is_live)
    else:
        announcement_callback(username, is_live)

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

async def start_single_observer(tiktok_username: str, announcement_callback, role_callback):
    username_clean = tiktok_username.lower().lstrip("@")
    current_session_id = None
    is_connected = False

    while True:
        client = TikTokLiveClient(unique_id=username_clean)

        @client.on(ConnectEvent)
        async def on_connect(event: ConnectEvent):
            nonlocal current_session_id, is_connected
            is_connected = True
            print(f"[{username_clean}] Connected to live stream.")
            current_session_id = start_live_session(username_clean)
            
            # Format: terhubung ke akun @user channel
            connect_msg = f"terhubung ke akun @{username_clean} channel"
            log_activity_event("connect", connect_msg, username_clean)
            
            await _trigger_announcement(announcement_callback, username_clean, True)

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent):
            nonlocal current_session_id, is_connected
            print(f"[{username_clean}] Disconnected from live stream.")
            if current_session_id:
                end_live_session(current_session_id)
                current_session_id = None
            
            if is_connected:
                is_connected = False
                # Format: terputus dari akun @user channel
                disconnect_msg = f"terputus dari akun @{username_clean} channel"
                log_activity_event("disconnect", disconnect_msg, username_clean)

            clear_active_live_status(username_clean)
            await _trigger_announcement(announcement_callback, username_clean, False)

        @client.on(GiftEvent)
        async def on_gift(event: GiftEvent):
            if event.gift.streakable and event.gift.streaking:
                return

            user = event.user.unique_id
            display_name = event.user.nickname
            coins = event.gift.diamond_count * event.repeat_count
            gift_name = event.gift.name

            if coins > 0:
                total_coins = save_gift_data(user, display_name, coins)
                save_daily_gift_data(user, display_name, coins)
                log_gift_transaction(
                    session_id=current_session_id,
                    target_user=username_clean,
                    viewer_user=user,
                    viewer_display=display_name,
                    gift_name=gift_name,
                    gift_count=event.repeat_count,
                    coins=coins
                )

                # Format Waktu: [jj:mm:dd] (WIB: HH:MM:SS)
                wib = pytz.timezone('Asia/Jakarta')
                now_wib = datetime.now(wib)
                time_jj_mm_dd = now_wib.strftime('%H:%M:%S')
                today_str = now_wib.strftime('%Y-%m-%d')

                channel_spent = get_user_channel_spent(user, username_clean)
                today_spent = get_user_today_spent(user, today_str)
                total_all = get_user_total_spent(user)

                # Format: [@user channel] [jj:mm:dd] @namapengguna - Nama gift - jumlah koin [(jumlah)c]- [(total koin spent di channel ini) c] - [today (total koin di spent hari ini)c] - [total (total koin keseluruhan)c]
                gift_msg = (
                    f"[@{username_clean} channel] [{time_jj_mm_dd}] @{user} - {gift_name} - "
                    f"{coins} koin [{coins}c]- [{channel_spent} c] - [today {today_spent}c] - [total {total_all}c]"
                )
                log_activity_event("gift", gift_msg, username_clean)
                print(f"[Activity] {gift_msg}")

                if role_callback:
                    if asyncio.iscoroutinefunction(role_callback):
                        await role_callback(user, total_coins)
                    else:
                        role_callback(user, total_coins)

        try:
            # Jalankan client koneksi TikTok Live
            await client.start()
        except Exception as e:
            # Jika stream offline atau error saat berusaha terhubung
            print(f"[{username_clean}] Account is offline / connection closed ({e}). Re-checking in 30s...")
        finally:
            if is_connected:
                is_connected = False
                disconnect_msg = f"terputus dari akun @{username_clean} channel"
                log_activity_event("disconnect", disconnect_msg, username_clean)

            if current_session_id:
                end_live_session(current_session_id)
                current_session_id = None
            clear_active_live_status(username_clean)
            await _trigger_announcement(announcement_callback, username_clean, False)

        # Interval jeda sebelum cek rutin ulang status live berikutnya
        await asyncio.sleep(30)

async def run_all_observers(announcement_callback, role_callback):
    """Membaca daftar host aktif dari DB dan menjalankan observer secara paralel."""
    target_users = get_active_hosts()
    print(f"[Observer System] Initializing observers for 5 target hosts in DB: {target_users}")
    
    tasks = []
    for username in target_users:
        tasks.append(
            asyncio.create_task(
                start_single_observer(username, announcement_callback, role_callback)
            )
        )
    await asyncio.gather(*tasks)

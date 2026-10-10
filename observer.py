import asyncio
from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, DisconnectEvent, GiftEvent
from db import (
    save_gift_data, 
    save_daily_gift_data, 
    start_live_session, 
    end_live_session, 
    log_gift_transaction
)

async def start_single_observer(tiktok_username: str, announcement_callback, role_callback):
    """
    Fungsi dasar pengawas (observer) untuk 1 username TikTok.
    """
    username_clean = tiktok_username.lower().lstrip("@")
    current_session_id = None
    client = TikTokLiveClient(unique_id=username_clean)

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        nonlocal current_session_id
        print(f"[{username_clean}] Connected to live stream.")
        current_session_id = start_live_session(username_clean)
        if announcement_callback:
            announcement_callback(username_clean, True)

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        nonlocal current_session_id
        print(f"[{username_clean}] Disconnected from live stream.")
        if current_session_id:
            end_live_session(current_session_id)
            current_session_id = None
        if announcement_callback:
            announcement_callback(username_clean, False)

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
                host_user=username_clean,
                viewer_user=user,
                viewer_display=display_name,
                gift_name=gift_name,
                gift_count=event.repeat_count,
                coins=coins
            )
            
            if role_callback:
                await role_callback(user, total_coins)

    while True:
        try:
            await client.start()
        except Exception as e:
            print(f"[{username_clean}] Observer error: {e}. Retrying in 15 seconds...")
            await asyncio.sleep(15)

async def run_all_observers(target_users: list, announcement_callback, role_callback):
    """
    Menjalankan observer secara paralel untuk seluruh username dalam daftar.
    """
    tasks = []
    for username in target_users:
        tasks.append(
            asyncio.create_task(
                start_single_observer(username, announcement_callback, role_callback)
            )
        )
    await asyncio.gather(*tasks)

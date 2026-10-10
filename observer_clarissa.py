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

TIKTOK_USERNAME = "calissadreams"
current_session_id = None

async def run_clarissa_observer(announcement_callback, role_callback):
    global current_session_id
    client = TikTokLiveClient(unique_id=TIKTOK_USERNAME)

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        global current_session_id
        print(f"[Clarissa] Connected to @{TIKTOK_USERNAME} live stream.")
        current_session_id = start_live_session(TIKTOK_USERNAME)
        if announcement_callback:
            announcement_callback(TIKTOK_USERNAME, True)

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        global current_session_id
        print(f"[Clarissa] Disconnected from @{TIKTOK_USERNAME} live stream.")
        if current_session_id:
            end_live_session(current_session_id)
            current_session_id = None
        if announcement_callback:
            announcement_callback(TIKTOK_USERNAME, False)

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        # Abaikan streak gift yang belum selesai
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
                host_user=TIKTOK_USERNAME,
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
            print(f"[Clarissa] Observer error: {e}. Retrying in 15 seconds...")
            await asyncio.sleep(15)

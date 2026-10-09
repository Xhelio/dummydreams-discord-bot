import asyncio
import time
from TikTokLive import TikTokLiveClient
from TikTokLive.events import GiftEvent, ConnectEvent, DisconnectEvent
from TikTokLive.client.errors import UserOfflineError

from db import save_gift_data, save_daily_gift_data
from utils import get_schedule_info

LOCAL_TARGET_USER = "clarissadreams"
LAST_GIFT_TIMESTAMP = {LOCAL_TARGET_USER.lower(): time.time()}

def create_clarissa_client(username: str, stop_event: asyncio.Event, announcement_callback, role_callback) -> TikTokLiveClient:
    client = TikTokLiveClient(unique_id=username.lower())

    @client.on(ConnectEvent)
    async def on_connect(event: ConnectEvent):
        print(f"[@{username}] Connected to TikTok Live!")
        await announcement_callback(username, is_live=True)

    @client.on(DisconnectEvent)
    async def on_disconnect(event: DisconnectEvent):
        print(f"[@{username}] Disconnected from TikTok Live!")
        await announcement_callback(username, is_live=False)
        stop_event.set()

    @client.on(GiftEvent)
    async def on_gift(event: GiftEvent):
        is_streakable = getattr(event.gift, "streakable", False)
        is_repeat_end = getattr(event, "repeat_end", True)

        if not is_streakable or is_repeat_end:
            user_name = event.user.unique_id.lower()
            display_name = getattr(event.user, "nickname", user_name)
            gift_name = getattr(event.gift, "name", "gift")

            diamond_count = getattr(event.gift, "diamond_count", 0)
            repeat_count = getattr(event, "repeat_count", 1) # Diperbaiki di sini
            coins = diamond_count * repeat_count

            if coins > 0:
                LAST_GIFT_TIMESTAMP[username.lower()] = time.time()
                total_coins = save_gift_data(user_name, display_name, coins)

                # Simpan juga ke akumulasi koin harian (Daily Rank)
                save_daily_gift_data(user_name, display_name, coins)

                print(f"[gift - @{client.unique_id}] {display_name} (@{user_name}) sent {gift_name} ({coins} c); total : {total_coins}")
                await role_callback(user_name, total_coins)

    return client

async def run_clarissa_observer(announcement_callback, role_callback):
    username = LOCAL_TARGET_USER
    retry_delay = 180

    while True:
        stop_event = asyncio.Event()
        client = create_clarissa_client(username, stop_event, announcement_callback, role_callback)

        try:
            print(f"[@{username}] Checking live status...")
            is_currently_live = await client.is_live()

            if not is_currently_live:
                await announcement_callback(username, is_live=False)
                print(f"[@{username}] Streamer is OFFLINE. Standby for 3 minutes...")
                await asyncio.sleep(retry_delay)
                continue

            print(f"[@{username}] Streamer is LIVE! Connecting...")
            asyncio.create_task(client.start())

            await stop_event.wait()
            print(f"[@{username}] Stream ended or connection lost. Waiting 30s before re-check...")
            await asyncio.sleep(30)

        except UserOfflineError:
            await announcement_callback(username, is_live=False)
            print(f"[@{username}] Streamer is offline (UserOfflineError). Retry in 3 minutes...")
            await asyncio.sleep(retry_delay)

        except Exception as e:
            error_msg = str(e).lower()
            try:
                await client.disconnect()
            except Exception:
                pass

            last_gift = LAST_GIFT_TIMESTAMP.get(username.lower(), time.time())
            seconds_since_last_gift = time.time() - last_gift
            is_high_traffic = seconds_since_last_gift <= 180

            if "rate_limit" in error_msg or "too many connections" in error_msg or "429" in error_msg:
                cooldown = 180 if is_high_traffic else 300
                print(f"[@{username}] Rate limit detected! Cooldown {cooldown}s...")
                await asyncio.sleep(cooldown)
            elif "offline" in error_msg or "not live" in error_msg:
                await announcement_callback(username, is_live=False)
                print(f"[@{username}] Streamer offline. Cooldown 3 minutes...")
                await asyncio.sleep(retry_delay)
            else:
                cooldown = 30 if is_high_traffic else 60
                print(f"[@{username}] Disconnected: {e} (retry in {cooldown}s)")
                await asyncio.sleep(cooldown)
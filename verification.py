import json
import random
import re
import time

import httpx
from TikTokLive import TikTokLiveClient
from TikTokLive.client.web.web_client import FetchRoomIdAPIRoute

MOBILE_HEADERS = {
    "User-Agent": "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 13; en_US; Pixel 7; Build/TQ3A.230805.001; Cronet/58.0.2991.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Cache-Control": "no-cache, no-store, max-age=0",
    "Pragma": "no-cache",
}


def _extract_bio_from_html(html: str) -> str | None:
    """Extract the profile bio/signature from TikTok page markup."""
    if not html:
        return None

    # Prefer structured JSON when present.
    for key in ("signature", "bioDescription"):
        m = re.search(rf'"{key}"\s*:\s*"((?:\\.|[^"\\])*)"', html)
        if m:
            try:
                value = json.loads('"' + m.group(1) + '"')
            except Exception:
                value = m.group(1)
            if value is not None:
                return str(value)
    return None


async def async_fetch_tiktok_bio(username: str) -> str:
    """
    Fetch a TikTok profile bio asynchronously.

    Uses cache-busting on every call so repeated "Verify Bio" clicks recheck the
    current profile instead of showing stale cached content.
    """
    u_clean = username.lower().lstrip("@")
    cache_bust = f"_cb={int(time.time() * 1000)}_{random.randint(1000, 9999)}"

    # Method 1: scrape the public profile page with no-cache headers.
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            resp = await client.get(
                f"https://www.tiktok.com/@{u_clean}?{cache_bust}",
                headers=MOBILE_HEADERS,
            )
            if resp.status_code == 200:
                bio = _extract_bio_from_html(resp.text)
                if bio is not None:
                    return bio
            else:
                print(f"[Bio Scraper] HTTP {resp.status_code} for @{u_clean}")
    except Exception as e:
        print(f"[Bio Scraper] HTTP request error for @{u_clean}: {e}")

    # Method 2: fallback to TikTokLive internal client API.
    try:
        c = TikTokLiveClient(unique_id=u_clean)
        data = await FetchRoomIdAPIRoute.fetch_user_room_data(c.web, u_clean)
        user_info = data.get("data", {}).get("user", {})
        bio = user_info.get("signature") or user_info.get("bioDescription")
        if bio is not None:
            return str(bio)
    except Exception as e:
        print(f"[Bio Scraper] TikTokLive fallback error for @{u_clean}: {e}")

    return ""


async def async_verify_tiktok_bio(username: str, expected_code: str) -> tuple[bool, str]:
    """
    Check whether expected_code exists in the TikTok bio for username.
    Returns (is_valid, current_bio).
    """
    bio = await async_fetch_tiktok_bio(username)
    if bio == "":
        return False, "(Bio not found or profile is private)"

    code_clean = expected_code.strip().lower()
    if code_clean in bio.lower():
        return True, bio

    return False, bio


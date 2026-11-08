import re
import json
import httpx
from TikTokLive import TikTokLiveClient
from TikTokLive.client.web.web_client import FetchRoomIdAPIRoute

MOBILE_HEADERS = {
    'User-Agent': 'com.zhiliaoapp.musically/2022600030 (Linux; U; Android 13; en_US; Pixel 7; Build/TQ3A.230805.001; Cronet/58.0.2991.0)',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9,id;q=0.8',
}

async def async_fetch_tiktok_bio(username: str) -> str:
    """
    Mengambil bio profil TikTok secara asynchronous.
    Mendukung fallback antara Web HTML Scrape dan TikTokLive Room API.
    """
    u_clean = username.lower().lstrip('@')
    
    # Metode 1: Scrape profil menggunakan Mobile App User-Agent
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(f"https://www.tiktok.com/@{u_clean}", headers=MOBILE_HEADERS)
            if resp.status_code == 200:
                m = re.search(r'\"signature\":\"(.*?)\"', resp.text)
                if m:
                    try:
                        return json.loads('"' + m.group(1) + '"')
                    except Exception:
                        return m.group(1)
    except Exception as e:
        print(f"[Bio Scraper] HTTP request error for @{u_clean}: {e}")

    # Metode 2: Fallback ke TikTokLive internal client API
    try:
        c = TikTokLiveClient(unique_id=u_clean)
        data = await FetchRoomIdAPIRoute.fetch_user_room_data(c.web, u_clean)
        user_info = data.get('data', {}).get('user', {})
        bio = user_info.get('signature') or user_info.get('bioDescription')
        if bio:
            return str(bio)
    except Exception:
        pass

    return ""

async def async_verify_tiktok_bio(username: str, expected_code: str) -> tuple[bool, str]:
    """
    Memeriksa apakah expected_code ada di dalam bio profil TikTok username.
    Mengembalikan (is_valid: bool, current_bio: str).
    """
    bio = await async_fetch_tiktok_bio(username)
    if not bio:
        return False, "(Bio not found or profile is private)"
    
    code_clean = expected_code.strip().lower()
    if code_clean in bio.lower():
        return True, bio
    
    return False, bio


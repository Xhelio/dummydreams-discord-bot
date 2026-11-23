"""Best-effort TikTok link previews for Discord.

TikTok's public oEmbed endpoint currently returns embed HTML and creator metadata,
but does not consistently expose a thumbnail URL. We therefore read Open Graph
metadata from public video pages first and use oEmbed as a metadata fallback.
No iframe/player HTML is ever rendered in Discord.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
import discord

log = logging.getLogger(__name__)

_ALLOWED_PAGE_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}
_ALLOWED_IMAGE_SUFFIXES = (
    ".tiktokcdn.com",
    ".tiktokcdn-us.com",
    ".tiktokv.com",
    ".byteoversea.com",
    ".ibytedtos.com",
    ".byteimg.com",
)
_MAX_HTML_BYTES = 1_000_000
_MAX_REDIRECTS = 4
_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ITEMS = 256
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_CACHE: dict[str, tuple[float, "TikTokPreview | None"]] = {}
_LIVE_CACHE_TTL_SECONDS = 45
_LIVE_CACHE: dict[str, tuple[float, "TikTokLivePreview | None"]] = {}


@dataclass(frozen=True)
class TikTokPreview:
    url: str
    title: str | None = None
    description: str | None = None
    author_name: str | None = None
    author_url: str | None = None
    thumbnail_url: str | None = None


@dataclass(frozen=True)
class TikTokLivePreview:
    url: str
    username: str
    display_name: str | None = None
    title: str | None = None
    viewer_count: int | None = None
    thumbnail_url: str | None = None


class _LiveStateParser(HTMLParser):
    """Extract only TikTok's public SIGI_STATE JSON script."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_state = False
        self.state_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "script":
            return
        values = {str(key).lower(): value or "" for key, value in attrs}
        self._in_state = values.get("id") == "SIGI_STATE"

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script":
            self._in_state = False

    def handle_data(self, data: str) -> None:
        if self._in_state:
            self.state_parts.append(data)


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(k).lower(): html.unescape(v or "") for k, v in attrs}
        if tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and data.strip():
            self.title_parts.append(data.strip())


def _is_allowed_page_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in {"http", "https"}
            and host in _ALLOWED_PAGE_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 80, 443)
        )
    except ValueError:
        return False


def normalize_tiktok_url(url: str) -> str | None:
    """Return a safe, normalized TikTok URL, or None for other/unsafe URLs."""
    candidate = (url or "").strip().rstrip(".,!?;:)]}'\"")
    if not candidate or not _is_allowed_page_url(candidate):
        return None
    parsed = urlsplit(candidate)
    # Fragments do not identify a different TikTok video and can be discarded.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def extract_tiktok_urls(content: str) -> list[str]:
    """Extract unique TikTok URLs from a Discord message."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.findall(content or ""):
        normalized = normalize_tiktok_url(match)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


def _safe_thumbnail_url(value: str | None, page_url: str) -> str | None:
    if not value:
        return None
    candidate = urljoin(page_url, html.unescape(value.strip()))
    try:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        allowed = any(host.endswith(suffix) for suffix in _ALLOWED_IMAGE_SUFFIXES)
        return candidate if parsed.scheme == "https" and allowed and not parsed.username and not parsed.password else None
    except ValueError:
        return None


def _parse_page_metadata(body: str, page_url: str) -> TikTokPreview:
    parser = _MetaParser()
    parser.feed(body)
    meta = parser.meta
    title = meta.get("og:title") or meta.get("twitter:title")
    description = meta.get("og:description") or meta.get("description") or meta.get("twitter:description")
    author = meta.get("author") or meta.get("og:video:tag")
    author_url = meta.get("og:url") or page_url
    thumbnail = _safe_thumbnail_url(
        meta.get("og:image") or meta.get("twitter:image") or meta.get("twitter:image:src"),
        page_url,
    )
    if not title and parser.title_parts:
        title = " ".join(parser.title_parts)
    return TikTokPreview(
        url=page_url,
        title=(title or "").strip() or None,
        description=(description or "").strip() or None,
        author_name=(author or "").strip() or None,
        author_url=author_url,
        thumbnail_url=thumbnail,
    )


async def _read_limited(response: aiohttp.ClientResponse) -> bytes | None:
    data = await response.content.read(_MAX_HTML_BYTES + 1)
    return data if len(data) <= _MAX_HTML_BYTES else None


async def fetch_tiktok_preview(url: str) -> TikTokPreview | None:
    """Fetch public TikTok metadata with bounded time, size, redirects and cache."""
    normalized = normalize_tiktok_url(url)
    if not normalized:
        return None

    now = time.monotonic()
    cached = _CACHE.get(normalized)
    if cached and cached[0] > now:
        return cached[1]
    if cached:
        _CACHE.pop(normalized, None)

    timeout = aiohttp.ClientTimeout(total=9, connect=3, sock_read=5)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; DummyDreamsPreview/1.0)",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
    }
    preview: TikTokPreview | None = None
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            current_url = normalized
            for _ in range(_MAX_REDIRECTS + 1):
                if not _is_allowed_page_url(current_url):
                    return None
                async with session.get(current_url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            break
                        current_url = urljoin(current_url, location)
                        continue
                    if response.status != 200:
                        break
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "html" not in content_type:
                        break
                    raw = await _read_limited(response)
                    if raw is None:
                        break
                    final_url = str(response.url)
                    if not _is_allowed_page_url(final_url):
                        break
                    preview = _parse_page_metadata(raw.decode("utf-8", errors="replace"), final_url)
                    break

            # TikTok oEmbed is an official metadata fallback. Its current public
            # response does not guarantee a thumbnail_url, so never invent one.
            if (preview is None or not (preview.title or preview.author_name)) and _is_allowed_page_url(current_url):
                oembed_url = "https://www.tiktok.com/oembed?" + urlencode({"url": current_url})
                async with session.get(oembed_url, allow_redirects=False) as response:
                    if response.status == 200:
                        payload = await response.json(content_type=None)
                        if isinstance(payload, dict):
                            author_url = payload.get("author_url")
                            if author_url and not _is_allowed_page_url(author_url):
                                author_url = None
                            fallback = TikTokPreview(
                                url=current_url,
                                title=str(payload.get("title") or "").strip() or None,
                                author_name=str(payload.get("author_name") or "").strip() or None,
                                author_url=author_url,
                                thumbnail_url=_safe_thumbnail_url(payload.get("thumbnail_url"), current_url),
                            )
                            if preview is None:
                                preview = fallback
                            else:
                                preview = TikTokPreview(
                                    url=preview.url,
                                    title=preview.title or fallback.title,
                                    description=preview.description,
                                    author_name=preview.author_name or fallback.author_name,
                                    author_url=preview.author_url or fallback.author_url,
                                    thumbnail_url=preview.thumbnail_url or fallback.thumbnail_url,
                                )
    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError, json.JSONDecodeError) as exc:
        log.debug("TikTok preview unavailable for %s: %s", normalized, type(exc).__name__)
    except Exception:
        log.exception("Unexpected TikTok preview failure for %s", normalized)

    if len(_CACHE) >= _CACHE_MAX_ITEMS:
        oldest_key = min(_CACHE, key=lambda key: _CACHE[key][0])
        _CACHE.pop(oldest_key, None)
    _CACHE[normalized] = (time.monotonic() + _CACHE_TTL_SECONDS, preview)
    return preview


def extract_tiktok_live_urls(content: str) -> list[str]:
    """Extract canonical TikTok LIVE URLs; short links are intentionally excluded."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.findall(content or ""):
        normalized = normalize_tiktok_url(match)
        if not normalized:
            continue
        path = urlsplit(normalized).path
        if not re.fullmatch(r"/@[A-Za-z0-9._-]+/live/?", path, re.IGNORECASE):
            continue
        if normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


def _parse_live_page_metadata(body: str, page_url: str) -> TikTokLivePreview | None:
    """Parse a public TikTok LIVE room; only explicit status=2 is considered live."""
    parser = _LiveStateParser()
    parser.feed(body)
    if not parser.state_parts:
        return None
    try:
        state = json.loads("".join(parser.state_parts))
    except (TypeError, ValueError):
        return None
    if not isinstance(state, dict):
        return None

    live_section = state.get("LiveRoom")
    if not isinstance(live_section, dict):
        return None
    user_info = live_section.get("liveRoomUserInfo")
    if not isinstance(user_info, dict):
        return None
    user = user_info.get("user")
    room = user_info.get("liveRoom") or live_section.get("liveRoom")
    if not isinstance(user, dict) or not isinstance(room, dict):
        return None

    try:
        status = int(room.get("status"))
    except (TypeError, ValueError):
        return None
    if status != 2:
        return None

    path_match = re.match(r"/@([^/]+)/live", urlsplit(page_url).path, re.IGNORECASE)
    username = str(user.get("uniqueId") or (path_match.group(1) if path_match else "")).strip()
    if not username:
        return None

    cover = room.get("coverUrl") or room.get("squareCoverImg") or room.get("cover")
    if isinstance(cover, dict):
        cover = cover.get("url_list") or cover.get("urlList") or cover.get("url")
    if isinstance(cover, (list, tuple)):
        cover = next((item for item in cover if isinstance(item, str) and item), None)
    thumbnail = _safe_thumbnail_url(str(cover) if cover else None, page_url)

    stats = room.get("liveRoomStats")
    if not isinstance(stats, dict):
        stats = {}
    viewer_value = stats.get("userCount", stats.get("user_count", stats.get("totalUser")))
    try:
        viewer_count = max(0, int(viewer_value)) if viewer_value is not None else None
    except (TypeError, ValueError):
        viewer_count = None

    display_name = str(user.get("nickname") or user.get("displayName") or "").strip() or None
    title = str(room.get("title") or "").strip() or None
    return TikTokLivePreview(
        url=page_url,
        username=username,
        display_name=display_name,
        title=title,
        viewer_count=viewer_count,
        thumbnail_url=thumbnail,
    )


def _parse_live_room_payload(payload: object, page_url: str) -> TikTokLivePreview | None:
    """Parse TikTok's public api-live/user/room response without using avatar URLs."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    # Accept the documented data.user/liveRoom shape and the HTML-like
    # liveRoomUserInfo shape used by some TikTok response variants.
    user_info = data.get("liveRoomUserInfo")
    if not isinstance(user_info, dict):
        user_info = {}
    user = data.get("user") or user_info.get("user")
    room = data.get("liveRoom") or user_info.get("liveRoom")
    if not isinstance(user, dict) or not isinstance(room, dict):
        return None
    try:
        if int(room.get("status")) != 2:
            return None
    except (TypeError, ValueError):
        return None

    path_match = re.match(r"/@([^/]+)/live", urlsplit(page_url).path, re.IGNORECASE)
    username = str(user.get("uniqueId") or (path_match.group(1) if path_match else "")).strip()
    if not username:
        return None

    cover = room.get("coverUrl") or room.get("squareCoverImg") or room.get("cover")
    if isinstance(cover, dict):
        cover = cover.get("url_list") or cover.get("urlList") or cover.get("url")
    if isinstance(cover, (list, tuple)):
        cover = next((item for item in cover if isinstance(item, str) and item), None)
    thumbnail = _safe_thumbnail_url(str(cover) if cover else None, page_url)

    stats = room.get("liveRoomStats")
    if not isinstance(stats, dict):
        stats = {}
    viewer_value = stats.get("userCount", stats.get("user_count", stats.get("totalUser")))
    try:
        viewer_count = max(0, int(viewer_value)) if viewer_value is not None else None
    except (TypeError, ValueError):
        viewer_count = None

    display_name = str(user.get("nickname") or user.get("displayName") or "").strip() or None
    title = str(room.get("title") or "").strip() or None
    return TikTokLivePreview(
        url=page_url,
        username=username,
        display_name=display_name,
        title=title,
        viewer_count=viewer_count,
        thumbnail_url=thumbnail,
    )


async def fetch_tiktok_live_preview(url: str) -> TikTokLivePreview | None:
    """Fetch bounded public LIVE metadata, falling back to TikTok's public room-info endpoint."""
    normalized = normalize_tiktok_url(url)
    if not normalized or not re.fullmatch(
        r"/@[A-Za-z0-9._-]+/live/?", urlsplit(normalized).path, re.IGNORECASE
    ):
        return None

    now = time.monotonic()
    cached = _LIVE_CACHE.get(normalized)
    if cached and cached[0] > now:
        return cached[1]
    if cached:
        _LIVE_CACHE.pop(normalized, None)

    timeout = aiohttp.ClientTimeout(total=9, connect=3, sock_read=5)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; DummyDreamsPreview/1.0)",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.5",
    }
    preview: TikTokLivePreview | None = None
    page_reason = "not_attempted"
    api_reason = "not_attempted"
    username_match = re.match(
        r"/@([^/]+)/live", urlsplit(normalized).path, re.IGNORECASE
    )
    username = username_match.group(1) if username_match else "unknown"
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            current_url = normalized
            for _ in range(_MAX_REDIRECTS + 1):
                if not _is_allowed_page_url(current_url):
                    page_reason = "redirect_rejected"
                    break
                async with session.get(current_url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            page_reason = "redirect_without_location"
                            break
                        current_url = urljoin(current_url, location)
                        page_reason = "redirect_limit_or_pending"
                        continue
                    if response.status != 200:
                        page_reason = f"http_{response.status}"
                        break
                    content_type = response.headers.get("Content-Type", "").lower()
                    if "html" not in content_type:
                        page_reason = "non_html_response"
                        break
                    raw = await _read_limited(response)
                    if raw is None:
                        page_reason = "response_too_large"
                        break
                    final_url = str(response.url)
                    if not _is_allowed_page_url(final_url):
                        page_reason = "final_url_rejected"
                        break
                    preview = _parse_live_page_metadata(
                        raw.decode("utf-8", errors="replace"), final_url
                    )
                    page_reason = (
                        "cover_found" if preview and preview.thumbnail_url
                        else "live_room_without_trusted_cover" if preview
                        else "live_room_not_found_or_not_live"
                    )
                    break

            # TikTok's public room-info endpoint exposes coverUrl separately from
            # profile avatars. Use it when the page state is blocked, incomplete,
            # or has no room cover; never use user.avatar as a thumbnail.
            if preview is None or not preview.thumbnail_url:
                api_url = "https://www.tiktok.com/api-live/user/room/?" + urlencode(
                    {"uniqueId": username, "sourceType": "54"}
                )
                if username != "unknown" and _is_allowed_page_url(api_url):
                    async with session.get(
                        api_url,
                        allow_redirects=False,
                        headers={"Referer": normalized},
                    ) as api_response:
                        if api_response.status == 200:
                            raw = await _read_limited(api_response)
                            if raw is None:
                                api_reason = "response_too_large"
                            else:
                                try:
                                    payload = json.loads(raw.decode("utf-8", errors="replace"))
                                except (TypeError, ValueError):
                                    payload = None
                                    api_reason = "invalid_json"
                                api_preview = _parse_live_room_payload(payload, normalized)
                                if api_preview is None:
                                    if api_reason == "not_attempted":
                                        api_reason = "room_not_live_or_unrecognized_payload"
                                elif api_preview.thumbnail_url:
                                    api_reason = "cover_found"
                                else:
                                    api_reason = "live_room_without_trusted_cover"
                                if api_preview and (
                                    preview is None or api_preview.thumbnail_url
                                ):
                                    preview = api_preview
                        else:
                            api_reason = f"http_{api_response.status}"
                else:
                    api_reason = "username_unavailable"

    except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError) as exc:
        page_reason = f"request_error_{type(exc).__name__}"
    except Exception as exc:
        page_reason = f"unexpected_error_{type(exc).__name__}"
        log.exception("Unexpected TikTok LIVE preview failure for @%s", username)

    if preview is None or not preview.thumbnail_url:
        # Log only structural metadata; never log response bodies, URLs, or user data.
        payload_shape = "unavailable"
        if "payload" in locals():
            if isinstance(payload, dict):
                top_keys = sorted(str(key)[:32] for key in payload.keys())[:12]
                data_value = payload.get("data")
                data_keys = (
                    sorted(str(key)[:32] for key in data_value.keys())[:12]
                    if isinstance(data_value, dict)
                    else []
                )
                room_value = None
                if isinstance(data_value, dict):
                    room_value = data_value.get("liveRoom")
                    if not isinstance(room_value, dict):
                        info = data_value.get("liveRoomUserInfo")
                        room_value = info.get("liveRoom") if isinstance(info, dict) else None
                room_status = (
                    str(room_value.get("status"))[:16]
                    if isinstance(room_value, dict) and room_value.get("status") is not None
                    else "missing"
                )
                payload_shape = (
                    f"top_keys={top_keys},data_keys={data_keys},room_status={room_status}"
                )
            else:
                payload_shape = f"type={type(payload).__name__}"
        log.info(
            "TikTok LIVE thumbnail unavailable for @%s (page=%s, room_api=%s, api_shape=%s)",
            username,
            page_reason,
            api_reason,
            payload_shape,
        )

    if len(_LIVE_CACHE) >= _CACHE_MAX_ITEMS:
        oldest_key = min(_LIVE_CACHE, key=lambda key: _LIVE_CACHE[key][0])
        _LIVE_CACHE.pop(oldest_key, None)
    _LIVE_CACHE[normalized] = (time.monotonic() + _LIVE_CACHE_TTL_SECONDS, preview)
    return preview


def build_tiktok_live_embed(preview: TikTokLivePreview) -> discord.Embed:
    """Build a static LIVE status card with a room cover when TikTok exposes one."""
    title = (preview.title or f"@{preview.username} sedang LIVE")[:256]
    details = ["🔴 **SEDANG LIVE**"]
    if preview.viewer_count is not None:
        details.append(f"👀 {preview.viewer_count:,} penonton")
    embed = discord.Embed(
        title=title,
        url=preview.url,
        description="\n".join(details),
        color=discord.Color.red(),
    )
    embed.set_author(name=(preview.display_name or preview.username)[:256])
    if preview.thumbnail_url:
        embed.set_image(url=preview.thumbnail_url)
    embed.set_footer(text="TikTok LIVE • Public room preview")
    return embed


def build_tiktok_embed(preview: TikTokPreview) -> discord.Embed:
    """Build a Discord-native preview card; never attempts to render TikTok iframe HTML."""
    title = (preview.title or "TikTok video")[:256]
    description = (preview.description or "").strip()
    if len(description) > 4000:
        description = description[:3997] + "..."
    embed = discord.Embed(
        title=title,
        url=preview.url,
        description=description or None,
        color=discord.Color.dark_theme(),
    )
    if preview.author_name:
        embed.set_author(
            name=preview.author_name[:256],
            url=preview.author_url if preview.author_url and _is_allowed_page_url(preview.author_url) else None,
        )
    if preview.thumbnail_url:
        embed.set_image(url=preview.thumbnail_url)
    embed.set_footer(text="TikTok • Video preview")
    return embed

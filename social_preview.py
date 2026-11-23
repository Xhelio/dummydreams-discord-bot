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
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

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


@dataclass(frozen=True)
class TikTokPreview:
    url: str
    title: str | None = None
    description: str | None = None
    author_name: str | None = None
    author_url: str | None = None
    thumbnail_url: str | None = None


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
            if preview is None or not (preview.title or preview.author_name):
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

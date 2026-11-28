"""TikTok URL helpers.

Thumbnail/preview fetching has been intentionally removed. The bot only needs
TikTok links and LiveIntro event handling.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_ALLOWED_PAGE_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}
_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


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


def extract_tiktok_live_urls(content: str) -> list[str]:
    """Extract unique canonical TikTok LIVE URLs from a Discord message."""
    found: list[str] = []
    seen: set[str] = set()
    for url in extract_tiktok_urls(content):
        parsed = urlsplit(url)
        if re.fullmatch(r"/@[A-Za-z0-9._-]+/live/?", parsed.path, re.IGNORECASE):
            normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
            if normalized not in seen:
                seen.add(normalized)
                found.append(normalized)
    return found

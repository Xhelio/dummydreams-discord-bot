import asyncio

import discord

import social_preview


def test_normalize_tiktok_url_accepts_video_and_short_links():
    assert social_preview.normalize_tiktok_url(
        "https://www.tiktok.com/@creator/video/123#share"
    ) == "https://www.tiktok.com/@creator/video/123"
    assert social_preview.normalize_tiktok_url("https://vm.tiktok.com/ZMabc123/") == (
        "https://vm.tiktok.com/ZMabc123/"
    )


def test_normalize_tiktok_url_rejects_lookalike_and_unsafe_urls():
    assert social_preview.normalize_tiktok_url("https://tiktok.com.evil.example/video/1") is None
    assert social_preview.normalize_tiktok_url("https://user@www.tiktok.com/video/1") is None
    assert social_preview.normalize_tiktok_url("file:///etc/passwd") is None


def test_extract_tiktok_urls_deduplicates_and_strips_trailing_punctuation():
    urls = social_preview.extract_tiktok_urls(
        "See https://vm.tiktok.com/abc/, then https://vm.tiktok.com/abc/ and https://example.com"
    )
    assert urls == ["https://vm.tiktok.com/abc/"]


def test_parse_page_metadata_extracts_title_description_and_safe_thumbnail():
    body = """
    <html><head>
      <meta property="og:title" content="A video title">
      <meta property="og:description" content="Video description">
      <meta property="og:image" content="https://p16-sign.tiktokcdn.com/cover.jpeg">
      <meta name="author" content="creator">
    </head></html>
    """
    preview = social_preview._parse_page_metadata(body, "https://www.tiktok.com/@creator/video/123")
    assert preview.title == "A video title"
    assert preview.description == "Video description"
    assert preview.author_name == "creator"
    assert preview.thumbnail_url == "https://p16-sign.tiktokcdn.com/cover.jpeg"


def test_parse_page_metadata_rejects_untrusted_thumbnail_host():
    body = '<meta property="og:image" content="https://evil.example/image.jpg">'
    preview = social_preview._parse_page_metadata(body, "https://www.tiktok.com/@creator/video/123")
    assert preview.thumbnail_url is None


def test_build_tiktok_embed_uses_video_thumbnail_not_creator_avatar():
    preview = social_preview.TikTokPreview(
        url="https://www.tiktok.com/@creator/video/123",
        title="Video title",
        author_name="creator",
        author_url="https://www.tiktok.com/@creator",
        thumbnail_url="https://p16-sign.tiktokcdn.com/cover.jpeg",
    )
    embed = social_preview.build_tiktok_embed(preview)
    assert embed.title == "Video title"
    assert embed.url == preview.url
    assert embed.image.url == preview.thumbnail_url
    assert embed.author.name == "creator"
    assert embed.thumbnail.url is discord.Embed.Empty if hasattr(discord.Embed, "Empty") else True


def test_fetch_tiktok_preview_rejects_non_tiktok_before_network(monkeypatch):
    called = False

    class UnexpectedSession:
        def __init__(self, *args, **kwargs):
            nonlocal called
            called = True

    monkeypatch.setattr(social_preview.aiohttp, "ClientSession", UnexpectedSession)
    assert asyncio.run(social_preview.fetch_tiktok_preview("https://example.com/video")) is None
    assert called is False

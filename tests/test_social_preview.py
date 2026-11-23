import asyncio

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
    assert embed.thumbnail.url is None


def test_fetch_tiktok_preview_rejects_non_tiktok_before_network(monkeypatch):
    called = False

    class UnexpectedSession:
        def __init__(self, *args, **kwargs):
            nonlocal called
            called = True

    monkeypatch.setattr(social_preview.aiohttp, "ClientSession", UnexpectedSession)
    assert asyncio.run(social_preview.fetch_tiktok_preview("https://example.com/video")) is None
    assert called is False


def test_extract_tiktok_live_urls_only_accepts_canonical_live_paths():
    assert social_preview.extract_tiktok_live_urls(
        "LIVE: https://www.tiktok.com/@creator/live and "
        "https://www.tiktok.com/@creator/video/123 https://example.com/@creator/live"
    ) == ["https://www.tiktok.com/@creator/live"]


def test_parse_live_page_metadata_extracts_live_room_and_cover():
    body = """
    <script id="SIGI_STATE" type="application/json">
    {
      "LiveRoom": {
        "liveRoomUserInfo": {
          "user": {"uniqueId": "creator", "nickname": "Creator Name"},
          "liveRoom": {
            "status": 2,
            "title": "Evening stream",
            "coverUrl": "https://p16-webcast.tiktokcdn-us.com/cover.jpeg",
            "liveRoomStats": {"userCount": 1234}
          }
        }
      }
    }
    </script>
    """
    preview = social_preview._parse_live_page_metadata(
        body, "https://www.tiktok.com/@creator/live"
    )
    assert preview is not None
    assert preview.username == "creator"
    assert preview.display_name == "Creator Name"
    assert preview.title == "Evening stream"
    assert preview.viewer_count == 1234
    assert preview.thumbnail_url == "https://p16-webcast.tiktokcdn-us.com/cover.jpeg"


def test_parse_live_page_metadata_never_marks_offline_or_unknown_room_live():
    offline = """
    <script id="SIGI_STATE" type="application/json">
    {"LiveRoom":{"liveRoomUserInfo":{"user":{"uniqueId":"creator"},
    "liveRoom":{"status":4,"title":"Old stream","coverUrl":"https://p16-webcast.tiktokcdn-us.com/old.jpg"}}}}
    </script>
    """
    unknown = """
    <script id="SIGI_STATE" type="application/json">
    {"LiveRoom":{"liveRoomUserInfo":{"user":{"uniqueId":"creator"},
    "liveRoom":{"title":"Unverified","coverUrl":"https://p16-webcast.tiktokcdn-us.com/old.jpg"}}}}
    </script>
    """
    assert social_preview._parse_live_page_metadata(
        offline, "https://www.tiktok.com/@creator/live"
    ) is None
    assert social_preview._parse_live_page_metadata(
        unknown, "https://www.tiktok.com/@creator/live"
    ) is None


def test_parse_live_room_payload_extracts_public_room_cover():
    payload = {
        "statusCode": 0,
        "data": {
            "user": {"uniqueId": "creator", "nickname": "Creator Name"},
            "liveRoom": {
                "status": 2,
                "title": "Room API title",
                "coverUrl": "https://p16-webcast.tiktokcdn.com/room-cover.jpeg",
                "liveRoomStats": {"userCount": 987},
            },
        },
    }
    preview = social_preview._parse_live_room_payload(
        payload, "https://www.tiktok.com/@creator/live"
    )
    assert preview is not None
    assert preview.username == "creator"
    assert preview.display_name == "Creator Name"
    assert preview.title == "Room API title"
    assert preview.viewer_count == 987
    assert preview.thumbnail_url == "https://p16-webcast.tiktokcdn.com/room-cover.jpeg"


def test_parse_live_room_payload_rejects_offline_and_untrusted_cover():
    offline = {
        "data": {
            "user": {"uniqueId": "creator", "nickname": "Creator"},
            "liveRoom": {"status": 4, "coverUrl": "https://p16-webcast.tiktokcdn.com/old.jpeg"},
        }
    }
    untrusted = {
        "data": {
            "user": {"uniqueId": "creator", "nickname": "Creator"},
            "liveRoom": {"status": 2, "coverUrl": "https://evil.example/avatar.jpeg"},
        }
    }
    page = "https://www.tiktok.com/@creator/live"
    assert social_preview._parse_live_room_payload(offline, page) is None
    preview = social_preview._parse_live_room_payload(untrusted, page)
    assert preview is not None
    assert preview.thumbnail_url is None


def test_live_embed_uses_room_cover_not_profile_avatar():
    preview = social_preview.TikTokLivePreview(
        url="https://www.tiktok.com/@creator/live",
        username="creator",
        display_name="Creator",
        title="Live title",
        viewer_count=42,
        thumbnail_url="https://p16-webcast.tiktokcdn-us.com/cover.jpeg",
    )
    embed = social_preview.build_tiktok_live_embed(preview)
    assert embed.title == "Live title"
    assert embed.url == preview.url
    assert "SEDANG LIVE" in embed.description
    assert "42" in embed.description
    assert embed.image.url == preview.thumbnail_url
    assert embed.thumbnail.url is None


def test_live_embed_without_room_cover_does_not_fall_back_to_avatar():
    preview = social_preview.TikTokLivePreview(
        url="https://www.tiktok.com/@creator/live",
        username="creator",
        display_name="Creator",
    )
    embed = social_preview.build_tiktok_live_embed(preview)
    assert embed.image.url is None


def test_fetch_tiktok_live_preview_rejects_video_url_before_network(monkeypatch):
    called = False

    class UnexpectedSession:
        def __init__(self, *args, **kwargs):
            nonlocal called
            called = True

    monkeypatch.setattr(social_preview.aiohttp, "ClientSession", UnexpectedSession)
    assert asyncio.run(
        social_preview.fetch_tiktok_live_preview(
            "https://www.tiktok.com/@creator/video/123"
        )
    ) is None
    assert called is False

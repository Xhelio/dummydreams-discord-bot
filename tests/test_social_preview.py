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


def test_extract_tiktok_live_urls_only_accepts_canonical_live_paths():
    assert social_preview.extract_tiktok_live_urls(
        "LIVE: https://www.tiktok.com/@creator/live and "
        "https://www.tiktok.com/@creator/video/123 https://example.com/@creator/live"
    ) == ["https://www.tiktok.com/@creator/live"]

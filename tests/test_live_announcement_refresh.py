import live_announcement_refresh as refresh


def test_refresh_targets_include_only_active_live_accounts():
    sessions = [
        {"tiktok_username": "creator_one"},
        {"tiktok_username": "@creator_two"},
        {"tiktok_username": "offline_creator"},
        {"tiktok_username": "CREATOR_ONE"},
        None,
    ]

    assert refresh.get_live_announcement_refresh_targets(
        sessions, ["@creator_one", "creator_two"]
    ) == ["creator_one", "creator_two"]


def test_refresh_targets_handle_empty_or_missing_data():
    assert refresh.get_live_announcement_refresh_targets([], ["creator"]) == []
    assert refresh.get_live_announcement_refresh_targets(
        [{"tiktok_username": "creator"}], []
    ) == []


def test_refresh_interval_is_one_minute():
    assert refresh.LIVE_ANNOUNCEMENT_REFRESH_INTERVAL_SECONDS == 60

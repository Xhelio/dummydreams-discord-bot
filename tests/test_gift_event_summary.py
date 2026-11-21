import sqlite3

import db


def test_gift_event_summary_fetches_totals_and_channel_name(tmp_path, monkeypatch):
    path = tmp_path / "gift-summary.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE gift_logs (
            viewer_username TEXT,
            target_username TEXT,
            coins INTEGER
        );
        CREATE TABLE daily_tiktok_gifts (
            tiktok_user TEXT,
            date TEXT,
            coins INTEGER
        );
        CREATE TABLE tiktok_gifts (
            tiktok_user TEXT,
            total_coins INTEGER
        );
        CREATE TABLE hosts (
            tiktok_username TEXT,
            display_name TEXT
        );
        INSERT INTO gift_logs VALUES ('viewer', 'creator', 20);
        INSERT INTO gift_logs VALUES ('viewer', 'creator', 30);
        INSERT INTO daily_tiktok_gifts VALUES ('viewer', '2026-11-21', 50);
        INSERT INTO tiktok_gifts VALUES ('viewer', 125);
        INSERT INTO hosts VALUES ('creator', 'Creator Channel');
        """
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(db, "get_db_connection", lambda: sqlite3.connect(path))
    monkeypatch.setattr(db, "VALID_TARGET_ACCOUNTS", [])

    result = db.get_gift_event_summary(
        viewer_user="@Viewer",
        target_user="@Creator",
        date_str="2026-11-21",
    )

    assert result == {
        "channel_spent": 50,
        "today_spent": 50,
        "total_spent": 125,
        "channel_name": "Creator Channel",
    }


def test_gift_event_summary_prefers_official_cached_channel_name(tmp_path, monkeypatch):
    path = tmp_path / "gift-summary-cache.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE gift_logs (viewer_username TEXT, target_username TEXT, coins INTEGER);
        CREATE TABLE daily_tiktok_gifts (tiktok_user TEXT, date TEXT, coins INTEGER);
        CREATE TABLE tiktok_gifts (tiktok_user TEXT, total_coins INTEGER);
        CREATE TABLE hosts (tiktok_username TEXT, display_name TEXT);
        """
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(db, "get_db_connection", lambda: sqlite3.connect(path))
    monkeypatch.setattr(
        db,
        "VALID_TARGET_ACCOUNTS",
        [("1", "creator", "Official Creator")],
    )

    result = db.get_gift_event_summary("viewer", "creator", "2026-11-21")

    assert result["channel_name"] == "Official Creator"

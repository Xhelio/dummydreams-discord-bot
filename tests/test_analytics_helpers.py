import sqlite3

import db


def _analytics_db(tmp_path, monkeypatch):
    database_path = tmp_path / "analytics.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE gift_logs (
            log_date TEXT,
            log_time TEXT,
            log_datetime TEXT,
            host_name TEXT,
            scheduled_host TEXT,
            target_username TEXT,
            viewer_username TEXT,
            viewer_display_name TEXT,
            gift_name TEXT,
            gift_count INTEGER,
            coins INTEGER
        );
        CREATE TABLE live_sessions (
            start_time TEXT,
            tiktok_username TEXT,
            scheduled_host TEXT,
            duration_seconds INTEGER
        );
        """
    )
    connection.executemany(
        """
        INSERT INTO gift_logs (
            log_date, log_time, log_datetime, host_name, scheduled_host,
            target_username, viewer_username, viewer_display_name,
            gift_name, gift_count, coins
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("2026-11-20", "10:15:00", "2026-11-20 10:15:00", "Host A", "",
             "creator_a", "viewer_1", "Viewer One", "Rose", 2, 10),
            ("2026-11-20", "11:30:00", "2026-11-20 11:30:00", "",
             "host_b", "creator_b", "viewer_2", "Viewer Two", "Heart", 3, 15),
            ("2026-11-21", "10:45:00", "2026-11-21 10:45:00", "Host A", "",
             "creator_a", "viewer_1", "Viewer One", "Rose", 1, 5),
        ],
    )
    connection.executemany(
        """
        INSERT INTO live_sessions (
            start_time, tiktok_username, scheduled_host, duration_seconds
        ) VALUES (?, ?, ?, ?)
        """,
        [
            ("2026-11-20 09:00:00", "creator_a", "host_a", 120),
            ("2026-11-20 12:00:00", "creator_b", "host_b", 240),
            ("2026-11-21 09:30:00", "creator_a", "host_a", 60),
        ],
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(
        db, "get_db_connection", lambda: sqlite3.connect(database_path)
    )
    return database_path


def test_analytics_gift_where_normalizes_filters():
    where, params = db._analytics_gift_where(
        "2026-11-20", "2026-11-21", host=" @Host A ", account="@CREATOR_A",
        viewer=" @Viewer_1 ",
    )

    assert "BETWEEN ? AND ?" in where
    assert params == (
        "2026-11-20", "2026-11-21", "host a", "creator_a", "viewer_1"
    )


def test_analytics_summary_aggregates_gifts_and_applies_filters(tmp_path, monkeypatch):
    _analytics_db(tmp_path, monkeypatch)

    assert db.get_analytics_summary("2026-11-20", "2026-11-21") == {
        "coins": 30,
        "gift_rows": 3,
        "gifts": 6,
        "viewers": 2,
        "hosts": 2,
        "accounts": 2,
    }
    assert db.get_analytics_summary(
        "2026-11-20", "2026-11-21", host="@HOST A", account="@CREATOR_A",
        viewer="@VIEWER_1",
    ) == {
        "coins": 15,
        "gift_rows": 2,
        "gifts": 3,
        "viewers": 1,
        "hosts": 1,
        "accounts": 1,
    }


def test_analytics_sessions_calculates_totals_and_average(tmp_path, monkeypatch):
    _analytics_db(tmp_path, monkeypatch)

    assert db.get_analytics_sessions("2026-11-20", "2026-11-21") == {
        "sessions": 3,
        "duration": 420,
        "avg_duration": 140,
        "accounts": 2,
    }
    assert db.get_analytics_sessions(
        "2026-11-20", "2026-11-21", host="@HOST_A", account="@CREATOR_A"
    ) == {
        "sessions": 2,
        "duration": 180,
        "avg_duration": 90,
        "accounts": 1,
    }


def test_analytics_session_breakdown_groups_by_host_and_account(tmp_path, monkeypatch):
    _analytics_db(tmp_path, monkeypatch)

    assert db.get_analytics_session_breakdown(
        "2026-11-20", "2026-11-21"
    ) == {
        "by_host": {"host_a": (2, 180), "host_b": (1, 240)},
        "by_account": {"creator_a": (2, 180), "creator_b": (1, 240)},
    }


def test_analytics_date_bounds_respect_host_and_account_filters(tmp_path, monkeypatch):
    _analytics_db(tmp_path, monkeypatch)

    assert db.get_analytics_date_bounds() == ("2026-11-20", "2026-11-21")
    assert db.get_analytics_date_bounds(
        host="@HOST_A", account="@CREATOR_A"
    ) == ("2026-11-20", "2026-11-21")

def test_analytics_date_indexes_match_range_filter_expressions(tmp_path):
    database_path = tmp_path / "analytics-indexes.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(
            """
            CREATE TABLE gift_logs (log_date TEXT);
            CREATE TABLE live_sessions (start_time TEXT);
            """
        )

        db._ensure_analytics_indexes(connection)

        gift_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(gift_logs)")
        }
        session_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(live_sessions)")
        }

        assert "idx_gift_logs_analytics_date" in gift_indexes
        assert "idx_live_sessions_analytics_date" in session_indexes
    finally:
        connection.close()

import sqlite3

import db
import routes.admin as admin


def _timeseries_db(tmp_path, monkeypatch):
    database_path = tmp_path / "timeseries.sqlite3"
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
            target_username, viewer_username, gift_count, coins
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("2026-11-20", "10:15:00", "2026-11-20 10:15:00",
             "Host A", "", "creator_a", "viewer_1", 2, 10),
            ("2026-11-20", "11:30:00", "2026-11-20 11:30:00",
             "", "host_b", "creator_b", "viewer_2", 3, 15),
            ("2026-11-21", "10:45:00", "2026-11-21 10:45:00",
             "Host A", "", "creator_a", "viewer_1", 1, 5),
            ("2026-12-01", "10:05:00", "2026-12-01 10:05:00",
             "Host A", "", "creator_a", "viewer_3", 1, 7),
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
            ("2026-12-01 10:30:00", "creator_a", "host_a", 300),
        ],
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(
        db, "get_db_connection", lambda: sqlite3.connect(database_path)
    )


def test_daily_timeseries_includes_empty_buckets(tmp_path, monkeypatch):
    _timeseries_db(tmp_path, monkeypatch)

    rows = db.get_analytics_timeseries("2026-11-20", "2026-11-22", "day")

    assert [row["key"] for row in rows] == [
        "2026-11-20", "2026-11-21", "2026-11-22"
    ]
    assert [row["coins"] for row in rows] == [25, 5, 0]
    assert [row["gift_events"] for row in rows] == [2, 1, 0]
    assert [row["sessions"] for row in rows] == [2, 1, 0]
    assert rows[-1]["viewers"] == 0


def test_weekly_and_monthly_timeseries_bucket_boundaries(tmp_path, monkeypatch):
    _timeseries_db(tmp_path, monkeypatch)

    weekly = db.get_analytics_timeseries(
        "2026-11-20", "2026-12-01", "week"
    )
    monthly = db.get_analytics_timeseries(
        "2026-11-20", "2026-12-01", "month"
    )

    assert [row["key"] for row in weekly] == [
        "2026-11-16", "2026-11-23", "2026-11-30"
    ]
    assert [row["coins"] for row in weekly] == [30, 0, 7]
    assert [row["key"] for row in monthly] == ["2026-11-01", "2026-12-01"]
    assert [row["coins"] for row in monthly] == [30, 7]


def test_hourly_timeseries_has_24_buckets_and_aggregates_metrics(
    tmp_path, monkeypatch
):
    _timeseries_db(tmp_path, monkeypatch)

    rows = db.get_analytics_timeseries(
        "2026-11-20", "2026-11-20", "hour"
    )

    assert len(rows) == 24
    assert rows[9]["key"] == "09"
    assert rows[9]["sessions"] == 1
    assert rows[9]["active_accounts"] == 1
    assert rows[10]["coins"] == 10
    assert rows[10]["gifts"] == 2
    assert rows[10]["gift_events"] == 1
    assert rows[10]["viewers"] == 1
    assert rows[11]["coins"] == 15
    assert rows[11]["viewers"] == 1
    assert rows[12]["sessions"] == 1
    assert rows[0]["coins"] == 0


def test_hour_of_day_timeseries_averages_across_calendar_days(
    tmp_path, monkeypatch
):
    _timeseries_db(tmp_path, monkeypatch)

    rows = db.get_analytics_timeseries(
        "2026-11-20", "2026-11-21", "hour_of_day"
    )

    assert len(rows) == 24
    ten_am = rows[10]
    assert ten_am["coins"] == 7.5
    assert ten_am["gifts"] == 1.5
    assert ten_am["gift_events"] == 1
    assert ten_am["viewers"] == 1
    assert rows[9]["sessions"] == 1
    assert rows[9]["active_accounts"] == 1
    assert rows[9]["active_hosts"] == 1
    assert rows[0]["coins"] == 0


def test_timeseries_rejects_invalid_ranges():
    assert db.get_analytics_timeseries("not-a-date", "2026-11-21") == []
    assert db.get_analytics_timeseries("2026-11-22", "2026-11-21") == []


def test_analytics_profile_cache_returns_cached_value(monkeypatch):
    admin._ANALYTICS_PROFILE_CACHE.clear()
    clock = [100.0]
    calls = []
    result = [{"key": "10", "coins": 12}]

    monkeypatch.setattr(admin.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        admin, "get_analytics_timeseries",
        lambda *args: calls.append(args) or result,
    )

    first = admin._get_cached_analytics_profile(
        "2026-11-01", "2026-11-21", "host_a", "creator_a", "viewer_1"
    )
    second = admin._get_cached_analytics_profile(
        "2026-11-01", "2026-11-21", "host_a", "creator_a", "viewer_1"
    )

    assert first is result
    assert second is result
    assert len(calls) == 1


def test_analytics_profile_cache_expires_after_ttl(monkeypatch):
    admin._ANALYTICS_PROFILE_CACHE.clear()
    clock = [100.0]
    calls = []
    results = [[{"version": 1}], [{"version": 2}]]

    monkeypatch.setattr(admin.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        admin, "get_analytics_timeseries",
        lambda *args: calls.append(args) or results[len(calls) - 1],
    )

    first = admin._get_cached_analytics_profile("2026-11-01", "2026-11-21")
    clock[0] += admin._ANALYTICS_PROFILE_CACHE_TTL + 0.01
    second = admin._get_cached_analytics_profile("2026-11-01", "2026-11-21")

    assert first == [{"version": 1}]
    assert second == [{"version": 2}]
    assert len(calls) == 2


def test_analytics_profile_cache_isolated_by_each_filter(monkeypatch):
    admin._ANALYTICS_PROFILE_CACHE.clear()
    calls = []

    def fake_timeseries(*args):
        calls.append(args)
        return [{"call": len(calls)}]

    monkeypatch.setattr(admin, "get_analytics_timeseries", fake_timeseries)

    admin._get_cached_analytics_profile("start", "end")
    admin._get_cached_analytics_profile("start", "end", host="host_a")
    admin._get_cached_analytics_profile("start", "end", account="creator_a")
    admin._get_cached_analytics_profile("start", "end", viewer="viewer_a")

    assert len(calls) == 4
    assert all(call[2] == "hour_of_day" for call in calls)

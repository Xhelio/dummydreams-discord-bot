import sqlite3

import db


def _setup_gift_database(path, daily_check=""):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE gift_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tiktok_username TEXT,
            session_id INTEGER,
            viewer_username TEXT NOT NULL,
            viewer_display_name TEXT,
            gift_name TEXT,
            gift_count INTEGER,
            coins INTEGER NOT NULL,
            log_date TEXT,
            log_time TEXT,
            log_datetime TEXT,
            target_username TEXT,
            scheduled_host TEXT,
            msg_id TEXT UNIQUE,
            host_name TEXT
        );
        CREATE TABLE live_sessions (
            session_id INTEGER PRIMARY KEY,
            total_coins INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE tiktok_gifts (
            tiktok_user TEXT PRIMARY KEY,
            display_name TEXT,
            total_coins INTEGER NOT NULL
        );
        """
    )
    connection.execute(
        f"""
        CREATE TABLE daily_tiktok_gifts (
            tiktok_user TEXT NOT NULL,
            display_name TEXT,
            coins INTEGER NOT NULL {daily_check},
            date TEXT NOT NULL,
            UNIQUE(tiktok_user, date)
        )
        """
    )
    connection.execute(
        "INSERT INTO live_sessions (session_id, total_coins) VALUES (7, 0)"
    )
    connection.commit()
    connection.close()


def _configure_database(monkeypatch, path):
    # Each test uses an isolated schema/database; production keeps the cache
    # process-local and refreshes it after its short TTL.
    monkeypatch.setattr(db, "_GIFT_LOG_COLUMN_INFO_CACHE", None)
    monkeypatch.setattr(db, "_GIFT_LOG_COLUMN_INFO_CACHE_AT", 0.0)
    monkeypatch.setattr(
        db, "get_db_connection", lambda: sqlite3.connect(path)
    )
    monkeypatch.setattr(
        db, "get_scheduled_host_name",
        lambda *args, **kwargs: "Scheduled Host",
    )
    monkeypatch.setattr(db, "set_setting", lambda *args, **kwargs: None)


def _record(msg_id="gift-message-1", coins=25):
    return db.record_gift_event_transaction(
        session_id=7,
        target_user="creator",
        viewer_user="viewer",
        viewer_display="Viewer",
        gift_name="Rose",
        gift_count=1,
        coins=coins,
        msg_id=msg_id,
        host_name="Current Host",
    )


def test_atomic_gift_event_updates_log_session_and_coin_totals_once(
    tmp_path, monkeypatch
):
    path = tmp_path / "gift-atomic.sqlite3"
    _setup_gift_database(path)
    _configure_database(monkeypatch, path)

    first = _record()
    duplicate = _record()

    connection = sqlite3.connect(path)
    try:
        log_count = connection.execute(
            "SELECT COUNT(*) FROM gift_logs"
        ).fetchone()[0]
        session_total = connection.execute(
            "SELECT total_coins FROM live_sessions WHERE session_id = 7"
        ).fetchone()[0]
        all_time = connection.execute(
            "SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = 'viewer'"
        ).fetchone()[0]
        daily = connection.execute(
            "SELECT coins FROM daily_tiktok_gifts WHERE tiktok_user = 'viewer'"
        ).fetchone()[0]
    finally:
        connection.close()

    assert first == {"status": "ok", "total_coins": 25}
    assert duplicate == {"status": "duplicate"}
    assert (log_count, session_total, all_time, daily) == (1, 25, 25, 25)


def test_atomic_gift_event_rolls_back_log_when_daily_aggregate_fails(
    tmp_path, monkeypatch
):
    path = tmp_path / "gift-rollback.sqlite3"
    _setup_gift_database(path, daily_check="CHECK(coins < 0)")
    _configure_database(monkeypatch, path)

    result = _record(msg_id="gift-message-failing", coins=25)

    connection = sqlite3.connect(path)
    try:
        log_count = connection.execute(
            "SELECT COUNT(*) FROM gift_logs"
        ).fetchone()[0]
        all_time_count = connection.execute(
            "SELECT COUNT(*) FROM tiktok_gifts"
        ).fetchone()[0]
        session_total = connection.execute(
            "SELECT total_coins FROM live_sessions WHERE session_id = 7"
        ).fetchone()[0]
    finally:
        connection.close()

    assert result["status"] == "failed"
    assert "IntegrityError" in result["error"]
    assert (log_count, all_time_count, session_total) == (0, 0, 0)



def test_atomic_gift_event_reads_gift_log_schema_once(tmp_path, monkeypatch):
    path = tmp_path / "gift-schema-once.sqlite3"
    _setup_gift_database(path)
    _configure_database(monkeypatch, path)

    original = db._get_table_column_info
    calls = []

    def counted_column_info(cursor, table):
        calls.append(table)
        return original(cursor, table)

    monkeypatch.setattr(db, "_get_table_column_info", counted_column_info)

    result = _record(msg_id="gift-schema-once")

    assert result == {"status": "ok", "total_coins": 25}
    assert calls == ["gift_logs"]

def test_atomic_gift_event_reuses_schema_metadata_across_events(
    tmp_path, monkeypatch
):
    path = tmp_path / "gift-schema-cache.sqlite3"
    _setup_gift_database(path)
    _configure_database(monkeypatch, path)

    original = db._get_table_column_info
    calls = []

    def counted_column_info(cursor, table):
        calls.append(table)
        return original(cursor, table)

    monkeypatch.setattr(db, "_get_table_column_info", counted_column_info)

    first = _record(msg_id="gift-schema-cache-1", coins=25)
    second = _record(msg_id="gift-schema-cache-2", coins=10)

    assert first == {"status": "ok", "total_coins": 25}
    assert second == {"status": "ok", "total_coins": 35}
    assert calls == ["gift_logs"]

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import db


def _connection_factory(path):
    return lambda: sqlite3.connect(path, timeout=5)


def _create_session_schema(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE live_sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tiktok_username TEXT NOT NULL,
                start_time TEXT,
                scheduled_host TEXT,
                last_seen_at TEXT,
                end_time TEXT,
                duration_seconds INTEGER DEFAULT 0,
                paused_seconds INTEGER DEFAULT 0
            );
            CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_concurrent_start_live_session_creates_only_one_open_session(
    tmp_path, monkeypatch
):
    path = tmp_path / "live-sessions.sqlite3"
    _create_session_schema(path)
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))
    monkeypatch.setattr(
        db, "get_current_host_for_account", lambda *args, **kwargs: "Host A"
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        session_ids = list(pool.map(lambda _: db.start_live_session("sample_user"), range(8)))

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT session_id, end_time FROM live_sessions "
            "WHERE tiktok_username = 'sample_user'"
        ).fetchall()
    finally:
        conn.close()

    assert len(set(session_ids)) == 1
    assert len(rows) == 1
    assert rows[0][1] is None


def test_end_live_session_uses_retrying_transaction(tmp_path, monkeypatch):
    path = tmp_path / "end-live-session.sqlite3"
    _create_session_schema(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO live_sessions "
        "(tiktok_username, start_time, last_seen_at) "
        "VALUES (?, ?, ?)",
        ("sample_user", "2026-11-21 10:00:00", "2026-11-21 10:10:00"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))

    db.end_live_session(1, "2026-11-21 10:10:00")

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT end_time, duration_seconds FROM live_sessions "
            "WHERE session_id = 1"
        ).fetchone()
    finally:
        conn.close()

    assert row == ("2026-11-21 10:10:00", 600)


def test_update_live_session_host_uses_retrying_transaction(tmp_path, monkeypatch):
    path = tmp_path / "host-update.sqlite3"
    _create_session_schema(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO live_sessions "
        "(tiktok_username, start_time, scheduled_host, last_seen_at) "
        "VALUES (?, ?, ?, ?)",
        ("sample_user", "2026-11-21 10:00:00", "Old Host", "2026-11-21 10:00:00"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))

    assert db.update_live_session_host(1, "New Host") is True
    assert db.update_live_session_host(1, "Closed Host", allow_closed=True) is True

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT scheduled_host FROM live_sessions WHERE session_id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("Closed Host",)


def test_add_live_session_paused_seconds_uses_retrying_transaction(tmp_path, monkeypatch):
    path = tmp_path / "pause-update.sqlite3"
    _create_session_schema(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO live_sessions "
        "(tiktok_username, start_time, last_seen_at, paused_seconds) "
        "VALUES (?, ?, ?, ?)",
        ("sample_user", "2026-11-21 10:00:00", "2026-11-21 10:00:00", 5),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))

    assert db.add_live_session_paused_seconds(1, 30) == 35
    assert db.add_live_session_paused_seconds(1, -10) == 0

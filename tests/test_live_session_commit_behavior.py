import sqlite3
from datetime import datetime, timedelta

import pytz

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


def test_start_live_session_resume_commits_session_and_clears_resume_keys(
    tmp_path, monkeypatch
):
    path = tmp_path / "resume-live-session.sqlite3"

    _create_session_schema(path)
    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    last_seen = now - timedelta(seconds=120)
    last_seen_str = last_seen.strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO live_sessions "
            "(tiktok_username, start_time, end_time, last_seen_at, paused_seconds) "
            "VALUES (?, ?, ?, ?, ?)",
            ("sample_user", last_seen_str, last_seen_str, last_seen_str, 10),
        )
        conn.executemany(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
            [
                ("resume_session_sample_user", "1"),
                ("resume_last_seen_sample_user", last_seen_str),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))
    monkeypatch.setattr(
        db, "get_current_host_for_account", lambda *args, **kwargs: "Host"
    )

    assert db.start_live_session("sample_user") == 1

    conn = sqlite3.connect(path)
    try:
        session = conn.execute(
            "SELECT end_time, paused_seconds FROM live_sessions WHERE session_id = 1"
        ).fetchone()
        remaining_settings = conn.execute(
            "SELECT COUNT(*) FROM bot_settings WHERE key IN (?, ?)"
        , ("resume_session_sample_user", "resume_last_seen_sample_user")).fetchone()[0]
    finally:
        conn.close()

    assert session[0] is None
    assert session[1] >= 120
    assert remaining_settings == 0


def test_start_live_session_closes_stale_session_and_creates_new_one(
    tmp_path, monkeypatch
):
    path = tmp_path / "stale-live-session.sqlite3"

    _create_session_schema(path)
    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    last_seen = now - timedelta(seconds=db.LIVE_SESSION_STALE_SECONDS + 60)
    last_seen_str = last_seen.strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO live_sessions "
            "(tiktok_username, start_time, last_seen_at) VALUES (?, ?, ?)",
            ("sample_user", last_seen_str, last_seen_str),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))
    monkeypatch.setattr(
        db, "get_current_host_for_account", lambda *args, **kwargs: "Host"
    )

    new_session_id = db.start_live_session("sample_user")

    conn = sqlite3.connect(path)
    try:
        old_session = conn.execute(
            "SELECT end_time FROM live_sessions WHERE session_id = 1"
        ).fetchone()
        new_session = conn.execute(
            "SELECT end_time FROM live_sessions WHERE session_id = ?",
            (new_session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert new_session_id != 1
    assert old_session[0] == last_seen_str
    assert new_session[0] is None

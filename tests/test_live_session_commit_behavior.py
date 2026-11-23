import sqlite3
import time
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
            "SELECT COUNT(*) FROM bot_settings WHERE key IN (?, ?)",
            ("resume_session_sample_user", "resume_last_seen_sample_user"),
        ).fetchone()[0]
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


# Batch-path integration tests exercise the same read sequence used by Turso.
class _BatchConnection:
    """SQLite-backed stand-in for Turso's one-request batch API."""

    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.batch_calls = []

    def batch(self, statements, mode=None):
        self.batch_calls.append((statements, mode))
        if mode == "immediate":
            self.connection.execute("BEGIN IMMEDIATE")
        try:
            results = []
            for sql, parameters in statements:
                cursor = self.connection.execute(sql, parameters)
                rows = cursor.fetchall() if cursor.description else []
                results.append(
                    type(
                        "BatchResult",
                        (),
                        {"rows": rows, "lastrowid": cursor.lastrowid},
                    )()
                )
            if mode == "immediate":
                self.connection.commit()
            return results
        except Exception:
            if mode == "immediate":
                self.connection.rollback()
            raise

    def cursor(self):
        return self.connection.cursor()

    def commit(self):
        return self.connection.commit()

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        return self.connection.close()


def test_start_live_session_batches_remote_state_reads(tmp_path, monkeypatch):
    path = tmp_path / "batched-session-start.sqlite3"
    _create_session_schema(path)
    connection = _BatchConnection(path)
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)
    monkeypatch.setattr(
        db, "get_current_host_for_account", lambda *args, **kwargs: "Host A"
    )

    session_id = db.start_live_session("sample_user")

    assert session_id == 1
    assert len(connection.batch_calls) == 2
    read_statements, read_mode = connection.batch_calls[0]
    write_statements, write_mode = connection.batch_calls[1]
    assert len(read_statements) == 3
    assert read_mode is None
    assert len(write_statements) == 1
    assert write_mode == "immediate"

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT tiktok_username, end_time FROM live_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("sample_user", None)


def test_start_live_session_batches_resume_state_reads(tmp_path, monkeypatch):
    path = tmp_path / "batched-session-resume.sqlite3"
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

    connection = _BatchConnection(path)
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)
    monkeypatch.setattr(
        db, "get_current_host_for_account", lambda *args, **kwargs: "Host"
    )

    assert db.start_live_session("sample_user") == 1
    assert len(connection.batch_calls) == 2
    assert len(connection.batch_calls[0][0]) == 3
    assert connection.batch_calls[0][1] is None
    assert len(connection.batch_calls[1][0]) == 2
    assert connection.batch_calls[1][1] == "immediate"

    conn = sqlite3.connect(path)
    try:
        session = conn.execute(
            "SELECT end_time, paused_seconds FROM live_sessions WHERE session_id = 1"
        ).fetchone()
        remaining_settings = conn.execute(
            "SELECT COUNT(*) FROM bot_settings WHERE key IN (?, ?)",
            ("resume_session_sample_user", "resume_last_seen_sample_user"),
        ).fetchone()[0]
    finally:
        conn.close()

    assert session[0] is None
    assert session[1] >= 120
    assert remaining_settings == 0


def test_start_live_session_batches_existing_session_touch_and_host_update(
    tmp_path, monkeypatch
):
    path = tmp_path / "batched-active-session.sqlite3"
    _create_session_schema(path)
    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    seen = (now - timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO live_sessions "
            "(tiktok_username, start_time, scheduled_host, last_seen_at) "
            "VALUES (?, ?, ?, ?)",
            ("sample_user", seen, "Old Host", seen),
        )
        conn.commit()
    finally:
        conn.close()

    connection = _BatchConnection(path)
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)

    assert db.start_live_session("sample_user", initial_host="New Host") == 1

    assert len(connection.batch_calls) == 2
    assert connection.batch_calls[1][1] == "immediate"
    assert len(connection.batch_calls[1][0]) == 1

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT scheduled_host, last_seen_at FROM live_sessions WHERE session_id = 1"
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM live_sessions").fetchone()[0]
    finally:
        conn.close()
    assert row[0] == "New Host"
    assert row[1] >= seen
    assert count == 1


def test_start_live_session_batches_stale_close_and_new_insert(
    tmp_path, monkeypatch
):
    path = tmp_path / "batched-stale-session.sqlite3"
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

    connection = _BatchConnection(path)
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)
    monkeypatch.setattr(
        db, "get_current_host_for_account", lambda *args, **kwargs: "Host"
    )

    new_session_id = db.start_live_session("sample_user")

    assert new_session_id == 2
    assert len(connection.batch_calls) == 2
    assert connection.batch_calls[1][1] == "immediate"
    assert len(connection.batch_calls[1][0]) == 2

    conn = sqlite3.connect(path)
    try:
        old_session = conn.execute(
            "SELECT end_time FROM live_sessions WHERE session_id = 1"
        ).fetchone()
        new_session = conn.execute(
            "SELECT end_time FROM live_sessions WHERE session_id = 2"
        ).fetchone()
    finally:
        conn.close()
    assert old_session[0] == last_seen_str
    assert new_session[0] is None


def test_atomic_live_session_batch_rolls_back_when_a_statement_fails(tmp_path):
    path = tmp_path / "atomic-session-write.sqlite3"
    connection = _BatchConnection(path)
    connection.connection.execute(
        "CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)"
    )
    connection.connection.commit()

    try:
        db._execute_atomic_batch(
            connection,
            [
                ("INSERT INTO sample (id, value) VALUES (?, ?)", (1, "first")),
                ("INSERT INTO sample (id, value) VALUES (?, ?)", (1, "duplicate")),
            ],
        )
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate insert should fail the atomic batch")

    count = connection.connection.execute(
        "SELECT COUNT(*) FROM sample"
    ).fetchone()[0]
    assert count == 0
    connection.close()


def test_start_live_session_batches_duplicate_active_session_cleanup(
    tmp_path, monkeypatch
):
    path = tmp_path / "batched-duplicate-sessions.sqlite3"
    _create_session_schema(path)
    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    seen = (now - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO live_sessions "
            "(tiktok_username, start_time, last_seen_at) VALUES (?, ?, ?)",
            [
                ("sample_user", seen, seen),
                ("sample_user", seen, seen),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    connection = _BatchConnection(path)
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)
    monkeypatch.setattr(
        db, "get_current_host_for_account", lambda *args, **kwargs: "Host"
    )

    assert db.start_live_session("sample_user") == 2

    assert len(connection.batch_calls) == 2
    assert connection.batch_calls[1][1] == "immediate"
    assert len(connection.batch_calls[1][0]) == 2

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT session_id, end_time FROM live_sessions ORDER BY session_id"
        ).fetchall()
    finally:
        conn.close()
    assert rows[0][1] is not None
    assert rows[1][1] is None


def test_start_live_session_batches_expired_resume_cleanup_with_insert(
    tmp_path, monkeypatch
):
    path = tmp_path / "batched-expired-resume.sqlite3"
    _create_session_schema(path)
    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    last_seen = now - timedelta(seconds=db.LIVE_RESUME_GRACE_SECONDS + 60)
    last_seen_str = last_seen.strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO live_sessions "
            "(tiktok_username, start_time, end_time, last_seen_at) "
            "VALUES (?, ?, ?, ?)",
            ("sample_user", last_seen_str, last_seen_str, last_seen_str),
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

    connection = _BatchConnection(path)
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)
    monkeypatch.setattr(
        db, "get_current_host_for_account", lambda *args, **kwargs: "Host"
    )

    assert db.start_live_session("sample_user") == 2

    assert len(connection.batch_calls) == 2
    assert connection.batch_calls[1][1] == "immediate"
    assert len(connection.batch_calls[1][0]) == 2

    conn = sqlite3.connect(path)
    try:
        settings_count = conn.execute(
            "SELECT COUNT(*) FROM bot_settings WHERE key IN (?, ?)",
            ("resume_session_sample_user", "resume_last_seen_sample_user"),
        ).fetchone()[0]
        sessions = conn.execute(
            "SELECT session_id, end_time FROM live_sessions ORDER BY session_id"
        ).fetchall()
    finally:
        conn.close()
    assert settings_count == 0
    assert sessions == [(1, last_seen_str), (2, None)]


def test_start_live_session_reports_read_and_write_batch_timings(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "timed-live-session.sqlite3"
    _create_session_schema(path)
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))
    monkeypatch.setattr(
        db, "get_current_host_for_account", lambda *args, **kwargs: "Host A"
    )

    def slow_state_read(*_args):
        time.sleep(0.11)
        return {}, None, []

    original_batch = db._execute_atomic_batch

    def slow_write_batch(conn, statements):
        time.sleep(0.11)
        return original_batch(conn, statements)

    monkeypatch.setattr(db, "_load_live_session_start_state", slow_state_read)
    monkeypatch.setattr(db, "_execute_atomic_batch", slow_write_batch)

    assert db.start_live_session("sample_user") == 1

    output = capsys.readouterr().out
    assert "operation=live_session_start stage=state_read_batch" in output
    assert "operation=live_session_start stage=write_batch" in output


def test_touch_live_session_throttles_writes_to_ninety_seconds(
    tmp_path, monkeypatch
):
    path = tmp_path / "touch-live-session.sqlite3"
    _create_session_schema(path)
    setup = sqlite3.connect(path)
    try:
        setup.execute(
            "INSERT INTO live_sessions (tiktok_username, start_time) VALUES (?, ?)",
            ("sample_user", "2026-11-23 10:00:00"),
        )
        setup.commit()
    finally:
        setup.close()

    now = [1000.0]
    calls = []

    def fake_write_transaction(operation, timing_label=None):
        connection = sqlite3.connect(path)
        try:
            operation(connection)
            connection.commit()
        finally:
            connection.close()
        calls.append(timing_label)

    monkeypatch.setattr(db, "write_transaction", fake_write_transaction)
    monkeypatch.setattr(db.time, "time", lambda: now[0])
    monkeypatch.setattr(db, "_TOUCH_LIVE_CACHE", {})
    monkeypatch.setattr(db, "_TOUCH_LIVE_MIN_INTERVAL", 90.0)

    db.touch_live_session(1)
    now[0] += 89
    db.touch_live_session(1)
    assert calls == ["live_session_touch"]

    now[0] += 1
    db.touch_live_session(1)
    assert calls == ["live_session_touch", "live_session_touch"]


def test_touch_live_session_explicit_timestamp_bypasses_throttle(
    tmp_path, monkeypatch
):
    path = tmp_path / "touch-live-session-explicit.sqlite3"
    _create_session_schema(path)
    setup = sqlite3.connect(path)
    try:
        setup.execute(
            "INSERT INTO live_sessions (tiktok_username, start_time) VALUES (?, ?)",
            ("sample_user", "2026-11-23 10:00:00"),
        )
        setup.commit()
    finally:
        setup.close()

    calls = []

    def fake_write_transaction(operation, timing_label=None):
        connection = sqlite3.connect(path)
        try:
            operation(connection)
            connection.commit()
        finally:
            connection.close()
        calls.append(timing_label)

    monkeypatch.setattr(db, "write_transaction", fake_write_transaction)
    monkeypatch.setattr(db, "_TOUCH_LIVE_CACHE", {})
    monkeypatch.setattr(db, "_TOUCH_LIVE_MIN_INTERVAL", 90.0)

    db.touch_live_session(1, when="2026-11-23 10:01:00")
    db.touch_live_session(1, when="2026-11-23 10:02:00")

    assert calls == ["live_session_touch", "live_session_touch"]



def test_touch_live_session_allows_retry_after_write_failure(monkeypatch):
    now = [1000.0]
    calls = []

    def flaky_write_transaction(operation, timing_label=None):
        calls.append(timing_label)
        if len(calls) == 1:
            raise RuntimeError("temporary database failure")

    monkeypatch.setattr(db, "write_transaction", flaky_write_transaction)
    monkeypatch.setattr(db.time, "time", lambda: now[0])
    monkeypatch.setattr(db, "_TOUCH_LIVE_CACHE", {})
    monkeypatch.setattr(db, "_TOUCH_LIVE_MIN_INTERVAL", 90.0)

    db.touch_live_session(42)
    db.touch_live_session(42)

    assert calls == ["live_session_touch", "live_session_touch"]
    assert db._TOUCH_LIVE_CACHE[42] == now[0]

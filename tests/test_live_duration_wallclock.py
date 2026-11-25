import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timedelta

import pytz
from flask import Flask

import db
from routes import api


def _client():
    app = Flask(__name__)
    app.register_blueprint(api.api_bp)
    app.config.update(TESTING=True)
    return app.test_client()


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


def _wib_str(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def test_end_live_session_uses_wall_clock_not_last_heartbeat(tmp_path, monkeypatch):
    """Regresi: durasi bot 5 menit lebih pendek karena clamp ke last_seen_at."""
    path = tmp_path / "wallclock.sqlite3"
    _create_session_schema(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO live_sessions "
        "(tiktok_username, start_time, last_seen_at, paused_seconds) "
        "VALUES (?, ?, ?, ?)",
        ("creator", "2026-11-25 10:00:00", "2026-11-25 10:55:00", 300),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))

    # Chat/gift berhenti pukul 10.55, tapi Studio baru selesai 11.00.
    db.end_live_session(1, "2026-11-25 11:00:00")

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT end_time, duration_seconds, paused_seconds FROM live_sessions WHERE session_id = 1"
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == "2026-11-25 11:00:00"
    # Wall-clock Studio = 60 menit. paused_seconds tetap tersimpan sebagai info.
    assert row[1] == 3600
    assert row[2] == 300


def test_start_live_session_uses_webhook_event_start_time(tmp_path, monkeypatch):
    """Webhook yang terlambat tidak boleh memperl pendek durasi dari awal live."""
    path = tmp_path / "event-start.sqlite3"
    _create_session_schema(path)
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))
    monkeypatch.setattr(db, "get_current_host_for_account", lambda *a, **k: "Host")

    wib = pytz.timezone("Asia/Jakarta")
    event_start = datetime.now(wib).replace(microsecond=0) - timedelta(minutes=5)

    db.start_live_session("creator", started_at=_wib_str(event_start))

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT start_time, end_time FROM live_sessions WHERE tiktok_username = 'creator'"
        ).fetchone()
    finally:
        conn.close()

    assert row[0] == _wib_str(event_start)
    assert row[1] is None


def test_extract_euler_event_time_supports_epoch_ms_iso_and_nested():
    wib = pytz.timezone("Asia/Jakarta")
    now = datetime.now(wib)

    epoch_ms = int(now.timestamp() * 1000)
    parsed = api._parse_webhook_datetime(epoch_ms, wib)
    assert parsed is not None
    assert abs((now - parsed).total_seconds()) < 5

    assert api._extract_euler_event_time({"timestamp": epoch_ms})
    assert api._extract_euler_event_time(
        {"data": {"event": {"created_at": now.isoformat()}}}
    )
    assert api._extract_euler_event_time(
        {"event_time": now.strftime("%Y-%m-%d %H:%M:%S")}
    )
    # Field tanpa tahu waktu yang masuk akal harus diabaikan.
    assert api._extract_euler_event_time({"time": "not-a-date"}) == ""


def test_euler_webhook_offline_forwards_event_time(monkeypatch):
    secret = "webhook-secret"
    wib = pytz.timezone("Asia/Jakarta")
    event_dt = datetime.now(wib).replace(microsecond=0) - timedelta(minutes=5)
    payload = {
        "username": "@creator",
        "is_live": False,
        "timestamp": int(event_dt.timestamp() * 1000),
    }
    raw_body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    ended = []
    monkeypatch.setenv("EULER_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(api, "get_open_live_session_id", lambda username: 42)
    monkeypatch.setattr(
        api, "end_live_session", lambda session_id, end_time=None: ended.append((session_id, end_time))
    )
    monkeypatch.setattr(api, "clear_live", lambda username: None)
    monkeypatch.setattr(api, "update_observer_diag", lambda *a, **k: None)
    monkeypatch.setattr(api, "log_activity_event", lambda *a, **k: None)
    monkeypatch.setattr(api, "_trigger_announcement_async", lambda *a, **k: True)

    response = _client().post(
        "/api/webhooks/eulerstream",
        data=raw_body,
        content_type="application/json",
        headers={"X-Webhook-Signature": signature},
    )

    assert response.status_code == 200
    assert ended == [(42, _wib_str(event_dt))]

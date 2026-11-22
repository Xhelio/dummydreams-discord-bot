import sqlite3

import db


def _connection_factory(path):
    return lambda: sqlite3.connect(path, timeout=5)


def _create_schema(path, legacy=False):
    conn = sqlite3.connect(path)
    try:
        primary_key = "tiktok_username" if legacy else "tiktok_username, channel_id"
        conn.execute(
            f"""
            CREATE TABLE active_live_announcements (
                tiktok_username TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                host_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY ({primary_key})
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_announcement_save_update_and_delete_use_transaction_wrapper(tmp_path, monkeypatch):
    path = tmp_path / "announcements.sqlite3"
    _create_schema(path)
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))

    assert db.save_active_live_announcement(
        "@sample_user", 123, 456, "Host A"
    ) is True
    db.update_active_live_announcement_host("sample_user", "Host B")

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT tiktok_username, channel_id, message_id, host_name "
            "FROM active_live_announcements"
        ).fetchone()
    finally:
        conn.close()

    assert row == ("sample_user", "123", "456", "Host B")

    db.delete_active_live_announcement("sample_user", 123)
    conn = sqlite3.connect(path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM active_live_announcements"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_announcement_save_preserves_legacy_schema_fallback(tmp_path, monkeypatch):
    path = tmp_path / "legacy-announcements.sqlite3"
    _create_schema(path, legacy=True)
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))

    assert db.save_active_live_announcement(
        "sample_user", 123, 456, "Host A"
    ) is True

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT tiktok_username, channel_id, message_id, host_name "
            "FROM active_live_announcements"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("sample_user", "123", "456", "Host A")


def test_announcement_delete_all_sessions_for_account(tmp_path, monkeypatch):
    path = tmp_path / "announcement-delete-all.sqlite3"
    _create_schema(path)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO active_live_announcements "
        "(tiktok_username, channel_id, message_id, host_name) VALUES (?, ?, ?, ?)",
        [("sample_user", "1", "10", "Host A"), ("sample_user", "2", "20", "Host A")],
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))

    db.delete_active_live_announcement("sample_user")

    conn = sqlite3.connect(path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM active_live_announcements"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 0

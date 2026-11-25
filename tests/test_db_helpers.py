import db


def test_transient_database_error_detection():
    assert db.is_transient_db_error(RuntimeError("database is locked"))
    assert db.is_transient_db_error(TimeoutError("request timed out"))
    assert not db.is_transient_db_error(ValueError("invalid input"))


def test_write_transaction_commits_and_closes_connection(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.committed = False
            self.closed = False

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

        def rollback(self):
            raise AssertionError("rollback should not be called after success")

    connection = FakeConnection()
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)

    result = db.write_transaction(lambda conn: "saved")

    assert result == "saved"
    assert connection.committed
    assert connection.closed


def test_write_transaction_rolls_back_on_failure(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.rolled_back = False
            self.closed = False

        def commit(self):
            raise AssertionError("commit should not be called")

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    connection = FakeConnection()
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)

    def fail(_connection):
        raise ValueError("invalid write")

    import pytest

    with pytest.raises(ValueError, match="invalid write"):
        db.write_transaction(fail)

    assert connection.rolled_back
    assert connection.closed


def test_sync_valid_hosts_retires_only_known_legacy_official_handle(monkeypatch):
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE hosts (host_id INTEGER PRIMARY KEY, tiktok_username TEXT UNIQUE, "
        "display_name TEXT, is_active INTEGER DEFAULT 1, is_priority INTEGER DEFAULT 1)"
    )
    connection.executemany(
        "INSERT INTO hosts (host_id, tiktok_username, display_name, is_active) "
        "VALUES (?, ?, ?, ?)",
        [
            (1, "dummy.dreams", "Dummy Dreams", 1),
            (7, "dummydreamsentertaiment", "Old Dummy Dreams", 1),
            (8, "custom.creator", "Custom Creator", 1),
        ],
    )
    monkeypatch.setattr(db, "VALID_TARGET_ACCOUNTS", [(1, "dummy.dreams", "Dummy Dreams")])

    db.sync_valid_hosts(connection)

    states = dict(
        connection.execute("SELECT tiktok_username, is_active FROM hosts").fetchall()
    )
    assert states["dummy.dreams"] == 1
    assert states["dummydreamsentertaiment"] == 0
    assert states["custom.creator"] == 1
    # The old row remains available for historical joins; it is not deleted.
    assert connection.execute(
        "SELECT COUNT(*) FROM hosts WHERE tiktok_username = ?",
        ("dummydreamsentertaiment",),
    ).fetchone()[0] == 1
    connection.close()

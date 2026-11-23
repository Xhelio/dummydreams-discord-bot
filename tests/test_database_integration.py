import sqlite3

import pytest

import db


def _sqlite_connection_factory(database_path):
    return lambda: sqlite3.connect(database_path)


def test_local_database_connection_uses_configured_path(tmp_path, monkeypatch):
    database_path = tmp_path / "configured.sqlite3"
    monkeypatch.setattr(db, "HAS_LIBSQL", False)
    monkeypatch.setattr(db, "TURSO_URL", None)
    monkeypatch.setenv("LOCAL_DB_PATH", str(database_path))

    connection = db.get_db_connection()
    try:
        connection.execute("CREATE TABLE connection_probe (value TEXT)")
        connection.execute(
            "INSERT INTO connection_probe (value) VALUES (?)", ("connected",)
        )
        connection.commit()
        row = connection.execute(
            "SELECT value FROM connection_probe"
        ).fetchone()
    finally:
        connection.close()

    assert row == ("connected",)
    assert database_path.exists()


def test_write_transaction_commits_callback_changes(tmp_path, monkeypatch):
    database_path = tmp_path / "transactions.sqlite3"
    monkeypatch.setattr(db, "get_db_connection",
                        _sqlite_connection_factory(database_path))

    def insert_record(connection):
        connection.execute("CREATE TABLE records (value TEXT)")
        connection.execute(
            "INSERT INTO records (value) VALUES (?)", ("committed",)
        )
        return "saved"

    result = db.write_transaction(insert_record)

    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute("SELECT value FROM records").fetchall()
    finally:
        connection.close()

    assert result == "saved"
    assert rows == [("committed",)]


def test_write_transaction_rolls_back_callback_failure(tmp_path, monkeypatch):
    database_path = tmp_path / "rollback.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE records (value TEXT)")
    connection.commit()
    connection.close()
    monkeypatch.setattr(db, "get_db_connection",
                        _sqlite_connection_factory(database_path))

    def failing_operation(connection):
        connection.execute(
            "INSERT INTO records (value) VALUES (?)", ("must-rollback",)
        )
        raise ValueError("invalid operation")

    with pytest.raises(ValueError, match="invalid operation"):
        db.write_transaction(failing_operation)

    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute("SELECT value FROM records").fetchall()
    finally:
        connection.close()

    assert rows == []


def test_write_transaction_retries_transient_error_with_fresh_connection(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "retry.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("CREATE TABLE records (value TEXT)")
    connection.commit()
    connection.close()
    monkeypatch.setattr(db, "get_db_connection",
                        _sqlite_connection_factory(database_path))
    attempts = []

    def flaky_operation(connection):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            connection.execute(
                "INSERT INTO records (value) VALUES (?)", ("rolled-back",)
            )
            raise sqlite3.OperationalError("database is locked")
        connection.execute(
            "INSERT INTO records (value) VALUES (?)", ("committed-on-retry",)
        )
        return "retried"

    result = db.write_transaction(flaky_operation, attempts=3, base_delay=0)

    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute("SELECT value FROM records").fetchall()
    finally:
        connection.close()

    assert result == "retried"
    assert attempts == [1, 2]
    assert rows == [("committed-on-retry",)]


def test_write_transaction_does_not_retry_permanent_errors(tmp_path, monkeypatch):
    database_path = tmp_path / "no-retry.sqlite3"
    monkeypatch.setattr(db, "get_db_connection",
                        _sqlite_connection_factory(database_path))
    calls = []

    def failing_operation(connection):
        calls.append(True)
        raise ValueError("permanent failure")

    with pytest.raises(ValueError, match="permanent failure"):
        db.write_transaction(failing_operation, attempts=4, base_delay=0)

    assert len(calls) == 1


def test_database_health_checks_connection_and_uses_short_cache(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "health.sqlite3"
    monkeypatch.setattr(db, "get_db_connection",
                        _sqlite_connection_factory(database_path))
    clock = [100.0]
    monkeypatch.setattr(db.time, "time", lambda: clock[0])
    monkeypatch.setattr(db, "_DB_HEALTH_CACHE", {"ok": True, "at": 0.0})

    assert db.check_database_health() is True

    def unavailable_connection():
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(db, "get_db_connection", unavailable_connection)
    clock[0] += 5

    # A recent successful probe is reused without opening another connection.
    assert db.check_database_health() is True

    scheduled = []
    monkeypatch.setattr(db, "_DB_HEALTH_REFRESHING", False)
    monkeypatch.setattr(
        db,
        "_schedule_database_health_refresh",
        lambda: scheduled.append(db._refresh_database_health),
    )

    clock[0] += 6
    # The stale successful result is served while the refresh runs off-thread.
    assert db.check_database_health() is True
    assert len(scheduled) == 1

    scheduled[0]()
    assert db.check_database_health() is False


def test_sync_valid_hosts_preserves_admin_disabled_hosts_unless_reactivated(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "hosts.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("""
        CREATE TABLE hosts (
            host_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tiktok_username TEXT UNIQUE,
            display_name TEXT,
            is_active INTEGER
        )
    """)
    connection.execute(
        "INSERT INTO hosts (host_id, tiktok_username, display_name, is_active) "
        "VALUES (10, 'official_a', 'Custom Display', 0)"
    )
    connection.execute(
        "INSERT INTO hosts (tiktok_username, display_name, is_active) "
        "VALUES ('my_custom_host', 'My Custom Host', 1)"
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(db, "get_db_connection",
                        _sqlite_connection_factory(database_path))
    monkeypatch.setattr(db, "VALID_TARGET_ACCOUNTS", [
        (1, "official_a", "Official A"),
        (2, "official_b", "Official B"),
    ])

    db.sync_valid_hosts()

    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT tiktok_username, display_name, is_active FROM hosts "
            "ORDER BY tiktok_username"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [
        ("my_custom_host", "My Custom Host", 1),
        ("official_a", "Custom Display", 0),
        ("official_b", "Official B", 1),
    ]

    db.sync_valid_hosts(reactivate=True)

    connection = sqlite3.connect(database_path)
    try:
        official = connection.execute(
            "SELECT display_name, is_active FROM hosts "
            "WHERE tiktok_username = 'official_a'"
        ).fetchone()
        custom = connection.execute(
            "SELECT is_active FROM hosts WHERE tiktok_username = 'my_custom_host'"
        ).fetchone()
    finally:
        connection.close()

    assert official == ("Custom Display", 1)
    assert custom == (1,)


class _TrackingLock:
    def __init__(self):
        self.held = False

    def acquire(self, blocking=True, timeout=-1):
        assert not self.held
        self.held = True
        return True

    def release(self):
        assert self.held
        self.held = False


class _CloseObservingConnection:
    def __init__(self, lock, rollback_error=False):
        self.lock = lock
        self.rollback_error = rollback_error
        self.lock_state_during_close = None

    def commit(self):
        return None

    def rollback(self):
        if self.rollback_error:
            raise RuntimeError("rollback failed")

    def close(self):
        self.lock_state_during_close = self.lock.held


def test_write_transaction_releases_lock_before_successful_close(monkeypatch):
    lock = _TrackingLock()
    connection = _CloseObservingConnection(lock)
    monkeypatch.setattr(db, "_DB_WRITE_LOCK", lock)
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)

    assert db.write_transaction(lambda _conn: "saved") == "saved"

    assert connection.lock_state_during_close is False
    assert lock.held is False


def test_write_transaction_keeps_lock_until_close_if_rollback_fails(monkeypatch):
    lock = _TrackingLock()
    connection = _CloseObservingConnection(lock, rollback_error=True)
    monkeypatch.setattr(db, "_DB_WRITE_LOCK", lock)
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)

    import pytest

    with pytest.raises(ValueError, match="operation failed"):
        db.write_transaction(
            lambda _conn: (_ for _ in ()).throw(ValueError("operation failed"))
        )

    assert connection.lock_state_during_close is True
    assert lock.held is False



def test_sync_valid_hosts_batches_existing_host_updates(tmp_path, monkeypatch):
    database_path = tmp_path / "hosts-batched.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("""
        CREATE TABLE hosts (
            host_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tiktok_username TEXT UNIQUE,
            display_name TEXT,
            is_active INTEGER
        )
    """)
    connection.executemany(
        "INSERT INTO hosts (host_id, tiktok_username, display_name, is_active) "
        "VALUES (?, ?, ?, ?)",
        [
            (10, "official_a", "", 0),
            (11, "official_b", "Admin Display", 0),
        ],
    )
    connection.commit()
    connection.close()

    statements = []

    def tracked_connection():
        connection = sqlite3.connect(database_path)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(db, "get_db_connection", tracked_connection)
    monkeypatch.setattr(db, "VALID_TARGET_ACCOUNTS", [
        (1, "official_a", "Official A"),
        (2, "official_b", "Official B"),
    ])

    db.sync_valid_hosts()

    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT tiktok_username, display_name, is_active "
            "FROM hosts ORDER BY tiktok_username"
        ).fetchall()
    finally:
        connection.close()

    selects = [
        sql for sql in statements
        if sql.lstrip().upper().startswith("SELECT HOST_ID")
    ]
    updates = [
        sql for sql in statements
        if sql.lstrip().upper().startswith("UPDATE HOSTS")
    ]

    assert rows == [
        ("official_a", "Official A", 0),
        ("official_b", "Admin Display", 0),
    ]
    assert len(selects) == 1
    assert len(updates) == 1


def test_sync_valid_hosts_reactivates_all_existing_hosts_in_one_update(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "hosts-reactivated.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("""
        CREATE TABLE hosts (
            host_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tiktok_username TEXT UNIQUE,
            display_name TEXT,
            is_active INTEGER
        )
    """)
    connection.executemany(
        "INSERT INTO hosts (host_id, tiktok_username, display_name, is_active) "
        "VALUES (?, ?, ?, ?)",
        [
            (10, "official_a", "Official A", 0),
            (11, "official_b", "Official B", 0),
        ],
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(db, "get_db_connection", _sqlite_connection_factory(database_path))
    monkeypatch.setattr(db, "VALID_TARGET_ACCOUNTS", [
        (1, "official_a", "Official A"),
        (2, "official_b", "Official B"),
    ])

    db.sync_valid_hosts(reactivate=True)

    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            "SELECT is_active FROM hosts ORDER BY tiktok_username"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [(1,), (1,)]

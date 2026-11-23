import threading
import time
from concurrent.futures import ThreadPoolExecutor

import db


class _FakeConnection:
    def __init__(self, *, rollback_fails=False):
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0
        self.rollback_fails = rollback_fails

    def execute(self, *_args, **_kwargs):
        return "ok"

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1
        if self.rollback_fails:
            raise RuntimeError("rollback failed")

    def close(self):
        self.close_count += 1


class OperationalError(Exception):
    pass


def _enable_persistent_turso(monkeypatch):
    monkeypatch.setattr(db, "HAS_LIBSQL", True)
    monkeypatch.setattr(db, "TURSO_URL", "libsql://unit-test")
    monkeypatch.setattr(db, "_DB_WRITE_CONNECTION", None)


def test_write_transaction_reuses_turso_connection_until_shutdown(monkeypatch):
    _enable_persistent_turso(monkeypatch)
    connections = []

    def connect():
        connection = _FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(db, "get_db_connection", connect)

    assert db.write_transaction(lambda conn: conn.execute("UPDATE")) == "ok"
    assert db.write_transaction(lambda conn: conn.execute("UPDATE")) == "ok"

    assert len(connections) == 1
    assert connections[0].commit_count == 2
    assert connections[0].close_count == 0

    db._close_persistent_write_connection()
    assert connections[0].close_count == 1
    assert db._DB_WRITE_CONNECTION is None


def test_expired_turso_stream_404_discards_connection_and_retries(monkeypatch):
    _enable_persistent_turso(monkeypatch)
    first = _FakeConnection()
    second = _FakeConnection()
    connections = iter([first, second])
    monkeypatch.setattr(db, "get_db_connection", lambda: next(connections))

    calls = 0

    def operation(_conn):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("HTTP status 404")
        return "recovered"

    result = db.write_transaction(operation, attempts=2, base_delay=0)

    assert result == "recovered"
    assert calls == 2
    assert first.rollback_count == 1
    assert first.close_count == 1
    assert second.commit_count == 1
    assert second.close_count == 0

    db._close_persistent_write_connection()
    assert second.close_count == 1


def test_failed_transaction_discards_cached_connection(monkeypatch):
    _enable_persistent_turso(monkeypatch)
    connections = []

    def connect():
        connection = _FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(db, "get_db_connection", connect)

    try:
        db.write_transaction(
            lambda _conn: (_ for _ in ()).throw(ValueError("invalid write"))
        )
    except ValueError:
        pass
    else:
        raise AssertionError("non-transient operation error should propagate")

    assert connections[0].rollback_count == 1
    assert connections[0].close_count == 1
    assert db._DB_WRITE_CONNECTION is None

    db.write_transaction(lambda conn: conn.execute("UPDATE"))
    assert len(connections) == 2
    assert connections[1].commit_count == 1

    db._close_persistent_write_connection()


def test_failed_rollback_discards_connection_and_releases_lock(monkeypatch):
    _enable_persistent_turso(monkeypatch)
    broken = _FakeConnection(rollback_fails=True)
    replacement = _FakeConnection()
    connections = iter([broken, replacement])
    monkeypatch.setattr(db, "get_db_connection", lambda: next(connections))

    try:
        db.write_transaction(
            lambda _conn: (_ for _ in ()).throw(ValueError("write failed"))
        )
    except ValueError:
        pass
    else:
        raise AssertionError("operation error should propagate")

    assert broken.rollback_count == 1
    assert broken.close_count == 1
    assert db._DB_WRITE_CONNECTION is None

    assert db.write_transaction(lambda conn: conn.execute("UPDATE")) == "ok"
    assert replacement.commit_count == 1

    db._close_persistent_write_connection()


def test_persistent_turso_connection_is_serialized_across_threads(monkeypatch):
    _enable_persistent_turso(monkeypatch)
    connections = []
    active = 0
    max_active = 0
    counter_lock = threading.Lock()

    def connect():
        connection = _FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(db, "get_db_connection", connect)

    def operation(_conn):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with counter_lock:
            active -= 1
        return "ok"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: db.write_transaction(operation), range(8)))

    assert results == ["ok"] * 8
    assert len(connections) == 1
    assert max_active == 1
    assert connections[0].commit_count == 8
    assert connections[0].close_count == 0

    db._close_persistent_write_connection()


def test_sqlite_fallback_keeps_per_transaction_connections(monkeypatch):
    monkeypatch.setattr(db, "HAS_LIBSQL", False)
    monkeypatch.setattr(db, "TURSO_URL", "libsql://unused")
    monkeypatch.setattr(db, "_DB_WRITE_CONNECTION", None)
    connections = []

    def connect():
        connection = _FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(db, "get_db_connection", connect)

    db.write_transaction(lambda conn: conn.execute("UPDATE"))
    db.write_transaction(lambda conn: conn.execute("UPDATE"))

    assert len(connections) == 2
    assert [connection.close_count for connection in connections] == [1, 1]
    assert db._DB_WRITE_CONNECTION is None

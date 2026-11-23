import db
import pytest
import server


@pytest.mark.parametrize(
    ("database_ok", "expected_status"),
    [(True, 200), (False, 503)],
)
def test_health_uses_cached_database_health_helper(
    monkeypatch, database_ok, expected_status
):
    calls = []

    def fake_health_check():
        calls.append(True)
        return database_ok

    def unexpected_connection():
        raise AssertionError("/health must not open its own database connection")

    monkeypatch.setattr(db, "check_database_health", fake_health_check)
    monkeypatch.setattr(db, "get_db_connection", unexpected_connection)

    response = server.app.test_client().get("/health")

    assert response.status_code == expected_status
    assert response.get_json()["ok"] is database_ok
    assert response.get_json()["db"] is database_ok
    assert calls == [True]
    assert {
        "bot_ready",
        "bot_user",
        "env",
        "version",
        "live_tracked",
        "boot_id",
        "started_at",
        "admin_auth",
    }.issubset(response.get_json())

@pytest.mark.parametrize("cached_value", [True, False])
def test_database_health_helper_reuses_fresh_cached_result(
    monkeypatch, cached_value
):
    import time

    monkeypatch.setattr(
        db,
        "_DB_HEALTH_CACHE",
        {"ok": cached_value, "at": time.time()},
    )

    def unexpected_connection():
        raise AssertionError("a fresh health-cache entry must avoid a DB connection")

    monkeypatch.setattr(db, "get_db_connection", unexpected_connection)

    assert db.check_database_health() is cached_value


def test_concurrent_health_cache_misses_share_one_database_probe(monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(db, "_DB_HEALTH_CACHE", {"ok": True, "at": 0.0})
    monkeypatch.setattr(db, "_DB_HEALTH_LOCK", threading.Lock())
    callers_ready = threading.Barrier(3)
    call_count = 0
    close_count = 0
    count_lock = threading.Lock()

    class FakeConnection:
        def cursor(self):
            return self

        def execute(self, _sql):
            time.sleep(0.05)

        def fetchone(self):
            return (1,)

        def close(self):
            nonlocal close_count
            with count_lock:
                close_count += 1

    def connect():
        nonlocal call_count
        with count_lock:
            call_count += 1
        return FakeConnection()

    monkeypatch.setattr(db, "get_db_connection", connect)

    def check():
        callers_ready.wait()
        return db.check_database_health()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(check) for _ in range(2)]
        callers_ready.wait()
        assert [future.result() for future in futures] == [True, True]

    assert call_count == 1
    assert close_count == 1


def test_database_health_closes_connection_when_probe_fails(monkeypatch):
    import threading

    monkeypatch.setattr(db, "_DB_HEALTH_CACHE", {"ok": True, "at": 0.0})
    monkeypatch.setattr(db, "_DB_HEALTH_LOCK", threading.Lock())

    class BrokenConnection:
        closed = False

        def cursor(self):
            return self

        def execute(self, _sql):
            raise RuntimeError("probe failed")

        def close(self):
            self.closed = True

    connection = BrokenConnection()
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)

    assert db.check_database_health() is False
    assert connection.closed is True

def test_stale_database_health_cache_refreshes_in_background(monkeypatch):
    import threading
    import time

    monkeypatch.setattr(
        db, "_DB_HEALTH_CACHE", {"ok": True, "at": time.time() - 11}
    )
    monkeypatch.setattr(db, "_DB_HEALTH_LOCK", threading.Lock())
    monkeypatch.setattr(db, "_DB_HEALTH_REFRESHING", False)
    scheduled = []
    monkeypatch.setattr(
        db,
        "_schedule_database_health_refresh",
        lambda: scheduled.append(db._refresh_database_health),
    )

    class BrokenConnection:
        closed = False

        def cursor(self):
            return self

        def execute(self, _sql):
            raise RuntimeError("probe failed")

        def close(self):
            self.closed = True

    connection = BrokenConnection()
    monkeypatch.setattr(db, "get_db_connection", lambda: connection)

    # Expired-but-recent cache is returned immediately; concurrent requests
    # do not start another probe while the background refresh is scheduled.
    assert db.check_database_health() is True
    assert db.check_database_health() is True
    assert len(scheduled) == 1

    scheduled[0]()

    assert db._DB_HEALTH_CACHE["ok"] is False
    assert db._DB_HEALTH_CACHE["at"] > 0
    assert db._DB_HEALTH_REFRESHING is False
    assert connection.closed is True


def test_stale_database_health_cache_fails_closed_after_max_age(monkeypatch):
    import threading
    import time

    monkeypatch.setattr(
        db, "_DB_HEALTH_CACHE", {"ok": True, "at": time.time() - 31}
    )
    monkeypatch.setattr(db, "_DB_HEALTH_LOCK", threading.Lock())
    monkeypatch.setattr(db, "_DB_HEALTH_REFRESHING", True)

    def unexpected_refresh():
        raise AssertionError("an active refresh must not be started twice")

    monkeypatch.setattr(db, "_schedule_database_health_refresh", unexpected_refresh)

    assert db.check_database_health() is False

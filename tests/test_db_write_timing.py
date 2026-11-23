import pytest

import db


class _FakeLock:
    def __init__(self, initially_busy=False):
        self.initially_busy = initially_busy

    def acquire(self, blocking=True, timeout=-1):
        if not blocking:
            return not self.initially_busy
        return True

    def release(self):
        return None


class _FakeConnection:
    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def test_write_transaction_emits_slow_stage_timings(monkeypatch, capsys):
    ticks = iter(index * 0.6 for index in range(30))
    monkeypatch.setattr(db.time, "perf_counter", lambda: next(ticks))
    monkeypatch.setattr(db, "_DB_WRITE_LOCK", _FakeLock(initially_busy=True))
    monkeypatch.setattr(db, "_DB_WRITE_WAITERS", 0)
    monkeypatch.setattr(db, "get_db_connection", lambda: _FakeConnection())

    result = db.write_transaction(lambda _conn: "ok", timing_label="test_gift")

    output = capsys.readouterr().out
    assert result == "ok"
    assert "operation=test_gift stage=connect" in output
    assert "operation=test_gift stage=operation" in output
    assert "operation=test_gift stage=commit" in output
    assert "operation=test_gift stage=transaction_total" in output
    assert "operation=test_gift stage=lock_hold" in output
    assert "operation=test_gift stage=lock_wait" in output
    assert "queue_depth=1" in output
    assert db._DB_WRITE_WAITERS == 0


def test_write_transaction_without_timing_label_remains_quiet(monkeypatch, capsys):
    monkeypatch.setattr(db, "_DB_WRITE_LOCK", _FakeLock())
    monkeypatch.setattr(db, "get_db_connection", lambda: _FakeConnection())

    assert db.write_transaction(lambda _conn: 42) == 42
    assert capsys.readouterr().out == ""


def test_db_init_stage_timing_reports_elapsed(monkeypatch, capsys):
    ticks = iter((2.75,))
    monkeypatch.setattr(db.time, "perf_counter", lambda: next(ticks))

    ended = db._log_db_init_stage("schema", 2.0)

    assert ended == 2.75
    assert "[DB Init Timing] stage=schema elapsed=0.750s" in capsys.readouterr().out


class _BusyLock:
    def acquire(self, blocking=True, timeout=-1):
        return False

    def release(self):
        raise AssertionError("release must not be called when acquire fails")


def test_write_transaction_clears_waiter_count_after_lock_timeout(monkeypatch):
    monkeypatch.setattr(db, "_DB_WRITE_LOCK", _BusyLock())
    monkeypatch.setattr(db, "_DB_WRITE_WAITERS", 0)

    with pytest.raises(TimeoutError, match="Database write lock busy"):
        db.write_transaction(lambda _conn: None, timing_label="test_timeout")

    assert db._DB_WRITE_WAITERS == 0

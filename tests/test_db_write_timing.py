import db


class _FakeLock:
    def acquire(self, timeout=None):
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
    monkeypatch.setattr(db, "_DB_WRITE_LOCK", _FakeLock())
    monkeypatch.setattr(db, "get_db_connection", lambda: _FakeConnection())

    result = db.write_transaction(lambda _conn: "ok", timing_label="test_gift")

    output = capsys.readouterr().out
    assert result == "ok"
    assert "operation=test_gift stage=lock_wait" in output
    assert "operation=test_gift stage=connect" in output
    assert "operation=test_gift stage=operation" in output
    assert "operation=test_gift stage=commit" in output
    assert "operation=test_gift stage=transaction_total" in output


def test_write_transaction_without_timing_label_remains_quiet(monkeypatch, capsys):
    monkeypatch.setattr(db, "_DB_WRITE_LOCK", _FakeLock())
    monkeypatch.setattr(db, "get_db_connection", lambda: _FakeConnection())

    assert db.write_transaction(lambda _conn: 42) == 42
    assert capsys.readouterr().out == ""


def test_db_init_stage_timing_reports_elapsed(monkeypatch, capsys):
    ticks = iter((2.0, 2.75))
    monkeypatch.setattr(db.time, "perf_counter", lambda: next(ticks))

    ended = db._log_db_init_stage("schema", 2.0)

    assert ended == 2.75
    assert "[DB Init Timing] stage=schema elapsed=0.750s" in capsys.readouterr().out

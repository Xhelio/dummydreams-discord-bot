import db


class FakeConnection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, tuple(params)))


def test_setting_batch_uses_one_upsert_and_keeps_latest_value(monkeypatch):
    captured = []

    def capture_transaction(operation, timing_label=None):
        captured.append((operation, timing_label))

    monkeypatch.setattr(db, "write_transaction", capture_transaction)

    count = db._write_setting_rows(
        [("alpha", "old"), ("beta", "two"), ("alpha", "latest")]
    )

    assert count == 2
    assert len(captured) == 1
    operation, timing_label = captured[0]
    assert timing_label == "setting_write_batch"

    connection = FakeConnection()
    operation(connection)

    assert len(connection.calls) == 1
    sql, params = connection.calls[0]
    assert sql.count("(?, ?)") == 2
    assert "ON CONFLICT(key) DO UPDATE" in sql
    assert params == ("alpha", "latest", "beta", "two")


def test_empty_setting_batch_skips_transaction(monkeypatch):
    called = False

    def capture_transaction(operation, timing_label=None):
        nonlocal called
        called = True

    monkeypatch.setattr(db, "write_transaction", capture_transaction)

    assert db._write_setting_rows([]) == 0
    assert called is False

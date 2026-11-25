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

import sqlite3

import db


class _BatchConnection:
    def __init__(self):
        self.calls = []

    def batch(self, statements, mode=None):
        self.calls.append((statements, mode))
        return ["batched"]


def test_atomic_statement_uses_single_remote_batch():
    connection = _BatchConnection()

    result = db._execute_atomic_statement(
        connection, "UPDATE records SET value = ?", ("saved",)
    )

    assert result == ["batched"]
    assert connection.calls == [
        ([("UPDATE records SET value = ?", ("saved",))], "immediate")
    ]


def test_atomic_statement_falls_back_to_dbapi_execute(tmp_path):
    path = tmp_path / "atomic-statement.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE records (value TEXT)")
        db._execute_atomic_statement(
            connection,
            "INSERT INTO records (value) VALUES (?)",
            ("saved",),
        )
        connection.commit()
        rows = connection.execute("SELECT value FROM records").fetchall()
    finally:
        connection.close()

    assert rows == [("saved",)]

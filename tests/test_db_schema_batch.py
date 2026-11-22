import pytest

import db


def test_schema_statements_use_one_atomic_batch_when_available():
    class BatchConnection:
        def __init__(self):
            self.batch_calls = []
            self.executed = []

        def batch(self, statements, mode=None):
            self.batch_calls.append((list(statements), mode))

        def execute(self, statement):
            self.executed.append(statement)

    connection = BatchConnection()
    statements = ["CREATE TABLE a (id INTEGER)", "DROP TABLE IF EXISTS old_a"]

    db._execute_schema_statements(connection, statements)

    assert connection.batch_calls == [
        ([(statements[0], ()), (statements[1], ())], "immediate")
    ]
    assert connection.executed == []


def test_schema_statements_fall_back_to_execute_without_batch():
    class ExecuteConnection:
        def __init__(self):
            self.executed = []

        def execute(self, statement):
            self.executed.append(statement)

    connection = ExecuteConnection()
    statements = ["CREATE TABLE a (id INTEGER)", "DROP TABLE IF EXISTS old_a"]

    db._execute_schema_statements(connection, statements)

    assert connection.executed == statements


def test_schema_batch_errors_propagate_without_replaying_statements():
    class FailingBatchConnection:
        def __init__(self):
            self.executed = []

        def batch(self, _statements, mode=None):
            raise RuntimeError("schema batch failed")

        def execute(self, statement):
            self.executed.append(statement)

    connection = FailingBatchConnection()

    with pytest.raises(RuntimeError, match="schema batch failed"):
        db._execute_schema_statements(connection, ["CREATE TABLE a (id INTEGER)"])

    assert connection.executed == []

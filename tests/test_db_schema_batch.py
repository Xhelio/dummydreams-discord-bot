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


def test_atomic_batch_uses_one_immediate_driver_batch():
    class BatchConnection:
        def __init__(self):
            self.calls = []
            self.executed = []

        def batch(self, statements, mode=None):
            self.calls.append((list(statements), mode))

        def execute(self, sql, params=()):
            self.executed.append((sql, params))

    connection = BatchConnection()
    statements = ["UPDATE a SET value = 1", ("UPDATE b SET value = ?", (2,))]

    db._execute_atomic_batch(connection, statements)

    assert connection.calls == [
        (
            [
                (statements[0], ()),
                ("UPDATE b SET value = ?", (2,)),
            ],
            "immediate",
        )
    ]
    assert connection.executed == []


def test_atomic_batch_falls_back_to_sequential_execute():
    class ExecuteConnection:
        def __init__(self):
            self.executed = []

        def execute(self, sql, params=()):
            self.executed.append((sql, params))

    connection = ExecuteConnection()
    statements = ["UPDATE a SET value = 1", ("UPDATE b SET value = ?", (2,))]

    db._execute_atomic_batch(connection, statements)

    assert connection.executed == [
        (statements[0], ()),
        ("UPDATE b SET value = ?", (2,)),
    ]


def test_atomic_batch_propagates_driver_failures():
    class FailingBatchConnection:
        def batch(self, _statements, mode=None):
            raise RuntimeError("batch rejected")

        def execute(self, *_args):
            raise AssertionError("must not replay a failed batch")

    with pytest.raises(RuntimeError, match="batch rejected"):
        db._execute_atomic_batch(FailingBatchConnection(), ["UPDATE a SET value = 1"])

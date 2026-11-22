import db


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _SchemaConnection:
    def __init__(self, columns):
        self.columns = set(columns)
        self.statements = []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement):
        self.statements.append(statement)
        if statement.startswith("PRAGMA table_info("):
            return _Cursor([(index, name, "TEXT", 0, None, 0) for index, name in enumerate(sorted(self.columns))])
        if statement.startswith("ALTER TABLE "):
            parts = statement.split()
            self.columns.add(parts[5])
        return _Cursor([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_add_missing_columns_skips_existing_columns_with_one_schema_read():
    connection = _SchemaConnection({"already_there", "also_there"})

    added = db._add_missing_columns(
        connection,
        "example",
        [("already_there", "TEXT"), ("also_there", "INTEGER"), ("new_column", "TEXT")],
    )

    assert added == ("new_column",)
    assert connection.statements.count("PRAGMA table_info(example)") == 1
    assert sum(statement.startswith("ALTER TABLE") for statement in connection.statements) == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_add_missing_columns_migrates_legacy_schema_idempotently():
    connection = _SchemaConnection({"existing"})

    first = db._add_missing_columns(
        connection, "legacy", [("existing", "TEXT"), ("added", "INTEGER DEFAULT 0")]
    )
    second = db._add_missing_columns(
        connection, "legacy", [("existing", "TEXT"), ("added", "INTEGER DEFAULT 0")]
    )

    assert first == ("added",)
    assert second == ()
    assert sum(statement.startswith("ALTER TABLE") for statement in connection.statements) == 1
    assert connection.commits == 1

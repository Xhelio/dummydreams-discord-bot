import db


class FakeConnection:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, tuple(params)))


def test_activity_log_batch_uses_one_multi_row_statement():
    connection = FakeConnection()
    rows = [
        ("gift", "first message", "viewer_one", "2026-11-22 15:00:00"),
        (
            "comment",
            "second message",
            "viewer_two",
            "2026-11-22 15:00:01",
            "technical",
            "warning",
            "observer",
        ),
    ]

    db._insert_activity_log_batch(connection, rows)

    assert len(connection.calls) == 1
    sql, params = connection.calls[0]
    assert sql.count("(?, ?, ?, ?, ?, ?, ?)") == 2
    assert params == (
        "gift",
        "viewer_one",
        "first message",
        "2026-11-22 15:00:00",
        "operational",
        "info",
        "",
        "comment",
        "viewer_two",
        "second message",
        "2026-11-22 15:00:01",
        "technical",
        "warning",
        "observer",
    )


def test_empty_activity_log_batch_does_not_execute():
    connection = FakeConnection()

    db._insert_activity_log_batch(connection, [])

    assert connection.calls == []

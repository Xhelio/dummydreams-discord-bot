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



def test_activity_log_collector_preserves_fifo_and_batch_limit():
    import queue

    events = queue.Queue()
    events.put("second")
    events.put("third")

    batch, shutdown = db._collect_activity_log_batch(
        "first", event_queue=events, max_size=2, max_wait=0.1
    )

    assert batch == ["first", "second"]
    assert shutdown is False
    assert events.get_nowait() == "third"


def test_activity_log_collector_acknowledges_shutdown_sentinel():
    import queue

    events = queue.Queue()
    events.put(None)

    batch, shutdown = db._collect_activity_log_batch(
        "first", event_queue=events, max_size=10, max_wait=0.1
    )

    assert batch == ["first"]
    assert shutdown is True
    # The sentinel is acknowledged by the collector.
    events.join()


def test_activity_log_batch_window_is_bounded():
    assert db._ACTIVITY_LOG_BATCH_MAX_SIZE == 100
    assert db._ACTIVITY_LOG_BATCH_WAIT_SECONDS == 1.25


def test_activity_log_writer_reports_batch_size_and_pending_backlog(monkeypatch):
    import queue

    events = queue.Queue()
    events.put(
        ("gift", "message", "viewer", "2026-11-23 14:00:00", "operational", "info", "test")
    )
    events.put(None)
    captured = {}

    def fake_write_transaction(operation, timing_label=None):
        captured["operation"] = operation
        captured["timing_label"] = timing_label

    monkeypatch.setattr(db, "_ACTIVITY_LOG_QUEUE", events)
    monkeypatch.setattr(db, "_ACTIVITY_LOG_PRUNE_COUNTER", 0)
    monkeypatch.setattr(db, "write_transaction", fake_write_transaction)

    db._activity_log_writer_loop()

    assert captured["timing_label"] == "activity_log_batch_rows_1_pending_0"
    assert events.unfinished_tasks == 0

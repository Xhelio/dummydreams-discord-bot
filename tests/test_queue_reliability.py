import queue
import threading

import db


def test_activity_log_event_drops_when_queue_is_full_without_blocking(monkeypatch):
    work_queue = queue.Queue(maxsize=1)
    work_queue.put(("already", "queued"))
    errors = []

    class FakeLogger:
        def log(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            errors.append(args)

    monkeypatch.setattr(db, "_ACTIVITY_LOG_QUEUE", work_queue)
    monkeypatch.setattr(db, "_ACTIVITY_LOGGER", FakeLogger())
    monkeypatch.setattr(db, "_ensure_activity_log_writer", lambda: None)

    result = db.log_activity_event("info", "overflow event")

    assert result is None
    assert work_queue.qsize() == 1
    assert errors
    assert "Queue is full" in errors[-1][0]


def test_queue_setting_returns_false_when_queue_is_full(monkeypatch):
    work_queue = queue.Queue(maxsize=1)
    work_queue.put(("existing", "value"))
    cache = {"overflow": ("old-value", 0)}
    cache_lock = threading.Lock()

    monkeypatch.setattr(db, "_SETTING_WRITE_QUEUE", work_queue)
    monkeypatch.setattr(db, "_SETTINGS_CACHE", cache)
    monkeypatch.setattr(db, "_SETTINGS_CACHE_LOCK", cache_lock)
    monkeypatch.setattr(db, "_ensure_setting_writer", lambda: None)

    assert db.queue_setting("overflow", "new-value") is False
    assert cache["overflow"][0] == "old-value"
    assert work_queue.qsize() == 1


def test_failed_setting_write_invalidates_optimistic_cache(monkeypatch):
    work_queue = queue.Queue(maxsize=2)
    cache = {}
    cache_lock = threading.Lock()
    writer_threads = []
    real_thread = threading.Thread

    def tracking_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        writer_threads.append(thread)
        return thread

    def fail_write(_rows):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(db, "_SETTING_WRITE_QUEUE", work_queue)
    monkeypatch.setattr(db, "_SETTINGS_CACHE", cache)
    monkeypatch.setattr(db, "_SETTINGS_CACHE_LOCK", cache_lock)
    monkeypatch.setattr(db, "_SETTING_WRITER_STARTED", False)
    monkeypatch.setattr(db, "_SETTING_WRITER_LOCK", threading.Lock())
    monkeypatch.setattr(db, "_write_setting_rows", fail_write)
    monkeypatch.setattr(db.threading, "Thread", tracking_thread)

    assert db.queue_setting("feature_enabled", "true") is True
    assert cache["feature_enabled"][0] == "true"

    work_queue.join()
    work_queue.put(None)
    writer_threads[0].join(timeout=3)

    assert not writer_threads[0].is_alive()
    assert "feature_enabled" not in cache


def test_activity_writer_drains_all_queued_events_before_shutdown(monkeypatch):
    work_queue = queue.Queue(maxsize=240)
    persisted = []
    persisted_lock = threading.Lock()

    class FakeConnection:
        def execute(self, _sql, params):
            with persisted_lock:
                persisted.extend(
                    tuple(params[index:index + 7])
                    for index in range(0, len(params), 7)
                )

    def fake_write_transaction(operation, **kwargs):
        operation(FakeConnection())

    monkeypatch.setattr(db, "_ACTIVITY_LOG_QUEUE", work_queue)
    monkeypatch.setattr(db, "_ACTIVITY_LOG_PRUNE_COUNTER", 0)
    monkeypatch.setattr(db, "_ACTIVITY_LOG_PRUNE_LOCK", threading.Lock())
    monkeypatch.setattr(db, "write_transaction", fake_write_transaction)
    monkeypatch.setattr(db, "prune_activity_logs", lambda: None)

    producer_count = 12
    events_per_producer = 20
    producers = []

    def produce(producer_id):
        for event_id in range(events_per_producer):
            work_queue.put((
                "info",
                f"producer-{producer_id}-event-{event_id}",
                f"viewer-{producer_id}",
                "2026-11-21 12:00:00",
                "operational",
                "info",
                "test",
            ))

    for producer_id in range(producer_count):
        thread = threading.Thread(target=produce, args=(producer_id,))
        producers.append(thread)
        thread.start()
    for thread in producers:
        thread.join(timeout=3)
        assert not thread.is_alive()

    worker = threading.Thread(target=db._activity_log_writer_loop, daemon=True)
    worker.start()
    work_queue.put(None)
    worker.join(timeout=5)

    assert not worker.is_alive()
    work_queue.join()
    assert len(persisted) == producer_count * events_per_producer


def test_settings_writer_drains_all_queued_updates_before_shutdown(monkeypatch):
    work_queue = queue.Queue(maxsize=240)
    persisted = []
    persisted_lock = threading.Lock()

    monkeypatch.setattr(db, "_SETTING_WRITE_QUEUE", work_queue)
    monkeypatch.setattr(db, "_SETTING_WRITER_STARTED", False)
    monkeypatch.setattr(db, "_SETTING_WRITER_LOCK", threading.Lock())

    def fake_write_settings(rows):
        with persisted_lock:
            persisted.extend(rows)

    monkeypatch.setattr(db, "_write_setting_rows", fake_write_settings)

    producer_count = 12
    updates_per_producer = 20
    producers = []

    def produce(producer_id):
        for update_id in range(updates_per_producer):
            work_queue.put((f"key-{producer_id}-{update_id}", str(update_id)))

    for producer_id in range(producer_count):
        thread = threading.Thread(target=produce, args=(producer_id,))
        producers.append(thread)
        thread.start()
    for thread in producers:
        thread.join(timeout=3)
        assert not thread.is_alive()

    real_thread = threading.Thread
    created = []

    def tracking_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(db.threading, "Thread", tracking_thread)
    db._ensure_setting_writer()
    work_queue.put(None)
    created[0].join(timeout=5)

    assert not created[0].is_alive()
    work_queue.join()
    assert len(persisted) == producer_count * updates_per_producer

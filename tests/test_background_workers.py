import queue
import threading

import db


def _join_worker(worker, work_queue):
    work_queue.put(None)
    worker.join(timeout=3)
    assert not worker.is_alive()
    work_queue.join()


def test_activity_log_writer_recovers_after_batch_failure(monkeypatch):
    work_queue = queue.Queue()
    monkeypatch.setattr(db, "_ACTIVITY_LOG_QUEUE", work_queue)
    monkeypatch.setattr(db, "_ACTIVITY_LOG_PRUNE_COUNTER", 0)
    monkeypatch.setattr(db, "_ACTIVITY_LOG_PRUNE_LOCK", threading.Lock())

    first_attempt = threading.Event()
    successful_batches = []

    def fake_write_transaction(operation, **kwargs):
        # The first batch fails; the worker must remain alive for the next batch.
        if not first_attempt.is_set():
            first_attempt.set()
            raise RuntimeError("temporary database outage")
        successful_batches.append(operation)

    monkeypatch.setattr(db, "write_transaction", fake_write_transaction)
    monkeypatch.setattr(db, "prune_activity_logs", lambda: None)

    worker = threading.Thread(target=db._activity_log_writer_loop, daemon=True)
    worker.start()
    work_queue.put(("info", "first event", "viewer_a", "2026-11-21 10:00:00"))
    assert first_attempt.wait(timeout=2)

    work_queue.put(("info", "second event", "viewer_b", "2026-11-21 10:00:01"))
    _join_worker(worker, work_queue)

    assert len(successful_batches) == 1


def test_setting_writer_continues_after_one_write_fails(monkeypatch):
    work_queue = queue.Queue()
    monkeypatch.setattr(db, "_SETTING_WRITE_QUEUE", work_queue)
    monkeypatch.setattr(db, "_SETTING_WRITER_STARTED", False)
    monkeypatch.setattr(db, "_SETTING_WRITER_LOCK", threading.Lock())
    attempts = []
    first_failed = threading.Event()

    def fake_write_settings(rows):
        for key, value in rows:
            attempts.append((key, value))
            if len(attempts) == 1:
                first_failed.set()
                raise RuntimeError("temporary database outage")

    monkeypatch.setattr(db, "_write_setting_rows", fake_write_settings)
    real_thread = threading.Thread
    created = []

    def tracking_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(db.threading, "Thread", tracking_thread)
    db._ensure_setting_writer()

    work_queue.put(("first", "one"))
    assert first_failed.wait(timeout=2)
    work_queue.put(("second", "two"))
    _join_worker(created[0], work_queue)

    assert attempts == [("first", "one"), ("second", "two")]


def test_activity_log_writer_starts_only_once_under_concurrency(monkeypatch):
    work_queue = queue.Queue()
    monkeypatch.setattr(db, "_ACTIVITY_LOG_QUEUE", work_queue)
    monkeypatch.setattr(db, "_ACTIVITY_LOG_WRITER_STARTED", False)
    monkeypatch.setattr(db, "_ACTIVITY_LOG_WRITER_LOCK", threading.Lock())

    real_thread = threading.Thread
    created = []
    created_lock = threading.Lock()

    def tracking_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        with created_lock:
            created.append(thread)
        return thread

    monkeypatch.setattr(db.threading, "Thread", tracking_thread)
    callers = [real_thread(target=db._ensure_activity_log_writer) for _ in range(12)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=2)

    assert len(created) == 1
    _join_worker(created[0], work_queue)


def test_settings_writer_starts_only_once_under_concurrency(monkeypatch):
    work_queue = queue.Queue()
    monkeypatch.setattr(db, "_SETTING_WRITE_QUEUE", work_queue)
    monkeypatch.setattr(db, "_SETTING_WRITER_STARTED", False)
    monkeypatch.setattr(db, "_SETTING_WRITER_LOCK", threading.Lock())

    real_thread = threading.Thread
    created = []
    created_lock = threading.Lock()

    def tracking_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        with created_lock:
            created.append(thread)
        return thread

    monkeypatch.setattr(db.threading, "Thread", tracking_thread)
    callers = [real_thread(target=db._ensure_setting_writer) for _ in range(12)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=2)

    assert len(created) == 1
    _join_worker(created[0], work_queue)

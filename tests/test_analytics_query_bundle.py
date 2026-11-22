import threading
from concurrent.futures import ThreadPoolExecutor

import routes.admin as admin


def test_analytics_bundle_runs_independent_queries_concurrently(monkeypatch):
    barrier = threading.Barrier(2)
    executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(admin, "_ANALYTICS_QUERY_EXECUTOR", executor)

    def query(value):
        barrier.wait(timeout=3)
        return value * 2

    tasks = {
        f"query_{value}": (query, (value,), {})
        for value in range(2)
    }

    try:
        results = admin._run_analytics_bundle(tasks)
    finally:
        executor.shutdown(wait=True)

    assert results == {
        "query_0": 0,
        "query_1": 2,
    }



def test_analytics_stage_runs_direct_call_without_queue_timestamp():
    result = admin._run_analytics_stage("direct", lambda left, right: left + right, 2, 3)
    assert result == 5


def test_analytics_stage_accepts_explicit_queue_timestamp():
    submitted_at = admin.time.monotonic()
    result = admin._run_analytics_stage(
        "queued", lambda value: value * 2, 4, _submitted_at=submitted_at
    )
    assert result == 8

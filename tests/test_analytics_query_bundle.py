import threading
from concurrent.futures import ThreadPoolExecutor

import routes.admin as admin


def test_analytics_bundle_runs_independent_queries_concurrently(monkeypatch):
    barrier = threading.Barrier(4)
    executor = ThreadPoolExecutor(max_workers=4)
    monkeypatch.setattr(admin, "_ANALYTICS_QUERY_EXECUTOR", executor)

    def query(value):
        barrier.wait(timeout=3)
        return value * 2

    tasks = {
        f"query_{value}": (query, (value,), {})
        for value in range(4)
    }

    try:
        results = admin._run_analytics_bundle(tasks)
    finally:
        executor.shutdown(wait=True)

    assert results == {
        "query_0": 0,
        "query_1": 2,
        "query_2": 4,
        "query_3": 6,
    }

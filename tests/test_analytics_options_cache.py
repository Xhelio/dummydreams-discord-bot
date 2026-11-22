import threading
import time
from concurrent.futures import ThreadPoolExecutor

import routes.admin as admin


def _reset_options_cache():
    with admin._ANALYTICS_OPTIONS_CACHE_LOCK:
        admin._ANALYTICS_OPTIONS_CACHE.clear()
        admin._ANALYTICS_OPTIONS_CACHE_INFLIGHT.clear()


def test_options_cache_reuses_fresh_result():
    _reset_options_cache()
    calls = []

    def query():
        calls.append(1)
        return ["A", "B"]

    first = admin._get_cached_analytics_options("hosts", query)
    second = admin._get_cached_analytics_options("hosts", query)

    assert first == second == ["A", "B"]
    assert len(calls) == 1


def test_options_cache_returns_independent_lists():
    _reset_options_cache()
    first = admin._get_cached_analytics_options("hosts", lambda: ["A"])
    first.append("MUTATED")

    assert admin._get_cached_analytics_options("hosts", lambda: ["B"]) == ["A"]


def test_options_cache_single_flight_for_same_key():
    _reset_options_cache()
    started = threading.Event()
    release = threading.Event()
    calls = []

    def query():
        calls.append(1)
        started.set()
        assert release.wait(timeout=3)
        return ["A"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(admin._get_cached_analytics_options, "hosts", query)
        assert started.wait(timeout=2)
        second = executor.submit(admin._get_cached_analytics_options, "hosts", query)
        time.sleep(0.05)
        release.set()
        assert first.result(timeout=3) == ["A"]
        assert second.result(timeout=3) == ["A"]

    assert len(calls) == 1


def test_options_cache_clears_inflight_after_error():
    _reset_options_cache()
    calls = []

    def query():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary database failure")
        return ["A"]

    try:
        admin._get_cached_analytics_options("hosts", query)
    except RuntimeError as exc:
        assert str(exc) == "temporary database failure"
    else:
        raise AssertionError("expected query failure")

    assert admin._get_cached_analytics_options("hosts", query) == ["A"]
    assert len(calls) == 2

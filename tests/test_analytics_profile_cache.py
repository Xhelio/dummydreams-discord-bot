import threading
import time
from concurrent.futures import ThreadPoolExecutor

import routes.admin as admin


def _reset_profile_cache():
    with admin._ANALYTICS_PROFILE_CACHE_LOCK:
        admin._ANALYTICS_PROFILE_CACHE.clear()
        admin._ANALYTICS_PROFILE_CACHE_INFLIGHT.clear()


def test_profile_cache_reuses_fresh_result(monkeypatch):
    _reset_profile_cache()
    calls = []

    def query(*args):
        calls.append(args)
        return [{"hour": 10}]

    monkeypatch.setattr(admin, "get_analytics_timeseries", query)

    first = admin._get_cached_analytics_profile("2026-11-01", "2026-11-02")
    second = admin._get_cached_analytics_profile("2026-11-01", "2026-11-02")

    assert first == second == [{"hour": 10}]
    assert len(calls) == 1


def test_profile_cache_does_not_serialize_different_keys(monkeypatch):
    _reset_profile_cache()
    both_started = threading.Barrier(2)

    def query(start, end, *_args):
        both_started.wait(timeout=3)
        return [start, end]

    monkeypatch.setattr(admin, "get_analytics_timeseries", query)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            admin._get_cached_analytics_profile, "2026-11-01", "2026-11-02"
        )
        second = executor.submit(
            admin._get_cached_analytics_profile, "2026-11-03", "2026-11-04"
        )
        assert first.result(timeout=4) == ["2026-11-01", "2026-11-02"]
        assert second.result(timeout=4) == ["2026-11-03", "2026-11-04"]


def test_profile_cache_single_flight_for_same_key(monkeypatch):
    _reset_profile_cache()
    query_started = threading.Event()
    release_query = threading.Event()
    calls = []

    def query(*_args):
        calls.append(1)
        query_started.set()
        assert release_query.wait(timeout=3)
        return [{"hour": 12}]

    monkeypatch.setattr(admin, "get_analytics_timeseries", query)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            admin._get_cached_analytics_profile, "2026-11-01", "2026-11-02"
        )
        assert query_started.wait(timeout=2)
        second = executor.submit(
            admin._get_cached_analytics_profile, "2026-11-01", "2026-11-02"
        )
        time.sleep(0.05)
        release_query.set()
        assert first.result(timeout=3) == [{"hour": 12}]
        assert second.result(timeout=3) == [{"hour": 12}]

    assert len(calls) == 1


def test_profile_cache_clears_inflight_after_query_error(monkeypatch):
    _reset_profile_cache()
    calls = []

    def query(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary database failure")
        return [{"hour": 8}]

    monkeypatch.setattr(admin, "get_analytics_timeseries", query)

    try:
        admin._get_cached_analytics_profile("2026-11-01", "2026-11-02")
    except RuntimeError as exc:
        assert str(exc) == "temporary database failure"
    else:
        raise AssertionError("expected query failure")

    assert admin._get_cached_analytics_profile(
        "2026-11-01", "2026-11-02"
    ) == [{"hour": 8}]
    assert len(calls) == 2

import asyncio

import pytest

import observer


@pytest.mark.parametrize(
    ("failures", "expected_seconds"),
    [
        (0, 30),
        (1, 30),
        (4, 30),
        (5, 60),
        (9, 60),
        (10, 120),
        (25, 120),
    ],
)
def test_observer_client_initialization_retry_delay_is_bounded(failures, expected_seconds):
    assert observer._observer_init_retry_delay(failures) == expected_seconds


def test_live_intro_detected_before_connect_wins_over_stale_session_host():
    assert observer._has_fresh_host_detection("Zize", True) is True
    assert observer._has_fresh_host_detection("Luna", False) is False
    assert observer._has_fresh_host_detection("Host", True) is False
    assert observer._has_fresh_host_detection("-", True) is False


def _reset_priority_probe_state(monkeypatch):
    monkeypatch.setattr(
        observer,
        "_priority_probe_cache",
        {"at": 0.0, "targets": (), "results": []},
    )
    monkeypatch.setattr(observer, "_priority_probe_lock", None)


def test_priority_probe_filters_normalizes_and_skips_already_live_hosts(monkeypatch):
    _reset_priority_probe_state(monkeypatch)
    monkeypatch.setattr(
        observer,
        "get_observable_hosts",
        lambda: [
            {"username": "@LiveHost", "priority": True},
            {"username": "@OfflineHost", "priority": True},
            {"username": "offlinehost", "priority": True},
            {"username": "normalhost", "priority": False},
            {"username": "", "priority": True},
            None,
        ],
    )
    monkeypatch.setattr(observer, "get_current_live_hosts", lambda: ["livehost"])
    probed = []

    async def fake_probe(username):
        probed.append(username)
        return False

    monkeypatch.setattr(observer, "probe_room_live", fake_probe)

    result = asyncio.run(observer.probe_priority_targets_once())

    assert result == [
        {"username": "livehost", "live": True},
        {"username": "offlinehost", "live": False},
    ]
    assert probed == ["offlinehost"]


def test_priority_probe_uses_ttl_cache_and_returns_defensive_copies(monkeypatch):
    _reset_priority_probe_state(monkeypatch)
    monkeypatch.setattr(
        observer,
        "get_observable_hosts",
        lambda: [{"username": "creator", "priority": True}],
    )
    monkeypatch.setattr(observer, "get_current_live_hosts", lambda: [])
    clock = {"now": 100.0}
    monkeypatch.setattr(observer.time, "time", lambda: clock["now"])
    calls = []

    async def fake_probe(username):
        calls.append(username)
        return True

    monkeypatch.setattr(observer, "probe_room_live", fake_probe)

    first = asyncio.run(observer.probe_priority_targets_once())
    first[0]["live"] = False
    second = asyncio.run(observer.probe_priority_targets_once())

    assert second == [{"username": "creator", "live": True}]
    assert calls == ["creator"]

    clock["now"] += observer._PRIORITY_PROBE_TTL_SECONDS + 1
    third = asyncio.run(observer.probe_priority_targets_once())
    assert third == [{"username": "creator", "live": True}]
    assert calls == ["creator", "creator"]


def test_priority_probe_serializes_concurrent_calls(monkeypatch):
    _reset_priority_probe_state(monkeypatch)
    monkeypatch.setattr(
        observer,
        "get_observable_hosts",
        lambda: [{"username": "creator", "priority": True}],
    )
    monkeypatch.setattr(observer, "get_current_live_hosts", lambda: [])
    calls = []

    async def fake_probe(username):
        calls.append(username)
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(observer, "probe_room_live", fake_probe)

    async def run_concurrently():
        return await asyncio.gather(
            observer.probe_priority_targets_once(),
            observer.probe_priority_targets_once(),
        )

    results = asyncio.run(run_concurrently())

    assert results == [
        [{"username": "creator", "live": None}],
        [{"username": "creator", "live": None}],
    ]
    assert calls == ["creator"]


def test_request_reconnect_normalizes_target_and_marks_host_reconfirmation(monkeypatch):
    monkeypatch.setattr(observer, "_ACTIVE_CLIENTS", {"creator": object()})
    monkeypatch.setattr(observer, "_RECONNECT_REQUESTED", set())
    monkeypatch.setattr(observer, "_HOST_RECONFIRM_REQUESTED", set())
    cleared = []
    logged = []

    monkeypatch.setattr(
        observer, "_clear_rate_limit_state", lambda username: cleared.append(username)
    )
    monkeypatch.setattr(
        observer, "log_activity_event", lambda *args: logged.append(args)
    )

    targets = observer.request_tiktok_reconnect("@Creator")

    assert targets == ["creator"]
    assert observer._RECONNECT_REQUESTED == {"creator"}
    assert observer._HOST_RECONFIRM_REQUESTED == {"creator"}
    assert cleared == ["creator"]
    assert logged


def test_request_reconnect_can_skip_host_reconfirmation(monkeypatch):
    monkeypatch.setattr(observer, "_ACTIVE_CLIENTS", {"creator": object()})
    monkeypatch.setattr(observer, "_RECONNECT_REQUESTED", set())
    monkeypatch.setattr(observer, "_HOST_RECONFIRM_REQUESTED", set())
    monkeypatch.setattr(observer, "_clear_rate_limit_state", lambda username: None)
    monkeypatch.setattr(observer, "log_activity_event", lambda *args: None)

    targets = observer.request_tiktok_reconnect("creator", reconfirm_host=False)

    assert targets == ["creator"]
    assert observer._RECONNECT_REQUESTED == {"creator"}
    assert observer._HOST_RECONFIRM_REQUESTED == set()



def test_observer_task_exit_reports_unexpected_exception(monkeypatch):
    logged = []
    printed = []
    monkeypatch.setattr(observer, "log_activity_event", lambda *args: logged.append(args))
    monkeypatch.setattr("builtins.print", lambda *args: printed.append(" ".join(map(str, args))))

    async def fail():
        raise RuntimeError("unexpected websocket failure")

    async def run():
        task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        observer._report_observer_task_exit("creator", task)

    asyncio.run(run())

    assert printed and "unexpected websocket failure" in printed[0]
    assert logged and logged[0][0] == "error"
    assert "@creator" in logged[0][1]


def test_observer_task_exit_ignores_clean_and_cancelled_tasks(monkeypatch):
    logged = []
    monkeypatch.setattr(observer, "log_activity_event", lambda *args: logged.append(args))

    async def run():
        clean = asyncio.create_task(asyncio.sleep(0))
        await clean
        observer._report_observer_task_exit("creator", clean)

        cancelled = asyncio.create_task(asyncio.sleep(10))
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        observer._report_observer_task_exit("creator", cancelled)

    asyncio.run(run())

    assert logged == []

import asyncio

import observer


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
            {"username": " @OfflineHost ", "priority": True},
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

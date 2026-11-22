import asyncio
from types import SimpleNamespace

import pytest

import observer


def _install_probe(monkeypatch, payload=None, *, error=None, web=True):
    monkeypatch.setattr(observer, "_ROOM_ID_PROBE_COOLDOWN_UNTIL", 0.0)
    constructed = []

    def make_client(*, unique_id):
        constructed.append(unique_id)
        return SimpleNamespace(web=object() if web else None)

    async def fetch_user_room_data(_web, username):
        if error:
            raise error
        return payload

    monkeypatch.setattr(observer, "TikTokLiveClient", make_client)
    monkeypatch.setattr(
        observer,
        "FetchRoomIdAPIRoute",
        SimpleNamespace(fetch_user_room_data=fetch_user_room_data),
    )
    return constructed


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"data": {"user": {"status": 2}}}, True),
        ({"data": {"user": {"status": "2"}}}, True),
        ({"data": {"liveRoom": {"status": 2, "roomId": "123"}}}, True),
        ({"data": {"live_room": {"status": "2"}}}, True),
        ({"data": {"user": {"status": 0}, "liveRoom": {"roomId": "123"}}}, False),
        ({"data": {"user": {"status": 0}}}, False),
        ({"data": {"user": {}, "liveRoom": {"status": 0}}}, False),
        ({}, None),
        ({"data": None}, None),
        ({"data": []}, None),
        ({"unexpected": "shape"}, False),
    ],
)
def test_probe_room_live_classifies_tiktok_payloads(monkeypatch, payload, expected):
    constructed = _install_probe(monkeypatch, payload)

    result = asyncio.run(observer.probe_room_live("@Creator"))

    assert result is expected
    assert constructed == ["creator"]


def test_probe_room_live_closes_http_session_after_fetch(monkeypatch):
    closed = []

    class FakeWeb:
        async def close(self):
            closed.append(True)

    async def fetch_user_room_data(_web, _username):
        return {"data": {"user": {"status": 2}}}

    monkeypatch.setattr(
        observer, "TikTokLiveClient",
        lambda *, unique_id: SimpleNamespace(web=FakeWeb()),
    )
    monkeypatch.setattr(
        observer, "FetchRoomIdAPIRoute",
        SimpleNamespace(fetch_user_room_data=fetch_user_room_data),
    )

    assert asyncio.run(observer.probe_room_live("Creator")) is True
    assert closed == [True]


def test_json_empty_websocket_cycle_preserves_failure_backoff():
    assert observer._should_reset_observer_cycle_state(False, True) is False
    assert observer._should_reset_observer_cycle_state(False, False) is False
    assert observer._should_reset_observer_cycle_state(True, False) is True
    assert observer._should_reset_observer_cycle_state(True, True) is False


def test_probe_room_live_returns_unknown_when_client_has_no_web(monkeypatch):
    constructed = _install_probe(monkeypatch, {"data": {"user": {"status": 2}}}, web=False)

    result = asyncio.run(observer.probe_room_live("Creator"))

    assert result is None
    assert constructed == ["creator"]


def test_probe_room_live_returns_unknown_when_fetch_fails(monkeypatch):
    constructed = _install_probe(monkeypatch, error=RuntimeError("temporary network failure"))

    result = asyncio.run(observer.probe_room_live("Creator"))

    assert result is None
    assert constructed == ["creator"]


def test_probe_room_live_skips_client_for_empty_username(monkeypatch):
    constructed = _install_probe(monkeypatch, {"data": {"user": {"status": 2}}})

    result = asyncio.run(observer.probe_room_live("@"))

    assert result is None
    assert constructed == []


def test_probe_room_live_returns_unknown_when_route_is_unavailable(monkeypatch):
    constructed = _install_probe(monkeypatch, {"data": {"user": {"status": 2}}})
    monkeypatch.setattr(observer, "FetchRoomIdAPIRoute", None)

    result = asyncio.run(observer.probe_room_live("Creator"))

    assert result is None
    assert constructed == []



def test_non_json_response_pauses_followup_priority_probes(monkeypatch):
    error = RuntimeError("FailedParseRoomIdError: TikTok returned a non-JSON response")
    constructed = _install_probe(monkeypatch, error=error)

    first = asyncio.run(observer.probe_room_live("Creator"))
    second = asyncio.run(observer.probe_room_live("AnotherCreator"))

    assert first is None
    assert second is None
    assert constructed == ["creator"]
    assert observer._ROOM_ID_PROBE_COOLDOWN_UNTIL > observer.time.time()

import asyncio
import time

import observer


class _FakeClient:
    def __init__(self, calls):
        self.calls = calls

    async def start(self, **kwargs):
        self.calls.append((time.monotonic(), kwargs))
        return kwargs


def test_signer_start_requests_are_globally_spaced(monkeypatch):
    async def run():
        calls = []
        monkeypatch.setattr(observer, "_SIGNER_LAST_START_MONOTONIC", 0.0)
        monkeypatch.setattr(observer, "_signer_start_interval_seconds", lambda: 0.03)

        first = _FakeClient(calls)
        second = _FakeClient(calls)
        results = await asyncio.gather(
            observer._start_client_with_signer_pacing(first, fetch_room_info=True),
            observer._start_client_with_signer_pacing(second, fetch_room_info=False),
        )

        assert results == [
            {"fetch_room_info": True},
            {"fetch_room_info": False},
        ]
        assert len(calls) == 2
        assert calls[1][0] - calls[0][0] >= 0.025

    asyncio.run(run())


def test_signer_start_interval_is_bounded(monkeypatch):
    monkeypatch.setenv("TIKTOK_SIGN_START_INTERVAL_SECONDS", "999")
    assert observer._signer_start_interval_seconds() == 300.0

    monkeypatch.setenv("TIKTOK_SIGN_START_INTERVAL_SECONDS", "-5")
    assert observer._signer_start_interval_seconds() == 0.0

    monkeypatch.setenv("TIKTOK_SIGN_START_INTERVAL_SECONDS", "invalid")
    assert observer._signer_start_interval_seconds() == observer._SIGNER_DEFAULT_START_INTERVAL_SECONDS

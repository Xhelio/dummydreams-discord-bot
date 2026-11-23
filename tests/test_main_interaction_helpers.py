import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import main


class FakeUser:
    id = 42
    name = "viewer"
    display_name = "Viewer Name"

    def __str__(self):
        return "viewer#0001"


def test_describe_user_includes_distinct_display_name():
    assert main._describe_user(FakeUser()) == "viewer#0001 (Viewer Name)"


def test_describe_user_does_not_duplicate_name():
    user = SimpleNamespace(name="viewer", display_name="viewer")
    assert main._describe_user(user) == "namespace(name='viewer', display_name='viewer')"


def test_describe_user_handles_string_conversion_failure():
    class BrokenUser:
        def __str__(self):
            raise RuntimeError("cannot format")

    assert main._describe_user(BrokenUser()) == "unknown"


def test_note_interaction_lag_logs_only_slow_handlers(monkeypatch):
    warnings = []
    monkeypatch.setattr(main.time, "time", lambda: 20.0)
    monkeypatch.setattr(main.log, "warning", lambda *args: warnings.append(args))

    fast = SimpleNamespace(
        created_at=datetime.fromtimestamp(19.0, timezone.utc),
        user=SimpleNamespace(id=1),
    )
    slow = SimpleNamespace(
        created_at=datetime.fromtimestamp(18.0, timezone.utc),
        user=SimpleNamespace(id=2),
    )

    main._note_interaction_lag(fast, "fast")
    main._note_interaction_lag(slow, "slow")

    assert len(warnings) == 1
    assert warnings[0][1] == "slow"


def test_note_interaction_lag_ignores_invalid_timestamps(monkeypatch):
    warnings = []
    monkeypatch.setattr(main.log, "warning", lambda *args: warnings.append(args))

    main._note_interaction_lag(SimpleNamespace(created_at=None), "invalid")

    assert warnings == []


async def _record_defer(calls, kwargs):
    calls.append(kwargs)


def test_safe_defer_acknowledges_unanswered_interaction(monkeypatch):
    deferred = []
    monkeypatch.setattr(main, "_bind_interaction_context", lambda interaction: None)
    monkeypatch.setattr(main.log, "info", lambda *args: None)
    response = SimpleNamespace(
        is_done=lambda: False,
        defer=lambda **kwargs: _record_defer(deferred, kwargs),
    )
    interaction = SimpleNamespace(
        response=response,
        user=FakeUser(),
        data={"custom_id": "verify"},
    )

    result = asyncio.run(
        main._safe_defer(interaction, "verify", ephemeral=True, thinking=True)
    )

    assert result is True
    assert deferred == [{"ephemeral": True, "thinking": True}]


def test_safe_defer_accepts_interaction_already_acknowledged(monkeypatch):
    monkeypatch.setattr(main, "_bind_interaction_context", lambda interaction: None)
    monkeypatch.setattr(main.log, "info", lambda *args: None)

    async def unexpected_defer(**kwargs):
        raise AssertionError("must not defer an acknowledged interaction")

    interaction = SimpleNamespace(
        response=SimpleNamespace(is_done=lambda: True, defer=unexpected_defer),
        user=FakeUser(),
        data={},
    )

    assert asyncio.run(main._safe_defer(interaction, "verify")) is True

import discord


def test_safe_defer_treats_interaction_responded_as_success(monkeypatch):
    monkeypatch.setattr(main, "_bind_interaction_context", lambda interaction: None)

    async def already_responded(**kwargs):
        raise discord.InteractionResponded(None)

    interaction = SimpleNamespace(
        response=SimpleNamespace(is_done=lambda: False, defer=already_responded),
        user=FakeUser(),
        data={},
    )

    assert asyncio.run(main._safe_defer(interaction, "verify")) is True


def test_safe_defer_returns_false_and_logs_unexpected_failure(monkeypatch):
    warnings = []
    monkeypatch.setattr(main, "_bind_interaction_context", lambda interaction: None)
    monkeypatch.setattr(main.log, "warning", lambda *args: warnings.append(args))

    async def failed_defer(**kwargs):
        raise RuntimeError("simulated transport failure")

    interaction = SimpleNamespace(
        response=SimpleNamespace(is_done=lambda: False, defer=failed_defer),
        user=FakeUser(),
        data={},
    )

    assert asyncio.run(main._safe_defer(interaction, "verify")) is False
    assert len(warnings) == 1
    assert "simulated transport failure" in warnings[0][-1]

from types import SimpleNamespace

import discord
import main


class FakeUser:
    id = 7
    name = "viewer"
    display_name = "Viewer"

    def __str__(self):
        return "viewer"


def test_bind_interaction_context_records_application_command(monkeypatch):
    bound = {}
    monkeypatch.setattr(main.log_setup, "bind_log_context", lambda **kwargs: bound.update(kwargs))
    interaction = SimpleNamespace(
        data={"name": "status"},
        type=discord.InteractionType.application_command,
        user=FakeUser(),
        guild=SimpleNamespace(name="Community"),
        channel=SimpleNamespace(name="general", id=10, guild=object()),
    )

    main._bind_interaction_context(interaction)

    assert bound == {
        "actor": "viewer (Viewer)",
        "user_id": 7,
        "guild": "Community",
        "channel": "#general",
        "command": "/status",
        "custom_id": None,
    }


def test_bind_interaction_context_records_component_and_dm(monkeypatch):
    bound = {}
    monkeypatch.setattr(main.log_setup, "bind_log_context", lambda **kwargs: bound.update(kwargs))
    user = type("User", (), {
        "id": 8,
        "name": "member",
        "display_name": "Member",
        "__str__": lambda self: "member",
    })()
    interaction = SimpleNamespace(
        data={"custom_id": "verify:confirm"},
        type=None,
        user=user,
        guild=None,
        channel=SimpleNamespace(name="private", id=11, guild=None),
    )

    main._bind_interaction_context(interaction)

    assert bound == {
        "actor": "member (Member)",
        "user_id": 8,
        "guild": "DM",
        "channel": "DM",
        "command": None,
        "custom_id": "verify:confirm",
    }


def test_bind_interaction_context_uses_channel_id_when_channel_has_no_name(monkeypatch):
    bound = {}
    monkeypatch.setattr(main.log_setup, "bind_log_context", lambda **kwargs: bound.update(kwargs))
    interaction = SimpleNamespace(
        data={},
        type=None,
        user=SimpleNamespace(id=9, name="user", display_name="user"),
        guild=SimpleNamespace(name="Guild"),
        channel=SimpleNamespace(id=1234, guild=object()),
    )

    main._bind_interaction_context(interaction)

    assert bound["channel"] == "1234"
    assert bound["command"] is None
    assert bound["custom_id"] is None


def test_bind_interaction_context_swallows_malformed_interaction(monkeypatch):
    calls = []
    monkeypatch.setattr(main.log_setup, "bind_log_context", lambda **kwargs: calls.append(kwargs))

    main._bind_interaction_context(object())

    assert calls == []

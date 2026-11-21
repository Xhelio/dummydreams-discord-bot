import runtime_status


def setup_function():
    with runtime_status._LOCK:
        runtime_status._LIVE.clear()
        runtime_status._OBSERVER.clear()
        runtime_status._BOT_OBJECT = None
        runtime_status._BOT.update(online=False, user=None, ready_at=None)


def test_set_live_normalizes_username_and_preserves_detected_host():
    runtime_status.set_live("@ExampleUser", "Detected Host", source="webhook")
    runtime_status.set_live("exampleuser", "Host", source="heartbeat")

    snapshot = runtime_status.get_live_snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["username"] == "exampleuser"
    assert snapshot[0]["host"] == "Detected Host"
    assert snapshot[0]["source"] == "heartbeat"


def test_set_live_ignores_empty_username():
    runtime_status.set_live("  ")

    assert runtime_status.get_live_snapshot() == []


def test_clear_live_removes_account():
    runtime_status.set_live("creator")
    runtime_status.clear_live("@CREATOR")

    assert runtime_status.get_live_snapshot() == []


def test_get_live_usernames_returns_normalized_names():
    runtime_status.set_live("@First")
    runtime_status.set_live("Second")

    assert set(runtime_status.get_live_usernames()) == {"first", "second"}

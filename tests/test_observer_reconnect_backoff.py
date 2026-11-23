import observer


def test_observer_cycle_state_resets_only_after_clean_connection():
    assert observer._should_reset_observer_cycle_state(True, False) is True
    assert observer._should_reset_observer_cycle_state(False, False) is False
    assert observer._should_reset_observer_cycle_state(True, True) is False
    assert observer._should_reset_observer_cycle_state(False, True) is False


def test_json_response_error_recognizes_known_signatures_case_insensitively():
    assert observer._is_json_response_error("JSONDecodeError: invalid response")
    assert observer._is_json_response_error("Expecting value at line 1")
    assert observer._is_json_response_error("FailedParseRoomIdError")
    assert observer._is_json_response_error("non-JSON response from upstream")
    assert not observer._is_json_response_error("websocket disconnected")
    assert not observer._is_json_response_error(None)


def test_reconnect_request_is_consumed_exactly_once(monkeypatch):
    monkeypatch.setattr(observer, "_RECONNECT_REQUESTED", {"creator"})
    assert observer._is_reconnect_requested("creator") is True
    assert observer._consume_reconnect_request("creator") is True
    assert observer._is_reconnect_requested("creator") is False
    assert observer._consume_reconnect_request("creator") is False


def test_clear_rate_limit_state_only_queues_nonzero_values(monkeypatch):
    settings = {"rl_strikes:creator": "3", "rl_until:creator": "12345"}
    queued = []
    monkeypatch.setattr(observer, "get_setting", lambda key, default=None: settings.get(key, default))
    monkeypatch.setattr(observer, "queue_setting", lambda key, value: queued.append((key, value)))

    observer._clear_rate_limit_state("creator")

    assert queued == [("rl_strikes:creator", "0"), ("rl_until:creator", "0")]


def test_clear_rate_limit_state_skips_already_clear_values(monkeypatch):
    queued = []
    monkeypatch.setattr(observer, "get_setting", lambda key, default=None: "0")
    monkeypatch.setattr(observer, "queue_setting", lambda *args: queued.append(args))

    observer._clear_rate_limit_state("creator")

    assert queued == []


def test_rate_limit_cooldown_escalates_and_persists(monkeypatch):
    strikes = {"value": 0}
    queued = []
    now = 1_000_000
    monkeypatch.setattr(
        observer,
        "get_setting",
        lambda key, default=None: str(strikes["value"]) if key == "rl_strikes:creator" else default,
    )

    def queue_setting(key, value):
        queued.append((key, value))
        if key == "rl_strikes:creator":
            strikes["value"] = int(value)

    monkeypatch.setattr(observer, "queue_setting", queue_setting)
    monkeypatch.setattr(observer.time, "time", lambda: now)

    expected = [1800, 3600, 7200, 10800]
    for cooldown in expected:
        actual = observer._rate_limit_cooldown_seconds("creator", "temporary websocket limit")
        assert actual == cooldown

    assert [item for item in queued if item[0] == "rl_strikes:creator"] == [
        ("rl_strikes:creator", "1"),
        ("rl_strikes:creator", "2"),
        ("rl_strikes:creator", "3"),
        ("rl_strikes:creator", "4"),
    ]
    assert queued[-1] == ("rl_until:creator", str(now + 10800))

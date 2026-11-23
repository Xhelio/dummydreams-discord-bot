import json

import observer
import scripts.tiktok_response_simulator as simulator


def test_simulator_covers_expected_response_shapes():
    assert set(simulator.SCENARIOS) == {
        "live",
        "offline",
        "empty",
        "malformed",
        "invalid-json",
        "failed-parse",
        "timeout",
        "http-500",
    }


def test_all_tiktok_response_scenarios_pass_without_external_services(monkeypatch):
    original_client = observer.TikTokLiveClient
    original_route = observer.FetchRoomIdAPIRoute
    original_cooldown = observer._ROOM_ID_PROBE_COOLDOWN_UNTIL

    results = [simulator.run_scenario(name) for name in simulator.SCENARIOS]

    assert all(item["passed"] for item in results)
    assert all(item["external_services_used"] is False for item in results)
    assert all(item["fake_http_session_closed"] for item in results)
    assert observer.TikTokLiveClient is original_client
    assert observer.FetchRoomIdAPIRoute is original_route
    assert observer._ROOM_ID_PROBE_COOLDOWN_UNTIL == original_cooldown


def test_invalid_json_simulation_activates_probe_cooldown():
    result = simulator.run_scenario("invalid-json")

    assert result["passed"] is True
    assert result["actual_probe_result"] is None
    assert result["actual_cooldown"] is True


def test_timeout_and_http_500_do_not_trigger_non_json_cooldown():
    timeout = simulator.run_scenario("timeout")
    http_500 = simulator.run_scenario("http-500")

    assert timeout["passed"] is True
    assert timeout["actual_cooldown"] is False
    assert http_500["passed"] is True
    assert http_500["actual_cooldown"] is False


def test_json_decode_error_fixture_is_explicit_and_offline():
    error = simulator.SCENARIOS["invalid-json"]["error"]

    assert isinstance(error, json.JSONDecodeError)
    assert "Expecting value" in str(error)

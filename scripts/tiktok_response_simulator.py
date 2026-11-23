#!/usr/bin/env python3
"""Offline TikTok room-response simulator for the observer's real probe logic.

No HTTP request, TikTok account, Discord client, or database is used.
Run: python scripts/tiktok_response_simulator.py [--scenario all|live|offline|empty|malformed|invalid-json|failed-parse|timeout|http-500]
"""

import argparse
import asyncio
import json
import os
import sys
from types import SimpleNamespace

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import observer  # noqa: E402


SCENARIOS = {
    "live": {
        "payload": {"data": {"user": {"status": 2}}},
        "expected": True,
        "cooldown": False,
    },
    "offline": {
        "payload": {"data": {"user": {"status": 0}}},
        "expected": False,
        "cooldown": False,
    },
    "empty": {"payload": {}, "expected": None, "cooldown": False},
    "malformed": {
        "payload": {"data": []},
        "expected": None,
        "cooldown": False,
    },
    "invalid-json": {
        "error": json.JSONDecodeError("Expecting value", "", 0),
        "expected": None,
        "cooldown": True,
    },
    "failed-parse": {
        "error": RuntimeError("FailedParseRoomIdError: invalid room response"),
        "expected": None,
        "cooldown": True,
    },
    "timeout": {
        "error": TimeoutError("simulated upstream timeout"),
        "expected": None,
        "cooldown": False,
    },
    "http-500": {
        "error": RuntimeError("HTTPStatusError: 500 Server Error"),
        "expected": None,
        "cooldown": False,
    },
}


async def _run_probe(scenario: dict) -> tuple:
    """Run observer.probe_room_live with an in-memory fake transport."""
    closed = []

    class FakeWeb:
        async def close(self):
            closed.append(True)

    class FakeTikTokClient:
        def __init__(self, *, unique_id):
            self.unique_id = unique_id
            self.web = FakeWeb()

    async def fake_fetch(_web, _username):
        error = scenario.get("error")
        if error is not None:
            raise error
        return scenario.get("payload")

    original_client = observer.TikTokLiveClient
    original_route = observer.FetchRoomIdAPIRoute
    original_cooldown = observer._ROOM_ID_PROBE_COOLDOWN_UNTIL
    observer.TikTokLiveClient = FakeTikTokClient
    observer.FetchRoomIdAPIRoute = SimpleNamespace(
        fetch_user_room_data=fake_fetch
    )
    observer._ROOM_ID_PROBE_COOLDOWN_UNTIL = 0.0
    try:
        result = await observer.probe_room_live("qa_simulated_creator")
        cooldown_active = (
            observer._ROOM_ID_PROBE_COOLDOWN_UNTIL > observer.time.time()
        )
        return result, cooldown_active, bool(closed)
    finally:
        observer.TikTokLiveClient = original_client
        observer.FetchRoomIdAPIRoute = original_route
        observer._ROOM_ID_PROBE_COOLDOWN_UNTIL = original_cooldown


def run_scenario(name: str) -> dict:
    scenario = SCENARIOS[name]
    result, cooldown_active, client_closed = asyncio.run(_run_probe(scenario))
    passed = (
        result is scenario["expected"]
        and cooldown_active is scenario["cooldown"]
        and client_closed
    )
    return {
        "scenario": name,
        "passed": passed,
        "expected_probe_result": scenario["expected"],
        "actual_probe_result": result,
        "expected_cooldown": scenario["cooldown"],
        "actual_cooldown": cooldown_active,
        "fake_http_session_closed": client_closed,
        "external_services_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("all", *SCENARIOS.keys()),
        default="all",
        help="response scenario to simulate (default: all)",
    )
    args = parser.parse_args()

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = [run_scenario(name) for name in names]
    report = {
        "suite": "DummyDreams TikTok Response Simulator",
        "passed": all(item["passed"] for item in results),
        "scenarios": results,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run deterministic gift-event QA simulations without TikTok or Discord.

All writes use a temporary SQLite database. No environment credentials,
configured Turso database, Discord client, or network API is used.
Run: python scripts/qa_simulator.py [--scenario all|gift|rollback]
"""

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import db  # noqa: E402


def _create_database(path: str, *, force_daily_failure: bool = False) -> None:
    """Create the minimum isolated schema needed by the real gift writer."""
    daily_constraint = "CHECK(coins < 0)" if force_daily_failure else ""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            f"""
            CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE gift_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tiktok_username TEXT,
                session_id INTEGER,
                viewer_username TEXT NOT NULL,
                viewer_display_name TEXT,
                gift_name TEXT,
                gift_count INTEGER,
                coins INTEGER NOT NULL,
                log_date TEXT,
                log_time TEXT,
                log_datetime TEXT,
                target_username TEXT,
                scheduled_host TEXT,
                msg_id TEXT UNIQUE,
                host_name TEXT
            );
            CREATE TABLE live_sessions (
                session_id INTEGER PRIMARY KEY,
                total_coins INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE tiktok_gifts (
                tiktok_user TEXT PRIMARY KEY,
                display_name TEXT,
                total_coins INTEGER NOT NULL
            );
            CREATE TABLE daily_tiktok_gifts (
                tiktok_user TEXT NOT NULL,
                display_name TEXT,
                coins INTEGER NOT NULL {daily_constraint},
                date TEXT NOT NULL,
                UNIQUE(tiktok_user, date)
            );
            INSERT INTO live_sessions (session_id, total_coins) VALUES (7, 0);
            """
        )
        connection.commit()
    finally:
        connection.close()


@contextmanager
def _isolated_database(path: str):
    """Redirect only the database dependencies used by the gift transaction."""
    original = {
        "get_db_connection": db.get_db_connection,
        "get_scheduled_host_name": db.get_scheduled_host_name,
        "set_setting": db.set_setting,
        "log_activity_event": db.log_activity_event,
        "column_cache": db._GIFT_LOG_COLUMN_INFO_CACHE,
        "column_cache_at": db._GIFT_LOG_COLUMN_INFO_CACHE_AT,
    }
    db.get_db_connection = lambda: sqlite3.connect(path)
    db.get_scheduled_host_name = lambda *args, **kwargs: "QA Scheduled Host"
    db.set_setting = lambda *args, **kwargs: None
    db.log_activity_event = lambda *args, **kwargs: None
    db._GIFT_LOG_COLUMN_INFO_CACHE = None
    db._GIFT_LOG_COLUMN_INFO_CACHE_AT = 0.0
    try:
        yield
    finally:
        db.get_db_connection = original["get_db_connection"]
        db.get_scheduled_host_name = original["get_scheduled_host_name"]
        db.set_setting = original["set_setting"]
        db.log_activity_event = original["log_activity_event"]
        db._GIFT_LOG_COLUMN_INFO_CACHE = original["column_cache"]
        db._GIFT_LOG_COLUMN_INFO_CACHE_AT = original["column_cache_at"]


def _read_totals(path: str) -> dict:
    connection = sqlite3.connect(path)
    try:
        return {
            "gift_logs": connection.execute(
                "SELECT COUNT(*) FROM gift_logs"
            ).fetchone()[0],
            "session_coins": connection.execute(
                "SELECT total_coins FROM live_sessions WHERE session_id = 7"
            ).fetchone()[0],
            "all_time_coins": connection.execute(
                "SELECT COALESCE(SUM(total_coins), 0) FROM tiktok_gifts"
            ).fetchone()[0],
            "daily_coins": connection.execute(
                "SELECT COALESCE(SUM(coins), 0) FROM daily_tiktok_gifts"
            ).fetchone()[0],
        }
    finally:
        connection.close()


def run_gift_simulation(*, force_failure: bool = False) -> dict:
    """Exercise the production gift transaction against a disposable SQLite DB."""
    with tempfile.TemporaryDirectory(prefix="dummydreams-qa-") as temp_dir:
        path = os.path.join(temp_dir, "qa.sqlite3")
        _create_database(path, force_daily_failure=force_failure)

        with _isolated_database(path):
            first = db.record_gift_event_transaction(
                session_id=7,
                target_user="qa_creator",
                viewer_user="qa_viewer",
                viewer_display="QA Viewer",
                gift_name="QA Rose",
                gift_count=1,
                coins=25,
                msg_id="qa-simulator-gift-001",
                host_name="QA Host",
            )
            duplicate = None
            if not force_failure:
                duplicate = db.record_gift_event_transaction(
                    session_id=7,
                    target_user="qa_creator",
                    viewer_user="qa_viewer",
                    viewer_display="QA Viewer",
                    gift_name="QA Rose",
                    gift_count=1,
                    coins=25,
                    msg_id="qa-simulator-gift-001",
                    host_name="QA Host",
                )

        totals = _read_totals(path)

    if force_failure:
        passed = (
            first.get("status") == "failed"
            and totals == {
                "gift_logs": 0,
                "session_coins": 0,
                "all_time_coins": 0,
                "daily_coins": 0,
            }
        )
        expected = "failed write rolled back every aggregate"
    else:
        passed = (
            first == {"status": "ok", "total_coins": 25}
            and duplicate == {"status": "duplicate"}
            and totals == {
                "gift_logs": 1,
                "session_coins": 25,
                "all_time_coins": 25,
                "daily_coins": 25,
            }
        )
        expected = "one write stored; duplicate did not increment totals"

    return {
        "scenario": "rollback" if force_failure else "gift_deduplication",
        "passed": passed,
        "expected": expected,
        "first_result": first,
        "duplicate_result": duplicate,
        "database_totals": totals,
        "database": "temporary SQLite (deleted after run)",
        "external_services_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("all", "gift", "rollback"),
        default="all",
        help="simulation scenario to run (default: all)",
    )
    args = parser.parse_args()

    scenarios = []
    if args.scenario in ("all", "gift"):
        scenarios.append(run_gift_simulation())
    if args.scenario in ("all", "rollback"):
        scenarios.append(run_gift_simulation(force_failure=True))

    report = {
        "suite": "DummyDreams Staging QA Simulator",
        "passed": all(item["passed"] for item in scenarios),
        "scenarios": scenarios,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

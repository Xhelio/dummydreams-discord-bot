#!/usr/bin/env python3
"""Preflight check for a test/stable deployment.

Run this before starting the bot:

    python scripts/preflight.py

It never writes data. It only verifies that the environment looks
consistent, so a test instance cannot silently point at production.
Exit code 0 = ready, 1 = blocking problem.
"""

import os
import sys

OK = "\033[92mOK\033[0m"
WARN = "\033[93mWARN\033[0m"
FAIL = "\033[91mFAIL\033[0m"

problems = []
warnings = []


def env(name, default=""):
    return str(os.environ.get(name, default) or "").strip()


def check(condition, message, blocking=True):
    if condition:
        print(f"[{OK}]   {message}")
        return True
    print(f"[{FAIL if blocking else WARN}]   {message}")
    (problems if blocking else warnings).append(message)
    return False


def main() -> int:
    bot_env = env("BOT_ENV") or env("APP_ENV") or "stable"
    bot_env = bot_env.lower()

    print(f"Deployment environment : {bot_env}")
    print("-" * 60)

    # --- identity -------------------------------------------------------
    print("Identity")
    check(bot_env in ("stable", "test"), f"BOT_ENV set to 'stable' or 'test' (got '{bot_env}')", blocking=False)
    check(bool(env("DISCORD_TOKEN")), "DISCORD_TOKEN present")
    check(bool(env("EXPECTED_GUILD_ID")), "EXPECTED_GUILD_ID present (guards against wrong server)", blocking=False)
    check(
        len(env("EXPECTED_GUILD_ID")) in (0, 17, 18, 19),
        "EXPECTED_GUILD_ID looks like a Discord snowflake",
        blocking=False,
    )

    # --- database -------------------------------------------------------
    print("\nDatabase")
    turso_url = env("TURSO_DATABASE_URL")
    turso_token = env("TURSO_AUTH_TOKEN")
    if turso_url:
        check(turso_url.startswith("libsql://") or turso_url.startswith("https://"), "TURSO_DATABASE_URL has a valid scheme")
        check(bool(turso_token), "TURSO_AUTH_TOKEN is set")
        try:
            import libsql_experimental  # noqa: F401

            print(f"[{OK}]   libsql_experimental importable")
        except Exception as exc:
            print(f"[{FAIL}]   libsql_experimental import failed: {exc}")
            problems.append("libsql_experimental not importable (bot would fall back to local SQLite)")
        try:
            from db import get_db_connection

            conn = get_db_connection()
            try:
                conn.execute("SELECT 1")
                tables = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
            finally:
                conn.close()
            print(f"[{OK}]   database reachable ({tables} tables)")
            check(tables >= 13, "expected schema present (>=13 tables)", blocking=False)
        except Exception as exc:
            print(f"[{FAIL}]   database connection failed: {exc}")
            problems.append("database not reachable")
    else:
        db_path = env("LOCAL_DB_PATH") or "database.db"
        print(f"[{WARN}]   TURSO_DATABASE_URL not set -> local SQLite fallback: {db_path}")
        warnings.append("using local SQLite instead of Turso (fine for dev only)")
        check(bot_env != "stable", "stable deployment uses Turso, not local SQLite")

    # --- admin panel ----------------------------------------------------
    print("\nAdmin panel")
    has_user = bool(env("ADMIN_USERNAME"))
    has_pass = bool(env("ADMIN_PASSWORD"))
    check(has_user and has_pass, "ADMIN_USERNAME and ADMIN_PASSWORD both present", blocking=False)
    check(has_user == has_pass, "admin credentials set as a pair", blocking=False)

    # --- notices --------------------------------------------------------
    print("\nNotices")
    if env("DEBUG_MODE").lower() in ("1", "true", "yes", "on"):
        print(f"[{WARN}]   DEBUG_MODE is on (extra presence label + verbose logs)")
    if bot_env == "test":
        print(f"[{WARN}]   TEST environment: make sure the token, database and guild are the test ones")
    if not env("SIGN_API_KEY") and not env("TIKTOK_SIGN_API_KEY"):
        print(f"[{WARN}]   no TikTok signing API key set (SIGN_API_KEY / TIKTOK_SIGN_API_KEY)")

    # --- summary --------------------------------------------------------
    print("-" * 60)
    if problems:
        print(f"{len(problems)} check(s) not satisfied:")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("Ready to start. Warnings:", len(warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())

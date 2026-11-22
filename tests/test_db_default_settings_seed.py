import sqlite3

import db


def test_default_settings_seed_uses_one_insert_and_preserves_admin_values():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
        ("master_enabled", "0"),
    )
    connection.commit()

    statements = []
    connection.set_trace_callback(statements.append)

    db._seed_default_settings(
        connection,
        {
            "master_enabled": "1",
            "debug_mode": 0,
            "feature_announcements": 1,
        },
    )
    connection.commit()

    rows = dict(connection.execute("SELECT key, value FROM bot_settings"))
    inserts = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("INSERT OR IGNORE")
    ]

    assert rows == {
        "master_enabled": "0",
        "debug_mode": "0",
        "feature_announcements": "1",
    }
    assert len(inserts) == 1
    connection.close()


def test_default_settings_seed_skips_empty_mapping():
    class Connection:
        def execute(self, *_args):
            raise AssertionError("empty defaults must not execute SQL")

    db._seed_default_settings(Connection(), {})


def test_role_tier_seed_state_reads_marker_and_count_together():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("CREATE TABLE role_tiers (id INTEGER PRIMARY KEY)")
    assert db._get_role_tier_seed_state(connection) == (False, 0)

    connection.execute(
        "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
        ("tiers_seeded", "1"),
    )
    connection.executemany("INSERT INTO role_tiers (id) VALUES (?)", [(1,), (2,), (3,)])
    assert db._get_role_tier_seed_state(connection) == (True, 3)
    connection.close()


def test_host_priority_normalization_skips_write_when_no_nulls_exist():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE hosts (host_id INTEGER PRIMARY KEY, is_priority INTEGER)"
    )
    connection.executemany(
        "INSERT INTO hosts (host_id, is_priority) VALUES (?, ?)",
        [(1, 1), (2, 0)],
    )
    statements = []
    connection.set_trace_callback(statements.append)

    assert db._normalize_host_priorities(connection) is False
    assert not any(
        statement.lstrip().upper().startswith("UPDATE HOSTS")
        for statement in statements
    )
    assert connection.execute(
        "SELECT is_priority FROM hosts ORDER BY host_id"
    ).fetchall() == [(1,), (0,)]
    connection.close()


def test_host_priority_normalization_repairs_only_null_values():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE hosts (host_id INTEGER PRIMARY KEY, is_priority INTEGER)"
    )
    connection.executemany(
        "INSERT INTO hosts (host_id, is_priority) VALUES (?, ?)",
        [(1, None), (2, 0), (3, 1)],
    )

    assert db._normalize_host_priorities(connection) is True
    assert connection.execute(
        "SELECT is_priority FROM hosts ORDER BY host_id"
    ).fetchall() == [(1,), (0,), (1,)]
    connection.close()


def test_host_alias_seed_state_reads_count_and_marker_together():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE host_aliases (alias TEXT PRIMARY KEY, host_name TEXT)")
    connection.execute("CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT)")
    assert db._get_host_alias_seed_state(connection) == (0, False)

    connection.executemany(
        "INSERT INTO host_aliases (alias, host_name) VALUES (?, ?)",
        [("angel", "Sinta"), ("mommy", "Sinta")],
    )
    connection.execute(
        "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
        ("host_aliases_repair_v1", "1"),
    )
    assert db._get_host_alias_seed_state(connection) == (2, True)
    connection.close()

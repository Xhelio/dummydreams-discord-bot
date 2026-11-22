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

import asyncio
import sqlite3

import db


def _connection_factory(path):
    return lambda: sqlite3.connect(path, timeout=5)


def _create_schema(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE role_tiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_key TEXT NOT NULL DEFAULT 'reset',
                tier_name TEXT NOT NULL,
                role_id TEXT NOT NULL,
                min_coins INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE user_mappings (
                discord_id TEXT PRIMARY KEY,
                tiktok_user TEXT UNIQUE
            );
            CREATE TABLE tiktok_gifts (
                tiktok_user TEXT PRIMARY KEY,
                display_name TEXT,
                total_coins INTEGER DEFAULT 0
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_update_role_tier_changes_min_coins_in_place(tmp_path, monkeypatch):
    path = tmp_path / "tiers.sqlite3"
    _create_schema(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO role_tiers (group_key, tier_name, role_id, min_coins) "
        "VALUES ('reset', 'Tier 1', '123', 100)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))

    db.update_role_tier(1, min_coins=2500)

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT group_key, tier_name, role_id, min_coins FROM role_tiers WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("reset", "Tier 1", "123", 2500)


def test_update_role_tier_clamps_negative_and_can_change_role(tmp_path, monkeypatch):
    path = tmp_path / "tiers-2.sqlite3"
    _create_schema(path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO role_tiers (group_key, tier_name, role_id, min_coins) "
        "VALUES ('permanent', 'Tier 1', '123', 100)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))

    db.update_role_tier(1, min_coins=-50, role_id="999")

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT role_id, min_coins FROM role_tiers WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("999", 0)


def test_get_all_user_coin_totals_includes_zero_coin_members(tmp_path, monkeypatch):
    path = tmp_path / "coins.sqlite3"
    _create_schema(path)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO user_mappings (discord_id, tiktok_user) VALUES (?, ?)",
        [("111", "alice"), ("222", "bob")],
    )
    conn.execute(
        "INSERT INTO tiktok_gifts (tiktok_user, display_name, total_coins) "
        "VALUES ('Alice', 'Alice', 500)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "get_db_connection", _connection_factory(path))

    rows = db.get_all_user_coin_totals()

    assert rows == [("111", "alice", 500), ("222", "bob", 0)]


def test_reconcile_tier_roles_syncs_every_mapping_without_announcements(monkeypatch):
    import main

    calls = []
    monkeypatch.setattr(
        main,
        "get_all_user_coin_totals",
        lambda: [("111", "alice", 500), ("222", "bob", 0)],
    )
    monkeypatch.setattr(main, "is_feature_enabled", lambda key: True)
    monkeypatch.setattr(main, "invalidate_role_tier_config_cache", lambda: None)
    monkeypatch.setattr(main, "log_activity_event", lambda *args, **kwargs: None)

    async def fake_assign(tiktok_user, total_coins, discord_id=None, announce=True):
        calls.append((tiktok_user, total_coins, discord_id, announce))

    monkeypatch.setattr(main, "auto_assign_role", fake_assign)

    class FakeBot:
        def is_ready(self):
            return True

    monkeypatch.setattr(main, "bot", FakeBot())

    async def run():
        # Recreate the lock so this test can use its own event loop.
        main._TIER_RECONCILE_LOCK = asyncio.Lock()
        return await main.reconcile_tier_roles("unit test")

    synced = asyncio.run(run())

    assert synced == 2
    assert calls == [
        ("alice", 500, "111", False),
        ("bob", 0, "222", False),
    ]

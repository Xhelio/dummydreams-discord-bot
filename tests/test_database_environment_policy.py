import pytest

import db


@pytest.mark.parametrize(
    ("has_driver", "database_url", "auth_token"),
    [
        (False, "libsql://example.turso.io", "test-token"),
        (True, None, "test-token"),
        (True, "libsql://example.turso.io", None),
    ],
)
def test_stable_environment_rejects_incomplete_turso_configuration(
    monkeypatch, has_driver, database_url, auth_token
):
    monkeypatch.setenv("BOT_ENV", "stable")
    monkeypatch.setattr(db, "HAS_LIBSQL", has_driver)
    monkeypatch.setattr(db, "TURSO_URL", database_url)
    monkeypatch.setattr(db, "TURSO_TOKEN", auth_token)

    with pytest.raises(
        RuntimeError, match="Production database configuration is incomplete"
    ):
        db.get_db_connection()


def test_stable_environment_connects_through_turso_driver(monkeypatch):
    sentinel = object()
    calls = []

    class FakeDriver:
        @staticmethod
        def connect(*args, **kwargs):
            calls.append((args, kwargs))
            return sentinel

    monkeypatch.setenv("BOT_ENV", "stable")
    monkeypatch.setattr(db, "libsql", FakeDriver)
    monkeypatch.setattr(db, "HAS_LIBSQL", True)
    monkeypatch.setattr(db, "TURSO_URL", "libsql://example.turso.io")
    monkeypatch.setattr(db, "TURSO_TOKEN", "test-token")

    assert db.get_db_connection() is sentinel
    assert calls == [
        (("libsql://example.turso.io",), {"auth_token": "test-token"})
    ]


def test_development_environment_keeps_local_sqlite_fallback(monkeypatch, tmp_path):
    database_path = tmp_path / "development.sqlite3"
    monkeypatch.delenv("BOT_ENV", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setattr(db, "HAS_LIBSQL", False)
    monkeypatch.setattr(db, "TURSO_URL", None)
    monkeypatch.setattr(db, "TURSO_TOKEN", None)
    monkeypatch.setenv("LOCAL_DB_PATH", str(database_path))

    connection = db.get_db_connection()
    try:
        connection.execute("CREATE TABLE probe (value TEXT)")
    finally:
        connection.close()

    assert database_path.exists()

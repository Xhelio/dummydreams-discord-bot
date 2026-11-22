def test_get_db_connection_uses_turso_serverless_driver(monkeypatch):
    import db

    sentinel = object()
    calls = []

    class FakeDriver:
        @staticmethod
        def connect(*args, **kwargs):
            calls.append((args, kwargs))
            return sentinel

    monkeypatch.setattr(db, "libsql", FakeDriver)
    monkeypatch.setattr(db, "HAS_LIBSQL", True)
    monkeypatch.setattr(db, "TURSO_URL", "libsql://example.turso.io")
    monkeypatch.setattr(db, "TURSO_TOKEN", "test-token")

    assert db.get_db_connection() is sentinel
    assert calls == [
        (("libsql://example.turso.io",), {"auth_token": "test-token"})
    ]

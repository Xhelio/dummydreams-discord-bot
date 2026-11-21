import db


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, row):
        self.row = row
        self.closed = False
        self.queries = []

    def execute(self, sql, params):
        self.queries.append((sql, params))
        return FakeResult(self.row)

    def close(self):
        self.closed = True


def reset_mapping_cache():
    with db._DISCORD_MAPPING_CACHE_LOCK:
        db._DISCORD_MAPPING_CACHE.clear()


def test_discord_mapping_cache_reuses_positive_result(monkeypatch):
    reset_mapping_cache()
    connection = FakeConnection(("123456789",))
    calls = []
    monkeypatch.setattr(db, "get_db_connection", lambda: calls.append(1) or connection)
    monkeypatch.setattr(db.time, "monotonic", lambda: 100.0)

    assert db.get_discord_id_from_tiktok("@ExampleUser") == "123456789"
    assert db.get_discord_id_from_tiktok("exampleuser") == "123456789"

    assert len(calls) == 1
    assert connection.closed


def test_discord_mapping_cache_expires(monkeypatch):
    reset_mapping_cache()
    connections = [
        FakeConnection(("123456789",)),
        FakeConnection(("987654321",)),
    ]
    monkeypatch.setattr(db, "get_db_connection", lambda: connections.pop(0))
    now = [100.0]
    monkeypatch.setattr(db.time, "monotonic", lambda: now[0])

    assert db.get_discord_id_from_tiktok("exampleuser") == "123456789"
    now[0] += db._DISCORD_MAPPING_CACHE_TTL + 0.01
    assert db.get_discord_id_from_tiktok("exampleuser") == "987654321"


def test_missing_mapping_is_not_cached(monkeypatch):
    reset_mapping_cache()
    connections = [FakeConnection(None), FakeConnection(("123456789",))]
    monkeypatch.setattr(db, "get_db_connection", lambda: connections.pop(0))

    assert db.get_discord_id_from_tiktok("newuser") is None
    assert db.get_discord_id_from_tiktok("newuser") == "123456789"

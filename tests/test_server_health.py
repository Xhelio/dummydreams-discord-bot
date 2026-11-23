import db
import pytest
import server


@pytest.mark.parametrize(
    ("database_ok", "expected_status"),
    [(True, 200), (False, 503)],
)
def test_health_uses_cached_database_health_helper(
    monkeypatch, database_ok, expected_status
):
    calls = []

    def fake_health_check():
        calls.append(True)
        return database_ok

    def unexpected_connection():
        raise AssertionError("/health must not open its own database connection")

    monkeypatch.setattr(db, "check_database_health", fake_health_check)
    monkeypatch.setattr(db, "get_db_connection", unexpected_connection)

    response = server.app.test_client().get("/health")

    assert response.status_code == expected_status
    assert response.get_json()["ok"] is database_ok
    assert response.get_json()["db"] is database_ok
    assert calls == [True]
    assert {
        "bot_ready",
        "bot_user",
        "env",
        "version",
        "live_tracked",
        "boot_id",
        "started_at",
        "admin_auth",
    }.issubset(response.get_json())

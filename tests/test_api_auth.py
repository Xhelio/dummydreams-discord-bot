from flask import Flask

import routes.api as api


def _client():
    app = Flask(__name__)
    app.register_blueprint(api.api_bp)
    app.config.update(TESTING=True)
    return app.test_client()


def test_api_key_rejects_when_secret_is_not_configured(monkeypatch):
    monkeypatch.delenv("API_SECRET_KEY", raising=False)

    assert api._api_key_valid("any-key") is False


def test_api_key_rejects_missing_or_blank_supplied_key(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "configured-secret")

    assert api._api_key_valid(None) is False
    assert api._api_key_valid("") is False
    assert api._api_key_valid("   ") is False


def test_api_key_rejects_incorrect_key(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "configured-secret")

    assert api._api_key_valid("wrong-secret") is False


def test_api_key_accepts_matching_key_with_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", " configured-secret ")

    assert api._api_key_valid(" configured-secret ") is True


def test_api_auth_error_returns_503_when_secret_is_not_configured(monkeypatch):
    monkeypatch.delenv("API_SECRET_KEY", raising=False)

    with api.request.test_request_context("/api/config"):
        response, status = api._api_auth_error()

    assert status == 503
    assert response.get_json()["message"] == "API authentication is not configured"


def test_api_auth_error_returns_401_for_missing_or_invalid_key(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "configured-secret")

    with api.request.test_request_context("/api/config"):
        response, status = api._api_auth_error()

    assert status == 401
    assert response.get_json()["message"] == "Unauthorized"

    with api.request.test_request_context(
        "/api/config", headers={"X-API-KEY": "incorrect"}
    ):
        response, status = api._api_auth_error()

    assert status == 401


def test_api_auth_error_allows_valid_key(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "configured-secret")

    with api.request.test_request_context(
        "/api/config", headers={"X-API-KEY": "configured-secret"}
    ):
        assert api._api_auth_error() is None


def test_heartbeat_requires_api_key(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "configured-secret")
    client = _client()

    response = client.post("/api/heartbeat", json={"username": "creator"})

    assert response.status_code == 401
    assert response.get_json()["message"] == "Unauthorized"


def test_heartbeat_rejects_missing_username(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "configured-secret")
    client = _client()

    response = client.post(
        "/api/heartbeat",
        headers={"X-API-KEY": "configured-secret"},
        json={},
    )

    assert response.status_code == 400
    assert response.get_json()["message"] == "Missing username"


def test_heartbeat_normalizes_username_and_updates_live_state(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "configured-secret")
    touched = []
    monkeypatch.setattr(api, "touch_live", lambda username: touched.append(username))
    monkeypatch.setattr(api, "get_open_live_session_id", lambda username: None)
    client = _client()

    response = client.post(
        "/api/heartbeat",
        headers={"X-API-KEY": "configured-secret"},
        json={"username": "@Creator"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert touched == ["creator"]

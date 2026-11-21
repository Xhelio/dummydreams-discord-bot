import base64

import server


def _basic_auth(username, password):
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def _client():
    server.app.config.update(TESTING=True)
    return server.app.test_client()


def test_admin_routes_fail_closed_when_credentials_are_not_configured(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_USERNAME", "")
    monkeypatch.setattr(server, "ADMIN_PASSWORD", "")
    client = _client()

    response = client.get("/admin/__auth_probe__")

    assert response.status_code == 503
    assert response.get_json() == {
        "ok": False,
        "error": "Admin authentication is not configured",
    }


def test_admin_routes_reject_missing_credentials(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(server, "ADMIN_PASSWORD", "correct-password")
    client = _client()

    response = client.get("/admin/__auth_probe__")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="Admin Panel"'


def test_admin_routes_reject_incorrect_credentials(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(server, "ADMIN_PASSWORD", "correct-password")
    client = _client()

    response = client.get(
        "/admin/__auth_probe__",
        headers=_basic_auth("admin", "wrong-password"),
    )

    assert response.status_code == 401


def test_admin_routes_allow_valid_credentials(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(server, "ADMIN_PASSWORD", "correct-password")
    client = _client()

    # The probe route intentionally does not exist; 404 confirms auth passed
    # without invoking database-backed admin handlers.
    response = client.get(
        "/admin/__auth_probe__",
        headers=_basic_auth("admin", "correct-password"),
    )

    assert response.status_code == 404


def test_non_admin_routes_are_not_blocked_by_admin_auth(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(server, "ADMIN_PASSWORD", "correct-password")
    client = _client()

    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert "Disallow: /admin/" in response.get_data(as_text=True)

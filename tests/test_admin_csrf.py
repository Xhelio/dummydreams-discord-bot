import base64

import server


def _auth_headers():
    credentials = base64.b64encode(b"admin:correct-password").decode("ascii")
    return {"Authorization": f"Basic {credentials}"}


def _client(monkeypatch):
    monkeypatch.setattr(server, "ADMIN_USERNAME", "admin")
    monkeypatch.setattr(server, "ADMIN_PASSWORD", "correct-password")
    server.app.config.update(TESTING=True, SECRET_KEY="csrf-test-secret")
    return server.app.test_client()


def _set_csrf_token(client, token="known-csrf-token"):
    with client.session_transaction() as session:
        session["_csrf_token"] = token
    return token


def test_admin_mutation_rejects_missing_csrf_token(monkeypatch):
    client = _client(monkeypatch)

    response = client.post("/admin/__csrf_probe__", headers=_auth_headers())

    assert response.status_code == 403
    assert b"Invalid or missing CSRF token" in response.data


def test_admin_mutation_rejects_incorrect_csrf_token(monkeypatch):
    client = _client(monkeypatch)
    _set_csrf_token(client)

    response = client.post(
        "/admin/__csrf_probe__",
        headers={**_auth_headers(), "X-CSRF-Token": "attacker-token"},
    )

    assert response.status_code == 403


def test_admin_mutation_accepts_valid_csrf_header(monkeypatch):
    client = _client(monkeypatch)
    token = _set_csrf_token(client)

    response = client.post(
        "/admin/__csrf_probe__",
        headers={
            **_auth_headers(),
            "X-CSRF-Token": token,
            "Origin": "http://localhost",
            "Sec-Fetch-Site": "same-origin",
        },
    )

    # The probe route is intentionally absent; 404 confirms both auth and CSRF
    # checks passed without invoking a database-backed admin handler.
    assert response.status_code == 404


def test_admin_mutation_rejects_cross_origin_even_with_valid_csrf(monkeypatch):
    client = _client(monkeypatch)
    token = _set_csrf_token(client)

    response = client.post(
        "/admin/__csrf_probe__",
        headers={
            **_auth_headers(),
            "X-CSRF-Token": token,
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "ok": False,
        "error": "Cross-origin request blocked",
    }


def test_non_admin_mutation_does_not_require_admin_csrf_token(monkeypatch):
    client = _client(monkeypatch)

    response = client.post("/api/__csrf_probe__")

    # The API probe route is intentionally absent.
    assert response.status_code == 404



def test_admin_mutation_accepts_valid_csrf_form_field(monkeypatch):
    client = _client(monkeypatch)
    token = _set_csrf_token(client)

    response = client.post(
        "/admin/__csrf_probe__",
        data={"csrf_token": token},
        headers=_auth_headers(),
    )

    # Missing route confirms the CSRF and authentication guards both passed.
    assert response.status_code == 404


def test_admin_mutation_accepts_legacy_csrf_header_alias(monkeypatch):
    client = _client(monkeypatch)
    token = _set_csrf_token(client)

    response = client.post(
        "/admin/__csrf_probe__",
        headers={**_auth_headers(), "X-CSRFToken": token},
    )

    assert response.status_code == 404


def test_admin_mutation_rejects_cross_origin_referer_without_origin(monkeypatch):
    client = _client(monkeypatch)
    token = _set_csrf_token(client)

    response = client.post(
        "/admin/__csrf_probe__",
        headers={
            **_auth_headers(),
            "X-CSRF-Token": token,
            "Referer": "https://attacker.example/submit",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "ok": False,
        "error": "Cross-origin request blocked",
    }


def test_admin_safe_method_does_not_require_csrf_token(monkeypatch):
    client = _client(monkeypatch)

    response = client.get(
        "/admin/__csrf_probe__",
        headers=_auth_headers(),
    )

    assert response.status_code == 404


def test_admin_json_mutation_returns_json_csrf_error(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        "/admin/__csrf_probe__",
        json={"action": "probe"},
        headers=_auth_headers(),
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "ok": False,
        "error": "Invalid or missing CSRF token",
    }

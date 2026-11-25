import hashlib
import hmac
import json

from flask import Flask

import routes.api as api


def _client():
    app = Flask(__name__)
    app.register_blueprint(api.api_bp)
    app.config.update(TESTING=True)
    return app.test_client()


def _api_headers():
    return {"X-API-KEY": "integration-secret"}


def test_health_endpoint_returns_dependency_status_and_payload(monkeypatch):
    monkeypatch.setattr(api, "check_database_health", lambda: True)
    monkeypatch.setattr(api, "get_bot_status", lambda: {
        "online": True, "user": "TestBot", "ready_at": "2026-11-21T10:00:00Z"
    })
    monkeypatch.setattr(api, "get_live_snapshot", lambda: {"creator": {"host": "Host A"}})
    monkeypatch.setattr(api, "get_observer_diag", lambda: {"creator": {"status": "connected"}})
    monkeypatch.setattr(api, "get_bool_setting", lambda key, default: default)

    response = _client().get("/api/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["database"] == "connected"
    assert payload["discord_bot"]["online"] is True
    assert payload["tiktok_live"]["count"] == 1
    assert payload["master_enabled"] is True


def test_health_endpoint_reports_database_warning(monkeypatch):
    monkeypatch.setattr(api, "check_database_health", lambda: False)
    monkeypatch.setattr(api, "get_bot_status", lambda: {})
    monkeypatch.setattr(api, "get_live_snapshot", lambda: {})
    monkeypatch.setattr(api, "get_observer_diag", lambda: {})
    monkeypatch.setattr(api, "get_bool_setting", lambda key, default: default)

    response = _client().get("/api/health")

    assert response.status_code == 200
    assert response.get_json()["status"] == "warning"
    assert response.get_json()["database"] == "error"


def test_config_endpoint_requires_authentication_when_secret_is_configured(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "integration-secret")

    response = _client().get("/api/config")

    assert response.status_code == 401
    assert response.get_json() == {"status": "error", "message": "Unauthorized"}


def test_config_endpoint_returns_matching_account_for_authenticated_request(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("API_SECRET_KEY", "integration-secret")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(json.dumps({
        "default_name": "Host",
        "summary_channel_id": 123,
        "overwrite_summary": True,
        "accounts": [{
            "account_name": "Creator One",
            "tiktok_username": "CreatorOne",
        }],
    }), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    response = _client().get(
        "/api/config?username=CREATORONE", headers=_api_headers()
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "account_name": "Creator One",
        "tiktok_username": "CreatorOne",
        "default_name": "Host",
        "summary_channel_id": 123,
        "overwrite_summary": True,
    }


def test_announcement_endpoint_validates_payload_and_invokes_handler(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "integration-secret")
    calls = []
    monkeypatch.setattr(api, "async_announcement_handler",
                        lambda username, is_live: calls.append((username, is_live)))

    client = _client()
    invalid = client.post("/api/announcement", headers=_api_headers(), json={
        "username": "creator"
    })
    valid = client.post("/api/announcement", headers=_api_headers(), json={
        "username": "creator", "is_live": True
    })

    assert invalid.status_code == 400
    assert valid.status_code == 200
    assert valid.get_json()["status"] == "success"
    assert calls == [("creator", True)]



def test_heartbeat_rejects_non_object_json_payloads(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "integration-secret")
    client = _client()

    for payload in ([], "creator", 42, None):
        response = client.post(
            "/api/heartbeat", headers=_api_headers(), json=payload
        )
        assert response.status_code == 400
        assert response.get_json()["message"] == "JSON object payload required"


def test_heartbeat_rejects_non_string_usernames(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "integration-secret")
    client = _client()

    for username in (123, [], {}, True):
        response = client.post(
            "/api/heartbeat", headers=_api_headers(), json={"username": username}
        )
        assert response.status_code == 400
        assert response.get_json()["message"] == "username must be a string"


def test_announcement_rejects_non_object_json_payloads(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "integration-secret")
    client = _client()

    for payload in ([], "creator", 42, None):
        response = client.post(
            "/api/announcement", headers=_api_headers(), json=payload
        )
        assert response.status_code == 400
        assert response.get_json()["message"] == "JSON object payload required"


def test_announcement_rejects_invalid_field_types(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "integration-secret")
    calls = []
    monkeypatch.setattr(
        api, "async_announcement_handler",
        lambda username, is_live: calls.append((username, is_live)),
    )
    client = _client()

    invalid_payloads = (
        {"username": 123, "is_live": True},
        {"username": "creator", "is_live": "true"},
        {"username": "creator", "is_live": 1},
        {"username": "   ", "is_live": False},
    )
    for payload in invalid_payloads:
        response = client.post(
            "/api/announcement", headers=_api_headers(), json=payload
        )
        assert response.status_code == 400

    assert calls == []

def test_euler_webhook_rejects_missing_secret(monkeypatch):
    monkeypatch.delenv("EULER_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("EULER_WEBHOOK_SIGNING_SECRET", raising=False)

    response = _client().post("/api/euler-webhook", json={
        "username": "creator", "is_live": False
    })

    assert response.status_code == 503
    assert response.get_json()["error"] == "EULER_WEBHOOK_SECRET not configured"


def test_euler_webhook_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("EULER_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setattr(api, "log_activity_event", lambda *args: None)

    response = _client().post(
        "/api/euler-webhook",
        data=b'{"username":"creator","is_live":false}',
        content_type="application/json",
        headers={"X-Webhook-Signature": "invalid"},
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "Invalid webhook signature"


def test_euler_webhook_processes_signed_offline_event(monkeypatch):
    secret = "webhook-secret"
    raw_body = b'{"username":"@Creator","is_live":false}'
    signature = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    ended = []
    cleared = []
    monkeypatch.setenv("EULER_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(api, "get_open_live_session_id", lambda username: 42)
    monkeypatch.setattr(api, "end_live_session", lambda session_id, end_time=None: ended.append((session_id, end_time)))
    monkeypatch.setattr(api, "clear_live", lambda username: cleared.append(username))
    monkeypatch.setattr(api, "update_observer_diag", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "log_activity_event", lambda *args: None)
    monkeypatch.setattr(api, "_trigger_announcement_async", lambda *args: True)

    response = _client().post(
        "/api/webhooks/eulerstream",
        data=raw_body,
        content_type="application/json",
        headers={"X-Webhook-Signature": signature},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["handled"] == 1
    assert payload["results"][0]["username"] == "creator"
    assert payload["results"][0]["is_live"] is False
    assert len(ended) == 1 and ended[0][0] == 42
    assert ended[0][1]  # end time forwarded (event time or receive fallback)
    assert cleared == ["creator"]



def test_heartbeat_and_announcement_reject_malformed_json(monkeypatch):
    monkeypatch.setenv("API_SECRET_KEY", "integration-secret")
    client = _client()

    for endpoint in ("/api/heartbeat", "/api/announcement"):
        response = client.post(
            endpoint,
            data=b"{",
            content_type="application/json",
            headers=_api_headers(),
        )
        assert response.status_code == 400
        assert response.get_json()["message"] == "JSON object payload required"


def test_config_endpoint_hides_internal_read_errors(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("API_SECRET_KEY", "integration-secret")
    monkeypatch.chdir(tmp_path)

    response = _client().get("/api/config", headers=_api_headers())

    assert response.status_code == 500
    assert response.get_json() == {
        "status": "error",
        "message": "Unable to read configuration",
    }
    assert str(tmp_path) not in response.get_data(as_text=True)
    assert "Failed to read API configuration" in caplog.text


def test_euler_webhook_rejects_oversized_event_batch(monkeypatch):
    secret = "webhook-secret"
    raw_body = json.dumps([{"event": "unrecognized"}] * (
        api.MAX_EULER_WEBHOOK_BATCH_SIZE + 1
    )).encode("utf-8")
    signature = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    monkeypatch.setenv("EULER_WEBHOOK_SECRET", secret)

    response = _client().post(
        "/api/euler-webhook",
        data=raw_body,
        content_type="application/json",
        headers={"X-Webhook-Signature": signature},
    )

    assert response.status_code == 413
    assert response.get_json() == {
        "ok": False,
        "error": "Webhook batch exceeds maximum event count",
        "max_events": api.MAX_EULER_WEBHOOK_BATCH_SIZE,
    }

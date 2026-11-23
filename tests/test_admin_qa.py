import base64
import subprocess

import server
import routes.qa as qa


def _auth_headers(username="qa-admin", password="qa-password"):
    credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {credentials}"}


def _client(monkeypatch):
    # QA credentials are deliberately independent from the shared admin account.
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("STAGING_QA_USERNAME", "qa-admin")
    monkeypatch.setenv("STAGING_QA_PASSWORD", "qa-password")
    monkeypatch.setenv("BOT_ENV", "test")
    monkeypatch.setenv("STAGING_QA_DASHBOARD_ENABLED", "1")
    server.app.config.update(TESTING=True, SECRET_KEY="qa-dashboard-test-secret")
    return server.app.test_client()


def test_dashboard_is_hidden_outside_test_environment(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setenv("BOT_ENV", "stable")

    response = client.get("/admin/qa", headers=_auth_headers())

    assert response.status_code == 404


def test_dashboard_is_disabled_until_explicit_feature_flag(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.delenv("STAGING_QA_DASHBOARD_ENABLED")

    response = client.get("/admin/qa", headers=_auth_headers())

    assert response.status_code == 404


def test_dashboard_fails_closed_when_dedicated_credentials_are_missing(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.delenv("STAGING_QA_USERNAME")
    monkeypatch.delenv("STAGING_QA_PASSWORD")

    response = client.get("/admin/qa", headers=_auth_headers())

    assert response.status_code == 503


def test_dashboard_requires_dedicated_qa_auth_even_if_admin_credentials_are_present(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(server, "ADMIN_USERNAME", "production-admin")
    monkeypatch.setattr(server, "ADMIN_PASSWORD", "production-password")

    response = client.get(
        "/admin/qa",
        headers=_auth_headers("production-admin", "production-password"),
    )

    assert response.status_code == 401


def test_dashboard_renders_only_with_dedicated_qa_credentials(monkeypatch):
    client = _client(monkeypatch)

    response = client.get("/admin/qa", headers=_auth_headers())

    assert response.status_code == 200
    assert b"Staging QA Console" in response.data
    assert b"X-CSRF-Token" in response.data


def test_run_scenario_uses_allowlisted_command_and_secret_free_environment(monkeypatch):
    client = _client(monkeypatch)
    with client.session_transaction() as session:
        session["_csrf_token"] = "qa-csrf-token"

    monkeypatch.setenv("DISCORD_TOKEN", "must-not-be-forwarded")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "must-not-be-forwarded")
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"passed": true}',
            stderr="",
        )

    monkeypatch.setattr(qa.subprocess, "run", fake_run)
    response = client.post(
        "/admin/qa/run",
        json={"scenario": "gift-all"},
        headers={
            **_auth_headers(),
            "X-CSRF-Token": "qa-csrf-token",
            "Origin": "https://staging.example",
            "Sec-Fetch-Site": "same-origin",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["passed"] is True
    assert captured["argv"][-2:] == ["--scenario", "all"]
    assert captured["argv"][-3].endswith("qa_simulator.py")
    assert "shell" not in captured
    assert captured["env"] == {
        "BOT_ENV": "test",
        "PYTHONPATH": str(qa.PROJECT_ROOT),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PATH": qa.os.environ["PATH"],
    }
    assert "DISCORD_TOKEN" not in captured["env"]
    assert "TURSO_AUTH_TOKEN" not in captured["env"]
    assert captured["timeout"] == 30


def test_unknown_scenario_is_rejected_without_running_a_process(monkeypatch):
    client = _client(monkeypatch)
    with client.session_transaction() as session:
        session["_csrf_token"] = "qa-csrf-token"

    def unexpected_run(*args, **kwargs):
        raise AssertionError("unknown scenarios must not launch a process")

    monkeypatch.setattr(qa.subprocess, "run", unexpected_run)
    response = client.post(
        "/admin/qa/run",
        json={"scenario": "../../main.py"},
        headers={
            **_auth_headers(),
            "X-CSRF-Token": "qa-csrf-token",
            "Sec-Fetch-Site": "same-origin",
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Unknown QA scenario"


def test_dashboard_runner_timeout_is_reported_safely(monkeypatch):
    client = _client(monkeypatch)
    with client.session_transaction() as session:
        session["_csrf_token"] = "qa-csrf-token"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(qa.subprocess, "run", timeout)
    response = client.post(
        "/admin/qa/run",
        json={"scenario": "tiktok-all"},
        headers={
            **_auth_headers(),
            "X-CSRF-Token": "qa-csrf-token",
            "Sec-Fetch-Site": "same-origin",
        },
    )

    assert response.status_code == 504
    assert response.get_json()["error"] == "QA runner exceeded the 30-second limit"

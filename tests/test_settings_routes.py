from flask import Flask

import routes.settings as settings


class FakeResult:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, rows=None, selected_row=None):
        self.rows = rows or []
        self.selected_row = selected_row
        self.executed = []
        self.committed = False
        self.closed = False

    def execute(self, query, params=()):
        self.executed.append((query, params))
        if query.lstrip().upper().startswith("SELECT"):
            if "COALESCE(is_priority,1)" in query:
                return FakeResult(row=self.selected_row)
            return FakeResult(rows=self.rows)
        return FakeResult()

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def _client():
    app = Flask(__name__)
    app.register_blueprint(settings.settings_bp)
    app.config.update(TESTING=True)
    return app.test_client()


def test_parse_id_list_normalizes_and_deduplicates_values():
    assert settings._parse_id_list(" 123, 456\n789 123 invalid -4  ") == [
        123, 456, 789
    ]
    assert settings._parse_id_list("") == []


def test_checkbox_reflects_form_presence():
    app = Flask(__name__)
    with app.test_request_context("/", method="POST", data={"enabled": "0"}):
        assert settings._checkbox("enabled") == "1"
        assert settings._checkbox("missing") == "0"


def test_save_host_priority_mode_normalizes_and_clamps_values(monkeypatch):
    saved = []
    events = []
    monkeypatch.setattr(settings, "set_setting", lambda key, value: saved.append((key, value)))
    monkeypatch.setattr(settings, "log_activity_event", lambda *args: events.append(args))

    response = _client().post(
        "/admin/settings/host-priority-mode",
        data={"non_priority_mode": " OFF ", "non_priority_slow_seconds": "12"},
    )

    assert response.status_code == 302
    assert saved == [
        ("non_priority_mode", "off"),
        ("non_priority_slow_seconds", "60"),
    ]
    assert len(events) == 1
    assert "non-priority=off" in events[0][1]


def test_save_host_priority_mode_falls_back_on_invalid_values(monkeypatch):
    saved = []
    monkeypatch.setattr(settings, "set_setting", lambda key, value: saved.append((key, value)))
    monkeypatch.setattr(settings, "log_activity_event", lambda *args: None)

    response = _client().post(
        "/admin/settings/host-priority-mode",
        data={"non_priority_mode": "unknown", "non_priority_slow_seconds": "not-a-number"},
    )

    assert response.status_code == 302
    assert saved == [
        ("non_priority_mode", "slow"),
        ("non_priority_slow_seconds", "300"),
    ]


def test_add_tier_accepts_numeric_role_and_clamps_negative_coins(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "add_role_tier", lambda *args: calls.append(args))

    response = _client().post(
        "/admin/settings/tier/add",
        data={"group_key": "permanent", "role_id": "12345", "min_coins": "-9"},
    )

    assert response.status_code == 302
    assert calls == [("permanent", "12345", 0)]
    assert "Tier%20added" in response.headers["Location"]


def test_add_tier_rejects_non_numeric_role_id(monkeypatch):
    calls = []
    monkeypatch.setattr(settings, "add_role_tier", lambda *args: calls.append(args))

    response = _client().post(
        "/admin/settings/tier/add",
        data={"role_id": "role-123", "min_coins": "not-a-number"},
    )

    assert response.status_code == 302
    assert calls == []
    assert "Invalid%20role%20ID" in response.headers["Location"]


def test_remove_tier_delegates_to_database_helper(monkeypatch):
    deleted = []
    monkeypatch.setattr(settings, "delete_role_tier", deleted.append)

    response = _client().post("/admin/settings/tier/delete/42")

    assert response.status_code == 302
    assert deleted == [42]


def test_alias_routes_delegate_form_values(monkeypatch):
    added = []
    deleted = []
    monkeypatch.setattr(settings, "add_host_alias", lambda *args: added.append(args))
    monkeypatch.setattr(settings, "delete_host_alias", deleted.append)

    client = _client()
    add_response = client.post(
        "/admin/settings/alias/add",
        data={"alias": "mommy", "host_name": "Sinta"},
    )
    delete_response = client.post(
        "/admin/settings/alias/delete",
        data={"alias": "mommy"},
    )

    assert add_response.status_code == 302
    assert delete_response.status_code == 302
    assert added == [("mommy", "Sinta")]
    assert deleted == ["mommy"]


def test_save_hosts_batch_updates_and_deletes_only_changed_rows(monkeypatch):
    conn = FakeConnection(rows=[
        (1, "alice", 1, 1),
        (2, "bob", 0, 1),
        (3, "charlie", 1, 0),
    ])
    events = []
    monkeypatch.setattr(settings, "get_db_connection", lambda: conn)
    monkeypatch.setattr(settings, "log_activity_event", lambda *args: events.append(args))

    response = _client().post(
        "/admin/settings/hosts/save",
        data={
            "active_1": "1",
            "priority_1": "1",
            "active_2": "1",
            "priority_2": "1",
            "delete_3": "1",
        },
    )

    writes = [(query, params) for query, params in conn.executed
              if query.lstrip().upper().startswith(("UPDATE", "DELETE"))]
    assert response.status_code == 302
    assert writes == [
        ("UPDATE hosts SET is_active = ?, is_priority = ? WHERE host_id = ?", (1, 1, 2)),
        ("DELETE FROM hosts WHERE host_id = ?", (3,)),
    ]
    assert conn.committed is True
    assert conn.closed is True
    assert "changed=2, deleted=1" in events[0][1]


def test_toggle_host_priority_flips_existing_value(monkeypatch):
    conn = FakeConnection(selected_row=("Alice", 1))
    events = []
    monkeypatch.setattr(settings, "get_db_connection", lambda: conn)
    monkeypatch.setattr(settings, "log_activity_event", lambda *args: events.append(args))

    response = _client().post("/admin/toggle-host-priority/7")

    assert response.status_code == 302
    assert conn.executed[-1] == (
        "UPDATE hosts SET is_priority = ? WHERE host_id = ?", (0, 7)
    )
    assert conn.committed is True
    assert conn.closed is True
    assert events[0][2] == "alice"


def test_toggle_host_priority_handles_missing_host(monkeypatch):
    conn = FakeConnection(selected_row=None)
    events = []
    monkeypatch.setattr(settings, "get_db_connection", lambda: conn)
    monkeypatch.setattr(settings, "log_activity_event", lambda *args: events.append(args))

    response = _client().post("/admin/toggle-host-priority/999")

    assert response.status_code == 302
    assert conn.committed is False
    assert conn.closed is True
    assert events == []

import db


def test_set_current_host_queues_name_and_timestamp(monkeypatch):
    queued = []
    monkeypatch.setattr(db, "_queue_setting_or_write", lambda key, value: queued.append((key, value)))

    db.set_current_host_for_account("@Sample_User", "Host A")

    assert [key for key, _ in queued] == [
        "current_host_sample_user",
        "current_host_at_sample_user",
    ]
    assert queued[0][1] == "Host A"
    assert len(queued[1][1]) == 19


def test_touch_current_host_queues_timestamp_only_when_host_exists(monkeypatch):
    queued = []
    monkeypatch.setattr(db, "get_setting", lambda key: "Host A")
    monkeypatch.setattr(db, "_queue_setting_or_write", lambda key, value: queued.append((key, value)))

    db.touch_current_host_detection("@Sample_User")

    assert len(queued) == 1
    assert queued[0][0] == "current_host_at_sample_user"
    assert len(queued[0][1]) == 19


def test_touch_current_host_skips_missing_host(monkeypatch):
    monkeypatch.setattr(db, "get_setting", lambda key: "")
    monkeypatch.setattr(
        db,
        "_queue_setting_or_write",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected write")),
    )

    db.touch_current_host_detection("sample_user")


def test_queue_setting_helper_falls_back_when_queue_is_full(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "queue_setting", lambda key, value: False)
    monkeypatch.setattr(db, "set_setting", lambda key, value: calls.append((key, value)))

    db._queue_setting_or_write("current_host_sample_user", "Host A")

    assert calls == [("current_host_sample_user", "Host A")]

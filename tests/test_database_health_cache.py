import threading
from types import SimpleNamespace

import db


def _reset_health_state(monkeypatch, *, ok=True, at=100.0, refreshing=False):
    monkeypatch.setattr(db, "_DB_HEALTH_CACHE", {"ok": ok, "at": at})
    monkeypatch.setattr(db, "_DB_HEALTH_LOCK", threading.Lock())
    monkeypatch.setattr(db, "_DB_HEALTH_REFRESHING", refreshing)
    monkeypatch.setattr(db, "time", SimpleNamespace(time=lambda: 105.0))


def test_database_health_returns_fresh_cached_result(monkeypatch):
    _reset_health_state(monkeypatch, ok=True, at=100.0)
    monkeypatch.setattr(
        db, "_probe_database_health",
        lambda: (_ for _ in ()).throw(AssertionError("probe should not run")),
    )

    assert db.check_database_health() is True


def test_database_health_refreshes_stale_cache_in_background(monkeypatch):
    _reset_health_state(monkeypatch, ok=True, at=90.0)
    scheduled = []
    monkeypatch.setattr(
        db, "_schedule_database_health_refresh",
        lambda: scheduled.append(True),
    )

    assert db.check_database_health() is True
    assert scheduled == [True]
    assert db._DB_HEALTH_REFRESHING is True


def test_database_health_does_not_schedule_duplicate_refresh(monkeypatch):
    _reset_health_state(monkeypatch, ok=False, at=90.0, refreshing=True)
    scheduled = []
    monkeypatch.setattr(
        db, "_schedule_database_health_refresh",
        lambda: scheduled.append(True),
    )

    assert db.check_database_health() is False
    assert scheduled == []


def test_database_health_fails_closed_when_cache_exceeds_max_stale(monkeypatch):
    _reset_health_state(monkeypatch, ok=True, at=60.0)
    scheduled = []
    monkeypatch.setattr(
        db, "_schedule_database_health_refresh",
        lambda: scheduled.append(True),
    )

    assert db.check_database_health() is False
    assert scheduled == [True]


def test_database_health_cold_start_probes_and_caches_result(monkeypatch):
    _reset_health_state(monkeypatch, ok=True, at=0.0)
    probes = []
    monkeypatch.setattr(
        db, "_probe_database_health",
        lambda: probes.append(True) or False,
    )

    assert db.check_database_health() is False
    assert db._DB_HEALTH_CACHE == {"ok": False, "at": 105.0}
    assert probes == [True]

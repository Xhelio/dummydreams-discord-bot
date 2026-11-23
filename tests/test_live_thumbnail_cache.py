import time

import routes.admin as admin


class _QueuedExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, func, *args):
        self.jobs.append((func, args))


def _reset_thumbnail_cache(monkeypatch):
    with admin._LIVE_THUMBNAIL_LOCK:
        admin._LIVE_THUMBNAIL_CACHE.clear()
        admin._LIVE_THUMBNAIL_INFLIGHT.clear()
    executor = _QueuedExecutor()
    monkeypatch.setattr(admin, "_LIVE_THUMBNAIL_EXECUTOR", executor)
    return executor


def test_live_thumbnail_refresh_is_single_flight(monkeypatch):
    executor = _reset_thumbnail_cache(monkeypatch)

    admin._schedule_live_thumbnail_refresh("@Creator")
    admin._schedule_live_thumbnail_refresh("creator")

    assert len(executor.jobs) == 1
    assert executor.jobs[0][1] == ("creator",)
    with admin._LIVE_THUMBNAIL_LOCK:
        assert "creator" in admin._LIVE_THUMBNAIL_INFLIGHT


def test_live_thumbnail_refresh_respects_cache_ttl(monkeypatch):
    executor = _reset_thumbnail_cache(monkeypatch)
    with admin._LIVE_THUMBNAIL_LOCK:
        admin._LIVE_THUMBNAIL_CACHE["creator"] = {
            "thumbnail_url": "https://p16-sign.tiktokcdn.com/cover.jpeg",
            "status": "ready",
            "checked_at": time.time(),
        }

    admin._schedule_live_thumbnail_refresh("creator")

    assert executor.jobs == []
    state = admin._get_live_thumbnail_state("creator")
    assert state["status"] == "ready"
    state["status"] = "mutated"
    assert admin._get_live_thumbnail_state("creator")["status"] == "ready"


def test_live_thumbnail_refresh_retries_after_cache_expires(monkeypatch):
    executor = _reset_thumbnail_cache(monkeypatch)
    with admin._LIVE_THUMBNAIL_LOCK:
        admin._LIVE_THUMBNAIL_CACHE["creator"] = {
            "thumbnail_url": None,
            "status": "unavailable",
            "checked_at": time.time() - admin._LIVE_THUMBNAIL_TTL - 1,
        }

    admin._schedule_live_thumbnail_refresh("creator")

    assert len(executor.jobs) == 1

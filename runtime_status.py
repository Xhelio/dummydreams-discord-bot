"""Small shared runtime state used by both the bot loop and the Flask dashboard.

The TikTok live state lives in memory instead of being inferred from stale rows
in the database, so restarting the bot cannot leave accounts stuck as "Live".
"""

import threading
from datetime import datetime, timezone

_LOCK = threading.Lock()
_BOT = {
    "online": False,
    "user": None,
    "ready_at": None,
}
_LIVE = {}
_OBSERVER = {}


def set_bot_ready(user_name: str):
    with _LOCK:
        _BOT["online"] = True
        _BOT["user"] = user_name
        _BOT["ready_at"] = datetime.now(timezone.utc).isoformat()


def set_bot_offline():
    with _LOCK:
        _BOT["online"] = False
        _BOT["ready_at"] = datetime.now(timezone.utc).isoformat()


def get_bot_status() -> dict:
    with _LOCK:
        return dict(_BOT)


def set_live(username: str, host_name: str = None, source: str = "event"):
    username = (username or "").lower().lstrip("@")
    if not username:
        return
    with _LOCK:
        item = _LIVE.get(username, {})
        item["host"] = host_name or item.get("host") or "Host"
        item["since"] = item.get("since") or datetime.now(timezone.utc).isoformat()
        item["last_event"] = datetime.now(timezone.utc).isoformat()
        item["source"] = source
        _LIVE[username] = item


def touch_live(username: str):
    username = (username or "").lower().lstrip("@")
    with _LOCK:
        if username in _LIVE:
            _LIVE[username]["last_event"] = datetime.now(timezone.utc).isoformat()


def clear_live(username: str):
    username = (username or "").lower().lstrip("@")
    with _LOCK:
        _LIVE.pop(username, None)


def clear_all_live():
    with _LOCK:
        _LIVE.clear()


def get_live_usernames() -> list:
    with _LOCK:
        return list(_LIVE.keys())


def get_live_snapshot() -> dict:
    with _LOCK:
        return {username: dict(data) for username, data in _LIVE.items()}


def update_observer_diag(username: str, **fields):
    username = (username or "").lower().lstrip("@")
    if not username:
        return
    with _LOCK:
        item = _OBSERVER.get(username, {})
        item.update(fields)
        item["updated_at"] = datetime.now(timezone.utc).isoformat()
        _OBSERVER[username] = item


def get_observer_diag() -> dict:
    with _LOCK:
        return {username: dict(data) for username, data in _OBSERVER.items()}

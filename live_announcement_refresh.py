"""Small pure helpers for periodic active LIVE announcement refreshes."""

LIVE_ANNOUNCEMENT_REFRESH_INTERVAL_SECONDS = 60


def get_live_announcement_refresh_targets(sessions, live_hosts):
    """Return unique stored announcement usernames that are currently live."""
    live = {
        str(username or "").lower().lstrip("@").strip()
        for username in (live_hosts or [])
        if str(username or "").strip()
    }
    targets = []
    seen = set()
    for session in sessions or []:
        if not isinstance(session, dict):
            continue
        username = str(session.get("tiktok_username") or "").lower().lstrip("@").strip()
        if username and username in live and username not in seen:
            seen.add(username)
            targets.append(username)
    return targets

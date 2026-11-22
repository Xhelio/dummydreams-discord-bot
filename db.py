import os
import json
import logging
import queue
import re
import time
import threading
import sys
from functools import lru_cache
try:
    # Pure-Python HTTP driver: unlike libsql-experimental's native block_on
    # calls, urllib socket I/O releases the GIL while worker threads wait.
    import turso_serverless as libsql
    HAS_LIBSQL = True
except ImportError:
    import sqlite3 as libsql
    HAS_LIBSQL = False

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

def get_db_connection():
    if HAS_LIBSQL and TURSO_URL:
        return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)
    import sqlite3
    db_file = os.environ.get("LOCAL_DB_PATH", "database.db")
    return sqlite3.connect(db_file)
from datetime import datetime, timedelta
import pytz

_DB_HEALTH_CACHE = {"ok": True, "at": 0.0}
_ACTIVITY_LOGGER = logging.getLogger("bot.activity")

# Short-lived cache for bot_settings. Hot paths (gift/comment handlers) read
# several flags per event; without this each read opened a new Turso connection
# and blocked the Discord event loop long enough to time out interactions.
_SETTINGS_CACHE = {}
_SETTINGS_CACHE_TTL = 15.0
# Missing keys (e.g. per-host rate-limit counters) are cached briefly so the
# event loop does not queue a refresh for them on every read.
_SETTINGS_MISS_TTL = 60.0
_SETTINGS_CACHE_LOCK = threading.Lock()
_SETTINGS_REFRESHING = set()
_SETTINGS_WARMED = False
_SETTINGS_REFRESH_QUEUE: "queue.Queue[str]" = queue.Queue(maxsize=2000)
_SETTINGS_REFRESH_WORKER = None
_SETTINGS_REFRESH_WORKER_LOCK = threading.Lock()


def invalidate_settings_cache():
    """Drop every cached setting (used after bulk admin saves)."""
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE.clear()


def _fetch_setting_value(key: str):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def warm_settings_cache():
    """Load every setting into memory (call once, off the event loop).

    After this, ``get_setting`` hits memory even for keys that have never been
    read, so a cache miss can never block the Discord event loop.
    """
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT key, value FROM bot_settings").fetchall()
    finally:
        conn.close()
    now = time.time()
    global _SETTINGS_WARMED
    with _SETTINGS_CACHE_LOCK:
        for key, value in rows:
            _SETTINGS_CACHE[key] = (value, now + _SETTINGS_CACHE_TTL)
        _SETTINGS_WARMED = True
    # Start the refresh worker here: warm-up runs in a worker thread, so the
    # event loop never pays for Thread.start() when a setting goes stale.
    start_settings_refresh_worker()
    # Start the other background writers here too. Thread.start() can block for
    # seconds on a busy CPU, and doing it lazily from the loop would freeze the
    # first interaction ACK after a restart.
    try:
        _ensure_activity_log_writer()
        _ensure_setting_writer()
    except Exception:
        pass


def _settings_refresh_worker() -> None:
    """Single background worker that refreshes stale settings.

    Spawning one ``threading.Thread`` per stale key from the Discord event loop
    was catastrophic: ``Thread.start()`` waits for the OS thread to start, which
    took 2-3 s on a busy Render CPU and froze the event loop long enough to miss
    Discord's 3-second interaction ACK window. This worker is started once and
    fed through a queue, so scheduling a refresh never blocks.
    """
    while True:
        key = _SETTINGS_REFRESH_QUEUE.get()
        if key is None:
            return
        try:
            value = _fetch_setting_value(key)
            with _SETTINGS_CACHE_LOCK:
                _SETTINGS_CACHE[key] = (value, time.time() + _SETTINGS_CACHE_TTL)
        except Exception as e:
            print(f"[DB] Settings refresh failed for {key!r}: {type(e).__name__}: {e}")
            # Keep serving the stale value and retry soon instead of hammering.
            with _SETTINGS_CACHE_LOCK:
                cached = _SETTINGS_CACHE.get(key)
                if cached is not None:
                    _SETTINGS_CACHE[key] = (cached[0], time.time() + 5.0)
        finally:
            with _SETTINGS_CACHE_LOCK:
                _SETTINGS_REFRESHING.discard(key)


def start_settings_refresh_worker() -> None:
    """Start the background settings-refresh thread exactly once."""
    global _SETTINGS_REFRESH_WORKER
    with _SETTINGS_REFRESH_WORKER_LOCK:
        if _SETTINGS_REFRESH_WORKER is not None and _SETTINGS_REFRESH_WORKER.is_alive():
            return
        _SETTINGS_REFRESH_WORKER = threading.Thread(
            target=_settings_refresh_worker,
            name="settings-refresh",
            daemon=True,
        )
        _SETTINGS_REFRESH_WORKER.start()


def _schedule_setting_refresh(key: str):
    """Queue one stale setting for refresh; never blocks the caller."""
    with _SETTINGS_CACHE_LOCK:
        if key in _SETTINGS_REFRESHING:
            return
        _SETTINGS_REFRESHING.add(key)
    if _SETTINGS_REFRESH_WORKER is None:
        start_settings_refresh_worker()
    try:
        _SETTINGS_REFRESH_QUEUE.put_nowait(key)
    except queue.Full:
        with _SETTINGS_CACHE_LOCK:
            _SETTINGS_REFRESHING.discard(key)


# SQLite/Turso has a single writer. Now that gift/comment handlers run in worker
# threads, concurrent write transactions can be rolled back by Turso with
# SQLITE_BUSY ("stream was idle for too long"). Serialise writes in this process
# and retry the transient failures with a fresh connection.
_DB_WRITE_LOCK = threading.Lock()
_DB_MODULE_DIAGNOSTIC_LOCK = threading.Lock()
_DB_MODULE_DIAGNOSTICS_EMITTED = False


def _log_duplicate_db_module_instances() -> None:
    """Detect multiple imports of db.py that would each own a different write lock."""
    global _DB_MODULE_DIAGNOSTICS_EMITTED
    if _DB_MODULE_DIAGNOSTICS_EMITTED:
        return
    with _DB_MODULE_DIAGNOSTIC_LOCK:
        if _DB_MODULE_DIAGNOSTICS_EMITTED:
            return
        module_path = os.path.realpath(__file__)
        aliases = []
        for name, module in tuple(sys.modules.items()):
            module_file = getattr(module, "__file__", None)
            if module_file and os.path.realpath(module_file) == module_path:
                aliases.append(
                    (name, id(module), id(getattr(module, "_DB_WRITE_LOCK", None)))
                )
        lock_ids = {entry[2] for entry in aliases}
        if len(lock_ids) > 1:
            print(f"[DB] Duplicate db.py imports detected; write locks differ: {aliases}")
        _DB_MODULE_DIAGNOSTICS_EMITTED = True
_TRANSIENT_DB_TOKENS = (
    "sqlite_busy",
    "database is locked",
    "database table is locked",
    "stream error",
    "stream was idle",
    "transaction was rolled back",
    "retry the transaction",
    "hrana",
    "connection reset",
    "connection closed",
    "connection refused",
    "bad gateway",
    "service unavailable",
    "timed out",
    "timeout",
)


def is_transient_db_error(exc) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(token in text for token in _TRANSIENT_DB_TOKENS)


def _execute_atomic_statement(conn, sql: str, params=()):
    """Execute one write as a single remote batch when the driver supports it."""
    batch = getattr(conn, "batch", None)
    if callable(batch):
        return batch([(sql, params)], mode="immediate")
    return conn.execute(sql, params)


def write_transaction(
    operation,
    attempts: int = 4,
    base_delay: float = 0.35,
    timing_label: str = None,
):
    """Run one complete write transaction under the process-wide lock.

    Connection creation, callback execution, commit, rollback, and close are
    all covered by the retry boundary. The optional timing label records slow
    lock/connection/operation/commit stages for selected hot paths.
    """
    try:
        attempts = max(1, int(attempts))
        base_delay = max(0.0, float(base_delay))
    except (TypeError, ValueError):
        raise ValueError("attempts and base_delay must be numeric")

    def _log_stage(stage: str, elapsed: float, attempt: int) -> None:
        if timing_label and elapsed >= 0.1:
            print(
                f"[DB Timing] operation={timing_label} stage={stage} "
                f"elapsed={elapsed:.3f}s attempt={attempt}"
            )

    _log_duplicate_db_module_instances()
    last_error = None
    for attempt in range(1, attempts + 1):
        attempt_started = time.perf_counter()
        lock_started = time.perf_counter()
        acquired = _DB_WRITE_LOCK.acquire(timeout=30.0)
        lock_wait = time.perf_counter() - lock_started
        _log_stage("lock_wait", lock_wait, attempt)
        if not acquired:
            raise TimeoutError(
                "Database write lock busy for 30s; another write is stalled"
            )
        conn = None
        lock_released = False

        def _release_write_lock():
            nonlocal lock_released
            if not lock_released:
                _DB_WRITE_LOCK.release()
                lock_released = True

        try:
            connect_started = time.perf_counter()
            conn = get_db_connection()
            _log_stage("connect", time.perf_counter() - connect_started, attempt)

            operation_started = time.perf_counter()
            result = operation(conn)
            _log_stage("operation", time.perf_counter() - operation_started, attempt)

            commit_started = time.perf_counter()
            conn.commit()
            _log_stage("commit", time.perf_counter() - commit_started, attempt)
            # The transaction is durable now. Do not make other writers wait
            # for the driver's HTTP stream-close cleanup request.
            _release_write_lock()
            return result
        except Exception as exc:
            last_error = exc
            rollback_succeeded = False
            if conn is not None:
                try:
                    conn.rollback()
                    rollback_succeeded = True
                except Exception:
                    pass
            # A confirmed rollback also ends the critical section. If rollback
            # failed, keep the lock until close() can tear down the stream.
            if rollback_succeeded:
                _release_write_lock()
        finally:
            close_started = time.perf_counter()
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            _log_stage("close", time.perf_counter() - close_started, attempt)
            _release_write_lock()
            _log_stage("transaction_total", time.perf_counter() - attempt_started, attempt)

        if attempt >= attempts or not is_transient_db_error(last_error):
            raise last_error

        delay = base_delay * (2 ** (attempt - 1))
        operation_name = f" operation={timing_label}" if timing_label else ""
        print(
            f"[DB] Transient write error{operation_name} "
            f"thread={threading.current_thread().name} "
            f"lock_id={id(_DB_WRITE_LOCK)} "
            f"(attempt {attempt}/{attempts}), retrying in {delay:.1f}s: "
            f"{type(last_error).__name__}: {last_error}"
        )
        time.sleep(delay)

    raise last_error

def get_database_backend() -> str:
    if HAS_LIBSQL and TURSO_URL:
        return "Turso/libSQL"
    return "SQLite lokal"


def check_database_health() -> bool:
    now = time.time()
    if now - _DB_HEALTH_CACHE["at"] < 10:
        return bool(_DB_HEALTH_CACHE["ok"])
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        row = cur.fetchone()
        conn.close()
        ok = bool(row and row[0] == 1)
    except Exception as e:
        print(f"[HealthCheck Error] {e}")
        ok = False
    _DB_HEALTH_CACHE["ok"] = ok
    _DB_HEALTH_CACHE["at"] = now
    return ok

# Hanya host asli yang digunakan untuk deteksi host (6 akun resmi).
def _load_valid_target_accounts():
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        accounts = data.get("accounts") or []
        out = []
        for i, a in enumerate(accounts, 1):
            uname = str(a.get("tiktok_username", "")).lower().lstrip("@").strip()
            dname = str(a.get("account_name") or a.get("display_name") or uname).strip()
            if uname:
                out.append((i, uname, dname))
        return out
    except Exception as e:
        print(f"[DB] Failed to load valid target accounts: {e}")
        return []

VALID_TARGET_ACCOUNTS = _load_valid_target_accounts()

def get_channel_display_name(tiktok_username: str) -> str:
    """Mengembalikan nama channel resmi dari akun target TikTok."""
    u_clean = tiktok_username.lower().lstrip("@")
    for _, u, name in VALID_TARGET_ACCOUNTS:
        if u == u_clean:
            return name
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT display_name FROM hosts WHERE LOWER(tiktok_username) = ?", (u_clean,)).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    finally:
        conn.close()
    return u_clean

def sync_valid_hosts(conn=None, reactivate: bool = False):
    """Ensure official target accounts exist without deleting custom hosts.

    Existing rows are left alone unless ``reactivate=True`` (the manual
    "Sync 6 Official Accounts" button). Startup must not re-enable hosts the
    admin turned off, otherwise every deploy resets the host connection setup.
    """
    own = conn is None
    if own:
        conn = get_db_connection()
    try:
        accounts = list(VALID_TARGET_ACCOUNTS)
        if not accounts:
            conn.commit()
            return

        usernames = [uname for _, uname, _ in accounts]
        placeholders = ", ".join("?" for _ in usernames)
        rows = conn.execute(
            "SELECT host_id, LOWER(tiktok_username), display_name FROM hosts "
            f"WHERE LOWER(tiktok_username) IN ({placeholders})",
            tuple(usernames),
        ).fetchall()
        existing = {str(row[1]).lower(): row for row in rows}

        display_updates = []
        missing = []
        existing_ids = []
        for host_id, uname, dname in accounts:
            row = existing.get(uname)
            if row:
                existing_ids.append(row[0])
                # Only fill an empty channel name; never overwrite an edited one.
                if not str(row[2] or "").strip():
                    display_updates.extend((row[0], dname))
            else:
                missing.append((host_id, uname, dname))

        # Update all existing official rows in one statement.
        if existing_ids and (display_updates or reactivate):
            assignments = []
            params = []
            if display_updates:
                cases = " ".join(
                    "WHEN ? THEN ?" for _ in range(len(display_updates) // 2)
                )
                assignments.append(
                    f"display_name = CASE host_id {cases} ELSE display_name END"
                )
                params.extend(display_updates)
            if reactivate:
                assignments.append("is_active = 1")
            id_placeholders = ", ".join("?" for _ in existing_ids)
            params.extend(existing_ids)
            conn.execute(
                f"UPDATE hosts SET {', '.join(assignments)} "
                f"WHERE host_id IN ({id_placeholders})",
                tuple(params),
            )

        # New official accounts retain their preferred IDs; if a custom host
        # already occupies one, fall back to an automatically assigned ID.
        cur = conn.cursor()
        for host_id, uname, dname in missing:
            try:
                cur.execute(
                    "INSERT INTO hosts "
                    "(host_id, tiktok_username, display_name, is_active) "
                    "VALUES (?, ?, ?, 1)",
                    (host_id, uname, dname),
                )
            except Exception:
                cur.execute(
                    "INSERT INTO hosts (tiktok_username, display_name, is_active) "
                    "VALUES (?, ?, 1)",
                    (uname, dname),
                )
        conn.commit()
    except Exception as e:
        print(f"[Hosts Sync Error] {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if own:
            conn.close()

def get_recent_gift_activities(limit: int = 100, channel_filter: str = "") -> list:
    """Mengambil riwayat aktivitas gift: tanggal, nama akun, user, nama gift, jumlah koin, total koin saat ini."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        sql = """
            SELECT 
                g.log_id,
                g.log_datetime,
                g.target_username,
                COALESCE(h.display_name, g.target_username) as channel_name,
                g.viewer_username,
                COALESCE(g.viewer_display_name, g.viewer_username) as viewer_display,
                g.gift_name,
                g.gift_count,
                g.coins,
                COALESCE(u.total_coins, 0) as current_total_coins,
                COALESCE(NULLIF(g.host_name, ''), NULLIF(g.scheduled_host, ''), g.target_username) as host_name
            FROM gift_logs g
            LEFT JOIN hosts h ON LOWER(g.target_username) = LOWER(h.tiktok_username)
            LEFT JOIN tiktok_gifts u ON LOWER(g.viewer_username) = LOWER(u.tiktok_user)
        """
        params = []
        if channel_filter:
            sql += " WHERE LOWER(g.target_username) = ?"
            params.append(channel_filter.lower().lstrip("@"))
        sql += " ORDER BY g.log_id DESC LIMIT ?"
        params.append(limit)
        return cur.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()


def get_last_gift_for_tiktok(tiktok_user: str):
    """Gift terakhir yang tercatat untuk sebuah username TikTok (viewer).

    Mengembalikan dict atau None. Dipakai oleh command admin !coins @user.
    """
    u_clean = str(tiktok_user or "").lower().lstrip("@")
    if not u_clean:
        return None
    conn = get_db_connection()
    try:
        row = conn.execute("""
            SELECT g.gift_name, g.gift_count, g.coins, g.log_datetime,
                   g.log_date, g.log_time, g.target_username, g.host_name,
                   g.scheduled_host, g.viewer_display_name
            FROM gift_logs g
            WHERE LOWER(g.viewer_username) = ?
            ORDER BY g.log_id DESC
            LIMIT 1
        """, (u_clean,)).fetchone()
        if not row:
            return None
        return {
            "gift_name": row[0] or "",
            "gift_count": int(row[1] or 1),
            "coins": int(row[2] or 0),
            "log_datetime": row[3] or "",
            "log_date": row[4] or "",
            "log_time": row[5] or "",
            "target_username": row[6] or "",
            "host_name": row[7] or row[8] or row[6] or "",
            "viewer_display_name": row[9] or "",
        }
    finally:
        conn.close()

# ----------------------------- Analytics -----------------------------

_ANALYTICS_HOST_KEY = (
    "LOWER(COALESCE(NULLIF(g.host_name, ''), NULLIF(g.scheduled_host, ''), g.target_username))"
)
_ANALYTICS_HOST_LABEL = (
    "COALESCE(MAX(NULLIF(g.host_name, '')), MAX(NULLIF(g.scheduled_host, '')), "
    "MAX(g.target_username))"
)


def _analytics_gift_where(start_date, end_date, host=None, account=None, viewer=None):
    # Use substr to be robust if log_date was stored with a time component.
    clauses = ["substr(g.log_date, 1, 10) BETWEEN ? AND ?"]
    params = [str(start_date), str(end_date)]
    if host:
        clauses.append(_ANALYTICS_HOST_KEY + " = ?")
        params.append(str(host).strip().lower().lstrip("@"))
    if account:
        clauses.append("LOWER(g.target_username) = ?")
        params.append(str(account).strip().lower().lstrip("@"))
    if viewer:
        clauses.append("LOWER(g.viewer_username) = ?")
        params.append(str(viewer).strip().lower().lstrip("@"))
    return " AND ".join(clauses), tuple(params)


def _analytics_session_where(start_date, end_date, host=None, account=None):
    clauses = ["substr(start_time, 1, 10) BETWEEN ? AND ?"]
    params = [str(start_date), str(end_date)]
    if account:
        clauses.append("LOWER(tiktok_username) = ?")
        params.append(str(account).strip().lower().lstrip("@"))
    if host:
        clauses.append("LOWER(COALESCE(NULLIF(scheduled_host, ''), tiktok_username)) = ?")
        params.append(str(host).strip().lower().lstrip("@"))
    return " AND ".join(clauses), tuple(params)


def get_analytics_date_bounds(host=None, account=None, viewer=None):
    """Return (min_date_str, max_date_str) across gift_logs/live_sessions for filters, or (None,None)."""
    g_where, g_params = _analytics_gift_where('0000-01-01', '9999-12-31', host, account, viewer)
    s_where, s_params = _analytics_session_where('0000-01-01', '9999-12-31', host, account)
    mins = []
    maxs = []
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        try:
            row = cur.execute(f"SELECT MIN(substr(g.log_date,1,10)), MAX(substr(g.log_date,1,10)) FROM gift_logs g WHERE {g_where}", g_params).fetchone()
            if row:
                if row[0]: mins.append(str(row[0])[:10])
                if row[1]: maxs.append(str(row[1])[:10])
        except Exception:
            pass
        try:
            row = cur.execute(f"SELECT MIN(substr(start_time,1,10)), MAX(substr(start_time,1,10)) FROM live_sessions WHERE {s_where}", s_params).fetchone()
            if row:
                if row[0]: mins.append(str(row[0])[:10])
                if row[1]: maxs.append(str(row[1])[:10])
        except Exception:
            pass
        if not mins or not maxs:
            return (None, None)
        return (min(mins), max(maxs))
    finally:
        conn.close()


def get_analytics_summary(start_date, end_date, host=None, account=None, viewer=None) -> dict:
    """Ringkasan koin/gift/viewer/host/akun pada rentang tanggal."""
    where, params = _analytics_gift_where(start_date, end_date, host, account, viewer)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute(f"""
            SELECT COALESCE(SUM(g.coins), 0),
                   COUNT(*),
                   COALESCE(SUM(g.gift_count), 0),
                   COUNT(DISTINCT LOWER(g.viewer_username)),
                   COUNT(DISTINCT {_ANALYTICS_HOST_KEY}),
                   COUNT(DISTINCT LOWER(g.target_username))
            FROM gift_logs g
            WHERE {where}
        """, params).fetchone()
        return {
            "coins": int(row[0] or 0) if row else 0,
            "gift_rows": int(row[1] or 0) if row else 0,
            "gifts": int(row[2] or 0) if row else 0,
            "viewers": int(row[3] or 0) if row else 0,
            "hosts": int(row[4] or 0) if row else 0,
            "accounts": int(row[5] or 0) if row else 0,
        }
    except Exception as e:
        print(f"[Analytics] summary failed: {e}")
        return {"coins": 0, "gift_rows": 0, "gifts": 0, "viewers": 0, "hosts": 0, "accounts": 0}
    finally:
        conn.close()


def get_analytics_sessions(start_date, end_date, host=None, account=None) -> dict:
    """Ringkasan sesi live (traffic) pada rentang tanggal."""
    where, params = _analytics_session_where(start_date, end_date, host, account)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute(f"""
            SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0),
                   COUNT(DISTINCT LOWER(tiktok_username))
            FROM live_sessions
            WHERE {where}
        """, params).fetchone()
        count = int(row[0] or 0) if row else 0
        duration = int(row[1] or 0) if row else 0
        return {
            "sessions": count,
            "duration": duration,
            "avg_duration": int(duration / count) if count else 0,
            "accounts": int(row[2] or 0) if row else 0,
        }
    except Exception as e:
        print(f"[Analytics] sessions failed: {e}")
        return {"sessions": 0, "duration": 0, "avg_duration": 0, "accounts": 0}
    finally:
        conn.close()


def get_analytics_session_breakdown(start_date, end_date, host=None, account=None) -> dict:
    """Sesi live per host dan per akun: {lower_key: (sessions, duration)}."""
    where, params = _analytics_session_where(start_date, end_date, host, account)
    by_host = {}
    by_account = {}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute(f"""
            SELECT LOWER(tiktok_username),
                   LOWER(COALESCE(NULLIF(scheduled_host, ''), tiktok_username)),
                   COUNT(*),
                   COALESCE(SUM(duration_seconds), 0)
            FROM live_sessions
            WHERE {where}
            GROUP BY 1, 2
        """, params).fetchall()
        for account_key, host_key, sessions, duration in rows:
            a = (account_key or "").lower()
            h = (host_key or "").lower()
            sa, da = by_account.get(a, (0, 0))
            by_account[a] = (sa + int(sessions or 0), da + int(duration or 0))
            sh, dh = by_host.get(h, (0, 0))
            by_host[h] = (sh + int(sessions or 0), dh + int(duration or 0))
        return {"by_host": by_host, "by_account": by_account}
    except Exception as e:
        print(f"[Analytics] session breakdown failed: {e}")
        return {"by_host": {}, "by_account": {}}
    finally:
        conn.close()


def _analytics_parse_date(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _analytics_bucket_start(value, group_by):
    if group_by == "week":
        return value - timedelta(days=value.weekday())
    if group_by == "month":
        return value.replace(day=1)
    return value


def _analytics_bucket_key(value, group_by):
    return _analytics_bucket_start(value, group_by).isoformat()


def _analytics_bucket_label(value, group_by):
    if group_by == "week":
        return f"W{value.isocalendar()[1]:02d} {value.year}"
    if group_by == "month":
        return value.strftime("%b %Y")
    return value.strftime("%d %b")


def _analytics_next_bucket(value, group_by):
    if group_by == "week":
        return value + timedelta(days=7)
    if group_by == "month":
        if value.month == 12:
            return value.replace(year=value.year + 1, month=1)
        return value.replace(month=value.month + 1)
    return value + timedelta(days=1)


def get_analytics_timeseries(start_date, end_date, group_by="day", host=None,
                             account=None, viewer=None) -> list:
    """Deret waktu koin/gift/viewer/sesi per hari, minggu, bulan, jam (dalam sehari),
    atau hour_of_day (rata-rata per jam tertentu/clock hour di seluruh rentang).

    Bucket kosong tetap disertakan (nilai 0) supaya grafik tidak bolong.
    Untuk hour_of_day, metrik aditif dirata-ratakan per hari kalender dalam rentang;
    viewer/sesi/akun/host adalah rata-rata jumlah unik per hari pada jam tersebut.
    """
    group_by = group_by if group_by in ("day", "week", "month", "hour", "hour_of_day") else "day"
    start_d = _analytics_parse_date(start_date)
    end_d = _analytics_parse_date(end_date)
    if not start_d or not end_d or start_d > end_d:
        return []

    where, params = _analytics_gift_where(start_date, end_date, host, account, viewer)
    swhere, sparams = _analytics_session_where(start_date, end_date, host, account)

    if group_by == "hour_of_day":
        days_count = max(1, (end_d - start_d).days + 1)
        order = [f"{h:02d}" for h in range(24)]
        buckets = {
            k: {
                "key": k, "label": f"{k}:00",
                "coins": 0.0, "gifts": 0.0, "gift_events": 0.0, "sessions": 0.0,
                "viewers": 0.0, "active_accounts": 0.0, "active_hosts": 0.0,
            } for k in order
        }
        hour_expr = "substr(COALESCE(NULLIF(g.log_time, ''), substr(COALESCE(g.log_datetime, ''), 12, 8), '00:00:00'), 1, 2)"
        day_expr = "substr(COALESCE(g.log_date, substr(COALESCE(g.log_datetime, ''), 1, 10), ''), 1, 10)"
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            # Additive gift metrics: average per calendar day for each clock hour
            for hour_key, coins, gifts, events in cur.execute(f"""
                SELECT h, COALESCE(SUM(coins), 0), COALESCE(SUM(gifts), 0), COALESCE(SUM(events), 0)
                FROM (
                    SELECT {hour_expr} AS h, {day_expr} AS d,
                           COALESCE(SUM(g.coins), 0) AS coins,
                           COALESCE(SUM(g.gift_count), 0) AS gifts,
                           COUNT(*) AS events
                    FROM gift_logs g
                    WHERE {where}
                    GROUP BY h, d
                )
                GROUP BY h
            """, params).fetchall():
                hk = str(hour_key or "00")[:2].zfill(2)
                bucket = buckets.get(hk)
                if not bucket:
                    continue
                bucket["coins"] = float(coins or 0) / days_count
                bucket["gifts"] = float(gifts or 0) / days_count
                bucket["gift_events"] = float(events or 0) / days_count

            # Unique viewers per day-hour, averaged across days
            for hour_key, viewer_sum in cur.execute(f"""
                SELECT h, COALESCE(SUM(daily_viewers), 0)
                FROM (
                    SELECT {hour_expr} AS h, {day_expr} AS d,
                           COUNT(DISTINCT LOWER(g.viewer_username)) AS daily_viewers
                    FROM gift_logs g
                    WHERE {where}
                    GROUP BY h, d
                )
                GROUP BY h
            """, params).fetchall():
                hk = str(hour_key or "00")[:2].zfill(2)
                bucket = buckets.get(hk)
                if bucket:
                    bucket["viewers"] = float(viewer_sum or 0) / days_count

            shour_expr = "substr(COALESCE(start_time, ''), 12, 2)"
            sday_expr = "substr(COALESCE(start_time, ''), 1, 10)"
            # Sessions and active accounts/hosts per day-hour, averaged across days
            for hour_key, sess_sum, acc_sum, host_sum in cur.execute(f"""
                SELECT h,
                       COALESCE(SUM(sessions), 0),
                       COALESCE(SUM(active_accounts), 0),
                       COALESCE(SUM(active_hosts), 0)
                FROM (
                    SELECT {shour_expr} AS h, {sday_expr} AS d,
                           COUNT(*) AS sessions,
                           COUNT(DISTINCT LOWER(tiktok_username)) AS active_accounts,
                           COUNT(DISTINCT LOWER(COALESCE(NULLIF(scheduled_host, ''), tiktok_username))) AS active_hosts
                    FROM live_sessions
                    WHERE {swhere}
                    GROUP BY h, d
                )
                GROUP BY h
            """, sparams).fetchall():
                hk = str(hour_key or "00")[:2].zfill(2)
                bucket = buckets.get(hk)
                if not bucket:
                    continue
                bucket["sessions"] = float(sess_sum or 0) / days_count
                bucket["active_accounts"] = float(acc_sum or 0) / days_count
                bucket["active_hosts"] = float(host_sum or 0) / days_count

            def _fmt_avg(v):
                # keep decimals where meaningful, avoid noisy floats for near-integers
                if abs(v - round(v)) < 1e-9:
                    return int(round(v))
                return round(v, 2)

            return [
                {
                    "key": buckets[k]["key"],
                    "label": buckets[k]["label"],
                    "coins": _fmt_avg(buckets[k]["coins"]),
                    "gifts": _fmt_avg(buckets[k]["gifts"]),
                    "gift_events": _fmt_avg(buckets[k]["gift_events"]),
                    "viewers": _fmt_avg(buckets[k]["viewers"]),
                    "sessions": _fmt_avg(buckets[k]["sessions"]),
                    "active_accounts": _fmt_avg(buckets[k]["active_accounts"]),
                    "active_hosts": _fmt_avg(buckets[k]["active_hosts"]),
                }
                for k in order
            ]
        except Exception as e:
            print(f"[Analytics] timeseries (hour_of_day) failed: {e}")
            return []
        finally:
            conn.close()

    if group_by == "hour":
        day_str = start_d.strftime("%Y-%m-%d")
        order = [f"{h:02d}" for h in range(24)]
        buckets = {
            k: {
                "key": k, "label": f"{k}:00",
                "coins": 0, "gifts": 0, "gift_events": 0, "sessions": 0,
                "_viewers": set(), "_accounts": set(), "_hosts": set(),
            } for k in order
        }
        hour_expr = "substr(COALESCE(NULLIF(g.log_time, ''), substr(COALESCE(g.log_datetime, ''), 12, 8), '00:00:00'), 1, 2)"
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            for hour_key, coins, gifts, events in cur.execute(f"""
                SELECT {hour_expr} AS h,
                       COALESCE(SUM(g.coins), 0),
                       COALESCE(SUM(g.gift_count), 0),
                       COUNT(*)
                FROM gift_logs g
                WHERE {where}
                GROUP BY h
            """, params).fetchall():
                hk = str(hour_key or "00")[:2].zfill(2)
                bucket = buckets.get(hk)
                if not bucket:
                    continue
                bucket["coins"] += int(coins or 0)
                bucket["gifts"] += int(gifts or 0)
                bucket["gift_events"] += int(events or 0)

            for hour_key, viewer_key in cur.execute(f"""
                SELECT {hour_expr} AS h, LOWER(g.viewer_username)
                FROM gift_logs g
                WHERE {where}
                GROUP BY h, LOWER(g.viewer_username)
            """, params).fetchall():
                hk = str(hour_key or "00")[:2].zfill(2)
                bucket = buckets.get(hk)
                if bucket and viewer_key:
                    bucket["_viewers"].add(viewer_key)

            shour_expr = "substr(COALESCE(start_time, ''), 12, 2)"
            for hour_key, session_count, account_key, host_key in cur.execute(f"""
                SELECT {shour_expr} AS h,
                       COUNT(*),
                       LOWER(tiktok_username),
                       LOWER(COALESCE(NULLIF(scheduled_host, ''), tiktok_username))
                FROM live_sessions
                WHERE {swhere}
                GROUP BY h, LOWER(tiktok_username),
                         LOWER(COALESCE(NULLIF(scheduled_host, ''), tiktok_username))
            """, sparams).fetchall():
                hk = str(hour_key or "00")[:2].zfill(2)
                bucket = buckets.get(hk)
                if not bucket:
                    continue
                bucket["sessions"] += int(session_count or 0)
                if account_key:
                    bucket["_accounts"].add(account_key)
                if host_key:
                    bucket["_hosts"].add(host_key)

            return [
                {
                    "key": buckets[k]["key"],
                    "label": buckets[k]["label"],
                    "coins": buckets[k]["coins"],
                    "gifts": buckets[k]["gifts"],
                    "gift_events": buckets[k]["gift_events"],
                    "viewers": len(buckets[k]["_viewers"]),
                    "sessions": buckets[k]["sessions"],
                    "active_accounts": len(buckets[k]["_accounts"]),
                    "active_hosts": len(buckets[k]["_hosts"]),
                }
                for k in order
            ]
        except Exception as e:
            print(f"[Analytics] timeseries (hour) failed: {e}")
            return []
        finally:
            conn.close()

    buckets = {}
    order = []
    cursor_day = _analytics_bucket_start(start_d, group_by)
    while cursor_day <= end_d:
        key = _analytics_bucket_key(cursor_day, group_by)
        if key not in buckets:
            buckets[key] = {
                "key": key,
                "label": _analytics_bucket_label(cursor_day, group_by),
                "coins": 0,
                "gifts": 0,
                "gift_events": 0,
                "sessions": 0,
                "_viewers": set(),
                "_accounts": set(),
                "_hosts": set(),
            }
            order.append(key)
        cursor_day = _analytics_next_bucket(cursor_day, group_by)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for log_date, coins, gifts, events in cur.execute(f"""
            SELECT substr(g.log_date, 1, 10) AS log_date,
                   COALESCE(SUM(g.coins), 0),
                   COALESCE(SUM(g.gift_count), 0),
                   COUNT(*)
            FROM gift_logs g
            WHERE {where}
            GROUP BY substr(g.log_date, 1, 10)
        """, params).fetchall():
            parsed = _analytics_parse_date(log_date)
            if not parsed:
                continue
            bucket = buckets.get(_analytics_bucket_key(parsed, group_by))
            if not bucket:
                continue
            bucket["coins"] += int(coins or 0)
            bucket["gifts"] += int(gifts or 0)
            bucket["gift_events"] += int(events or 0)

        for log_date, viewer_key in cur.execute(f"""
            SELECT substr(g.log_date, 1, 10) AS log_date, LOWER(g.viewer_username)
            FROM gift_logs g
            WHERE {where}
            GROUP BY substr(g.log_date, 1, 10), LOWER(g.viewer_username)
        """, params).fetchall():
            parsed = _analytics_parse_date(log_date)
            if not parsed:
                continue
            bucket = buckets.get(_analytics_bucket_key(parsed, group_by))
            if bucket and viewer_key:
                bucket["_viewers"].add(viewer_key)

        for log_date, session_count, account_key, host_key in cur.execute(f"""
            SELECT substr(start_time, 1, 10),
                   COUNT(*),
                   LOWER(tiktok_username),
                   LOWER(COALESCE(NULLIF(scheduled_host, ''), tiktok_username))
            FROM live_sessions
            WHERE {swhere}
            GROUP BY substr(start_time, 1, 10), LOWER(tiktok_username),
                     LOWER(COALESCE(NULLIF(scheduled_host, ''), tiktok_username))
        """, sparams).fetchall():
            parsed = _analytics_parse_date(log_date)
            if not parsed:
                continue
            bucket = buckets.get(_analytics_bucket_key(parsed, group_by))
            if not bucket:
                continue
            bucket["sessions"] += int(session_count or 0)
            if account_key:
                bucket["_accounts"].add(account_key)
            if host_key:
                bucket["_hosts"].add(host_key)

        return [
            {
                "key": buckets[k]["key"],
                "label": buckets[k]["label"],
                "coins": buckets[k]["coins"],
                "gifts": buckets[k]["gifts"],
                "gift_events": buckets[k]["gift_events"],
                "viewers": len(buckets[k]["_viewers"]),
                "sessions": buckets[k]["sessions"],
                "active_accounts": len(buckets[k]["_accounts"]),
                "active_hosts": len(buckets[k]["_hosts"]),
            }
            for k in order
        ]
    except Exception as e:
        print(f"[Analytics] timeseries failed: {e}")
        return []
    finally:
        conn.close()


def get_analytics_host_engagement_timeseries(start_date, end_date, group_by="day", host=None,
                                             account=None, viewer=None, limit=12) -> dict:
    """Per-host gift engagement over time buckets (from gift_logs).

    Engagement metrics per bucket per host: transactions (gift rows), coins, unique viewers.
    group_by: day/week/month/hour/hour_of_day.
    hour_of_day = rata-rata per jam tertentu (00-23) di seluruh rentang tanggal.
    Returns {"labels": [...], "keys": [...], "hosts": [{key,name,transactions[],coins[],viewers[],totals...}, ...]}.
    """
    group_by = group_by if group_by in ("day", "week", "month", "hour", "hour_of_day") else "day"
    start_d = _analytics_parse_date(start_date)
    end_d = _analytics_parse_date(end_date)
    if not start_d or not end_d or start_d > end_d:
        return {"labels": [], "keys": [], "hosts": []}

    where, params = _analytics_gift_where(start_date, end_date, host, account, viewer)

    def _fmt_avg(v):
        if abs(v - round(v)) < 1e-9:
            return int(round(v))
        return round(v, 2)

    if group_by == "hour_of_day":
        days_count = max(1, (end_d - start_d).days + 1)
        order = [f"{h:02d}" for h in range(24)]
        buckets = {k: {"key": k, "label": f"{k}:00"} for k in order}
        hour_expr = "substr(COALESCE(NULLIF(g.log_time, ''), substr(COALESCE(g.log_datetime, ''), 12, 8), '00:00:00'), 1, 2)"
        day_expr = "substr(COALESCE(g.log_date, substr(COALESCE(g.log_datetime, ''), 1, 10), ''), 1, 10)"
        per_host = {}
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            rows = cur.execute(f"""
                SELECT h, host_key, host_name,
                       COALESCE(SUM(transactions), 0) AS transactions_sum,
                       COALESCE(SUM(coins), 0) AS coins_sum
                FROM (
                    SELECT {hour_expr} AS h,
                           {day_expr} AS d,
                           {_ANALYTICS_HOST_KEY} AS host_key,
                           {_ANALYTICS_HOST_LABEL} AS host_name,
                           COUNT(*) AS transactions,
                           COALESCE(SUM(g.coins), 0) AS coins
                    FROM gift_logs g
                    WHERE {where}
                    GROUP BY h, d, {_ANALYTICS_HOST_KEY}
                )
                GROUP BY h, host_key
            """, params).fetchall()
            for hour_key, hkey, hname, tr_sum, coins_sum in rows:
                bkey = str(hour_key or "00")[:2].zfill(2)
                if bkey not in buckets:
                    continue
                hk = (str(hkey or "").lower().strip()) or "-"
                if hk not in per_host:
                    per_host[hk] = {
                        "key": hk, "name": str(hname or hk or "-").strip() or "-",
                        "transactions": {}, "coins": {}, "viewers": {},
                    }
                item = per_host[hk]
                if hname and (not item["name"] or item["name"] == hk):
                    item["name"] = str(hname).strip()
                item["transactions"][bkey] = float(tr_sum or 0) / days_count
                item["coins"][bkey] = float(coins_sum or 0) / days_count

            vrows = cur.execute(f"""
                SELECT h, host_key, COALESCE(SUM(daily_viewers), 0) AS viewer_sum
                FROM (
                    SELECT {hour_expr} AS h,
                           {day_expr} AS d,
                           {_ANALYTICS_HOST_KEY} AS host_key,
                           COUNT(DISTINCT LOWER(g.viewer_username)) AS daily_viewers
                    FROM gift_logs g
                    WHERE {where}
                    GROUP BY h, d, {_ANALYTICS_HOST_KEY}
                )
                GROUP BY h, host_key
            """, params).fetchall()
            for hour_key, hkey, viewer_sum in vrows:
                bkey = str(hour_key or "00")[:2].zfill(2)
                if bkey not in buckets:
                    continue
                hk = (str(hkey or "").lower().strip()) or "-"
                if hk not in per_host:
                    per_host[hk] = {
                        "key": hk, "name": hk or "-",
                        "transactions": {}, "coins": {}, "viewers": {},
                    }
                per_host[hk]["viewers"][bkey] = float(viewer_sum or 0) / days_count
        except Exception as e:
            print(f"[Analytics] host engagement timeseries (hour_of_day) failed: {e}")
            return {"labels": [buckets[k]["label"] for k in order], "keys": list(order), "hosts": []}
        finally:
            conn.close()

        try:
            limit_n = max(1, min(int(limit or 12), 20))
        except Exception:
            limit_n = 12
        # rank by sum of averaged coins across 24h
        def _rank(item):
            return sum(float(item["coins"].get(k, 0) or 0) for k in order)
        ranked = sorted(per_host.values(), key=lambda x: (-_rank(x), str(x.get("name") or "").lower()))
        selected = ranked[:limit_n]
        if host:
            h = str(host).strip().lower().lstrip("@")
            if h and all(x["key"] != h for x in selected):
                match = per_host.get(h)
                if match:
                    selected = [match] + selected[: max(0, limit_n - 1)]
        hosts_out = []
        for item in selected:
            tr_arr = [_fmt_avg(float(item["transactions"].get(k, 0) or 0)) for k in order]
            coins_arr = [_fmt_avg(float(item["coins"].get(k, 0) or 0)) for k in order]
            viewers_arr = [_fmt_avg(float(item["viewers"].get(k, 0) or 0)) for k in order]
            hosts_out.append({
                "key": item["key"],
                "name": item["name"],
                "transactions": tr_arr,
                "coins": coins_arr,
                "viewers": viewers_arr,
                "total_transactions": _fmt_avg(sum(float(item["transactions"].get(k, 0) or 0) for k in order)),
                "total_coins": _fmt_avg(sum(float(item["coins"].get(k, 0) or 0) for k in order)),
                "total_viewers": _fmt_avg(sum(float(item["viewers"].get(k, 0) or 0) for k in order)),
            })
        return {"labels": [buckets[k]["label"] for k in order], "keys": list(order), "hosts": hosts_out}

    if group_by == "hour":
        order = [f"{h:02d}" for h in range(24)]
        buckets = {k: {"key": k, "label": f"{k}:00"} for k in order}
        hour_expr = "substr(COALESCE(NULLIF(g.log_time, ''), substr(COALESCE(g.log_datetime, ''), 12, 8), '00:00:00'), 1, 2)"
        per_host = {}
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            rows = cur.execute(f"""
                SELECT {hour_expr} AS h,
                       {_ANALYTICS_HOST_KEY} AS host_key,
                       {_ANALYTICS_HOST_LABEL} AS host_name,
                       COUNT(*) AS transactions,
                       COALESCE(SUM(g.coins), 0) AS coins
                FROM gift_logs g
                WHERE {where}
                GROUP BY h, {_ANALYTICS_HOST_KEY}
            """, params).fetchall()
            for hour_key, hkey, hname, transactions, coins in rows:
                bkey = str(hour_key or "00")[:2].zfill(2)
                if bkey not in buckets:
                    continue
                hk = (str(hkey or "").lower().strip()) or "-"
                if hk not in per_host:
                    per_host[hk] = {
                        "key": hk, "name": str(hname or hk or "-").strip() or "-",
                        "transactions": {}, "coins": {}, "_viewers": {},
                        "total_transactions": 0, "total_coins": 0,
                    }
                item = per_host[hk]
                if hname and (not item["name"] or item["name"] == hk):
                    item["name"] = str(hname).strip()
                tr = int(transactions or 0)
                c = int(coins or 0)
                item["transactions"][bkey] = item["transactions"].get(bkey, 0) + tr
                item["coins"][bkey] = item["coins"].get(bkey, 0) + c
                item["total_transactions"] += tr
                item["total_coins"] += c

            vrows = cur.execute(f"""
                SELECT {hour_expr} AS h,
                       {_ANALYTICS_HOST_KEY} AS host_key,
                       LOWER(g.viewer_username) AS viewer_key
                FROM gift_logs g
                WHERE {where}
                GROUP BY h, {_ANALYTICS_HOST_KEY}, LOWER(g.viewer_username)
            """, params).fetchall()
            for hour_key, hkey, viewer_key in vrows:
                bkey = str(hour_key or "00")[:2].zfill(2)
                if bkey not in buckets or not viewer_key:
                    continue
                hk = (str(hkey or "").lower().strip()) or "-"
                if hk not in per_host:
                    per_host[hk] = {
                        "key": hk, "name": hk or "-",
                        "transactions": {}, "coins": {}, "_viewers": {},
                        "total_transactions": 0, "total_coins": 0,
                    }
                item = per_host[hk]
                if bkey not in item["_viewers"]:
                    item["_viewers"][bkey] = set()
                item["_viewers"][bkey].add(viewer_key)
        except Exception as e:
            print(f"[Analytics] host engagement timeseries (hour) failed: {e}")
            return {"labels": [buckets[k]["label"] for k in order], "keys": list(order), "hosts": []}
        finally:
            conn.close()

        try:
            limit_n = max(1, min(int(limit or 12), 20))
        except Exception:
            limit_n = 12
        ranked = sorted(
            per_host.values(),
            key=lambda x: (-(x.get("total_coins") or 0), -(x.get("total_transactions") or 0), str(x.get("name") or "").lower())
        )
        selected = ranked[:limit_n]
        if host:
            h = str(host).strip().lower().lstrip("@")
            if h and all(x["key"] != h for x in selected):
                match = per_host.get(h)
                if match:
                    selected = [match] + selected[: max(0, limit_n - 1)]
        hosts_out = []
        for item in selected:
            vsets = item.get("_viewers") or {}
            viewers_arr = [len(vsets.get(k, set())) for k in order]
            hosts_out.append({
                "key": item["key"],
                "name": item["name"],
                "transactions": [int(item["transactions"].get(k, 0)) for k in order],
                "coins": [int(item["coins"].get(k, 0)) for k in order],
                "viewers": viewers_arr,
                "total_transactions": int(item.get("total_transactions") or 0),
                "total_coins": int(item.get("total_coins") or 0),
                "total_viewers": int(sum(viewers_arr)),
            })
        return {"labels": [buckets[k]["label"] for k in order], "keys": list(order), "hosts": hosts_out}

    buckets = {}
    order = []
    cursor_day = _analytics_bucket_start(start_d, group_by)
    while cursor_day <= end_d:
        key = _analytics_bucket_key(cursor_day, group_by)
        if key not in buckets:
            buckets[key] = {"key": key, "label": _analytics_bucket_label(cursor_day, group_by)}
            order.append(key)
        cursor_day = _analytics_next_bucket(cursor_day, group_by)

    per_host = {}  # host_key -> stats
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute(f"""
            SELECT substr(g.log_date, 1, 10) AS d,
                   {_ANALYTICS_HOST_KEY} AS host_key,
                   {_ANALYTICS_HOST_LABEL} AS host_name,
                   COUNT(*) AS transactions,
                   COALESCE(SUM(g.coins), 0) AS coins
            FROM gift_logs g
            WHERE {where}
            GROUP BY substr(g.log_date, 1, 10), {_ANALYTICS_HOST_KEY}
        """, params).fetchall()
        for d, hkey, hname, transactions, coins in rows:
            parsed = _analytics_parse_date(d)
            if not parsed:
                continue
            bkey = _analytics_bucket_key(parsed, group_by)
            if bkey not in buckets:
                continue
            hk = (str(hkey or "").lower().strip()) or "-"
            if hk not in per_host:
                per_host[hk] = {
                    "key": hk, "name": str(hname or hk or "-").strip() or "-",
                    "transactions": {}, "coins": {}, "_viewers": {},
                    "total_transactions": 0, "total_coins": 0,
                }
            item = per_host[hk]
            if hname and (not item["name"] or item["name"] == hk):
                item["name"] = str(hname).strip()
            tr = int(transactions or 0)
            c = int(coins or 0)
            item["transactions"][bkey] = item["transactions"].get(bkey, 0) + tr
            item["coins"][bkey] = item["coins"].get(bkey, 0) + c
            item["total_transactions"] += tr
            item["total_coins"] += c

        vrows = cur.execute(f"""
            SELECT substr(g.log_date, 1, 10) AS d,
                   {_ANALYTICS_HOST_KEY} AS host_key,
                   LOWER(g.viewer_username) AS viewer_key
            FROM gift_logs g
            WHERE {where}
            GROUP BY substr(g.log_date, 1, 10), {_ANALYTICS_HOST_KEY}, LOWER(g.viewer_username)
        """, params).fetchall()
        for d, hkey, viewer_key in vrows:
            parsed = _analytics_parse_date(d)
            if not parsed or not viewer_key:
                continue
            bkey = _analytics_bucket_key(parsed, group_by)
            if bkey not in buckets:
                continue
            hk = (str(hkey or "").lower().strip()) or "-"
            if hk not in per_host:
                per_host[hk] = {
                    "key": hk, "name": hk or "-",
                    "transactions": {}, "coins": {}, "_viewers": {},
                    "total_transactions": 0, "total_coins": 0,
                }
            item = per_host[hk]
            if bkey not in item["_viewers"]:
                item["_viewers"][bkey] = set()
            item["_viewers"][bkey].add(viewer_key)
    except Exception as e:
        print(f"[Analytics] host engagement timeseries failed: {e}")
        return {"labels": [buckets[k]["label"] for k in order], "keys": list(order), "hosts": []}
    finally:
        conn.close()

    try:
        limit_n = max(1, min(int(limit or 12), 20))
    except Exception:
        limit_n = 12

    ranked = sorted(
        per_host.values(),
        key=lambda x: (-(x.get("total_coins") or 0), -(x.get("total_transactions") or 0), str(x.get("name") or "").lower())
    )
    selected = ranked[:limit_n]
    if host:
        h = str(host).strip().lower().lstrip("@")
        if h and all(x["key"] != h for x in selected):
            match = per_host.get(h)
            if match:
                selected = [match] + selected[: max(0, limit_n - 1)]

    hosts_out = []
    for item in selected:
        vsets = item.get("_viewers") or {}
        viewers_arr = [len(vsets.get(k, set())) for k in order]
        total_viewers = sum(viewers_arr)
        hosts_out.append({
            "key": item["key"],
            "name": item["name"],
            "transactions": [int(item["transactions"].get(k, 0)) for k in order],
            "coins": [int(item["coins"].get(k, 0)) for k in order],
            "viewers": viewers_arr,
            "total_transactions": int(item.get("total_transactions") or 0),
            "total_coins": int(item.get("total_coins") or 0),
            "total_viewers": int(total_viewers),
        })

    return {
        "labels": [buckets[k]["label"] for k in order],
        "keys": list(order),
        "hosts": hosts_out,
    }


def get_analytics_top_gift_items(start_date, end_date, host=None, account=None, viewer=None,
                                 limit=12) -> list:
    """Top gift objects by coins spent in the selected range.

    Returns rows: (gift_name, unit_coins, item_count, total_coins, transactions, top_sender).
    unit_coins = average coin value per item (total coins / total item count).
    top_sender = top sender for this gift object in the range (by coins).
    """
    where, params = _analytics_gift_where(start_date, end_date, host, account, viewer)
    where2 = where.replace('g.', 'g2.')
    gift_name_expr = "COALESCE(NULLIF(g.gift_name, ''), 'Unknown')"
    gift_name_expr2 = "COALESCE(NULLIF(g2.gift_name, ''), 'Unknown')"
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        return cur.execute(f"""
            SELECT {gift_name_expr} AS gift_name,
                   CASE WHEN COALESCE(SUM(g.gift_count), 0) > 0
                        THEN CAST(ROUND(SUM(g.coins) * 1.0 / SUM(g.gift_count)) AS INTEGER)
                        ELSE 0 END AS unit_coins,
                   COALESCE(SUM(g.gift_count), 0) AS item_count,
                   COALESCE(SUM(g.coins), 0) AS total_coins,
                   COUNT(*) AS transactions,
                   (
                       SELECT COALESCE(NULLIF(TRIM(g2.viewer_display_name), ''), '@' || LOWER(g2.viewer_username))
                       FROM gift_logs g2
                       WHERE {where2}
                         AND {gift_name_expr2} = {gift_name_expr}
                       GROUP BY LOWER(COALESCE(NULLIF(g2.viewer_username, ''), ''))
                       ORDER BY SUM(g2.coins) DESC, SUM(g2.gift_count) DESC
                       LIMIT 1
                   ) AS top_sender
            FROM gift_logs g
            WHERE {where}
            GROUP BY {gift_name_expr}
            ORDER BY total_coins DESC, item_count DESC
            LIMIT ?
        """, tuple(params) + tuple(params) + (int(limit or 12),)).fetchall()
    except Exception as e:
        print(f"[Analytics] top gift items failed: {e}")
        return []
    finally:
        conn.close()


def get_analytics_gift_by_host(start_date, end_date, account=None, viewer=None) -> list:
    where, params = _analytics_gift_where(start_date, end_date, None, account, viewer)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        return cur.execute(f"""
            SELECT {_ANALYTICS_HOST_KEY} AS host_key,
                   {_ANALYTICS_HOST_LABEL} AS host,
                   COALESCE(SUM(g.coins), 0),
                   COALESCE(SUM(g.gift_count), 0),
                   COUNT(DISTINCT LOWER(g.viewer_username)),
                   COUNT(DISTINCT LOWER(g.target_username))
            FROM gift_logs g
            WHERE {where}
            GROUP BY host_key
            ORDER BY 3 DESC
        """, params).fetchall()
    except Exception as e:
        print(f"[Analytics] host ranking failed: {e}")
        return []
    finally:
        conn.close()


def get_analytics_gift_by_account(start_date, end_date, host=None, viewer=None) -> list:
    where, params = _analytics_gift_where(start_date, end_date, host, None, viewer)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        return cur.execute(f"""
            SELECT LOWER(g.target_username) AS account_key,
                   MAX(g.target_username) AS account,
                   COALESCE(SUM(g.coins), 0),
                   COALESCE(SUM(g.gift_count), 0),
                   COUNT(DISTINCT LOWER(g.viewer_username)),
                   COUNT(DISTINCT {_ANALYTICS_HOST_KEY})
            FROM gift_logs g
            WHERE {where}
            GROUP BY account_key
            ORDER BY 3 DESC
        """, params).fetchall()
    except Exception as e:
        print(f"[Analytics] account ranking failed: {e}")
        return []
    finally:
        conn.close()


def get_analytics_viewer_ranking(start_date, end_date, host=None, account=None, limit=100) -> list:
    where, params = _analytics_gift_where(start_date, end_date, host, account, None)
    params = tuple(list(params) + [int(limit)])
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        return cur.execute(f"""
            SELECT LOWER(g.viewer_username),
                   COALESCE(MAX(NULLIF(g.viewer_display_name, '')), MAX(g.viewer_username)),
                   COALESCE(SUM(g.coins), 0),
                   COALESCE(SUM(g.gift_count), 0),
                   COUNT(DISTINCT {_ANALYTICS_HOST_KEY}),
                   MAX(g.log_datetime)
            FROM gift_logs g
            WHERE {where}
            GROUP BY LOWER(g.viewer_username)
            ORDER BY 3 DESC
            LIMIT ?
        """, params).fetchall()
    except Exception as e:
        print(f"[Analytics] viewer ranking failed: {e}")
        return []
    finally:
        conn.close()


def get_analytics_viewer_host_spend(start_date, end_date, viewer=None, host=None,
                                    account=None, limit=200) -> list:
    where, params = _analytics_gift_where(start_date, end_date, host, account, viewer)
    params = tuple(list(params) + [int(limit)])
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        return cur.execute(f"""
            SELECT LOWER(g.viewer_username),
                   COALESCE(MAX(NULLIF(g.viewer_display_name, '')), MAX(g.viewer_username)),
                   {_ANALYTICS_HOST_LABEL} AS host,
                   COALESCE(SUM(g.coins), 0),
                   COALESCE(SUM(g.gift_count), 0)
            FROM gift_logs g
            WHERE {where}
            GROUP BY LOWER(g.viewer_username), {_ANALYTICS_HOST_KEY}
            ORDER BY 4 DESC
            LIMIT ?
        """, params).fetchall()
    except Exception as e:
        print(f"[Analytics] viewer-host spend failed: {e}")
        return []
    finally:
        conn.close()


def get_analytics_host_options() -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT host FROM (
                SELECT COALESCE(NULLIF(display_name, ''), NULLIF(tiktok_username, '')) AS host FROM hosts
                UNION
                SELECT COALESCE(NULLIF(host_name, ''), NULLIF(scheduled_host, ''), target_username) AS host
                FROM gift_logs
                UNION
                SELECT COALESCE(NULLIF(scheduled_host, ''), tiktok_username) AS host
                FROM live_sessions
            )
            WHERE host IS NOT NULL AND TRIM(host) != '' AND LOWER(TRIM(host)) != 'host'
            GROUP BY LOWER(TRIM(host))
            ORDER BY LOWER(TRIM(host))
        """).fetchall()
        return [str(r[0]).strip() for r in rows if r and r[0] and str(r[0]).strip()]
    except Exception as e:
        print(f"[Analytics] host options failed: {e}")
        return []
    finally:
        conn.close()


def get_analytics_account_options() -> list:
    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT account FROM (
                SELECT LOWER(TRIM(tiktok_username)) AS account FROM hosts
                UNION
                SELECT LOWER(TRIM(target_username)) AS account FROM gift_logs
                UNION
                SELECT LOWER(TRIM(tiktok_username)) AS account FROM live_sessions
            )
            WHERE account IS NOT NULL AND account != ''
            GROUP BY account
            ORDER BY account
        """).fetchall()
        return [str(r[0]).strip().lower().lstrip("@") for r in rows if r and r[0] and str(r[0]).strip()]
    except Exception as e:
        print(f"[Analytics] account options failed: {e}")
        return []
    finally:
        conn.close()


def get_all_host_names() -> list:
    """Return display names for every target host stored in the database."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT COALESCE(display_name, tiktok_username) FROM hosts ORDER BY host_id ASC"
        ).fetchall()
        names = [r[0] for r in rows if r[0]]
    finally:
        conn.close()
    return names or [t[2] for t in VALID_TARGET_ACCOUNTS]


def get_all_hosts() -> list:
    """Return full host rows for the admin panel."""
    conn = get_db_connection()
    try:
        return conn.execute("""
            SELECT host_id, tiktok_username, COALESCE(display_name, tiktok_username), is_active, COALESCE(is_priority, 1)
            FROM hosts ORDER BY host_id ASC
        """).fetchall()
    finally:
        conn.close()


def get_observable_hosts() -> list:
    """Return hosts that should be observed now with their connect mode.

    Each item: {"username": str, "priority": bool, "mode": "normal"|"slow"}
    - Active + priority hosts => normal.
    - Active + non-priority hosts => slow or excluded, based on non_priority_mode setting.
    - Inactive hosts are excluded.
    """
    non_priority_mode = str(get_setting("non_priority_mode", "slow") or "slow").strip().lower()
    if non_priority_mode not in ("slow", "off"):
        non_priority_mode = "slow"
    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT tiktok_username, COALESCE(is_active, 1), COALESCE(is_priority, 1)
            FROM hosts ORDER BY host_id ASC
        """).fetchall()
    finally:
        conn.close()

    targets = []
    for row in rows:
        username = (row[0] or "").lower().lstrip("@")
        if not username:
            continue
        is_active = int(row[1] or 0) == 1
        is_priority = int(row[2] or 0) == 1
        if not is_active:
            continue
        if is_priority:
            targets.append({"username": username, "priority": True, "mode": "normal"})
        elif non_priority_mode == "slow":
            targets.append({"username": username, "priority": False, "mode": "slow"})
        # mode == off => excluded

    if not targets:
        # Fallback: if no configured/observable hosts, keep previous behavior using defaults as priority.
        for t in VALID_TARGET_ACCOUNTS:
            targets.append({"username": t[1], "priority": True, "mode": "normal"})
    return targets


def get_host_connect_mode(username: str) -> str:
    """Return 'normal', 'slow', or 'off' for a host based on active/priority settings."""
    u = (username or "").lower().lstrip("@")
    non_priority_mode = str(get_setting("non_priority_mode", "slow") or "slow").strip().lower()
    if non_priority_mode not in ("slow", "off"):
        non_priority_mode = "slow"
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(is_active, 1), COALESCE(is_priority, 1) FROM hosts WHERE LOWER(tiktok_username)=?",
            (u,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return "normal"
    if int(row[0] or 0) != 1:
        return "off"
    if int(row[1] or 0) == 1:
        return "normal"
    return non_priority_mode


def get_active_hosts() -> list:
    """Backward-compatible: usernames that should currently be observed."""
    targets = get_observable_hosts()
    hosts = [t["username"] for t in targets if t.get("username")]
    if not hosts:
        hosts = [t[1] for t in VALID_TARGET_ACCOUNTS]
    return hosts


def set_host_priority(host_id: int, is_priority: int):
    conn = get_db_connection()
    try:
        conn.execute("UPDATE hosts SET is_priority = ? WHERE host_id = ?", (1 if is_priority else 0, host_id))
        conn.commit()
    finally:
        conn.close()

def _announcement_row_to_dict(row):
    return {
        "tiktok_username": row[0],
        "channel_id": str(row[1]) if row[1] is not None else None,
        "message_id": str(row[2]) if row[2] is not None else None,
        "host_name": row[3] if len(row) > 3 else None,
        "created_at": row[4] if len(row) > 4 else None,
    }


def get_active_live_announcement(tiktok_username: str, channel_id=None):
    """Return the stored announcement session for one account (and channel)."""
    u_clean = tiktok_username.lower().lstrip("@")
    conn = get_db_connection()
    try:
        if channel_id is None:
            row = conn.execute(
                """
                SELECT tiktok_username, channel_id, message_id, host_name, created_at
                FROM active_live_announcements
                WHERE tiktok_username = ?
                ORDER BY created_at ASC LIMIT 1
                """,
                (u_clean,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT tiktok_username, channel_id, message_id, host_name, created_at
                FROM active_live_announcements
                WHERE tiktok_username = ? AND channel_id = ?
                """,
                (u_clean, str(channel_id)),
            ).fetchone()
        return _announcement_row_to_dict(row) if row else None
    finally:
        conn.close()


def get_active_live_announcements_for_account(tiktok_username: str) -> list:
    """Return every per-channel announcement session for one TikTok account."""
    u_clean = tiktok_username.lower().lstrip("@")
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT tiktok_username, channel_id, message_id, host_name, created_at
            FROM active_live_announcements
            WHERE tiktok_username = ?
            ORDER BY created_at ASC
            """,
            (u_clean,),
        ).fetchall()
        return [_announcement_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_all_active_live_announcements() -> list:
    """Mengambil semua sesi pengumuman live aktif (untuk rekonsiliasi saat startup)."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT tiktok_username, channel_id, message_id, host_name, created_at
            FROM active_live_announcements
            """
        ).fetchall()
        return [_announcement_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def save_active_live_announcement(tiktok_username: str, channel_id, message_id, host_name: str = None):
    """Store one announcement message per TikTok account per Discord channel."""
    u_clean = tiktok_username.lower().lstrip("@")

    def _op(conn):
        try:
            conn.execute(
                """
                INSERT INTO active_live_announcements (tiktok_username, channel_id, message_id, host_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tiktok_username, channel_id) DO UPDATE SET
                    message_id = excluded.message_id,
                    host_name = COALESCE(excluded.host_name, active_live_announcements.host_name)
                """,
                (u_clean, str(channel_id), str(message_id), host_name),
            )
        except Exception as e:
            # Compatibility fallback if a legacy single-channel PK schema remains.
            err = str(e).lower()
            if "conflict" not in err and "constraint" not in err and "on conflict" not in err:
                raise
            try:
                conn.rollback()
            except Exception:
                pass
            conn.execute(
                """
                INSERT INTO active_live_announcements (tiktok_username, channel_id, message_id, host_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tiktok_username) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id,
                    host_name = COALESCE(excluded.host_name, active_live_announcements.host_name)
                """,
                (u_clean, str(channel_id), str(message_id), host_name),
            )
            print(f"[DB] Announcement save used legacy schema fallback for @{u_clean}: {e}")

    write_transaction(_op, timing_label="live_announcement_save")
    return True


def update_active_live_announcement_host(tiktok_username: str, host_name: str):
    """Update the host name on every announcement session for an account."""
    u_clean = tiktok_username.lower().lstrip("@")

    def _op(conn):
        conn.execute(
            "UPDATE active_live_announcements SET host_name = ? WHERE tiktok_username = ?",
            (host_name, u_clean),
        )

    write_transaction(_op, timing_label="live_announcement_update")


def delete_active_live_announcement(tiktok_username: str, channel_id=None):
    """Delete announcement session(s) for an account, optionally one channel only."""
    u_clean = tiktok_username.lower().lstrip("@")

    def _op(conn):
        if channel_id is None:
            conn.execute(
                "DELETE FROM active_live_announcements WHERE tiktok_username = ?",
                (u_clean,),
            )
        else:
            conn.execute(
                "DELETE FROM active_live_announcements WHERE tiktok_username = ? AND channel_id = ?",
                (u_clean, str(channel_id)),
            )

    write_transaction(_op, timing_label="live_announcement_delete")


def suspend_open_live_sessions() -> int:
    """Tutup sesi yang masih terbuka saat proses bot berhenti/restart.

    Sesi ditutup pada ``last_seen_at`` terakhir (bukan waktu startup), lalu
    disimpan sebagai kandidat resume. Kalau observer reconnect dalam
    ``LIVE_RESUME_GRACE_SECONDS``, sesi yang sama dilanjutkan dan jeda restart
    dihitung sebagai paused time. Dengan begitu redeploy tidak memecah history
    live yang sebenarnya sedang berjalan.
    """
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    suspended = 0
    try:
        cur = conn.cursor()
        try:
            rows = cur.execute("""
                SELECT session_id, tiktok_username, start_time,
                       COALESCE(paused_seconds, 0), last_seen_at
                FROM live_sessions
                WHERE end_time IS NULL
            """).fetchall()
        except Exception:
            # Kolom last_seen_at belum ada (DB sangat lama).
            rows = cur.execute("""
                SELECT session_id, tiktok_username, start_time,
                       COALESCE(paused_seconds, 0), NULL
                FROM live_sessions
                WHERE end_time IS NULL
            """).fetchall()
        for session_id, username, start_time, paused_seconds, last_seen in rows:
            user_clean = str(username or "").lower().lstrip("@")
            if not user_clean:
                continue
            seen_str = str(last_seen or "")[:19]
            try:
                seen_dt = wib.localize(datetime.strptime(seen_str, '%Y-%m-%d %H:%M:%S'))
            except Exception:
                seen_dt = now_dt
            # Jangan pernah menutup di masa depan / sebelum sesi dimulai.
            if seen_dt > now_dt:
                seen_dt = now_dt
            seen_str = seen_dt.strftime('%Y-%m-%d %H:%M:%S')
            duration_sec = 0
            if start_time:
                try:
                    start_dt = wib.localize(
                        datetime.strptime(str(start_time)[:19], '%Y-%m-%d %H:%M:%S')
                    )
                    duration_sec = max(
                        0, int((seen_dt - start_dt).total_seconds()) - int(paused_seconds or 0)
                    )
                except Exception:
                    duration_sec = 0
            cur.execute("""
                UPDATE live_sessions
                SET end_time = ?, duration_seconds = ?, last_seen_at = ?
                WHERE session_id = ? AND end_time IS NULL
            """, (seen_str, duration_sec, seen_str, session_id))
            for key, value in (
                (f"resume_session_{user_clean}", str(session_id)),
                (f"resume_last_seen_{user_clean}", seen_str),
            ):
                cur.execute("""
                    INSERT INTO bot_settings (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """, (key, value))
            suspended += 1
        conn.commit()
        if suspended:
            print(f"[LiveSession] Suspended {suspended} open session(s) at last activity time.")
        return suspended
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[LiveSession] Failed to suspend open sessions: {e}")
        return 0
    finally:
        conn.close()


def close_all_open_live_sessions():
    """Backward-compatible alias: suspend (not force-close) open sessions."""
    return suspend_open_live_sessions()


def reset_live_state(clear_logs: bool = True):
    """Clear live/runtime-persisted state so observers can start from a clean slate.

    Removes live sessions, announcement sessions, current detected host settings,
    and optionally activity logs. Does NOT remove gift totals/history or user mappings.
    """
    conn = get_db_connection()
    try:
        if clear_logs:
            conn.execute("DELETE FROM activity_logs")
        conn.execute("DELETE FROM active_live_announcements")
        conn.execute("DELETE FROM live_sessions")
        conn.execute("DELETE FROM bot_settings WHERE key LIKE 'current_host_%'")
        conn.commit()
    finally:
        conn.close()
    # Raw DELETE bypasses the settings cache; drop it so a stale host is not
    # served immediately after Reset Live State.
    try:
        invalidate_settings_cache()
    except Exception:
        pass

def _ensure_analytics_indexes(conn):
    """Create indexes matching the date expressions used by analytics filters."""
    stage_started = time.perf_counter()
    statements = (
        ("gift_logs", "CREATE INDEX IF NOT EXISTS idx_gift_logs_analytics_date "
         "ON gift_logs(substr(log_date, 1, 10))"),
        ("live_sessions", "CREATE INDEX IF NOT EXISTS idx_live_sessions_analytics_date "
         "ON live_sessions(substr(start_time, 1, 10))"),
    )
    for label, statement in statements:
        index_started = time.perf_counter()
        try:
            conn.execute(statement)
            commit_started = time.perf_counter()
            conn.commit()
            _log_db_init_stage(f"analytics_index_{label}_commit", commit_started)
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DB Migration] analytics date index skipped: {exc}")
        _log_db_init_stage(f"analytics_index_{label}", index_started)
    _log_db_init_stage("analytics_indexes", stage_started)


def _add_missing_columns(conn, table, columns):
    """Add legacy columns only when absent, avoiding repeated failed ALTERs on startup.

    The table/column identifiers passed here are static migration definitions,
    never user input. One PRAGMA per table replaces one failing ALTER round-trip
    per already-present column on remote Turso databases.
    """
    stage_started = time.perf_counter()
    try:
        existing = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        existing = set()

    added = []
    for column, dtype in columns:
        if column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}")
            conn.commit()
            existing.add(column)
            added.append(column)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
    _log_db_init_stage(f"column_compatibility_{table}", stage_started)
    return tuple(added)


def _seed_default_settings(conn, defaults):
    """Insert missing default settings in one statement without overwriting admins."""
    if not defaults:
        return
    rows = list(defaults.items())
    placeholders = ", ".join("(?, ?)" for _ in rows)
    params = tuple(
        value
        for key, value in rows
        for value in (key, str(value))
    )
    conn.execute(
        "INSERT OR IGNORE INTO bot_settings (key, value) VALUES " + placeholders,
        params,
    )


def _get_role_tier_seed_state(conn):
    """Read the one-time seed marker and tier count in one database round trip."""
    row = conn.execute(
        "SELECT "
        "EXISTS(SELECT 1 FROM bot_settings WHERE key = ?), "
        "(SELECT COUNT(*) FROM role_tiers)",
        ("tiers_seeded",),
    ).fetchone()
    return bool(row[0]), int(row[1])


def _normalize_host_priorities(conn):
    """Repair legacy NULL priorities only when a row actually needs it."""
    try:
        needs_normalization = conn.execute(
            "SELECT 1 FROM hosts WHERE is_priority IS NULL LIMIT 1"
        ).fetchone()
    except Exception:
        # Preserve the old best-effort repair if the preflight read itself fails.
        needs_normalization = True
    if not needs_normalization:
        return False
    _execute_atomic_batch(conn, [
        ("UPDATE hosts SET is_priority = 1 WHERE is_priority IS NULL", ())
    ])
    conn.commit()
    return True


def _execute_atomic_batch(conn, statements):
    """Run an ordered group of SQL statements atomically when batch is available."""
    normalized = [
        statement if isinstance(statement, tuple) else (statement, ())
        for statement in statements
    ]
    if not normalized:
        return
    batch = getattr(conn, "batch", None)
    if callable(batch):
        batch(normalized, mode="immediate")
        return
    for sql, params in normalized:
        if params:
            conn.execute(sql, params)
        else:
            conn.execute(sql)


def _execute_schema_statements(conn, statements):
    """Execute startup DDL in one atomic remote batch when the driver supports it."""
    _execute_atomic_batch(conn, list(statements))


def _log_db_init_stage(stage, started):
    """Emit a monotonic duration for one init_db phase and return its end time."""
    ended = time.perf_counter()
    print(f"[DB Init Timing] stage={stage} elapsed={ended - started:.3f}s")
    return ended


def init_db():
    """Initialize schema and seed defaults.

    Uses short commit checkpoints because Turso/LibSQL Hrana connections can
    roll back an interactive transaction if the stream is idle for too long.
    """
    conn = get_db_connection()
    init_started = time.perf_counter()
    stage_started = init_started
    try:
        schema_statements = [
            """
            CREATE TABLE IF NOT EXISTS tiktok_gifts (
                tiktok_user TEXT PRIMARY KEY,
                display_name TEXT,
                total_coins INTEGER DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_mappings (
                discord_id TEXT PRIMARY KEY,
                tiktok_user TEXT UNIQUE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS discord_verification_status (
                discord_id TEXT PRIMARY KEY,
                discord_name TEXT,
                discord_handle TEXT,
                guild_name TEXT,
                has_role INTEGER,
                role_name TEXT,
                checked_at TEXT,
                error TEXT,
                method TEXT,
                detail TEXT,
                verified_at TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS active_live_announcements (
                tiktok_username TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                host_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tiktok_username, channel_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS daily_tiktok_gifts (
                tiktok_user TEXT,
                display_name TEXT,
                coins INTEGER DEFAULT 0,
                date TEXT,
                PRIMARY KEY (tiktok_user, date)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS hosts (
                host_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tiktok_username TEXT UNIQUE NOT NULL,
                display_name TEXT,
                is_active INTEGER DEFAULT 1,
                is_priority INTEGER DEFAULT 1
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS live_sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tiktok_username TEXT NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration_seconds INTEGER DEFAULT 0,
                paused_seconds INTEGER DEFAULT 0,
                last_seen_at TEXT,
                total_coins INTEGER DEFAULT 0,
                scheduled_host TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS gift_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                viewer_username TEXT NOT NULL,
                viewer_display_name TEXT,
                gift_name TEXT NOT NULL,
                gift_count INTEGER DEFAULT 1,
                coins INTEGER NOT NULL,
                log_date TEXT NOT NULL,
                log_time TEXT NOT NULL,
                log_datetime TEXT NOT NULL,
                target_username TEXT NOT NULL,
                scheduled_host TEXT NOT NULL,
                host_name TEXT,
                msg_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "DROP TABLE IF EXISTS host_schedules",
            "DROP TABLE IF EXISTS temp_schedules",
            "DROP TABLE IF EXISTS host_offlines",
            """
            CREATE TABLE IF NOT EXISTS activity_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                tiktok_username TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'operational',
                level TEXT NOT NULL DEFAULT 'info',
                source TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pending_verifications (
                discord_id TEXT PRIMARY KEY,
                tiktok_user TEXT NOT NULL,
                code TEXT NOT NULL,
                method TEXT DEFAULT 'bio',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS host_aliases (
                alias TEXT PRIMARY KEY COLLATE NOCASE,
                host_name TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS role_tiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_key TEXT NOT NULL DEFAULT 'reset',
                tier_name TEXT NOT NULL,
                role_id TEXT NOT NULL,
                min_coins INTEGER NOT NULL DEFAULT 0,
                sort_order INTEGER DEFAULT 0
            )
            """,
        ]
        _execute_schema_statements(conn, schema_statements)
        conn.commit()
        stage_started = _log_db_init_stage("base_schema", stage_started)

        # Migrate Discord snowflake IDs from INTEGER to TEXT to avoid
        # precision loss (IDs > 2^53) in float/JSON/driver conversions.
        for table, create_sql, copy_sql in [
            ("user_mappings",
             "CREATE TABLE user_mappings (discord_id TEXT PRIMARY KEY, tiktok_user TEXT UNIQUE)",
             "INSERT OR REPLACE INTO user_mappings (discord_id, tiktok_user) "
             "SELECT CAST(discord_id AS TEXT), tiktok_user FROM user_mappings_old_did"),
            ("discord_verification_status",
             "CREATE TABLE discord_verification_status (discord_id TEXT PRIMARY KEY, discord_name TEXT, discord_handle TEXT, guild_name TEXT, has_role INTEGER, role_name TEXT, checked_at TEXT, error TEXT, method TEXT, detail TEXT, verified_at TEXT)",
             "INSERT OR REPLACE INTO discord_verification_status "
             "(discord_id, discord_name, discord_handle, guild_name, has_role, role_name, checked_at, error) "
             "SELECT CAST(discord_id AS TEXT), discord_name, discord_handle, guild_name, has_role, role_name, checked_at, error "
             "FROM discord_verification_status_old_did"),
            ("pending_verifications",
             "CREATE TABLE pending_verifications (discord_id TEXT PRIMARY KEY, tiktok_user TEXT NOT NULL, code TEXT NOT NULL, method TEXT DEFAULT 'bio', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMP NOT NULL)",
             "INSERT OR REPLACE INTO pending_verifications "
             "(discord_id, tiktok_user, code, method, created_at, expires_at) "
             "SELECT CAST(discord_id AS TEXT), tiktok_user, code, method, created_at, expires_at "
             "FROM pending_verifications_old_did"),
        ]:
            table_stage_started = time.perf_counter()
            try:
                info = conn.execute(f"PRAGMA table_info({table})").fetchall()
                id_type = ""
                for r in info:
                    if r[1] == "discord_id":
                        id_type = str(r[2] or "").upper()
                        break
                if id_type and "INT" in id_type:
                    conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old_did")
                    conn.execute(create_sql)
                    conn.execute(copy_sql)
                    conn.execute(f"DROP TABLE {table}_old_did")
                    conn.commit()
                    print(f"[DB Migration] {table}.discord_id migrated INTEGER -> TEXT (snowflake-safe).")
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(f"[DB Migration] {table} discord_id migration skipped: {e}")
            _log_db_init_stage(f"snowflake_id_{table}", table_stage_started)

        stage_started = _log_db_init_stage("snowflake_ids", stage_started)

        # Migrate the old one-message-per-account announcement table to
        # one-message-per-account-per-channel.
        try:
            info = conn.execute("PRAGMA table_info(active_live_announcements)").fetchall()
            pk_cols = [r[1] for r in info if len(r) > 5 and r[5]]
            if pk_cols != ["tiktok_username", "channel_id"]:
                conn.execute("ALTER TABLE active_live_announcements RENAME TO active_live_announcements_legacy")
                conn.commit()
                conn.execute("""
                    CREATE TABLE active_live_announcements (
                        tiktok_username TEXT NOT NULL,
                        channel_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        host_name TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (tiktok_username, channel_id)
                    )
                """)
                conn.commit()
                conn.execute("""
                    INSERT OR IGNORE INTO active_live_announcements
                        (tiktok_username, channel_id, message_id, host_name, created_at)
                    SELECT tiktok_username, channel_id, message_id, host_name, created_at
                    FROM active_live_announcements_legacy
                """)
                conn.commit()
                conn.execute("DROP TABLE active_live_announcements_legacy")
                conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DB Migration] Announcement table migration skipped: {e}")

        stage_started = _log_db_init_stage("announcement_schema", stage_started)

        # Migrate legacy gift-log columns with one schema read per table.
        _add_missing_columns(conn, "gift_logs", [
            ("log_date", "TEXT"),
            ("log_time", "TEXT"),
            ("log_datetime", "TEXT"),
            ("target_username", "TEXT"),
            ("scheduled_host", "TEXT"),
            ("msg_id", "TEXT"),
            ("host_name", "TEXT"),
        ])

        # Migrasi live_sessions: akumulasi detik jeda grace period supaya
        # durasi live tidak menghitung waktu kosong/pending. last_seen_at
        # dipakai untuk resume sesi setelah restart/deploy.
        _add_missing_columns(conn, "live_sessions", [
            ("paused_seconds", "INTEGER DEFAULT 0"),
            ("last_seen_at", "TEXT"),
        ])

        # Migrasi discord_verification_status: metode grant, detail (koin/kode/
        # pesan yang terdeteksi), dan waktu role verified diberikan.
        _add_missing_columns(conn, "discord_verification_status", [
            ("method", "TEXT"),
            ("detail", "TEXT"),
            ("verified_at", "TEXT"),
        ])

        # Migrasi activity_logs: pisahkan log operasional vs teknikal.
        _add_missing_columns(conn, "activity_logs", [
            ("category", "TEXT DEFAULT 'operational'"),
            ("level", "TEXT DEFAULT 'info'"),
            ("source", "TEXT DEFAULT ''"),
        ])
        stage_started = _log_db_init_stage("legacy_columns", stage_started)
        try:
            _execute_atomic_batch(conn, [
                (
                    "UPDATE activity_logs SET category = 'operational' "
                    "WHERE category IS NULL OR TRIM(category) = ''",
                    (),
                ),
                (
                    "UPDATE activity_logs SET level = 'info' "
                    "WHERE level IS NULL OR TRIM(level) = ''",
                    (),
                ),
                (
                    "UPDATE activity_logs SET source = '' WHERE source IS NULL",
                    (),
                ),
                (
                    "CREATE INDEX IF NOT EXISTS idx_activity_logs_cat_id "
                    "ON activity_logs(category, log_id DESC)",
                    (),
                ),
                (
                    "CREATE INDEX IF NOT EXISTS idx_activity_logs_cat_created "
                    "ON activity_logs(category, created_at DESC)",
                    (),
                ),
            ])
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

        stage_started = _log_db_init_stage("activity_log_migration", stage_started)

        # Dedupe index: TikTok can replay/duplicate gift messages (especially
        # across reconnects), so a gift log row is unique per message id.
        gift_index_started = time.perf_counter()
        try:
            _execute_atomic_batch(conn, [
                (
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_gift_logs_msg_id "
                    "ON gift_logs(msg_id) WHERE msg_id IS NOT NULL",
                    (),
                )
            ])
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DB Migration] gift_logs msg_id index skipped: {e}")
        _log_db_init_stage("gift_log_msg_id_index", gift_index_started)

        # Backfill host at gift time for old rows (host_name was added later).
        gift_host_started = time.perf_counter()
        try:
            _execute_atomic_batch(conn, [
                ("""
                    UPDATE gift_logs
                    SET host_name = scheduled_host
                    WHERE (host_name IS NULL OR host_name = '')
                      AND scheduled_host IS NOT NULL AND scheduled_host != ''
                """, ())
            ])
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DB Migration] gift_logs host backfill skipped: {e}")
        _log_db_init_stage("gift_log_host_backfill", gift_host_started)

        # Backfill log_date/log_time for legacy rows so analytics and range
        # filters can see every gift that is already in the log.
        gift_dates_started = time.perf_counter()
        try:
            _execute_atomic_batch(conn, [
                ("""
                    UPDATE gift_logs
                    SET log_datetime = COALESCE(NULLIF(log_datetime, ''), created_at)
                    WHERE log_datetime IS NULL OR log_datetime = ''
                """, ()),
                ("""
                    UPDATE gift_logs
                    SET log_date = substr(COALESCE(NULLIF(log_datetime, ''), created_at), 1, 10)
                    WHERE (log_date IS NULL OR log_date = '')
                      AND COALESCE(NULLIF(log_datetime, ''), created_at) IS NOT NULL
                """, ()),
                ("""
                    UPDATE gift_logs
                    SET log_time = substr(COALESCE(NULLIF(log_datetime, ''), created_at), 12, 8)
                    WHERE (log_time IS NULL OR log_time = '')
                      AND COALESCE(NULLIF(log_datetime, ''), created_at) IS NOT NULL
                """, ()),
            ])
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DB Migration] gift_logs date backfill skipped: {e}")
        _log_db_init_stage("gift_log_date_backfills", gift_dates_started)

        stage_started = _log_db_init_stage("gift_log_backfills", stage_started)

        # Date-range analytics filters wrap these columns in substr(), so
        # matching expression indexes avoid full-table scans on narrower ranges.
        _ensure_analytics_indexes(conn)

        # Normalize viewer names to lowercase so the same viewer never gets two
        # separate coin rows ("Alice" vs "alice"). Idempotent & safe.
        try:
            viewer_scan_started = time.perf_counter()
            mixed = conn.execute(
                "SELECT tiktok_user FROM tiktok_gifts WHERE tiktok_user != LOWER(tiktok_user)"
            ).fetchall()
            _log_db_init_stage("viewer_name_scan", viewer_scan_started)
            print(f"[DB Init Timing] stage=viewer_name_scan_rows count={len(mixed)}")
            viewer_updates_started = time.perf_counter()
            for (key_value,) in mixed:
                lower_key = (key_value or "").strip().lower()
                if not lower_key:
                    continue
                row = conn.execute(
                    "SELECT COALESCE(total_coins, 0) FROM tiktok_gifts WHERE tiktok_user = ?",
                    (key_value,),
                ).fetchone()
                coins_value = int(row[0] or 0) if row else 0
                existing = conn.execute(
                    "SELECT 1 FROM tiktok_gifts WHERE tiktok_user = ?", (lower_key,)
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE tiktok_gifts SET total_coins = COALESCE(total_coins, 0) + ? "
                        "WHERE tiktok_user = ?",
                        (coins_value, lower_key),
                    )
                    conn.execute("DELETE FROM tiktok_gifts WHERE tiktok_user = ?", (key_value,))
                else:
                    conn.execute(
                        "UPDATE tiktok_gifts SET tiktok_user = ? WHERE tiktok_user = ?",
                        (lower_key, key_value),
                    )
            _log_db_init_stage("viewer_name_updates", viewer_updates_started)
            viewer_commit_started = time.perf_counter()
            conn.commit()
            _log_db_init_stage("viewer_name_commit", viewer_commit_started)
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DB Migration] tiktok_gifts case normalization skipped: {e}")

        stage_started = _log_db_init_stage("viewer_name_normalization", stage_started)
        try:
            daily_scan_started = time.perf_counter()
            mixed_daily = conn.execute(
                "SELECT tiktok_user, date FROM daily_tiktok_gifts "
                "WHERE tiktok_user != LOWER(tiktok_user)"
            ).fetchall()
            _log_db_init_stage("daily_gift_name_scan", daily_scan_started)
            print(f"[DB Init Timing] stage=daily_gift_name_scan_rows count={len(mixed_daily)}")
            daily_updates_started = time.perf_counter()
            for key_value, date_value in mixed_daily:
                lower_key = (key_value or "").strip().lower()
                if not lower_key:
                    continue
                row = conn.execute(
                    "SELECT COALESCE(coins, 0) FROM daily_tiktok_gifts "
                    "WHERE tiktok_user = ? AND date = ?",
                    (key_value, date_value),
                ).fetchone()
                coins_value = int(row[0] or 0) if row else 0
                existing = conn.execute(
                    "SELECT 1 FROM daily_tiktok_gifts WHERE tiktok_user = ? AND date = ?",
                    (lower_key, date_value),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE daily_tiktok_gifts SET coins = COALESCE(coins, 0) + ? "
                        "WHERE tiktok_user = ? AND date = ?",
                        (coins_value, lower_key, date_value),
                    )
                    conn.execute(
                        "DELETE FROM daily_tiktok_gifts WHERE tiktok_user = ? AND date = ?",
                        (key_value, date_value),
                    )
                else:
                    conn.execute(
                        "UPDATE daily_tiktok_gifts SET tiktok_user = ? "
                        "WHERE tiktok_user = ? AND date = ?",
                        (lower_key, key_value, date_value),
                    )
            _log_db_init_stage("daily_gift_name_updates", daily_updates_started)
            daily_commit_started = time.perf_counter()
            conn.commit()
            _log_db_init_stage("daily_gift_name_commit", daily_commit_started)
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DB Migration] daily_tiktok_gifts case normalization skipped: {e}")

        stage_started = _log_db_init_stage("daily_gift_normalization", stage_started)

        # Legacy announcement schema compatibility.
        _add_missing_columns(conn, "active_live_announcements", [
            ("host_name", "TEXT"),
        ])

        # Host priority (1 = normal/prioritized connect, 0 = non-prioritized).
        _add_missing_columns(conn, "hosts", [
            ("is_priority", "INTEGER DEFAULT 1"),
        ])
        priority_started = time.perf_counter()
        try:
            _normalize_host_priorities(conn)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        _log_db_init_stage("host_priority_normalization", priority_started)
        stage_started = _log_db_init_stage("host_schema_compatibility", stage_started)

        # Default settings (never overwrite values saved by the admin)
        wib = pytz.timezone('Asia/Jakarta')
        now_str = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
        defaults = {
            "master_enabled": "1",
            "debug_mode": "0",
            "feature_announcements": "1",
            "feature_verification": "1",
            "feature_role_tiers": "1",
            "feature_leaderboard": "1",
            "feature_live_tracking": "1",
            "allowed_channels_enabled": "0",
            "allowed_channel_ids": "[]",
            "announcement_channel_ids": json.dumps(
                [str(os.environ.get("ANNOUNCEMENT_CHANNEL_ID"))]
                if os.environ.get("ANNOUNCEMENT_CHANNEL_ID") else []
            ),
            "role_tiers_enabled": "1",
            "role_tiers_mode": "replace",
            "role_tier_reset_enabled": "0",
            "role_tier_reset_interval": "4",
            "role_tier_reset_unit": "weeks",
            "role_tier_announce": "1",
            "role_tier_permanent_enabled": "0",
            "role_tier_last_reset": now_str,
            "non_priority_mode": "slow",  # slow | off (for non-prioritized active hosts)
            "non_priority_slow_seconds": "300",
            # Coin-only verification: grant the verified role when a claimed
            # TikTok account already has at least N coins (no proof required).
            "coin_only_verification_enabled": "0",
            "coin_only_min_coins": "100",
            # Max seconds a Verify Bio / Live Greeting click may wait for
            # TikTok or DB work before the bot answers with a timeout message.
            "verification_click_timeout_seconds": "45",
        }
        settings_seed_started = time.perf_counter()
        _seed_default_settings(conn, defaults)
        _log_db_init_stage("settings_seed_statement", settings_seed_started)
        settings_commit_started = time.perf_counter()
        conn.commit()
        _log_db_init_stage("settings_seed_commit", settings_commit_started)
        stage_started = _log_db_init_stage("settings_seed", stage_started)

        # Read the seed and repair state together to avoid a second remote
        # round trip when the one-time repair has already completed.
        alias_count, aliases_repaired = _get_host_alias_seed_state(conn)
        if not alias_count:
            for alias, host_name in HOST_ALIASES.items():
                conn.execute(
                    "INSERT OR IGNORE INTO host_aliases (alias, host_name) VALUES (?, ?)",
                    (alias, host_name),
                )
            conn.commit()
        stage_started = _log_db_init_stage("host_alias_seed", stage_started)

        # Repair stale/incorrect aliases once (e.g. an "angel" alias pointing
        # to another host). This is safe: it only fills missing canonical
        # aliases and corrects aliases whose name is itself a real host name.
        try:
            repair_host_aliases(conn, marker_present=aliases_repaired)
        except Exception as e:
            print(f"[DB Migration] Host alias repair failed: {e}")
        stage_started = _log_db_init_stage("host_alias_repair", stage_started)

        # Seed role tiers from the legacy JSON config only once.
        role_seed_check_started = time.perf_counter()
        seeded, tier_count = _get_role_tier_seed_state(conn)
        _log_db_init_stage("role_tier_seed_state_check", role_seed_check_started)
        print(
            f"[DB Init Timing] stage=role_tier_seed_state "
            f"marker_present={seeded} tier_count={tier_count}"
        )
        if not seeded and not tier_count:
            role_config_started = time.perf_counter()
            try:
                if os.path.exists("config/roles.json"):
                    with open("config/roles.json", "r") as f:
                        cfg = json.load(f)
                    guild_tiers = cfg.get("role_tiers", {})
                    first = next(iter(guild_tiers.values()), [])
                    _log_db_init_stage("role_tier_seed_config_load", role_config_started)
                    role_insert_started = time.perf_counter()
                    for idx, item in enumerate(first, start=1):
                        min_coins = item.get("min_coins") if isinstance(item, dict) else item[0]
                        role_id = item.get("role_id") if isinstance(item, dict) else item[1]
                        conn.execute("""
                            INSERT INTO role_tiers
                                (group_key, tier_name, role_id, min_coins, sort_order)
                            VALUES ('reset', ?, ?, ?, ?)
                        """, (f"Tier {idx}", str(role_id), int(min_coins), idx))
                    _log_db_init_stage("role_tier_seed_inserts", role_insert_started)
                    print(f"[DB Init Timing] stage=role_tier_seed_rows count={len(first)}")
                else:
                    _log_db_init_stage("role_tier_seed_config_load", role_config_started)
            except Exception as e:
                print(f"[DB Seed] Role tier seed skipped: {e}")
            conn.execute(
                "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('tiers_seeded', '1')"
            )
            role_commit_started = time.perf_counter()
            conn.commit()
            _log_db_init_stage("role_tier_seed_commit", role_commit_started)
        stage_started = _log_db_init_stage("role_tier_seed", stage_started)

        # Sinkronisasi 6 akun target resmi dengan ID 1-6 (reusing this connection)
        sync_valid_hosts(conn)
        conn.commit()
        stage_started = _log_db_init_stage("host_sync", stage_started)
    finally:
        conn.close()
        _log_db_init_stage("total", init_started)

def _classify_activity(event_type: str, message: str, category: str = None, level: str = None):
    """Infer (category, level) for an activity log entry.

    Default is operational/info for business events. Technical captures system
    diagnostics, failures, and debugging-relevant errors.
    """
    type_key = str(event_type or "").strip().lower()
    msg = str(message or "")
    msg_l = msg.lower()

    if level:
        level_n = str(level).strip().lower()
    else:
        if type_key in ("error", "critical", "crash", "timeout", "failed", "failure", "exception"):
            level_n = "error"
        elif type_key in ("warning", "warn"):
            level_n = "warning"
        else:
            level_n = "info"

    if category:
        cat_n = str(category).strip().lower()
        if cat_n not in ("operational", "technical"):
            cat_n = "operational"
        # Promote obvious technical severities into technical category.
        if cat_n == "operational" and level_n in ("error", "critical") and type_key in ("error", "critical", "crash", "exception", "timeout", "failed", "failure"):
            cat_n = "technical"
        return cat_n, level_n

    # Explicit technical event types
    if type_key in ("error", "critical", "crash", "exception", "timeout", "failed", "failure"):
        return "technical", ("error" if level_n == "info" else level_n)
    if type_key in ("warning", "warn"):
        return "technical", "warning"

    # Heuristics for mixed "system"/debug messages
    technical_markers = (
        "[debug", "traceback", "exception", "failed to", "could not", "error",
        "timeout", "timed out", "backoff", "rate limit", "rate-limit", "ratelimit",
        "jsondecodeerror", "expecting value", "cooldown", "connection error",
        "websocket", "sign-server", "sign server", "http 4", "http 5",
        "invalid webhook signature", "rejected eulerstream webhook",
        "sqlite", "database is locked", "turso", "hrana", "unavailable",
    )
    operational_markers = (
        "admin ", "requested by admin", "admin requested", "cleared", "reset",
        "settings saved", "tier ", "granted role", "revoked", "unclaimed",
        "re-checked verification", "verification panel", "gift log selftest",
        "reconnect requested", "started observer task", "stopped observer",
        "observer supervisor started", "host priority mode updated",
    )

    if type_key == "system":
        if any(m in msg_l for m in operational_markers):
            return "operational", level_n
        if msg_l.startswith("[debug @") or any(m in msg_l for m in ("backoff", "rate limit", "rate-limit", "ratelimit", "jsondecodeerror", "expecting value", "cooldown", "connection error", "failed to", "could not", "timeout", "traceback", "exception")):
            return "technical", (level_n if level_n != "info" else "warning")
        if msg_l.startswith("[debug") or any(m in msg_l for m in technical_markers):
            return "technical", (level_n if level_n != "info" else "warning")
        return "operational", level_n

    # Webhook rejections/failures are technical diagnostics
    if type_key == "webhook" and any(m in msg_l for m in ("rejected", "invalid", "failed", "error", "unrecognized")):
        return "technical", (level_n if level_n != "info" else "warning")

    return "operational", level_n


def log_activity_event(event_type: str, message: str, tiktok_username: str = "", category: str = None, level: str = None, source: str = ""):
    """Mencatat aktivitas ke tabel activity_logs (operasional/teknikal) dan ke logger.

    Penulisan DB dilakukan oleh satu thread penulis di belakang queue. Dengan
    begitu pemanggilan dari event loop Discord tidak pernah menunggu Turso.
    """
    event_type = str(event_type or "info")
    message = str(message or "")
    user_clean = (tiktok_username or "").lower().lstrip("@")
    cat_n, level_n = _classify_activity(event_type, message, category=category, level=level)
    source_n = str(source or "")

    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')

    # Route activity to the central logger: everything is persisted to
    # logs/bot.log, and error/critical activity is also sent to the Discord
    # error channel by log_setup.
    if level_n == "error":
        py_level = logging.ERROR
    elif level_n == "warning":
        py_level = logging.WARNING
    else:
        py_level = logging.INFO
    _ACTIVITY_LOGGER.log(
        py_level,
        "[ActivityLog] [%s/%s] @%s %s",
        cat_n,
        event_type,
        user_clean or "-",
        message,
        extra={
            "activity_type": event_type,
            "tiktok_user": user_clean or None,
            "log_category": cat_n,
            "log_source": source_n or "activity_event",
            "persisted_activity_log": True,
        },
    )

    _ensure_activity_log_writer()
    try:
        _ACTIVITY_LOG_QUEUE.put_nowait((event_type, message, user_clean, now_str, cat_n, level_n, source_n))
    except queue.Full:
        _ACTIVITY_LOGGER.error("[ActivityLog] Queue is full; event dropped to keep the event loop responsive.")


def log_technical_event(message: str, level: str = "warning", event_type: str = "technical", tiktok_username: str = "", source: str = "system"):
    """Convenience helper for technical/system diagnostics in activity logs."""
    return log_activity_event(
        event_type=event_type,
        message=message,
        tiktok_username=tiktok_username,
        category="technical",
        level=level,
        source=source,
    )


_ACTIVITY_LOG_QUEUE = queue.Queue(maxsize=5000)
_ACTIVITY_LOG_WRITER_STARTED = False
_ACTIVITY_LOG_WRITER_LOCK = threading.Lock()
_ACTIVITY_LOG_PRUNE_COUNTER = 0
_ACTIVITY_LOG_PRUNE_LOCK = threading.Lock()


def _ensure_activity_log_writer():
    global _ACTIVITY_LOG_WRITER_STARTED
    if _ACTIVITY_LOG_WRITER_STARTED:
        return
    with _ACTIVITY_LOG_WRITER_LOCK:
        if _ACTIVITY_LOG_WRITER_STARTED:
            return
        _ACTIVITY_LOG_WRITER_STARTED = True
        threading.Thread(
            target=_activity_log_writer_loop,
            name="activity-log-writer",
            daemon=True,
        ).start()


def _insert_activity_log_batch(conn, rows):
    """Insert an activity-log batch with one remote SQL statement."""
    normalized_rows = []
    for row in rows:
        # Backward compatibility with old 4-tuple shape
        if len(row) == 4:
            event_type, message, user_clean, now_str = row
            cat_n, lvl_n, src_n = "operational", "info", ""
        else:
            event_type, message, user_clean, now_str, cat_n, lvl_n, src_n = row
        normalized_rows.append((
            event_type,
            user_clean,
            message,
            now_str,
            cat_n or "operational",
            lvl_n or "info",
            src_n or "",
        ))

    if not normalized_rows:
        return

    # One multi-row statement avoids up to 100 sequential remote
    # Turso round-trips while holding the interactive transaction.
    row_placeholders = "(?, ?, ?, ?, ?, ?, ?)"
    values_sql = ", ".join(row_placeholders for _ in normalized_rows)
    params = tuple(value for row in normalized_rows for value in row)
    _execute_atomic_statement(
        conn,
        "INSERT INTO activity_logs "
        "(event_type, tiktok_username, message, created_at, category, level, source) "
        f"VALUES {values_sql}",
        params,
    )


_ACTIVITY_LOG_BATCH_MAX_SIZE = 100
# A slightly wider collection window reduces small remote Turso transactions
# when events arrive in bursts, while keeping dashboard log freshness bounded.
_ACTIVITY_LOG_BATCH_WAIT_SECONDS = 1.25


def _collect_activity_log_batch(first_item, event_queue=None, max_size=None, max_wait=None):
    """Collect queued events without changing FIFO order or losing shutdown signals."""
    event_queue = event_queue or _ACTIVITY_LOG_QUEUE
    max_size = _ACTIVITY_LOG_BATCH_MAX_SIZE if max_size is None else max(1, int(max_size))
    max_wait = _ACTIVITY_LOG_BATCH_WAIT_SECONDS if max_wait is None else max(0.0, float(max_wait))
    batch = [first_item]
    shutdown = False
    deadline = time.monotonic() + max_wait

    while len(batch) < max_size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            nxt = event_queue.get(timeout=remaining)
        except queue.Empty:
            break
        if nxt is None:
            event_queue.task_done()
            shutdown = True
            break
        batch.append(nxt)

    return batch, shutdown


def _activity_log_writer_loop():
    """Persist activity log rows in small batches."""
    global _ACTIVITY_LOG_PRUNE_COUNTER
    while True:
        item = _ACTIVITY_LOG_QUEUE.get()
        if item is None:
            _ACTIVITY_LOG_QUEUE.task_done()
            return
        batch, shutdown = _collect_activity_log_batch(item)

        def _write_batch(conn, rows=batch):
            _insert_activity_log_batch(conn, rows)

        try:
            write_transaction(_write_batch, timing_label="activity_log_batch")
            with _ACTIVITY_LOG_PRUNE_LOCK:
                _ACTIVITY_LOG_PRUNE_COUNTER += len(batch)
                should_prune = _ACTIVITY_LOG_PRUNE_COUNTER >= 250
                if should_prune:
                    _ACTIVITY_LOG_PRUNE_COUNTER = 0
            if should_prune:
                try:
                    prune_activity_logs()
                except Exception:
                    pass
        except Exception as e:
            _ACTIVITY_LOGGER.error(
                "[ActivityLog] Failed to persist %d event(s): %s", len(batch), e
            )
        finally:
            for _ in batch:
                _ACTIVITY_LOG_QUEUE.task_done()

        if shutdown:
            return


def prune_activity_logs(max_operational: int = 4000, max_technical: int = 6000):
    """Keep activity_logs bounded per category (DB-backed retention)."""
    try:
        max_operational = max(200, int(max_operational or 4000))
        max_technical = max(200, int(max_technical or 6000))
    except Exception:
        max_operational, max_technical = 4000, 6000

    def _op(conn):
        for cat, keep in (("operational", max_operational), ("technical", max_technical)):
            conn.execute(
                """
                DELETE FROM activity_logs
                WHERE category = ? AND log_id NOT IN (
                    SELECT log_id FROM activity_logs
                    WHERE category = ?
                    ORDER BY log_id DESC
                    LIMIT ?
                )
                """,
                (cat, cat, keep),
            )

    try:
        write_transaction(_op)
    except Exception as e:
        _ACTIVITY_LOGGER.error("[ActivityLog] Prune failed: %s", e)


def get_gift_event_summary(viewer_user: str, target_user: str, date_str: str = "") -> dict:
    """Fetch gift spending totals and channel name with one Turso query."""
    viewer_clean = (viewer_user or "").lower().lstrip("@")
    target_clean = (target_user or "").lower().lstrip("@")
    if not date_str:
        date_str = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%Y-%m-%d")

    channel_name = next(
        (name for _, username, name in VALID_TARGET_ACCOUNTS if username == target_clean),
        None,
    )
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT
                COALESCE((
                    SELECT SUM(coins) FROM gift_logs
                    WHERE LOWER(viewer_username) = ?
                      AND LOWER(target_username) = ?
                ), 0),
                COALESCE((
                    SELECT coins FROM daily_tiktok_gifts
                    WHERE LOWER(tiktok_user) = ? AND date = ?
                    LIMIT 1
                ), 0),
                COALESCE((
                    SELECT SUM(total_coins) FROM tiktok_gifts
                    WHERE LOWER(tiktok_user) = ?
                ), 0),
                (
                    SELECT display_name FROM hosts
                    WHERE LOWER(tiktok_username) = ?
                    LIMIT 1
                )
            """,
            (viewer_clean, target_clean, viewer_clean, date_str, viewer_clean, target_clean),
        ).fetchone()
        if row and not channel_name:
            channel_name = row[3]
        return {
            "channel_spent": int(row[0] or 0) if row else 0,
            "today_spent": int(row[1] or 0) if row else 0,
            "total_spent": int(row[2] or 0) if row else 0,
            "channel_name": str(channel_name or target_clean),
        }
    finally:
        conn.close()


def get_user_channel_spent(viewer_user: str, target_user: str) -> int:
    """Menghitung total koin yang sudah di-spent oleh viewer di target channel ini."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("""
            SELECT COALESCE(SUM(coins), 0) FROM gift_logs 
            WHERE LOWER(viewer_username) = ? AND LOWER(target_username) = ?
        """, (viewer_user.lower().lstrip("@"), target_user.lower().lstrip("@"))).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

def get_user_today_spent(viewer_user: str, date_str: str = "") -> int:
    """Menghitung total koin yang sudah di-spent oleh viewer hari ini."""
    if not date_str:
        wib = pytz.timezone('Asia/Jakarta')
        date_str = datetime.now(wib).strftime('%Y-%m-%d')
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("""
            SELECT COALESCE(coins, 0) FROM daily_tiktok_gifts 
            WHERE LOWER(tiktok_user) = ? AND date = ?
        """, (viewer_user.lower().lstrip("@"), date_str)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

def get_user_total_spent(viewer_user: str) -> int:
    """Menghitung total koin keseluruhan yang sudah di-spent oleh viewer di database."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("""
            SELECT COALESCE(SUM(total_coins), 0) FROM tiktok_gifts 
            WHERE LOWER(tiktok_user) = ?
        """, (viewer_user.lower().lstrip("@"),)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

def get_activity_logs(limit: int = 100, channel_filter: str = "", category: str = "operational", level: str = "", search: str = "", source: str = "") -> list:
    """Mengambil riwayat log aktivitas terbaru.

    Returns rows: (log_id, event_type, tiktok_username, message, created_at, category, level, source)
    """
    try:
        limit = max(1, min(int(limit or 100), 2000))
    except (TypeError, ValueError):
        limit = 100
    cat = (category or "").strip().lower()
    lvl = (level or "").strip().lower()
    q = (search or "").strip()
    src = (source or "").strip().lower()
    user_filter = (channel_filter or "").strip().lower().lstrip("@")

    where = []
    params = []
    if cat in ("operational", "technical"):
        where.append("COALESCE(category, 'operational') = ?")
        params.append(cat)
    elif cat in ("all", ""):
        pass
    else:
        where.append("COALESCE(category, 'operational') = ?")
        params.append("operational")
    if lvl in ("info", "warning", "error"):
        where.append("COALESCE(level, 'info') = ?")
        params.append(lvl)
    if user_filter:
        where.append("LOWER(tiktok_username) = ?")
        params.append(user_filter)
    if src:
        where.append("LOWER(COALESCE(source, '')) = ?")
        params.append(src)
    if q:
        where.append("(message LIKE ? OR event_type LIKE ? OR tiktok_username LIKE ? OR COALESCE(source,'') LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like, like])

    sql = """
        SELECT log_id, event_type, tiktok_username, message, created_at,
               COALESCE(category, 'operational') AS category,
               COALESCE(level, 'info') AS level,
               COALESCE(source, '') AS source
        FROM activity_logs
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY log_id DESC LIMIT ?"
    params.append(limit)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute(sql, tuple(params)).fetchall()
        return rows
    finally:
        conn.close()


def export_activity_logs(limit: int = 5000, channel_filter: str = "", category: str = "operational", level: str = "", search: str = "") -> list:
    """Fetch logs for export (same filters as get_activity_logs, higher cap)."""
    try:
        limit = max(1, min(int(limit or 5000), 20000))
    except (TypeError, ValueError):
        limit = 5000
    return get_activity_logs(limit=limit, channel_filter=channel_filter, category=category, level=level, search=search)


def clear_activity_logs(category: str = None):
    """Clear activity logs.

    category: 'operational', 'technical', or None/'all' for all.
    """
    cat = (category or "").strip().lower()
    conn = get_db_connection()
    try:
        if cat in ("operational", "technical"):
            conn.execute("DELETE FROM activity_logs WHERE category = ?", (cat,))
        else:
            conn.execute("DELETE FROM activity_logs")
        conn.commit()
    finally:
        conn.close()


# Host asli yang dideteksi (selain ini dihapus)
HOST_ALIASES = {
    # Luna
    "luna": "Luna",
    # Sinta
    "sinta": "Sinta",
    "shinta": "Sinta",
    "mommy": "Sinta",
    "momy": "Sinta",
    # Zize
    "zize": "Zize",
    "zeze": "Zize",
    # Beatrix
    "beatrix": "Beatrix",
    "bea": "Beatrix",
    # Fearless
    "fearless": "Fearless",
    # Yosa
    "yosa": "Yosa",
    # Yuca
    "yuca": "Yuca",
    "yuc": "Yuca",
    # Angel
    "angel": "Angel",
}


def _get_host_alias_seed_state(conn):
    """Read alias count and one-time repair marker in a single DB round trip."""
    row = conn.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM host_aliases), "
        "EXISTS(SELECT 1 FROM bot_settings "
        "WHERE key = ? AND value = ?)",
        ("host_aliases_repair_v1", "1"),
    ).fetchone()
    return int(row[0]), bool(row[1])


def repair_host_aliases(conn=None, *, marker_present=None):
    """One-time repair of stale/corrupt host alias mappings.

    - Adds missing canonical aliases from HOST_ALIASES.
    - Corrects an alias that is itself a real host name but points elsewhere
      (for example ``angel -> Sinta``).
    - Leaves custom aliases such as ``mommy -> Sinta`` untouched.
    """
    own = conn is None
    if own:
        conn = get_db_connection()
    try:
        cur = conn.cursor()
        if marker_present is None:
            marker = cur.execute(
                "SELECT value FROM bot_settings WHERE key = 'host_aliases_repair_v1'"
            ).fetchone()
            marker_present = bool(marker and str(marker[0]) == "1")
        if marker_present:
            return

        canonical = {str(k).lower(): str(v) for k, v in HOST_ALIASES.items()}
        canonical_hosts = {host.lower(): host for host in set(canonical.values())}
        changes = []
        for alias, host in canonical.items():
            row = cur.execute(
                "SELECT host_name FROM host_aliases WHERE alias = ?", (alias,)
            ).fetchone()
            if not row:
                cur.execute(
                    "INSERT OR IGNORE INTO host_aliases (alias, host_name) VALUES (?, ?)",
                    (alias, host),
                )
                changes.append(f"added {alias}->{host}")
                continue
            existing = str(row[0] or "").strip()
            if alias in canonical_hosts and existing.lower() != host.lower():
                cur.execute(
                    "UPDATE host_aliases SET host_name = ? WHERE alias = ?",
                    (host, alias),
                )
                changes.append(f"fixed {alias}: {existing or '(empty)'}->{host}")

        cur.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES ('host_aliases_repair_v1', '1')"
        )
        conn.commit()
        invalidate_host_alias_cache()
        if changes:
            print("[DB Migration] Host alias repair: " + "; ".join(changes[:30]))
    finally:
        if own:
            conn.close()

_HOST_ALIAS_CACHE = {"loaded_at": 0.0, "items": None}
_HOST_ALIAS_TTL = 30.0
_HOST_ALIAS_REFRESH_LOCK = threading.Lock()
_HOST_ALIAS_REFRESHING = False


def invalidate_host_alias_cache():
    _HOST_ALIAS_CACHE["loaded_at"] = 0.0
    _HOST_ALIAS_CACHE["items"] = None


def _load_host_aliases_from_db() -> list:
    conn = None
    try:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT alias, host_name FROM host_aliases ORDER BY LENGTH(alias) DESC"
        ).fetchall()
        return [(r[0], r[1]) for r in rows if r[0] and r[1]]
    except Exception:
        return list(HOST_ALIASES.items())
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _refresh_host_aliases_async() -> None:
    """Refresh the alias cache in a worker thread (single flight)."""
    global _HOST_ALIAS_REFRESHING
    with _HOST_ALIAS_REFRESH_LOCK:
        if _HOST_ALIAS_REFRESHING:
            return
        _HOST_ALIAS_REFRESHING = True

    def _run():
        global _HOST_ALIAS_REFRESHING
        try:
            items = _load_host_aliases_from_db()
            _HOST_ALIAS_CACHE["items"] = items
            _HOST_ALIAS_CACHE["loaded_at"] = time.time()
        finally:
            with _HOST_ALIAS_REFRESH_LOCK:
                _HOST_ALIAS_REFRESHING = False

    threading.Thread(target=_run, name="host-alias-refresh", daemon=True).start()


def get_host_aliases(*, block: bool = True) -> list:
    """Return all aliases as (alias, host_name), cached for live chat speed.

    Live Intros arrive on the Discord event loop. A stale cache used to open a
    Turso connection right there and froze the loop for 2.4 s (enough to miss
    interaction ACKs). On the loop pass ``block=False``: cached aliases are
    returned immediately and a worker thread refreshes them.
    """
    now = time.time()
    items = _HOST_ALIAS_CACHE["items"]
    if items is not None and now - _HOST_ALIAS_CACHE["loaded_at"] <= _HOST_ALIAS_TTL:
        return list(items)

    if not block:
        _refresh_host_aliases_async()
        return list(items if items is not None else HOST_ALIASES.items())

    items = _load_host_aliases_from_db()
    _HOST_ALIAS_CACHE["items"] = items
    _HOST_ALIAS_CACHE["loaded_at"] = time.time()
    return list(items)


def add_host_alias(alias: str, host_name: str):
    alias_clean = (alias or "").strip().lower().lstrip("@")
    host_clean = (host_name or "").strip()
    if not alias_clean or not host_clean:
        return False
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO host_aliases (alias, host_name) VALUES (?, ?)
            ON CONFLICT(alias) DO UPDATE SET host_name = excluded.host_name
        """, (alias_clean, host_clean))
        conn.commit()
        invalidate_host_alias_cache()
        _refresh_host_aliases_async()
        return True
    finally:
        conn.close()


def delete_host_alias(alias: str):
    alias_clean = (alias or "").strip().lower().lstrip("@")
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM host_aliases WHERE alias = ?", (alias_clean,))
        conn.commit()
        invalidate_host_alias_cache()
        _refresh_host_aliases_async()
    finally:
        conn.close()


@lru_cache(maxsize=512)
def _compile_host_alias_pattern(alias: str):
    """Compile each stable host alias once instead of on every LiveIntro event."""
    pattern = rf"(?<![A-Za-z0-9]){re.escape(alias)}(?:['\u2019`s]+s|s)?(?![A-Za-z0-9])"
    return re.compile(pattern, re.IGNORECASE)


def detect_host_match_from_welcome_message(text: str, *, block: bool = True):
    """
    Deteksi host dari teks Live Intro/welcome.

    Returns:
        (host_name, matched_alias) jika ada alias cocok di database host_aliases,
        atau (None, None) jika tidak ada alias yang cocok.
    """
    if not text:
        return None, None
    for kw, host_name in get_host_aliases(block=block):
        # Reuse the compiled token pattern across repeated LiveIntro events.
        if _compile_host_alias_pattern(kw).search(text):
            return host_name, kw
    return None, None


def detect_host_from_welcome_message(text: str):
    """
    Mendeteksi nama host dari welcome message / chat / intro di TikTok.
    Case-insensitive, alias dikelola dari admin panel.
    Mendukung bentuk possessive seperti luna's / angel's / luna’s.
    """
    host_name, _alias = detect_host_match_from_welcome_message(text)
    return host_name

def live_host_detected_at(tiktok_username: str) -> str:
    """Kapan host untuk akun ini terakhir terdeteksi (format WIB)."""
    u_clean = tiktok_username.lower().lstrip("@")
    return str(get_setting(f"current_host_at_{u_clean}") or "")


def _parse_wib_ts(value):
    try:
        text = str(value or "")[:19]
        if not text:
            return None
        return pytz.timezone('Asia/Jakarta').localize(
            datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
        )
    except Exception:
        return None


# A live session that has not heartbeated for this long is treated as gone.
LIVE_HOST_STALE_AFTER_SECONDS = 900


def host_belongs_to_current_live(tiktok_username: str) -> bool:
    """True kalau host tersimpan dideteksi di sesi live yang masih berjalan.

    ``current_host_<account>`` disimpan per akun, bukan per sesi, sehingga saat
    live baru dimulai panel/scheduled_host bisa menampilkan host dari live
    sebelumnya (mis. "Yosa" padahal yang live "Windy"). Host hanya dianggap
    valid kalau deteksinya terjadi setelah sesi terbuka dimulai dan sesi itu
    masih mengirim heartbeat.
    """
    u_clean = tiktok_username.lower().lstrip("@")
    detected = _parse_wib_ts(live_host_detected_at(u_clean))
    if not detected:
        return False
    conn = get_db_connection()
    try:
        row = conn.execute("""
            SELECT start_time, last_seen_at FROM live_sessions
            WHERE tiktok_username = ? AND end_time IS NULL
            ORDER BY session_id DESC LIMIT 1
        """, (u_clean,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    started = _parse_wib_ts(row[0])
    if not started or detected < started:
        return False
    heartbeat = _parse_wib_ts(row[1]) or started
    now = datetime.now(pytz.timezone('Asia/Jakarta'))
    return (now - heartbeat).total_seconds() <= LIVE_HOST_STALE_AFTER_SECONDS


def get_current_host_for_account(tiktok_username: str, fresh_only: bool = False) -> str:
    """Mengambil nama host live yang sedang/terakhir terdeteksi untuk akun TikTok ini.

    Jangan fallback ke hosts.display_name karena itu nama channel/akun, bukan host live terdeteksi.

    ``fresh_only`` dipakai saat memulai/menyambung live: host dari live sebelumnya
    diabaikan supaya panel menampilkan "Host" (belum terdeteksi) alih-alih nama
    host yang salah sampai LiveIntro untuk live baru diterima.
    """
    u_clean = tiktok_username.lower().lstrip("@")
    val = get_setting(f"current_host_{u_clean}")
    if val and val != "Host":
        if not fresh_only or host_belongs_to_current_live(u_clean):
            return val
        return "Host"

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("""
            SELECT scheduled_host, start_time, last_seen_at FROM live_sessions 
            WHERE tiktok_username = ? AND end_time IS NULL 
            ORDER BY session_id DESC LIMIT 1
        """, (u_clean,)).fetchone()
        if row and row[0] and row[0] != "Host":
            if not fresh_only:
                return row[0]
            started = _parse_wib_ts(row[1])
            heartbeat = _parse_wib_ts(row[2]) or started
            now = datetime.now(pytz.timezone('Asia/Jakarta'))
            if started and (now - heartbeat).total_seconds() <= LIVE_HOST_STALE_AFTER_SECONDS:
                return row[0]
    finally:
        conn.close()
    return "Host"

def set_current_host_for_account(tiktok_username: str, host_name: str):
    """Menyimpan nama host terdeteksi tanpa menahan worker pada round-trip Turso."""
    u_clean = tiktok_username.lower().lstrip("@")
    _queue_setting_or_write(f"current_host_{u_clean}", host_name)
    wib = pytz.timezone('Asia/Jakarta')
    _queue_setting_or_write(
        f"current_host_at_{u_clean}",
        datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S'),
    )


def touch_current_host_detection(tiktok_username: str):
    """Perbarui waktu deteksi host tanpa mengubah namanya (LiveIntro berulang)."""
    u_clean = tiktok_username.lower().lstrip("@")
    if not str(get_setting(f"current_host_{u_clean}") or ""):
        return
    wib = pytz.timezone('Asia/Jakarta')
    _queue_setting_or_write(
        f"current_host_at_{u_clean}",
        datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S'),
    )

def update_live_session_host(session_id: int, host_name: str, allow_closed: bool = False):
    """Update the detected host for a live session.

    By default only open sessions are updated (auto-detection during a live).
    Pass ``allow_closed=True`` for manual/admin resolution of ended sessions
    (host-detection "debt").
    """
    if not session_id:
        return False
    wib = pytz.timezone('Asia/Jakarta')
    now_str = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    where = "WHERE session_id = ?" + ("" if allow_closed else " AND end_time IS NULL")

    def _update(conn):
        cur = conn.execute(
            "UPDATE live_sessions SET scheduled_host = ?, last_seen_at = ? " + where,
            (host_name, now_str, session_id),
        )
        return (cur.rowcount or 0) > 0

    return write_transaction(_update, timing_label="live_session_host_update")


_TOUCH_LIVE_CACHE = {}
_TOUCH_LIVE_LOCK = threading.Lock()
_TOUCH_LIVE_MIN_INTERVAL = 60.0


def touch_live_session(session_id: int, when: str = None):
    """Catat bahwa sesi live masih aktif (heartbeat/event) untuk resume setelah restart."""
    if not session_id:
        return

    # Heartbeats arrive on every gift and comment. ``last_seen_at`` only matters
    # for the restart grace period, so update Turso at most once a minute per
    # session instead of writing on every live-chat message.
    if not when:
        now_ts = time.time()
        with _TOUCH_LIVE_LOCK:
            if now_ts - _TOUCH_LIVE_CACHE.get(session_id, 0.0) < _TOUCH_LIVE_MIN_INTERVAL:
                return
            if len(_TOUCH_LIVE_CACHE) > 500:
                _TOUCH_LIVE_CACHE.clear()
            _TOUCH_LIVE_CACHE[session_id] = now_ts
        wib = pytz.timezone('Asia/Jakarta')
        when = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')

    def _store(conn):
        _execute_atomic_statement(
            conn,
            "UPDATE live_sessions SET last_seen_at = ? "
            "WHERE session_id = ? AND end_time IS NULL",
            (str(when)[:19], session_id),
        )

    try:
        write_transaction(_store, timing_label="live_session_touch")
    except Exception as e:
        print(f"[DB] Failed to touch live session {session_id}: {type(e).__name__}: {e}")


def add_live_session_paused_seconds(session_id: int, seconds: int) -> int:
    """Tambahkan detik jeda (grace period) ke sesi live yang masih terbuka.

    Detik ini dikurangkan dari durasi akhir supaya waktu kosong/pending saat
    live terputus tidak dihitung sebagai durasi live.
    """
    try:
        seconds = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        seconds = 0
    if not session_id or seconds <= 0:
        return 0
    wib = pytz.timezone('Asia/Jakarta')
    now_str = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')

    def _update(conn):
        cur = conn.cursor()
        cur.execute(
            "UPDATE live_sessions "
            "SET paused_seconds = COALESCE(paused_seconds, 0) + ?, last_seen_at = ? "
            "WHERE session_id = ? AND end_time IS NULL",
            (seconds, now_str, session_id),
        )
        row = cur.execute(
            "SELECT COALESCE(paused_seconds, 0) FROM live_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    return write_transaction(_update, timing_label="live_session_pause_update")

def get_scheduled_host_name(tiktok_username: str, dt_obj: datetime = None, session_id: int = None) -> str:
    """Host for a gift: fresh detection first, then this live session's host.

    Never fall back to a host from a previous live (that would misattribute
    gifts at the start of a new stream).
    """
    host = str(get_current_host_for_account(tiktok_username, fresh_only=True) or "").strip()
    if host and host not in ("Host", "-"):
        return host
    if session_id:
        try:
            conn = get_db_connection()
            try:
                row = conn.execute(
                    "SELECT scheduled_host FROM live_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                session_host = str((row or [None])[0] or "").strip()
                if session_host and session_host not in ("Host", "-"):
                    return session_host
            finally:
                conn.close()
        except Exception:
            pass
    return host or "Host"

# Sesi yang ditutup karena proses bot restart/deploy boleh disambung lagi
# kalau observer sudah reconnect dalam jendela ini. Ini mencegah history live
# terpecah-pecah hanya karena redeploy, tanpa menghitung waktu bot mati.
LIVE_RESUME_GRACE_SECONDS = 600
# Open sessions whose heartbeat stopped longer than this are considered stale
# and closed at their last heartbeat instead of being resumed.
LIVE_SESSION_STALE_SECONDS = 1800


def get_open_live_session_id(tiktok_username: str):
    """Return the latest open live session id for an account, or None."""
    user_clean = (tiktok_username or "").lower().lstrip("@")
    if not user_clean:
        return None
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT session_id FROM live_sessions WHERE tiktok_username = ? AND end_time IS NULL "
            "ORDER BY session_id DESC LIMIT 1",
            (user_clean,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    finally:
        conn.close()


def start_live_session(tiktok_username: str, initial_host: str = None) -> int:
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    user_clean = tiktok_username.lower().lstrip("@")
    explicit_host = initial_host is not None
    current_host = initial_host if explicit_host else get_current_host_for_account(user_clean, fresh_only=True)

    def _start(conn):
        cur = conn.cursor()

        # 1) Coba lanjutkan sesi yang di-suspend saat bot restart/deploy.
        resume_key = f"resume_session_{user_clean}"
        resume_last_key = f"resume_last_seen_{user_clean}"
        resume_settings = {
            str(key): value
            for key, value in cur.execute(
                "SELECT key, value FROM bot_settings WHERE key IN (?, ?)",
                (resume_key, resume_last_key),
            ).fetchall()
        }
        resume_value = resume_settings.get(resume_key)
        resume_last_value = resume_settings.get(resume_last_key)
        if resume_value and str(resume_value).strip():
            try:
                resume_id = int(str(resume_value).strip())
            except (TypeError, ValueError):
                resume_id = 0
            last_row = (resume_last_value,)
            resume_last_dt = None
            if last_row and last_row[0]:
                try:
                    resume_last_dt = wib.localize(
                        datetime.strptime(str(last_row[0])[:19], '%Y-%m-%d %H:%M:%S')
                    )
                except Exception:
                    resume_last_dt = None
            gap = 0
            if resume_last_dt is not None:
                gap = max(0, int((now_dt - resume_last_dt).total_seconds()))
            if resume_id and resume_last_dt is not None and gap <= LIVE_RESUME_GRACE_SECONDS:
                old = cur.execute(
                    "SELECT tiktok_username, end_time FROM live_sessions WHERE session_id = ?",
                    (resume_id,),
                ).fetchone()
                if old and str(old[0] or "").lower() == user_clean and old[1]:
                    resume_host = str(current_host or "").strip()
                    if resume_host in ("Host", "-"):
                        resume_host = None
                    cur.execute("""
                        UPDATE live_sessions
                        SET end_time = NULL,
                            duration_seconds = 0,
                            paused_seconds = COALESCE(paused_seconds, 0) + ?,
                            scheduled_host = CASE WHEN ? IS NULL THEN scheduled_host ELSE ? END,
                            last_seen_at = ?
                        WHERE session_id = ?
                    """, (gap, resume_host, resume_host, now_str, resume_id))
                    cur.execute(
                        "DELETE FROM bot_settings WHERE key IN (?, ?)",
                        (resume_key, resume_last_key),
                    )
                    print(
                        f"[LiveSession] Resumed session {resume_id} for @{user_clean} "
                        f"after {gap}s restart gap (paused time excluded)."
                    )
                    return resume_id
            # Resume window expired / data tidak valid: jangan buka sesi lama.
            cur.execute(
                "DELETE FROM bot_settings WHERE key IN (?, ?)",
                (resume_key, resume_last_key),
            )

        active_rows = cur.execute("""
            SELECT session_id, last_seen_at FROM live_sessions 
            WHERE tiktok_username = ? AND end_time IS NULL
            ORDER BY session_id DESC
        """, (user_clean,)).fetchall()

        if active_rows:
            active_id, last_seen_value = active_rows[0]
            # A session left open by a missed disconnect (e.g. websocket error
            # instead of DisconnectEvent) must not be resumed hours later: that
            # inflates live duration. If heartbeats stopped long ago, close it at
            # its last confirmation time and start a fresh session below.
            stale_end = None
            try:
                last_seen = str(last_seen_value or "")[:19]
                if last_seen:
                    last_seen_dt = wib.localize(datetime.strptime(last_seen, '%Y-%m-%d %H:%M:%S'))
                    if (now_dt - last_seen_dt).total_seconds() > LIVE_SESSION_STALE_SECONDS:
                        stale_end = last_seen
            except Exception:
                stale_end = None
            if stale_end:
                cur.execute("""
                    UPDATE live_sessions
                    SET end_time = ?, duration_seconds = CASE
                        WHEN start_time IS NULL THEN 0
                        ELSE MAX(0, CAST((julianday(?) - julianday(start_time)) * 86400 AS INTEGER) - COALESCE(paused_seconds, 0))
                    END,
                    last_seen_at = ?
                    WHERE session_id = ? AND end_time IS NULL
                """, (stale_end, stale_end, stale_end, active_id))
                print(
                    f"[LiveSession] Closed stale open session {active_id} for @{user_clean} "
                    f"at last heartbeat {stale_end} (no activity > {LIVE_SESSION_STALE_SECONDS // 60}m)."
                )
                active_rows = active_rows[1:]
            # Only one live session may be open per account. Close stale
            # duplicates left behind by crashes/older versions.
            for stale_id, _last_seen in active_rows[1:]:
                cur.execute("""
                    UPDATE live_sessions
                    SET end_time = ?, duration_seconds = CASE
                        WHEN start_time IS NULL THEN 0
                        ELSE CASE
                            WHEN CAST((julianday(?) - julianday(start_time)) * 86400 AS INTEGER) - COALESCE(paused_seconds, 0) < 0 THEN 0
                            ELSE CAST((julianday(?) - julianday(start_time)) * 86400 AS INTEGER) - COALESCE(paused_seconds, 0)
                        END
                    END,
                    last_seen_at = COALESCE(last_seen_at, ?)
                    WHERE session_id = ?
                """, (now_str, now_str, now_str, now_str, stale_id))
            if active_rows:
                active_id = active_rows[0][0]
                cur.execute(
                    "UPDATE live_sessions SET last_seen_at = ? WHERE session_id = ?",
                    (now_str, active_id),
                )
                if explicit_host:
                    cur.execute("UPDATE live_sessions SET scheduled_host = ? WHERE session_id = ?", (current_host, active_id))
                return active_id

        # A live that has already ended must NOT be reopened: a new connect
        # after the disconnect/offline grace period always starts a new session
        # (with the host detected for that new live).
        cur.execute("""
            INSERT INTO live_sessions (tiktok_username, start_time, scheduled_host, last_seen_at) 
            VALUES (?, ?, ?, ?)
        """, (user_clean, now_str, current_host, now_str))
        return cur.lastrowid
    return write_transaction(_start, timing_label="live_session_start")
def end_live_session(session_id: int, end_time: str = None):
    """Close a live session. ``end_time`` lets callers record the actual
    disconnect moment instead of the moment the grace timer fired."""
    wib = pytz.timezone('Asia/Jakarta')
    now = datetime.now(wib)
    end_dt = now
    if end_time:
        try:
            end_naive = datetime.strptime(str(end_time)[:19], '%Y-%m-%d %H:%M:%S')
            end_dt = wib.localize(end_naive)
        except Exception:
            end_dt = now
    end_str = end_dt.strftime('%Y-%m-%d %H:%M:%S')
    def _end(conn):
        effective_end_dt = end_dt
        effective_end_str = end_str
        cur = conn.cursor()
        try:
            row = cur.execute(
                "SELECT start_time, end_time, COALESCE(paused_seconds, 0), last_seen_at FROM live_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        except Exception:
            row = cur.execute(
                "SELECT start_time, end_time, 0, NULL FROM live_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return
        if row[1]:
            # Already closed (idempotent); never overwrite a recorded end.
            return
        # A late webhook must not inflate duration: heartbeats keep last_seen_at
        # fresh during a live, so an end far past it is an offline-report delay.
        last_seen = str((row[3] if len(row) > 3 else None) or "")[:19]
        if last_seen:
            try:
                seen_dt = wib.localize(datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S"))
                if effective_end_dt > seen_dt + timedelta(minutes=2):
                    effective_end_dt = seen_dt
                    effective_end_str = effective_end_dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        paused_sec = max(0, int(row[2] or 0))
        duration_sec = 0
        if row[0]:
            try:
                start_dt_naive = datetime.strptime(str(row[0])[:19], '%Y-%m-%d %H:%M:%S')
                start_dt = wib.localize(start_dt_naive)
                duration_sec = max(0, int((effective_end_dt - start_dt).total_seconds()) - paused_sec)
            except Exception:
                duration_sec = 0

        cur.execute("""
            UPDATE live_sessions 
            SET end_time = ?, duration_seconds = ?, last_seen_at = COALESCE(last_seen_at, ?)
            WHERE session_id = ?
        """, (effective_end_str, duration_sec, effective_end_str, session_id))
    return write_transaction(_end)
def _get_table_column_info(cur, table: str) -> list:
    """Ambil detail kolom tabel: nama, tipe, NOT NULL, default, primary key."""
    try:
        rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return []
    info = []
    for row in rows:
        if len(row) < 2:
            continue
        info.append({
            "name": str(row[1]),
            "type": str(row[2] or ""),
            "notnull": bool(row[3]) if len(row) > 3 else False,
            "default": row[4] if len(row) > 4 else None,
            "pk": bool(row[5]) if len(row) > 5 else False,
        })
    return info


def _get_table_columns(cur, table: str) -> set:
    """Ambil nama kolom tabel (aman untuk SQLite maupun Turso/libsql)."""
    return {col["name"] for col in _get_table_column_info(cur, table)}

# gift_logs schema is stable between application startups. Cache its metadata
# briefly so every GiftEvent does not pay an extra remote Turso PRAGMA round-trip.
_GIFT_LOG_COLUMN_INFO_CACHE = None
_GIFT_LOG_COLUMN_INFO_CACHE_AT = 0.0
_GIFT_LOG_COLUMN_INFO_CACHE_TTL = 300.0
_GIFT_LOG_COLUMN_INFO_CACHE_LOCK = threading.Lock()


def _get_cached_gift_log_column_info(cur) -> list:
    """Return cached gift_logs schema metadata, refreshing it at most every 5 minutes."""
    global _GIFT_LOG_COLUMN_INFO_CACHE, _GIFT_LOG_COLUMN_INFO_CACHE_AT
    now = time.monotonic()
    with _GIFT_LOG_COLUMN_INFO_CACHE_LOCK:
        if (
            _GIFT_LOG_COLUMN_INFO_CACHE is not None
            and now - _GIFT_LOG_COLUMN_INFO_CACHE_AT < _GIFT_LOG_COLUMN_INFO_CACHE_TTL
        ):
            return [dict(column) for column in _GIFT_LOG_COLUMN_INFO_CACHE]

    info = _get_table_column_info(cur, "gift_logs")
    if info:
        cached = tuple(dict(column) for column in info)
        with _GIFT_LOG_COLUMN_INFO_CACHE_LOCK:
            _GIFT_LOG_COLUMN_INFO_CACHE = cached
            _GIFT_LOG_COLUMN_INFO_CACHE_AT = time.monotonic()
        return [dict(column) for column in cached]
    return info


def _fallback_gift_log_value(name: str, col_type: str, data: dict):
    """Nilai aman untuk kolom NOT NULL lama yang tidak ada di data kita."""
    col_type = (col_type or "").upper()
    if name == "tiktok_username":
        # Schema lama menyimpan akun target di kolom NOT NULL ini.
        return data.get("target_username") or data.get("viewer_username") or ""
    if name == "target_username":
        return data.get("tiktok_username") or "unknown"
    if name == "viewer_username":
        return "__unknown__"
    if any(token in col_type for token in ("INT", "REAL", "NUM", "DEC", "FLOA", "DOUB")):
        return 0
    return ""


def _build_gift_log_insert_statement(
    data: dict, or_ignore: bool = True, column_info: list = None, cur=None
) -> tuple:
    """Build a schema-compatible gift-log INSERT and its bound parameters."""
    info = (
        column_info
        if column_info is not None
        else _get_table_column_info(cur, "gift_logs")
    )
    existing = {col["name"] for col in info}
    if not existing:
        existing = set(data.keys())
    insert_cols = [col for col in data.keys() if col in existing]

    # Lengkapi kolom NOT NULL tanpa default yang belum kita isi.
    for col in info:
        name = col["name"]
        if name in insert_cols:
            continue
        if col["notnull"] and col["default"] is None and not col["pk"]:
            fallback = _fallback_gift_log_value(name, col["type"], data)
            data[name] = fallback
            insert_cols.append(name)
            print(
                f"[GiftLog] Filling legacy NOT NULL column '{name}' with "
                f"{fallback!r} so the gift log can be stored."
            )

    required = {"viewer_username", "coins"}
    missing = required - set(insert_cols)
    if missing:
        raise RuntimeError(
            "Kolom wajib gift_logs tidak ditemukan: " + ", ".join(sorted(missing))
        )
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    placeholders = ", ".join(["?"] * len(insert_cols))
    sql = (
        f"{verb} INTO gift_logs ({', '.join(insert_cols)}) "
        f"VALUES ({placeholders})"
    )
    return sql, tuple(data[col] for col in insert_cols), insert_cols


def _insert_gift_log_row(
    cur, data: dict, or_ignore: bool = True, column_info: list = None
) -> list:
    """Insert a gift log using only columns present in the active schema."""
    sql, params, insert_cols = _build_gift_log_insert_statement(
        data, or_ignore=or_ignore, column_info=column_info, cur=cur
    )
    cur.execute(sql, params)
    return insert_cols


def record_gift_event_transaction(
    session_id: int,
    target_user: str,
    viewer_user: str,
    viewer_display: str,
    gift_name: str,
    gift_count: int,
    coins: int,
    msg_id: str = None,
    host_name: str = None,
) -> dict:
    """Atomically store a gift log and update all coin aggregates.

    The detailed log remains the source of truth: duplicate/failed inserts never
    increment session, all-time, or daily totals.
    """
    wib = pytz.timezone("Asia/Jakarta")
    now_dt = datetime.now(wib)
    log_date = now_dt.strftime("%Y-%m-%d")
    log_time = now_dt.strftime("%H:%M:%S")
    log_datetime = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    target_clean = (target_user or "").lower()
    viewer_clean = (viewer_user or "").lower()
    # The observer passes its in-memory host only after validating it against
    # the current live session and refreshing it from LiveIntro detections.
    # Reuse that confirmed value instead of opening another DB connection on
    # every gift. Keep the DB lookup for placeholder/unknown host states so a
    # resumed session can still supply its scheduled host.
    supplied_host = str(host_name or "").strip()
    if supplied_host and supplied_host not in ("Host", "-"):
        scheduled_host = supplied_host
    else:
        scheduled_host = get_scheduled_host_name(
            target_clean, now_dt, session_id=session_id
        )
    host_clean = (
        supplied_host
        or (scheduled_host or "").strip()
        or "Host"
    )
    data = {
        "tiktok_username": target_clean,
        "session_id": session_id,
        "viewer_username": viewer_clean,
        "viewer_display_name": viewer_display,
        "gift_name": gift_name,
        "gift_count": gift_count,
        "coins": coins,
        "log_date": log_date,
        "log_time": log_time,
        "log_datetime": log_datetime,
        "target_username": target_clean,
        "scheduled_host": scheduled_host,
        "msg_id": str(msg_id) if msg_id else None,
        "host_name": host_clean,
    }

    def _store(conn):
        cur = conn.cursor()
        # Reuse schema metadata across gifts; a remote PRAGMA on every event
        # adds an avoidable Turso round-trip to the critical write path.
        column_info = _get_cached_gift_log_column_info(cur)
        insert_sql, insert_params, _ = _build_gift_log_insert_statement(
            data, or_ignore=True, column_info=column_info
        )

        batch = getattr(conn, "batch", None)
        if callable(batch):
            # One SQL-over-HTTP batch replaces several sequential requests.
            # changes() carries the insert's affected-row count forward:
            # duplicate msg_ids make every aggregate step a no-op.
            results = batch(
                [
                    (insert_sql, insert_params),
                    (
                        """
                        INSERT INTO tiktok_gifts
                            (tiktok_user, display_name, total_coins)
                        SELECT ?, ?, ? WHERE changes() > 0
                        ON CONFLICT(tiktok_user) DO UPDATE SET
                            total_coins = total_coins + excluded.total_coins,
                            display_name = excluded.display_name
                        RETURNING total_coins
                        """,
                        (viewer_clean, viewer_display, coins),
                    ),
                    (
                        """
                        INSERT INTO daily_tiktok_gifts
                            (tiktok_user, display_name, coins, date)
                        SELECT ?, ?, ?, ? WHERE changes() > 0
                        ON CONFLICT(tiktok_user, date) DO UPDATE SET
                            coins = coins + excluded.coins,
                            display_name = excluded.display_name
                        """,
                        (viewer_clean, viewer_display, coins, log_date),
                    ),
                    (
                        """
                        INSERT INTO bot_settings (key, value)
                        SELECT 'gift_log_last_ok', ? WHERE changes() > 0
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        (log_datetime,),
                    ),
                    (
                        """
                        UPDATE live_sessions
                        SET total_coins = total_coins + ?
                        WHERE session_id = ? AND changes() > 0
                        """,
                        (coins, session_id),
                    ),
                ],
                mode="immediate",
            )
            if results[0].rowcount == 0:
                return {"status": "duplicate"}
            total_rows = results[1].rows
            return {
                "status": "ok",
                "total_coins": int(total_rows[0][0]) if total_rows else 0,
            }

        # SQLite fallback: preserve the existing DB-API transaction path.
        _insert_gift_log_row(
            cur, data, or_ignore=True, column_info=column_info
        )
        if cur.rowcount == 0:
            return {"status": "duplicate"}

        if session_id:
            cur.execute(
                "UPDATE live_sessions SET total_coins = total_coins + ? "
                "WHERE session_id = ?",
                (coins, session_id),
            )

        total_row = cur.execute(
            """
            INSERT INTO tiktok_gifts (tiktok_user, display_name, total_coins)
            VALUES (?, ?, ?)
            ON CONFLICT(tiktok_user) DO UPDATE SET
                total_coins = total_coins + excluded.total_coins,
                display_name = excluded.display_name
            RETURNING total_coins
            """,
            (viewer_clean, viewer_display, coins),
        ).fetchone()

        cur.execute(
            """
            INSERT INTO daily_tiktok_gifts (tiktok_user, display_name, coins, date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tiktok_user, date) DO UPDATE SET
                coins = coins + excluded.coins,
                display_name = excluded.display_name
            """,
            (viewer_clean, viewer_display, coins, log_date),
        )
        cur.execute(
            """
            INSERT INTO bot_settings (key, value) VALUES ('gift_log_last_ok', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (log_datetime,),
        )
        return {
            "status": "ok",
            "total_coins": int(total_row[0]) if total_row else 0,
        }

    try:
        result = write_transaction(_store, timing_label="gift_event")
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        print(
            f"[GiftLog] Atomic write failed for @{viewer_user} "
            f"({gift_name}, {coins}c): {error_text}"
        )
        try:
            set_setting("gift_log_last_error", f"{log_datetime} | {error_text}")
        except Exception:
            pass
        try:
            log_activity_event(
                "system",
                f"[GiftLog] GAGAL menulis gift dan memperbarui total koin "
                f"untuk @{viewer_user} ({gift_name}, {coins}c): {error_text}",
                target_clean,
            )
        except Exception:
            pass
        return {"status": "failed", "error": error_text}
    if result["status"] == "ok":
        # The success marker is committed atomically with the gift aggregates.
        # Refresh the process cache without opening another write transaction.
        _cache_setting_value("gift_log_last_ok", log_datetime)
    return result


def log_gift_transaction(session_id: int, target_user: str, viewer_user: str, viewer_display: str, gift_name: str, gift_count: int, coins: int, msg_id: str = None, host_name: str = None):
    """Mencatat log gift secara terperinci sesuai waktu WIB & host terjadwal.

    Returns:
      True  -> row stored (hanya kolom yang ada di schema).
      False -> duplicate/replayed message id, nothing stored.
      None  -> the gift log could not be written at all (already reported).
    """
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)

    log_date = now_dt.strftime('%Y-%m-%d')
    log_time = now_dt.strftime('%H:%M:%S')
    log_datetime = now_dt.strftime('%Y-%m-%d %H:%M:%S')

    target_clean = target_user.lower()
    scheduled_host = get_scheduled_host_name(target_clean, now_dt, session_id=session_id)
    # Host yang sedang tampil saat gift masuk (hasil LiveIntroEvent). Fallback ke
    # host terjadwal bila observer belum sempat mendeteksi host.
    host_clean = (host_name or "").strip() or (scheduled_host or "").strip() or "Host"
    viewer_clean = (viewer_user or "").lower()

    data = {
        "tiktok_username": target_clean,
        "session_id": session_id,
        "viewer_username": viewer_clean,
        "viewer_display_name": viewer_display,
        "gift_name": gift_name,
        "gift_count": gift_count,
        "coins": coins,
        "log_date": log_date,
        "log_time": log_time,
        "log_datetime": log_datetime,
        "target_username": target_clean,
        "scheduled_host": scheduled_host,
        "msg_id": (str(msg_id) if msg_id else None),
        "host_name": host_clean,
    }

    def _store(conn):
        cur = conn.cursor()
        columns = _get_table_columns(cur, "gift_logs")
        msg_id_clean = data["msg_id"]
        if msg_id_clean and "msg_id" in columns:
            try:
                already = cur.execute(
                    "SELECT 1 FROM gift_logs WHERE msg_id = ? LIMIT 1", (msg_id_clean,)
                ).fetchone()
                if already:
                    return False
            except Exception as dedupe_error:
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(f"[GiftLog] Dedupe check failed ({dedupe_error}); continuing insert.")

        _insert_gift_log_row(cur, data, or_ignore=True)
        if session_id:
            cur.execute("UPDATE live_sessions SET total_coins = total_coins + ? WHERE session_id = ?", (coins, session_id))
        return True

    try:
        stored = write_transaction(_store, timing_label="gift_log")
    except Exception as e:
        error_text = f"{type(e).__name__}: {e}"
        print(f"[GiftLog] FAILED to store gift log for @{viewer_user} ({gift_name}, {coins}c): {error_text}")
        try:
            set_setting("gift_log_last_error", f"{log_datetime} | {error_text}")
        except Exception:
            pass
        try:
            log_activity_event(
                "system",
                f"[GiftLog] GAGAL menulis gift log untuk @{viewer_user} ({gift_name}, {coins}c): {error_text}",
                target_clean,
            )
        except Exception:
            pass
        return None

    if stored is False:
        return False

    try:
        set_setting("gift_log_last_ok", log_datetime)
    except Exception:
        pass
    return True

def test_gift_log_write() -> dict:
    """Uji tulis/hapus gift_logs untuk mendiagnosis kegagalan insert di produksi."""
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    stamp = now_dt.strftime('%Y%m%d%H%M%S%f')
    data = {
        "tiktok_username": "__selftest__",
        "session_id": None,
        "viewer_username": "__selftest__",
        "viewer_display_name": "Self Test",
        "gift_name": "__selftest__",
        "gift_count": 1,
        "coins": 0,
        "log_date": now_dt.strftime('%Y-%m-%d'),
        "log_time": now_dt.strftime('%H:%M:%S'),
        "log_datetime": now_dt.strftime('%Y-%m-%d %H:%M:%S'),
        "target_username": "__selftest__",
        "scheduled_host": "Self Test",
        "msg_id": f"selftest-{stamp}",
        "host_name": "Self Test",
    }
    columns = []
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        columns = sorted(_get_table_columns(cur, "gift_logs"))
        inserted_columns = _insert_gift_log_row(cur, data, or_ignore=False)
        conn.commit()
        cur.execute("DELETE FROM gift_logs WHERE viewer_username = ?", ("__selftest__",))
        conn.commit()
        return {
            "ok": True,
            "backend": get_database_backend(),
            "columns": columns,
            "inserted_columns": inserted_columns,
            "message": "Insert & delete baris tes gift_logs berhasil.",
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {
            "ok": False,
            "backend": get_database_backend(),
            "columns": columns,
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        conn.close()


def backfill_gift_logs_from_daily() -> dict:
    """Isi gift_logs historis dari daily_tiktok_gifts + saldo awal tersimpan.

    Berguna jika koin lama sudah masuk ke tiktok_gifts tetapi detail gift_logs
    belum pernah tercatat. Baris buatan ditandai jelas sebagai "Backfill" dan
    tidak menimpa tanggal yang sudah punya detail asli.
    """
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        inserted_daily = 0
        inserted_opening = 0
        daily_coins = 0
        opening_coins = 0

        daily_rows = cur.execute("""
            SELECT LOWER(d.tiktok_user) AS u, d.date, COALESCE(SUM(d.coins), 0),
                   MAX(d.display_name)
            FROM daily_tiktok_gifts d
            WHERE d.date IS NOT NULL AND d.date != ''
              AND NOT EXISTS (
                  SELECT 1 FROM gift_logs g
                  WHERE LOWER(g.viewer_username) = LOWER(d.tiktok_user)
                    AND g.log_date = d.date
              )
            GROUP BY LOWER(d.tiktok_user), d.date
            ORDER BY d.date
        """).fetchall()
        for user, date, coins, display_name in daily_rows:
            coins = int(coins or 0)
            if coins <= 0:
                continue
            data = {
                "tiktok_username": "backfill",
                "session_id": None,
                "viewer_username": user or "",
                "viewer_display_name": display_name or user or "",
                "gift_name": "Backfill harian",
                "gift_count": 1,
                "coins": coins,
                "log_date": date,
                "log_time": "00:00:00",
                "log_datetime": f"{date} 00:00:00",
                "target_username": "backfill",
                "scheduled_host": "Backfill",
                "msg_id": f"backfill-daily-{user}-{date}",
                "host_name": "Backfill",
            }
            _insert_gift_log_row(cur, data, or_ignore=True)
            inserted_daily += 1
            daily_coins += coins
        conn.commit()

        diff_rows = cur.execute("""
            SELECT LOWER(g.tiktok_user) AS u, MAX(g.display_name),
                   COALESCE(g.total_coins, 0) - COALESCE(l.coins, 0) AS diff
            FROM tiktok_gifts g
            LEFT JOIN (
                SELECT LOWER(viewer_username) AS u, SUM(coins) AS coins
                FROM gift_logs GROUP BY LOWER(viewer_username)
            ) l ON LOWER(g.tiktok_user) = l.u
            WHERE COALESCE(g.total_coins, 0) > COALESCE(l.coins, 0)
        """).fetchall()
        for user, display_name, diff in diff_rows:
            diff = int(diff or 0)
            if diff <= 0:
                continue
            date_row = cur.execute("""
                SELECT MIN(d) FROM (
                    SELECT MIN(date) AS d FROM daily_tiktok_gifts
                    WHERE LOWER(tiktok_user) = LOWER(?)
                    UNION ALL
                    SELECT MIN(log_date) AS d FROM gift_logs
                    WHERE LOWER(viewer_username) = LOWER(?)
                )
            """, (user, user)).fetchone()
            date = (date_row[0] if date_row and date_row[0] else "2020-01-01")
            data = {
                "tiktok_username": "backfill",
                "session_id": None,
                "viewer_username": user or "",
                "viewer_display_name": display_name or user or "",
                "gift_name": "Backfill saldo awal",
                "gift_count": 1,
                "coins": diff,
                "log_date": date,
                "log_time": "00:00:00",
                "log_datetime": f"{date} 00:00:00",
                "target_username": "backfill",
                "scheduled_host": "Backfill",
                "msg_id": f"backfill-opening-{user}",
                "host_name": "Backfill",
            }
            _insert_gift_log_row(cur, data, or_ignore=True)
            inserted_opening += 1
            opening_coins += diff
        conn.commit()
        return {
            "inserted_daily": inserted_daily,
            "daily_coins": daily_coins,
            "inserted_opening": inserted_opening,
            "opening_coins": opening_coins,
            "at": now_dt.strftime('%Y-%m-%d %H:%M:%S'),
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


def save_gift_data(username: str, display_name: str, coins: int) -> int:
    def _store(conn):
        conn.execute("""
            INSERT INTO tiktok_gifts (tiktok_user, display_name, total_coins)
            VALUES (?, ?, ?)
            ON CONFLICT(tiktok_user) DO UPDATE SET
            total_coins = total_coins + excluded.total_coins,
            display_name = excluded.display_name
        """, (username.lower(), display_name, coins))
        row = conn.execute(
            "SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (username.lower(),)
        ).fetchone()
        return row[0] if row else 0

    return write_transaction(_store, timing_label="gift_total")

def save_daily_gift_data(username: str, display_name: str, coins: int):
    wib = pytz.timezone('Asia/Jakarta')
    today_str = datetime.now(wib).strftime('%Y-%m-%d')

    def _store(conn):
        conn.execute("""
            INSERT INTO daily_tiktok_gifts (tiktok_user, display_name, coins, date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tiktok_user, date) DO UPDATE SET
            coins = coins + excluded.coins,
            display_name = excluded.display_name
        """, (username.lower(), display_name, coins, today_str))

    write_transaction(_store, timing_label="gift_daily_total")

def recalculate_coin_totals() -> dict:
    """Rebuild all coin totals from the authoritative gift_logs table.

    Use this after imports/edits/fixes so tiktok_gifts (all-time) and
    daily_tiktok_gifts (per day) match the detailed gift log exactly.
    """
    wib = pytz.timezone('Asia/Jakarta')
    today_str = datetime.now(wib).strftime('%Y-%m-%d')
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Jangan kosongkan total tersimpan kalau gift log benar-benar kosong:
        # itu tanda penulisan log gagal, bukan data yang memang nol.
        log_count_row = cur.execute("SELECT COUNT(*) FROM gift_logs").fetchone()
        stored_row = cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_coins), 0) FROM tiktok_gifts"
        ).fetchone()
        if (log_count_row and int(log_count_row[0] or 0) == 0
                and stored_row and int(stored_row[0] or 0) > 0
                and int(stored_row[1] or 0) > 0):
            return {
                "error": (
                    "Gift log masih kosong, recalculate dibatalkan supaya total koin "
                    "tidak terhapus. Pastikan gift baru tercatat dulu (cek Coin Integrity "
                    "dan Activity Log)."
                )
            }
        # All-time totals per viewer (lowercase, single row per viewer).
        cur.execute("DELETE FROM tiktok_gifts")
        cur.execute("""
            INSERT INTO tiktok_gifts (tiktok_user, display_name, total_coins)
            SELECT LOWER(viewer_username), MAX(viewer_display_name), SUM(coins)
            FROM gift_logs
            GROUP BY LOWER(viewer_username)
        """)
        # Daily totals per viewer/day.
        cur.execute("DELETE FROM daily_tiktok_gifts")
        cur.execute("""
            INSERT INTO daily_tiktok_gifts (tiktok_user, display_name, coins, date)
            SELECT LOWER(viewer_username), MAX(viewer_display_name), SUM(coins), log_date
            FROM gift_logs
            GROUP BY LOWER(viewer_username), log_date
        """)
        conn.commit()
        row = cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_coins), 0) FROM tiktok_gifts"
        ).fetchone()
        today_row = cur.execute(
            "SELECT COALESCE(SUM(coins), 0) FROM daily_tiktok_gifts WHERE date = ?",
            (today_str,),
        ).fetchone()
        return {
            "viewers": int(row[0] or 0) if row else 0,
            "total_coins": int(row[1] or 0) if row else 0,
            "today_coins": int(today_row[0] or 0) if today_row else 0,
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[Coin] Recalculate failed: {e}")
        return {"error": str(e)}
    finally:
        conn.close()


def get_coin_integrity() -> dict:
    """Compare aggregate coin tables against the detailed gift_logs table."""
    wib = pytz.timezone('Asia/Jakarta')
    today_str = datetime.now(wib).strftime('%Y-%m-%d')
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        viewers_row = cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_coins), 0) FROM tiktok_gifts"
        ).fetchone()
        logs_row = cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(coins), 0) FROM gift_logs"
        ).fetchone()
        today_row = cur.execute(
            "SELECT COALESCE(SUM(coins), 0) FROM daily_tiktok_gifts WHERE date = ?",
            (today_str,),
        ).fetchone()
        today_log_row = cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(coins), 0), MAX(log_datetime) "
            "FROM gift_logs WHERE log_date = ?",
            (today_str,),
        ).fetchone()
        last_log_row = cur.execute("SELECT MAX(log_datetime) FROM gift_logs").fetchone()
        try:
            setting_rows = cur.execute(
                "SELECT key, value FROM bot_settings "
                "WHERE key IN ('gift_log_last_error', 'gift_log_last_ok')"
            ).fetchall()
            gift_log_settings = {r[0]: r[1] for r in setting_rows}
        except Exception:
            gift_log_settings = {}
        last_write_error = gift_log_settings.get("gift_log_last_error", "") or ""
        last_write_ok = gift_log_settings.get("gift_log_last_ok", "") or ""
        # Sembunyikan error lama jika penulisan terakhir sudah berhasil.
        if last_write_error and last_write_ok and last_write_error[:19] <= last_write_ok[:19]:
            last_write_error = ""
        try:
            gift_log_columns = sorted(_get_table_columns(cur, "gift_logs"))
        except Exception:
            gift_log_columns = []
        mismatches = cur.execute("""
            SELECT g.tiktok_user, COALESCE(g.total_coins, 0) AS stored,
                   COALESCE(l.coins, 0) AS logged
            FROM tiktok_gifts g
            LEFT JOIN (
                SELECT LOWER(viewer_username) AS u, SUM(coins) AS coins
                FROM gift_logs GROUP BY LOWER(viewer_username)
            ) l ON LOWER(g.tiktok_user) = l.u
            WHERE COALESCE(g.total_coins, 0) != COALESCE(l.coins, 0)
            ORDER BY ABS(COALESCE(g.total_coins, 0) - COALESCE(l.coins, 0)) DESC
            LIMIT 8
        """).fetchall()
        return {
            "viewers": int(viewers_row[0] or 0) if viewers_row else 0,
            "stored_total": int(viewers_row[1] or 0) if viewers_row else 0,
            "gift_log_rows": int(logs_row[0] or 0) if logs_row else 0,
            "logged_total": int(logs_row[1] or 0) if logs_row else 0,
            "today_total": int(today_row[0] or 0) if today_row else 0,
            "today_log_rows": int(today_log_row[0] or 0) if today_log_row else 0,
            "today_log_coins": int(today_log_row[1] or 0) if today_log_row else 0,
            "last_log_at": last_log_row[0] if last_log_row and last_log_row[0] else "",
            "backend": get_database_backend(),
            "gift_log_columns": gift_log_columns,
            "last_write_error": last_write_error,
            "last_write_ok": last_write_ok,
            "mismatches": [
                {"viewer": r[0], "stored": int(r[1] or 0), "logged": int(r[2] or 0)}
                for r in mismatches
            ],
        }
    except Exception as e:
        return {"error": str(e), "mismatches": []}
    finally:
        conn.close()


def get_daily_top_supporters():
    wib = pytz.timezone('Asia/Jakarta')
    today_str = datetime.now(wib).strftime('%Y-%m-%d')
    conn = get_db_connection()
    try:
        res = conn.execute(
            "SELECT tiktok_user, display_name, coins FROM daily_tiktok_gifts WHERE date = ? ORDER BY coins DESC LIMIT 10",
            (today_str,)
        )
        return res.fetchall()
    finally:
        conn.close()

def _normalize_discord_id(value) -> str:
    """Normalize Discord snowflake IDs to a digit string (no float/precision loss)."""
    if value is None:
        return ""
    s = str(value).strip()
    # Handle accidental float-like representations (e.g. "123.0")
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".", 1)[0]
    return s


# Role assignment runs after every gift. Cache mappings for several minutes
# to avoid repeated remote Turso lookups during an active live. Mapping changes
# made through bind_user_mapping invalidate both hit and miss entries immediately.
_DISCORD_MAPPING_CACHE = {}
_DISCORD_MAPPING_CACHE_TTL = 300.0
# Most live viewers are not linked to Discord. A short miss TTL avoids repeating
# remote lookups for viewers who send gifts more than once during a live.
# bind_user_mapping() invalidates both positive and negative entries immediately.
_DISCORD_MAPPING_MISS_CACHE_TTL = 90.0
_DISCORD_MAPPING_CACHE_LOCK = threading.Lock()


def invalidate_discord_mapping_cache() -> None:
    """Clear positive and negative TikTok-to-Discord lookups after a mapping changes."""
    with _DISCORD_MAPPING_CACHE_LOCK:
        _DISCORD_MAPPING_CACHE.clear()


def get_discord_id_from_tiktok(tiktok_user: str):
    username = str(tiktok_user or "").lower().lstrip("@").strip()
    if not username:
        return None

    now = time.monotonic()
    with _DISCORD_MAPPING_CACHE_LOCK:
        cached = _DISCORD_MAPPING_CACHE.get(username)
        if cached:
            cached_id, cached_at = cached
            ttl = (
                _DISCORD_MAPPING_CACHE_TTL
                if cached_id
                else _DISCORD_MAPPING_MISS_CACHE_TTL
            )
            if now - cached_at < ttl:
                return cached_id

    conn = get_db_connection()
    try:
        res = conn.execute(
            "SELECT discord_id FROM user_mappings WHERE tiktok_user = ?",
            (username,),
        )
        row = res.fetchone()
        discord_id = (
            _normalize_discord_id(row[0])
            if row and row[0] is not None
            else None
        )
    finally:
        conn.close()

    # Cache both hits and misses. The short miss TTL limits staleness, while
    # bind_user_mapping() clears the cache immediately when verification links
    # a previously unlinked viewer.
    with _DISCORD_MAPPING_CACHE_LOCK:
        _DISCORD_MAPPING_CACHE[username] = (discord_id, time.monotonic())
        if len(_DISCORD_MAPPING_CACHE) > 2000:
            _DISCORD_MAPPING_CACHE.clear()
            _DISCORD_MAPPING_CACHE[username] = (discord_id, time.monotonic())
    return discord_id

import random

def get_setting(key: str, default: str = None) -> str:
    """Mengambil nilai setting dari tabel bot_settings.

    Cache + stale-while-revalidate: setelah warm-up, pembacaan tidak pernah
    menyentuh Turso secara sinkron di event loop. Nilai basi dikembalikan
    langsung sementara refresh berjalan di thread latar.
    """
    now = time.time()
    with _SETTINGS_CACHE_LOCK:
        cached = _SETTINGS_CACHE.get(key)
    if cached is not None:
        value, expires = cached
        if expires <= now:
            _schedule_setting_refresh(key)
        return value if value is not None else default

    # Cold miss (key belum pernah dibaca). Setelah warm-up startup, key yang
    # tidak ada di cache berarti key tersebut memang tidak ada di DB, jadi
    # kembalikan default dan refresh di thread latar. Dengan begitu pembacaan
    # dari event loop Discord tidak pernah menunggu Turso.
    if _SETTINGS_WARMED:
        _schedule_setting_refresh(key)
        with _SETTINGS_CACHE_LOCK:
            _SETTINGS_CACHE.setdefault(key, (None, time.time() + _SETTINGS_MISS_TTL))
        return default

    value = _fetch_setting_value(key)
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE[key] = (value, time.time() + _SETTINGS_CACHE_TTL)
    return value if value is not None else default


def _write_setting_row(key: str, value: str) -> None:
    """Upsert satu setting lewat write_transaction (serial + retry SQLITE_BUSY)."""
    def _op(conn):
        conn.execute("""
            INSERT INTO bot_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))

    write_transaction(_op, timing_label="setting_write")


def _write_setting_rows(rows) -> int:
    """Upsert a batch of settings with one remote SQL statement."""
    latest_values = {}
    for key, value in rows:
        latest_values[str(key)] = str(value)

    if not latest_values:
        return 0

    items = tuple(latest_values.items())
    values_sql = ", ".join("(?, ?)" for _ in items)
    params = tuple(value for item in items for value in item)

    def _op(conn):
        _execute_atomic_statement(
            conn,
            "INSERT INTO bot_settings (key, value) "
            f"VALUES {values_sql} "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params,
        )

    write_transaction(_op, timing_label="setting_write_batch")
    return len(items)


def _cache_setting_value(key: str, value: str) -> None:
    with _SETTINGS_CACHE_LOCK:
        _SETTINGS_CACHE[key] = (str(value), time.time() + _SETTINGS_CACHE_TTL)
        _SETTINGS_REFRESHING.discard(key)


def _invalidate_setting_cache_if_value(key: str, value: str) -> None:
    """Remove an optimistic cache value only if it still matches a failed write."""
    with _SETTINGS_CACHE_LOCK:
        cached = _SETTINGS_CACHE.get(key)
        if cached is not None and cached[0] == str(value):
            _SETTINGS_CACHE.pop(key, None)
        _SETTINGS_REFRESHING.discard(key)


def set_setting(key: str, value: str):
    """Menyimpan setting secara sinkron (dipakai dari thread non-event-loop).

    Penulisan memakai ``write_transaction`` sehingga kegagalan Turso sementara
    (SQLITE_BUSY / "stream was idle") di-retry alih-alih langsung gagal.
    """
    _write_setting_row(key, value)
    # Update the cache in place so the next read cannot cause a sync DB hit.
    _cache_setting_value(key, value)


# Queue penulisan setting untuk dipanggil dari event loop Discord. Callback
# TikTok/observer berjalan di event loop; menulis ke Turso di sana memblokir
# ACK interaksi sampai Discord menampilkan "didn't respond in time".
_SETTING_WRITE_QUEUE = queue.Queue(maxsize=2000)
_SETTING_WRITER_STARTED = False
_SETTING_WRITER_LOCK = threading.Lock()


def _ensure_setting_writer() -> None:
    global _SETTING_WRITER_STARTED
    with _SETTING_WRITER_LOCK:
        if _SETTING_WRITER_STARTED:
            return
        _SETTING_WRITER_STARTED = True

    def _run():
        while True:
            item = _SETTING_WRITE_QUEUE.get()
            if item is None:
                _SETTING_WRITE_QUEUE.task_done()
                return

            batch = [item]
            shutdown = False
            deadline = time.monotonic() + 0.2
            while len(batch) < 50:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    nxt = _SETTING_WRITE_QUEUE.get(timeout=remaining)
                except queue.Empty:
                    break
                if nxt is None:
                    _SETTING_WRITE_QUEUE.task_done()
                    shutdown = True
                    break
                batch.append(nxt)

            # Keep FIFO semantics for each key while collapsing redundant
            # intermediate writes queued before this batch is persisted.
            latest_values = {}
            for key, value in batch:
                latest_values[key] = value

            try:
                _write_setting_rows(latest_values.items())
            except Exception as e:
                for key, value in latest_values.items():
                    _invalidate_setting_cache_if_value(key, value)
                print(
                    f"[DB] Deferred setting batch failed "
                    f"({len(latest_values)} key(s)): {type(e).__name__}: {e}"
                )
            finally:
                for _ in batch:
                    _SETTING_WRITE_QUEUE.task_done()

            if shutdown:
                return

    threading.Thread(target=_run, name="settings-writer", daemon=True).start()


def queue_setting(key: str, value: str) -> bool:
    """Simpan setting tanpa memblokir pemanggil (aman dari event loop).

    Nilai langsung masuk cache sehingga pembacaan berikutnya sudah melihat nilai
    baru; penulisan ke Turso dilakukan satu thread penulis di latar belakang.
    """
    _ensure_setting_writer()
    try:
        _SETTING_WRITE_QUEUE.put_nowait((key, str(value)))
    except queue.Full:
        print(f"[DB] Setting write queue full; dropping update for {key!r}.")
        return False

    # Publish the optimistic value only after the queue accepts the write.
    _cache_setting_value(key, value)
    return True


def _queue_setting_or_write(key: str, value: str) -> None:
    """Queue hot-path setting writes; fall back to synchronous persistence on overflow."""
    if not queue_setting(key, value):
        set_setting(key, value)


def get_bool_setting(key: str, default: bool = True) -> bool:
    value = get_setting(key)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled")


def get_json_list_setting(key: str) -> list:
    raw = get_setting(key, "[]")
    try:
        value = json.loads(raw or "[]")
        if isinstance(value, list):
            return value
    except Exception:
        pass
    # Backwards-compatible comma/space separated input
    return [p.strip() for p in re.split(r"[\s,]+", str(raw or "")) if p.strip()]


def get_int_list_setting(key: str) -> list:
    result = []
    for item in get_json_list_setting(key):
        try:
            result.append(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return result


def is_feature_enabled(feature: str) -> bool:
    """Master switch gates every individual feature."""
    if not get_bool_setting("master_enabled", True):
        return False
    return get_bool_setting(f"feature_{feature}", True)


def get_announcement_channel_ids() -> list:
    ids = get_int_list_setting("announcement_channel_ids")
    if ids:
        return list(dict.fromkeys(ids))
    env_id = os.environ.get("ANNOUNCEMENT_CHANNEL_ID")
    try:
        return [int(env_id)] if env_id else []
    except (TypeError, ValueError):
        return []


def get_role_tiers(group_key: str = None) -> list:
    conn = get_db_connection()
    try:
        if group_key:
            rows = conn.execute("""
                SELECT id, group_key, tier_name, role_id, min_coins, sort_order
                FROM role_tiers WHERE group_key = ?
                ORDER BY min_coins ASC, sort_order ASC, id ASC
            """, (group_key,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, group_key, tier_name, role_id, min_coins, sort_order
                FROM role_tiers
                ORDER BY group_key ASC, min_coins ASC, sort_order ASC, id ASC
            """).fetchall()
        return [
            {
                "id": r[0],
                "group_key": r[1],
                "tier_name": r[2],
                "role_id": str(r[3]),
                "min_coins": int(r[4] or 0),
                "sort_order": int(r[5] or 0),
            }
            for r in rows
        ]
    finally:
        conn.close()


def add_role_tier(group_key: str, role_id: str, min_coins: int, tier_name: str = None):
    group_key = group_key if group_key in ("reset", "permanent") else "reset"
    conn = get_db_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM role_tiers WHERE group_key = ?", (group_key,)
        ).fetchone()[0]
        name = tier_name or f"Tier {count + 1}"
        conn.execute("""
            INSERT INTO role_tiers (group_key, tier_name, role_id, min_coins, sort_order)
            VALUES (?, ?, ?, ?, ?)
        """, (group_key, name, str(role_id), int(min_coins), count + 1))
        conn.commit()
    finally:
        conn.close()


def delete_role_tier(tier_id: int):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM role_tiers WHERE id = ?", (tier_id,))
        conn.commit()
    finally:
        conn.close()


def get_tier_settings() -> dict:
    return {
        "enabled": get_bool_setting("role_tiers_enabled", True),
        "permanent_enabled": get_bool_setting("role_tier_permanent_enabled", False),
        "mode": get_setting("role_tiers_mode", "replace"),
        "reset_enabled": get_bool_setting("role_tier_reset_enabled", False),
        "reset_interval": int(get_setting("role_tier_reset_interval", "4") or 4),
        "reset_unit": get_setting("role_tier_reset_unit", "weeks"),
        "announce": get_bool_setting("role_tier_announce", True),
        "announce_channel_id": get_setting("role_tier_announce_channel_id", ""),
    }


def _add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    import calendar
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def tier_reset_due() -> bool:
    """True when the configured weeks/months reset interval has elapsed."""
    if not get_bool_setting("role_tier_reset_enabled", False):
        return False
    wib = pytz.timezone('Asia/Jakarta')
    now = datetime.now(wib)
    raw = get_setting("role_tier_last_reset", "")
    try:
        last = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        last = wib.localize(last)
    except Exception:
        set_setting("role_tier_last_reset", now.strftime("%Y-%m-%d %H:%M:%S"))
        return False
    try:
        interval = max(1, int(get_setting("role_tier_reset_interval", "4") or 4))
    except ValueError:
        interval = 4
    unit = get_setting("role_tier_reset_unit", "weeks")
    if unit == "months":
        next_dt = _add_months(last, interval)
    else:
        next_dt = last + timedelta(weeks=interval)
    return now >= next_dt


def mark_tier_reset_done():
    wib = pytz.timezone('Asia/Jakarta')
    set_setting("role_tier_last_reset", datetime.now(wib).strftime("%Y-%m-%d %H:%M:%S"))

def is_tiktok_user_in_db(tiktok_user: str) -> tuple:
    """
    Memeriksa apakah akun TikTok ada di database (pernah gift / tercatat).
    Mengembalikan (exists: bool, total_coins: int, display_name: str).
    """
    u_clean = tiktok_user.lower().lstrip("@")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT total_coins, display_name FROM tiktok_gifts WHERE LOWER(tiktok_user) = ?", (u_clean,)).fetchone()
        if row:
            return True, int(row[0] or 0), row[1] or u_clean

        row_log = cur.execute("""
            SELECT COALESCE(SUM(coins), 0), viewer_display_name 
            FROM gift_logs 
            WHERE LOWER(viewer_username) = ?
            GROUP BY viewer_username
        """, (u_clean,)).fetchone()
        if row_log:
            return True, int(row_log[0] or 0), row_log[1] or u_clean

        return False, 0, ""
    finally:
        conn.close()

def is_user_already_claimed(tiktok_user: str) -> tuple:
    """
    Memeriksa apakah akun TikTok sudah terikat (claimed) ke akun Discord lain.
    Mengembalikan (is_claimed: bool, discord_id: str).
    """
    u_clean = tiktok_user.lower().lstrip("@")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT discord_id FROM user_mappings WHERE LOWER(tiktok_user) = ?", (u_clean,)).fetchone()
        if row and row[0] is not None:
            return True, _normalize_discord_id(row[0])
        return False, ""
    finally:
        conn.close()

def get_user_mapping(discord_id) -> str:
    """Mengambil username TikTok yang terikat ke akun Discord."""
    did = _normalize_discord_id(discord_id)
    if not did:
        return None
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (did,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()

def bind_user_mapping(discord_id, tiktok_user: str) -> bool:
    """
    Mengikat (binding) akun TikTok dengan Discord user ID.
    Menghapus verifikasi pending jika ada.
    """
    did = _normalize_discord_id(discord_id)
    if not did:
        return False
    u_clean = tiktok_user.lower().lstrip("@")
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO user_mappings (discord_id, tiktok_user)
            VALUES (?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET tiktok_user = excluded.tiktok_user
        """, (did, u_clean))
        conn.execute("DELETE FROM pending_verifications WHERE discord_id = ?", (did,))
        conn.commit()
        invalidate_discord_mapping_cache()
        return True
    except Exception as e:
        print(f"[DB Error] Failed to bind user mapping: {e}")
        return False
    finally:
        conn.close()

def save_verification_status(discord_id, discord_name: str = "", discord_handle: str = "",
                             guild_name: str = "", has_role=None, role_name: str = "",
                             error: str = "") -> bool:
    """Menyimpan hasil verifikasi ulang role Discord untuk seorang member.

    ``has_role`` = True (punya role), False (tidak punya), atau None (tidak bisa dicek).
    """
    did = _normalize_discord_id(discord_id)
    if not did:
        return False
    wib = pytz.timezone('Asia/Jakarta')
    checked_at = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    if has_role is None:
        has_role_val = None
    else:
        has_role_val = 1 if has_role else 0
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO discord_verification_status
                (discord_id, discord_name, discord_handle, guild_name, has_role, role_name, checked_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                discord_name = excluded.discord_name,
                discord_handle = excluded.discord_handle,
                guild_name = excluded.guild_name,
                has_role = excluded.has_role,
                role_name = excluded.role_name,
                checked_at = excluded.checked_at,
                error = excluded.error
        """, (
            did, discord_name or "", discord_handle or "", guild_name or "",
            has_role_val, role_name or "", checked_at, error or "",
        ))
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB Error] Failed to save verification status for {discord_id}: {e}")
        return False
    finally:
        conn.close()


def record_verification_method(discord_id, method: str, detail: str = "",
                               verified_at: str = None) -> bool:
    """Catat bagaimana seorang member memperoleh role verified.

    ``method``: coin_only | bio | live_code | live_greeting | manual
    ``detail``: koin saat grant, kode bio, atau pesan live yang terdeteksi.
    Baris lain (has_role, checked_at, dst.) tidak diubah.
    """
    did = _normalize_discord_id(discord_id)
    if not did:
        return False
    if not verified_at:
        wib = pytz.timezone('Asia/Jakarta')
        verified_at = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO discord_verification_status
                (discord_id, method, detail, verified_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                method = excluded.method,
                detail = excluded.detail,
                verified_at = excluded.verified_at
        """, (did, method or "", detail or "", verified_at))
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB Error] Failed to record verification method for {discord_id}: {e}")
        return False
    finally:
        conn.close()


def get_all_verification_status() -> dict:
    """Mengambil status verifikasi role terakhir, dipetakan per discord_id (string)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT discord_id, discord_name, discord_handle, guild_name,
                   has_role, role_name, checked_at, error, method, detail, verified_at
            FROM discord_verification_status
        """).fetchall()
        out = {}
        for r in rows:
            key = _normalize_discord_id(r[0])
            if key:
                out[key] = r
        return out
    except Exception as e:
        print(f"[DB Error] Failed to load verification status: {e}")
        return {}
    finally:
        conn.close()


def delete_verification_status(discord_id):
    did = _normalize_discord_id(discord_id)
    if not did:
        return
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM discord_verification_status WHERE discord_id = ?", (did,))
        conn.commit()
    finally:
        conn.close()


def create_pending_verification(discord_id, tiktok_user: str, method: str = 'bio') -> str:
    """
    Membuat kode verifikasi acak unik (contoh: DM-8392) dan menyimpannya di DB (berlaku 15 menit).
    """
    did = _normalize_discord_id(discord_id)
    if not did:
        raise ValueError("discord_id tidak valid")
    u_clean = tiktok_user.lower().lstrip("@")
    rand_num = random.randint(1000, 9999)
    code = f"DM-{rand_num}"
    
    wib = pytz.timezone('Asia/Jakarta')
    now = datetime.now(wib)
    expires = now + timedelta(minutes=15)
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    expires_str = expires.strftime('%Y-%m-%d %H:%M:%S')

    def _store(conn):
        conn.execute("""
            INSERT INTO pending_verifications (discord_id, tiktok_user, code, method, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                tiktok_user = excluded.tiktok_user,
                code = excluded.code,
                method = excluded.method,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
        """, (did, u_clean, code, method, now_str, expires_str))

    # Turso can roll back an idle stream ("SQLITE_BUSY ... idle for too long")
    # during a CPU stall. Retry on a fresh connection instead of failing the
    # whole verification with a generic error.
    write_transaction(_store)
    return code

def get_pending_verification(discord_id) -> dict:
    """Mengambil status verifikasi pending untuk Discord ID tertentu."""
    did = _normalize_discord_id(discord_id)
    if not did:
        return None
    wib = pytz.timezone('Asia/Jakarta')
    now_str = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        row = conn.execute("""
            SELECT discord_id, tiktok_user, code, method, created_at, expires_at
            FROM pending_verifications
            WHERE discord_id = ? AND expires_at >= ?
        """, (did, now_str)).fetchone()
        if row:
            return {
                "discord_id": _normalize_discord_id(row[0]),
                "tiktok_user": row[1],
                "code": row[2],
                "method": row[3],
                "created_at": row[4],
                "expires_at": row[5]
            }
        return None
    finally:
        conn.close()


def get_pending_live_usernames() -> list:
    """Return ``(tiktok_user, expires_at)`` pairs for live pending requests.

    Used at startup so the in-memory guard in ``main.py`` still knows about
    requests that survived a restart/redeploy.
    """
    wib = pytz.timezone('Asia/Jakarta')
    now_str = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT LOWER(tiktok_user), expires_at FROM pending_verifications
            WHERE method IN ('live_greeting', 'live_code') AND expires_at >= ?
            """,
            (now_str,),
        ).fetchall()
    finally:
        conn.close()
    return [(row[0], row[1]) for row in rows if row and row[0]]


def find_pending_live_verification(tiktok_user: str, comment_text: str) -> dict:
    """
    Mencari apakah ada verifikasi live pending untuk username ini yang kodenya terkandung di comment_text.
    """
    u_clean = tiktok_user.lower().lstrip("@")
    wib = pytz.timezone('Asia/Jakarta')
    now_str = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        rows = conn.execute("""
            SELECT discord_id, tiktok_user, code, method
            FROM pending_verifications
            WHERE LOWER(tiktok_user) = ? AND expires_at >= ?
        """, (u_clean, now_str)).fetchall()

        for r in rows:
            code = r[2]
            if code and code.lower() in comment_text.lower():
                return {
                    "discord_id": _normalize_discord_id(r[0]),
                    "tiktok_user": r[1],
                    "code": r[2],
                    "method": r[3]
                }
        return None
    finally:
        conn.close()

def find_pending_live_greeting(tiktok_user: str) -> dict:
    """Mencari verifikasi live pending berbasis sapaan (welcome message) untuk username ini."""
    u_clean = tiktok_user.lower().lstrip("@")
    wib = pytz.timezone('Asia/Jakarta')
    now_str = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        row = conn.execute("""
            SELECT discord_id, tiktok_user, code, method
            FROM pending_verifications
            WHERE LOWER(tiktok_user) = ? AND method = 'live_greeting' AND expires_at >= ?
            ORDER BY created_at ASC
            LIMIT 1
        """, (u_clean, now_str)).fetchone()
        if row:
            return {
                "discord_id": _normalize_discord_id(row[0]),
                "tiktok_user": row[1],
                "code": row[2],
                "method": row[3]
            }
        return None
    finally:
        conn.close()

def cancel_pending_verification(discord_id):
    """Membatalkan verifikasi yang sedang pending."""
    did = _normalize_discord_id(discord_id)
    if not did:
        return
    def _delete(conn):
        conn.execute("DELETE FROM pending_verifications WHERE discord_id = ?", (did,))
    try:
        write_transaction(_delete)
    except Exception as e:
        if is_transient_db_error(e):
            # Best-effort: a pending row will expire on its own.
            print(f"[DB] cancel_pending_verification skipped after transient error: {e}")
        else:
            raise


# ----------------------------- Timeline -----------------------------


# Gap tolerance for merging adjacent live sessions (bot detection glitches).
TIMELINE_MERGE_GAP_SECONDS = 600


def _wib_localize(dt_naive, wib=None):
    wib = wib or pytz.timezone("Asia/Jakarta")
    try:
        return wib.localize(dt_naive)
    except Exception:
        try:
            return dt_naive.replace(tzinfo=wib)
        except Exception:
            return dt_naive


def _parse_wib_dt(value, wib=None):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "-":
        return None
    wib = wib or pytz.timezone("Asia/Jakarta")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _wib_localize(datetime.strptime(s[:19], fmt), wib)
        except Exception:
            continue
    return None


def _normalize_timeline_host(host_name):
    h = str(host_name or "").strip()
    if h in ("", "Host", "-"):
        return "-"
    return h


def _merge_timeline_sessions(raw_sessions, tolerance_seconds=None):
    """Merge adjacent sessions for the same channel+host when the gap is small.

    Sessions are merged when next.start - prev.end <= tolerance (including
    overlaps). Durations and coins are summed; the merged span runs from the
    earliest start to the latest end. View-time only (DB untouched).
    """
    tol = TIMELINE_MERGE_GAP_SECONDS if tolerance_seconds is None else int(tolerance_seconds)
    groups = {}
    for s in raw_sessions:
        key = (str(s.get("tiktok_username") or "").lower(), str(s.get("host_key") or ""))
        groups.setdefault(key, []).append(s)

    merged = []
    for _key, items in groups.items():
        items = sorted(items, key=lambda x: (x.get("start_dt"), int(x.get("session_id") or 0)))
        cur = None
        for s in items:
            if cur is None:
                cur = {
                    "session_ids": [s["session_id"]],
                    "parts": [s],
                    "tiktok_username": s["tiktok_username"],
                    "channel_name": s.get("channel_name") or s["tiktok_username"],
                    "host_name": s.get("host_name") or "-",
                    "host_key": s.get("host_key") or "",
                    "start_dt": s["start_dt"],
                    "end_dt": s["end_dt"],
                    "duration_seconds": int(s.get("duration_seconds") or 0),
                    "total_coins": int(s.get("total_coins") or 0),
                    "is_open": bool(s.get("is_open")),
                }
                continue
            gap = (s["start_dt"] - cur["end_dt"]).total_seconds()
            if gap <= tol:
                cur["session_ids"].append(s["session_id"])
                cur["parts"].append(s)
                if s["end_dt"] > cur["end_dt"]:
                    cur["end_dt"] = s["end_dt"]
                if s["start_dt"] < cur["start_dt"]:
                    cur["start_dt"] = s["start_dt"]
                cur["duration_seconds"] += int(s.get("duration_seconds") or 0)
                cur["total_coins"] += int(s.get("total_coins") or 0)
                cur["is_open"] = bool(cur["is_open"] or s.get("is_open"))
                if (not cur["channel_name"] or cur["channel_name"] == cur["tiktok_username"]) and s.get("channel_name"):
                    cur["channel_name"] = s["channel_name"]
                if cur["host_name"] in ("", "-") and s.get("host_name") not in ("", "-", None):
                    cur["host_name"] = s["host_name"]
                    cur["host_key"] = s.get("host_key") or ""
            else:
                merged.append(cur)
                cur = {
                    "session_ids": [s["session_id"]],
                    "parts": [s],
                    "tiktok_username": s["tiktok_username"],
                    "channel_name": s.get("channel_name") or s["tiktok_username"],
                    "host_name": s.get("host_name") or "-",
                    "host_key": s.get("host_key") or "",
                    "start_dt": s["start_dt"],
                    "end_dt": s["end_dt"],
                    "duration_seconds": int(s.get("duration_seconds") or 0),
                    "total_coins": int(s.get("total_coins") or 0),
                    "is_open": bool(s.get("is_open")),
                }
        if cur is not None:
            merged.append(cur)

    merged.sort(key=lambda x: (x.get("start_dt"), str(x.get("channel_name") or ""), str(x.get("host_name") or "")))
    return merged


def _timeline_period(date_str=None, mode="day", start=None, end=None):
    """Resolve mode/date into (mode, anchor_date, period_start_date, period_end_date, wib)."""
    wib = pytz.timezone("Asia/Jakarta")
    today = datetime.now(wib).date()
    try:
        anchor = datetime.strptime(str(date_str or today.isoformat())[:10], "%Y-%m-%d").date()
    except Exception:
        anchor = today
    mode = str(mode or "day").lower()
    if mode not in ("day", "week", "month", "custom"):
        mode = "day"

    if mode == "week":
        p_start = anchor - timedelta(days=anchor.weekday())  # Monday
        p_end = p_start + timedelta(days=6)
    elif mode == "month":
        p_start = anchor.replace(day=1)
        if p_start.month == 12:
            next_month = p_start.replace(year=p_start.year + 1, month=1, day=1)
        else:
            next_month = p_start.replace(month=p_start.month + 1, day=1)
        p_end = next_month - timedelta(days=1)
    elif mode == "custom":
        try:
            p_start = datetime.strptime(str(start or anchor.isoformat())[:10], "%Y-%m-%d").date()
        except Exception:
            p_start = anchor
        try:
            p_end = datetime.strptime(str(end or anchor.isoformat())[:10], "%Y-%m-%d").date()
        except Exception:
            p_end = p_start
        if p_end < p_start:
            p_start, p_end = p_end, p_start
        # Safety cap.
        if (p_end - p_start).days > 62:
            p_end = p_start + timedelta(days=62)
    else:
        mode = "day"
        p_start = anchor
        p_end = anchor

    return mode, anchor, p_start, p_end, wib


def get_timeline_data(date_str=None, mode="day", start=None, end=None):
    """Timeline sessions + summary for day/week/month/custom periods.

    - day: sessions attributed to the day they started; axis may extend past
      24:00 if a session continues into the next day.
    - week/month/custom: sessions attributed to the period by start date.
    Adjacent same-channel+host sessions within the merge tolerance are combined.
    """
    mode, anchor, p_start, p_end, wib = _timeline_period(date_str, mode, start, end)
    now = datetime.now(wib)
    range_start_dt = _wib_localize(datetime.combine(p_start, datetime.min.time()), wib)
    range_end_dt = _wib_localize(datetime.combine(p_end + timedelta(days=1), datetime.min.time()), wib)

    fetch_start = (p_start - timedelta(days=1)).isoformat()
    fetch_end = p_end.isoformat()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT s.session_id, s.tiktok_username,
                   COALESCE(h.display_name, s.tiktok_username) AS channel_name,
                   COALESCE(NULLIF(s.scheduled_host, ''), '') AS host_name,
                   s.start_time, s.end_time, s.duration_seconds, s.paused_seconds,
                   s.total_coins
            FROM live_sessions s
            LEFT JOIN hosts h ON LOWER(h.tiktok_username) = LOWER(s.tiktok_username)
            WHERE substr(s.start_time, 1, 10) BETWEEN ? AND ?
            ORDER BY s.start_time ASC, s.session_id ASC
            """,
            (fetch_start, fetch_end),
        ).fetchall()
    finally:
        conn.close()

    raw = []
    for r in rows:
        start_dt = _parse_wib_dt(r[4], wib)
        if not start_dt:
            continue
        # Attribute by start date falling in the requested period.
        if not (p_start <= start_dt.date() <= p_end):
            continue
        is_open = r[5] in (None, "")
        end_dt = _parse_wib_dt(r[5], wib)
        if end_dt is None:
            end_dt = now
        if end_dt <= start_dt:
            continue
        duration_sec = int(r[6] or 0)
        if is_open:
            # Open sessions have no stored duration yet: derive it from the
            # live start up to now (minus paused time) so timeline bars and the
            # per-period duration summary are not under-reported as zero.
            paused = int(r[7] or 0)
            duration_sec = max(0, int((end_dt - start_dt).total_seconds()) - paused)
        host_name = _normalize_timeline_host(r[3])
        raw.append({
            "session_id": int(r[0]),
            "tiktok_username": str(r[1] or "").lower().lstrip("@"),
            "channel_name": str(r[2] or r[1] or "").strip(),
            "host_name": host_name,
            "host_key": "" if host_name == "-" else host_name.lower(),
            "start_time": str(r[4] or ""),
            "end_time": str(r[5] or "") if r[5] else None,
            "duration_seconds": duration_sec,
            "total_coins": int(r[8] or 0),
            "is_open": is_open,
            "start_dt": start_dt,
            "end_dt": end_dt,
        })

    merged = _merge_timeline_sessions(raw)

    # Determine axis range.
    axis_start = range_start_dt
    axis_end = range_end_dt
    if mode == "day":
        # Allow extension past midnight for sessions started on this day.
        min_end = range_start_dt + timedelta(days=1)
        max_end = min_end
        for s in merged:
            if s["end_dt"] > max_end:
                max_end = s["end_dt"]
        axis_end = max(min_end, max_end)
        cap = range_start_dt + timedelta(days=3)
        if axis_end > cap:
            axis_end = cap
    else:
        # For ranges, allow a session that ends after the period to extend the axis.
        for s in merged:
            if s["end_dt"] > axis_end:
                axis_end = s["end_dt"]
        cap = range_end_dt + timedelta(days=2)
        if axis_end > cap:
            axis_end = cap

    total_min = max(1.0, (axis_end - axis_start).total_seconds() / 60.0)

    sessions_out = []
    for s in merged:
        seg_start = max(s["start_dt"], axis_start)
        seg_end = min(s["end_dt"], axis_end)
        if seg_end <= seg_start:
            continue
        part_durations = [int(p.get("duration_seconds") or 0) for p in s.get("parts", [])]
        sessions_out.append({
            "session_ids": s["session_ids"],
            "session_id": s["session_ids"][0] if s["session_ids"] else None,
            "merged_count": len(s["session_ids"]),
            "tiktok_username": s["tiktok_username"],
            "channel_name": s["channel_name"],
            "host_name": s["host_name"],
            "host_key": s["host_key"],
            "start_time": s["start_dt"].strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (None if s["is_open"] else s["end_dt"].strftime("%Y-%m-%d %H:%M:%S")),
            "seg_start": seg_start.isoformat(),
            "seg_end": seg_end.isoformat(),
            "seg_start_min": round((seg_start - axis_start).total_seconds() / 60.0, 3),
            "seg_end_min": round((seg_end - axis_start).total_seconds() / 60.0, 3),
            "duration_seconds": int(s["duration_seconds"] or 0),
            "part_durations": part_durations,
            "total_coins": int(s["total_coins"] or 0),
            "is_open": bool(s["is_open"]),
        })

    # Summary over the period: duration from (merged) sessions started in period,
    # coins from gift_logs in period.
    dur_map = {}
    for s in sessions_out:
        host_disp = s["host_name"] if s["host_name"] != "-" else "-"
        account = s["tiktok_username"]
        key = (host_disp.lower() if host_disp != "-" else "-", account)
        entry = dur_map.setdefault(key, {
            "host_name": host_disp,
            "host_key": s["host_key"],
            "account": account,
            "channel_name": s["channel_name"],
            "total_duration": 0,
            "session_durations": [],
            "session_count": 0,
        })
        entry["total_duration"] += int(s.get("duration_seconds") or 0)
        parts = s.get("part_durations") or [int(s.get("duration_seconds") or 0)]
        entry["session_durations"].extend([int(x or 0) for x in parts])
        entry["session_count"] += int(s.get("merged_count") or 1)
        if entry["host_name"] == "-" and host_disp != "-":
            entry["host_name"] = host_disp
            entry["host_key"] = s["host_key"]

    coin_map = {}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute(
            """
            SELECT COALESCE(NULLIF(g.host_name, ''), NULLIF(g.scheduled_host, ''), '-') AS host_name,
                   LOWER(g.target_username) AS account,
                   COALESCE(MAX(NULLIF(h.display_name, '')), MAX(g.target_username)) AS channel_name,
                   COALESCE(SUM(g.coins), 0) AS coins
            FROM gift_logs g
            LEFT JOIN hosts h ON LOWER(h.tiktok_username) = LOWER(g.target_username)
            WHERE substr(g.log_date, 1, 10) BETWEEN ? AND ?
            GROUP BY 1, 2
            """,
            (p_start.isoformat(), p_end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    for host_name, account, channel_name, coins in rows:
        host_disp = _normalize_timeline_host(host_name)
        acct = str(account or "").lower().lstrip("@")
        key = (host_disp.lower() if host_disp != "-" else "-", acct)
        entry = coin_map.setdefault(key, {
            "host_name": host_disp,
            "host_key": "" if host_disp == "-" else host_disp.lower(),
            "account": acct,
            "channel_name": str(channel_name or acct),
            "coins": 0,
        })
        entry["coins"] += int(coins or 0)
        if entry["host_name"] == "-" and host_disp != "-":
            entry["host_name"] = host_disp
            entry["host_key"] = host_disp.lower()
        if (not entry["channel_name"] or entry["channel_name"] == acct) and channel_name:
            entry["channel_name"] = str(channel_name)

    keys = set(dur_map.keys()) | set(coin_map.keys())
    summary = []
    for key in keys:
        d = dur_map.get(key, {})
        c = coin_map.get(key, {})
        host_name = d.get("host_name") or c.get("host_name") or "-"
        account = d.get("account") or c.get("account") or ""
        channel_name = d.get("channel_name") or c.get("channel_name") or account
        total_duration = int(d.get("total_duration") or 0)
        coins = int(c.get("coins") or 0)
        session_durations = list(d.get("session_durations") or [])
        session_count = int(d.get("session_count") or len(session_durations))
        if total_duration <= 0 and coins <= 0 and session_count <= 0:
            continue
        summary.append({
            "host_name": host_name,
            "host_key": "" if host_name == "-" else host_name.lower(),
            "account": account,
            "channel_name": channel_name,
            "total_duration": total_duration,
            "session_durations": session_durations,
            "session_count": session_count,
            "total_coins": coins,
        })
    summary.sort(key=lambda x: ((x["host_name"] or "").lower(), (x["channel_name"] or "").lower(), x["account"]))

    now_min = (now - axis_start).total_seconds() / 60.0
    return {
        "mode": mode,
        "date": anchor.isoformat(),
        "start": p_start.isoformat(),
        "end": p_end.isoformat(),
        "today": now.date().isoformat(),
        "now_iso": now.isoformat(),
        "range_start": axis_start.isoformat(),
        "range_end": axis_end.isoformat(),
        "range_total_min": round(total_min, 3),
        "now_min": round(now_min, 3),
        "merge_gap_seconds": TIMELINE_MERGE_GAP_SECONDS,
        "sessions": sessions_out,
        "summary": summary,
    }


# Backward-compatible wrappers (older route shape).
def get_timeline_sessions_for_date(date_str: str) -> list:
    return get_timeline_data(date_str, mode="day").get("sessions", [])


def get_timeline_daily_summary(date_str: str) -> list:
    return get_timeline_data(date_str, mode="day").get("summary", [])

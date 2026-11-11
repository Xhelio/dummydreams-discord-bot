import os
import json
import re
import time
try:
    import libsql_experimental as libsql
    HAS_LIBSQL = True
except ImportError:
    import sqlite3 as libsql
    HAS_LIBSQL = False

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

def get_db_connection():
    if HAS_LIBSQL and TURSO_URL:
        return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    import sqlite3
    db_file = os.environ.get("LOCAL_DB_PATH", "database.db")
    return sqlite3.connect(db_file)
from datetime import datetime, timedelta
import pytz

_DB_HEALTH_CACHE = {"ok": True, "at": 0.0}


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

# Hanya host asli yang digunakan untuk deteksi host.
VALID_TARGET_ACCOUNTS = [
]

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

def sync_valid_hosts(conn=None):
    """Ensure the 6 official target accounts exist without deleting custom hosts."""
    own = conn is None
    if own:
        conn = get_db_connection()
    try:
        cur = conn.cursor()
        for host_id, uname, dname in VALID_TARGET_ACCOUNTS:
            row = cur.execute(
                "SELECT host_id FROM hosts WHERE LOWER(tiktok_username) = ?",
                (uname,),
            ).fetchone()
            if row:
                cur.execute(
                    "UPDATE hosts SET display_name = ?, is_active = 1 WHERE host_id = ?",
                    (dname, row[0]),
                )
            else:
                try:
                    cur.execute("""
                        INSERT INTO hosts (host_id, tiktok_username, display_name, is_active)
                        VALUES (?, ?, ?, 1)
                    """, (host_id, uname, dname))
                except Exception:
                    # The preferred ID is already used by a custom host.
                    cur.execute("""
                        INSERT INTO hosts (tiktok_username, display_name, is_active)
                        VALUES (?, ?, 1)
                    """, (uname, dname))
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
                COALESCE(u.total_coins, 0) as current_total_coins
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
    conn = get_db_connection()
    try:
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
            conn.commit()
            return True
        except Exception as e:
            # Compatibility fallback if a legacy single-channel PK schema remains.
            err = str(e).lower()
            if "conflict" in err or "constraint" in err or "on conflict" in err:
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
                conn.commit()
                print(f"[DB] Announcement save used legacy schema fallback for @{u_clean}: {e}")
                return True
            raise
    finally:
        conn.close()


def update_active_live_announcement_host(tiktok_username: str, host_name: str):
    """Update the host name on every announcement session for an account."""
    u_clean = tiktok_username.lower().lstrip("@")
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE active_live_announcements SET host_name = ? WHERE tiktok_username = ?",
            (host_name, u_clean),
        )
        conn.commit()
    finally:
        conn.close()


def delete_active_live_announcement(tiktok_username: str, channel_id=None):
    """Delete announcement session(s) for an account, optionally one channel only."""
    u_clean = tiktok_username.lower().lstrip("@")
    conn = get_db_connection()
    try:
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
        conn.commit()
    finally:
        conn.close()


def close_all_open_live_sessions():
    """Close live sessions left open by a previous process (stale 'Live' status)."""
    wib = pytz.timezone('Asia/Jakarta')
    now_str = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        conn.execute("""
            UPDATE live_sessions
            SET end_time = ?, duration_seconds = CASE
                WHEN start_time IS NULL THEN 0
                ELSE CAST((julianday(?) - julianday(start_time)) * 86400 AS INTEGER)
            END
            WHERE end_time IS NULL
        """, (now_str, now_str))
        conn.commit()
    finally:
        conn.close()


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

def init_db():
    """Initialize schema and seed defaults.

    Uses short commit checkpoints because Turso/LibSQL Hrana connections can
    roll back an interactive transaction if the stream is idle for too long.
    """
    conn = get_db_connection()
    try:
        for statement in [
            """
            CREATE TABLE IF NOT EXISTS tiktok_gifts (
                tiktok_user TEXT PRIMARY KEY,
                display_name TEXT,
                total_coins INTEGER DEFAULT 0
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_mappings (
                discord_id INTEGER PRIMARY KEY,
                tiktok_user TEXT UNIQUE
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
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS pending_verifications (
                discord_id INTEGER PRIMARY KEY,
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
        ]:
            conn.execute(statement)
        conn.commit()

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

        # Migrasi kolom jika tabel lama sudah ada
        for col, dtype in [
            ("log_date", "TEXT"),
            ("log_time", "TEXT"),
            ("log_datetime", "TEXT"),
            ("target_username", "TEXT"),
            ("scheduled_host", "TEXT"),
            ("msg_id", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE gift_logs ADD COLUMN {col} {dtype}")
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

        # Dedupe index: TikTok can replay/duplicate gift messages (especially
        # across reconnects), so a gift log row is unique per message id.
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_gift_logs_msg_id "
                "ON gift_logs(msg_id) WHERE msg_id IS NOT NULL"
            )
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DB Migration] gift_logs msg_id index skipped: {e}")

        # Normalize viewer names to lowercase so the same viewer never gets two
        # separate coin rows ("Alice" vs "alice"). Idempotent & safe.
        try:
            mixed = conn.execute(
                "SELECT tiktok_user FROM tiktok_gifts WHERE tiktok_user != LOWER(tiktok_user)"
            ).fetchall()
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
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DB Migration] tiktok_gifts case normalization skipped: {e}")

        try:
            mixed_daily = conn.execute(
                "SELECT tiktok_user, date FROM daily_tiktok_gifts "
                "WHERE tiktok_user != LOWER(tiktok_user)"
            ).fetchall()
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
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[DB Migration] daily_tiktok_gifts case normalization skipped: {e}")

        # Migrasi kolom host_name pada sesi pengumuman live (database lama)
        try:
            conn.execute("ALTER TABLE active_live_announcements ADD COLUMN host_name TEXT")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

        # Host priority (1 = normal/prioritized connect, 0 = non-prioritized)
        try:
            conn.execute("ALTER TABLE hosts ADD COLUMN is_priority INTEGER DEFAULT 1")
            conn.commit()
            conn.execute("UPDATE hosts SET is_priority = 1 WHERE is_priority IS NULL")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

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
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        conn.commit()

        # Seed aliases once so existing welcome-message detection keeps working.
        alias_count = conn.execute("SELECT COUNT(*) FROM host_aliases").fetchone()[0]
        if not alias_count:
            for alias, host_name in HOST_ALIASES.items():
                conn.execute(
                    "INSERT OR IGNORE INTO host_aliases (alias, host_name) VALUES (?, ?)",
                    (alias, host_name),
                )
            conn.commit()

        # Repair stale/incorrect aliases once (e.g. an "angel" alias pointing
        # to another host). This is safe: it only fills missing canonical
        # aliases and corrects aliases whose name is itself a real host name.
        try:
            repair_host_aliases(conn)
        except Exception as e:
            print(f"[DB Migration] Host alias repair failed: {e}")

        # Seed role tiers from the legacy JSON config only once.
        seeded = conn.execute("SELECT value FROM bot_settings WHERE key = 'tiers_seeded'").fetchone()
        tier_count = conn.execute("SELECT COUNT(*) FROM role_tiers").fetchone()[0]
        if not seeded and not tier_count:
            try:
                if os.path.exists("config/roles.json"):
                    with open("config/roles.json", "r") as f:
                        cfg = json.load(f)
                    guild_tiers = cfg.get("role_tiers", {})
                    first = next(iter(guild_tiers.values()), [])
                    for idx, item in enumerate(first, start=1):
                        min_coins = item.get("min_coins") if isinstance(item, dict) else item[0]
                        role_id = item.get("role_id") if isinstance(item, dict) else item[1]
                        conn.execute("""
                            INSERT INTO role_tiers
                                (group_key, tier_name, role_id, min_coins, sort_order)
                            VALUES ('reset', ?, ?, ?, ?)
                        """, (f"Tier {idx}", str(role_id), int(min_coins), idx))
            except Exception as e:
                print(f"[DB Seed] Role tier seed skipped: {e}")
            conn.execute(
                "INSERT OR IGNORE INTO bot_settings (key, value) VALUES ('tiers_seeded', '1')"
            )
            conn.commit()

        # Sinkronisasi 6 akun target resmi dengan ID 1-6 (reusing this connection)
        sync_valid_hosts(conn)
        conn.commit()
    finally:
        conn.close()

def log_activity_event(event_type: str, message: str, tiktok_username: str = ""):
    """Mencatat aktivitas ke tabel activity_logs (dan selalu ke console)."""
    event_type = str(event_type or "info")
    message = str(message or "")
    user_clean = (tiktok_username or "").lower().lstrip("@")

    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')

    # Always emit to console so logs remain visible in Render even if DB insert fails.
    print(f"[ActivityLog] [{event_type}] @{user_clean or '-'} {message}")

    conn = None
    try:
        conn = get_db_connection()
        conn.execute("""
            INSERT INTO activity_logs (event_type, tiktok_username, message, created_at)
            VALUES (?, ?, ?, ?)
        """, (event_type, user_clean, message, now_str))
        conn.commit()
    except Exception as e:
        print(f"[ActivityLog Error] Failed to persist event ({event_type} @{user_clean}): {e}")
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

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

def get_activity_logs(limit: int = 150, channel_filter: str = "") -> list:
    """Mengambil riwayat log aktivitas terbaru."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if channel_filter:
            rows = cur.execute("""
                SELECT log_id, event_type, tiktok_username, message, created_at 
                FROM activity_logs 
                WHERE LOWER(tiktok_username) = ? 
                ORDER BY log_id DESC LIMIT ?
            """, (channel_filter.lower().lstrip("@"), limit)).fetchall()
        else:
            rows = cur.execute("""
                SELECT log_id, event_type, tiktok_username, message, created_at 
                FROM activity_logs 
                ORDER BY log_id DESC LIMIT ?
            """, (limit,)).fetchall()
        return rows
    finally:
        conn.close()

def clear_activity_logs():
    conn = get_db_connection()
    try:
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


def repair_host_aliases(conn=None):
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
        marker = cur.execute(
            "SELECT value FROM bot_settings WHERE key = 'host_aliases_repair_v1'"
        ).fetchone()
        if marker and str(marker[0]) == "1":
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

_HOST_ALIAS_CACHE = {"loaded_at": 0, "items": None}


def invalidate_host_alias_cache():
    _HOST_ALIAS_CACHE["loaded_at"] = 0
    _HOST_ALIAS_CACHE["items"] = None


def get_host_aliases() -> list:
    """Return all aliases as (alias, host_name), cached briefly for live chat speed."""
    now = time.time()
    if _HOST_ALIAS_CACHE["items"] is None or now - _HOST_ALIAS_CACHE["loaded_at"] > 30:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT alias, host_name FROM host_aliases ORDER BY LENGTH(alias) DESC"
            ).fetchall()
            items = [(r[0], r[1]) for r in rows if r[0] and r[1]]
        except Exception:
            items = list(HOST_ALIASES.items())
        finally:
            conn.close()
        _HOST_ALIAS_CACHE["items"] = items
        _HOST_ALIAS_CACHE["loaded_at"] = now
    return list(_HOST_ALIAS_CACHE["items"])


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
    finally:
        conn.close()


def detect_host_match_from_welcome_message(text: str):
    """
    Deteksi host dari teks Live Intro/welcome.

    Returns:
        (host_name, matched_alias) jika ada alias cocok di database host_aliases,
        atau (None, None) jika tidak ada alias yang cocok.
    """
    if not text:
        return None, None
    for kw, host_name in get_host_aliases():
        # Match alias as a token, allowing common possessive suffixes ('s, ’s, `s, s)
        # so "luna's" / "angel's" is detected as "luna" / "angel".
        pattern = rf"(?<![A-Za-z0-9]){re.escape(kw)}(?:['\u2019`s]+s|s)?(?![A-Za-z0-9])"
        if re.search(pattern, text, re.IGNORECASE):
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

def get_current_host_for_account(tiktok_username: str) -> str:
    """Mengambil nama host live yang sedang/terakhir terdeteksi untuk akun TikTok ini.

    Jangan fallback ke hosts.display_name karena itu nama channel/akun, bukan host live terdeteksi.
    """
    u_clean = tiktok_username.lower().lstrip("@")
    val = get_setting(f"current_host_{u_clean}")
    if val and val != "Host":
        return val

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("""
            SELECT scheduled_host FROM live_sessions 
            WHERE tiktok_username = ? AND end_time IS NULL 
            ORDER BY session_id DESC LIMIT 1
        """, (u_clean,)).fetchone()
        if row and row[0] and row[0] != "Host":
            return row[0]
    finally:
        conn.close()
    return "Host"

def set_current_host_for_account(tiktok_username: str, host_name: str):
    """Menyimpan nama host yang terdeteksi untuk akun TikTok ini."""
    u_clean = tiktok_username.lower().lstrip("@")
    set_setting(f"current_host_{u_clean}", host_name)

def update_live_session_host(session_id: int, host_name: str):
    """Memperbarui nama host di live_sessions saat terdeteksi dari welcome message."""
    if not session_id:
        return
    conn = get_db_connection()
    try:
        conn.execute("UPDATE live_sessions SET scheduled_host = ? WHERE session_id = ?", (host_name, session_id))
        conn.commit()
    finally:
        conn.close()

def get_scheduled_host_name(tiktok_username: str, dt_obj: datetime = None) -> str:
    """Mengambil nama host akun TikTok (berdasarkan welcome message yang terdeteksi)."""
    return get_current_host_for_account(tiktok_username)

def start_live_session(tiktok_username: str, initial_host: str = None) -> int:
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    user_clean = tiktok_username.lower().lstrip("@")
    explicit_host = initial_host is not None
    current_host = initial_host if explicit_host else get_current_host_for_account(user_clean)

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        active_row = cur.execute("""
            SELECT session_id FROM live_sessions 
            WHERE tiktok_username = ? AND end_time IS NULL
            ORDER BY session_id DESC LIMIT 1
        """, (user_clean,)).fetchone()

        if active_row:
            if explicit_host:
                cur.execute("UPDATE live_sessions SET scheduled_host = ? WHERE session_id = ?", (current_host, active_row[0]))
                conn.commit()
            return active_row[0]

        recent_row = cur.execute("""
            SELECT session_id FROM live_sessions 
            WHERE tiktok_username = ? AND end_time IS NOT NULL 
            AND datetime(end_time) >= datetime('now', '-1 hour')
            ORDER BY session_id DESC LIMIT 1
        """, (user_clean,)).fetchone()

        if recent_row:
            recent_id = recent_row[0]
            cur.execute("""
                UPDATE live_sessions 
                SET end_time = NULL, duration_seconds = 0, scheduled_host = ? 
                WHERE session_id = ?
            """, (current_host, recent_id))
            conn.commit()
            return recent_id

        cur.execute("""
            INSERT INTO live_sessions (tiktok_username, start_time, scheduled_host) 
            VALUES (?, ?, ?)
        """, (user_clean, now_str, current_host))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

def end_live_session(session_id: int):
    wib = pytz.timezone('Asia/Jakarta')
    now = datetime.now(wib)
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT start_time FROM live_sessions WHERE session_id = ?", (session_id,)).fetchone()
        duration_sec = 0
        if row and row[0]:
            start_dt_naive = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
            start_dt = wib.localize(start_dt_naive)
            duration_sec = int((now - start_dt).total_seconds())

        cur.execute("""
            UPDATE live_sessions 
            SET end_time = ?, duration_seconds = ?
            WHERE session_id = ?
        """, (now_str, duration_sec, session_id))
        conn.commit()
    finally:
        conn.close()

def log_gift_transaction(session_id: int, target_user: str, viewer_user: str, viewer_display: str, gift_name: str, gift_count: int, coins: int, msg_id: str = None) -> bool:
    """Mencatat log gift secara terperinci sesuai waktu WIB & host terjadwal.

    Returns True when a new row was stored, False when the same TikTok message
    id was already recorded (duplicate/replayed event) or the insert failed.
    """
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    
    log_date = now_dt.strftime('%Y-%m-%d')
    log_time = now_dt.strftime('%H:%M:%S')
    log_datetime = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    
    target_clean = target_user.lower()
    scheduled_host = get_scheduled_host_name(target_clean, now_dt)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        msg_id_clean = str(msg_id) if msg_id else None
        if msg_id_clean:
            already = cur.execute(
                "SELECT 1 FROM gift_logs WHERE msg_id = ? LIMIT 1", (msg_id_clean,)
            ).fetchone()
            if already:
                return False
        cur.execute("""
            INSERT OR IGNORE INTO gift_logs (
                session_id, 
                viewer_username, 
                viewer_display_name, 
                gift_name, 
                gift_count, 
                coins, 
                log_date, 
                log_time, 
                log_datetime, 
                target_username, 
                scheduled_host,
                msg_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, 
            (viewer_user or "").lower(), 
            viewer_display, 
            gift_name, 
            gift_count, 
            coins, 
            log_date, 
            log_time, 
            log_datetime, 
            target_clean, 
            scheduled_host,
            msg_id_clean
        ))
        inserted = True
        if inserted and session_id:
            cur.execute("UPDATE live_sessions SET total_coins = total_coins + ? WHERE session_id = ?", (coins, session_id))
        conn.commit()
        return inserted
    except Exception as e:
        print(f"[GiftLog] Failed to store gift log for @{viewer_user} ({gift_name}): {e}")
        return False
    finally:
        conn.close()

def save_gift_data(username: str, display_name: str, coins: int) -> int:
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO tiktok_gifts (tiktok_user, display_name, total_coins)
            VALUES (?, ?, ?)
            ON CONFLICT(tiktok_user) DO UPDATE SET
            total_coins = total_coins + excluded.total_coins,
            display_name = excluded.display_name
        """, (username.lower(), display_name, coins))
        conn.commit()
        res = conn.execute("SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (username.lower(),))
        row = res.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

def save_daily_gift_data(username: str, display_name: str, coins: int):
    wib = pytz.timezone('Asia/Jakarta')
    today_str = datetime.now(wib).strftime('%Y-%m-%d')
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO daily_tiktok_gifts (tiktok_user, display_name, coins, date)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tiktok_user, date) DO UPDATE SET
            coins = coins + excluded.coins,
            display_name = excluded.display_name
        """, (username.lower(), display_name, coins, today_str))
        conn.commit()
    finally:
        conn.close()

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

def get_discord_id_from_tiktok(tiktok_user: str):
    conn = get_db_connection()
    try:
        res = conn.execute("SELECT discord_id FROM user_mappings WHERE tiktok_user = ?", (tiktok_user.lower(),))
        row = res.fetchone()
        return row[0] if row else None
    finally:
        conn.close()

import random

def get_setting(key: str, default: str = None) -> str:
    """Mengambil nilai setting dari tabel bot_settings."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default
    finally:
        conn.close()

def set_setting(key: str, value: str):
    """Menyimpan atau memperbarui nilai setting ke tabel bot_settings."""
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO bot_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))
        conn.commit()
    finally:
        conn.close()


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
    Mengembalikan (is_claimed: bool, discord_id: int).
    """
    u_clean = tiktok_user.lower().lstrip("@")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT discord_id FROM user_mappings WHERE LOWER(tiktok_user) = ?", (u_clean,)).fetchone()
        if row:
            return True, int(row[0])
        return False, 0
    finally:
        conn.close()

def get_user_mapping(discord_id: int) -> str:
    """Mengambil username TikTok yang terikat ke akun Discord."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT tiktok_user FROM user_mappings WHERE discord_id = ?", (discord_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()

def bind_user_mapping(discord_id: int, tiktok_user: str) -> bool:
    """
    Mengikat (binding) akun TikTok dengan Discord user ID.
    Menghapus verifikasi pending jika ada.
    """
    u_clean = tiktok_user.lower().lstrip("@")
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO user_mappings (discord_id, tiktok_user)
            VALUES (?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET tiktok_user = excluded.tiktok_user
        """, (discord_id, u_clean))
        conn.execute("DELETE FROM pending_verifications WHERE discord_id = ?", (discord_id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB Error] Failed to bind user mapping: {e}")
        return False
    finally:
        conn.close()

def create_pending_verification(discord_id: int, tiktok_user: str, method: str = 'bio') -> str:
    """
    Membuat kode verifikasi acak unik (contoh: DM-8392) dan menyimpannya di DB (berlaku 15 menit).
    """
    u_clean = tiktok_user.lower().lstrip("@")
    rand_num = random.randint(1000, 9999)
    code = f"DM-{rand_num}"
    
    wib = pytz.timezone('Asia/Jakarta')
    now = datetime.now(wib)
    expires = now + timedelta(minutes=15)
    now_str = now.strftime('%Y-%m-%d %H:%M:%S')
    expires_str = expires.strftime('%Y-%m-%d %H:%M:%S')

    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO pending_verifications (discord_id, tiktok_user, code, method, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                tiktok_user = excluded.tiktok_user,
                code = excluded.code,
                method = excluded.method,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
        """, (discord_id, u_clean, code, method, now_str, expires_str))
        conn.commit()
        return code
    finally:
        conn.close()

def get_pending_verification(discord_id: int) -> dict:
    """Mengambil status verifikasi pending untuk Discord ID tertentu."""
    wib = pytz.timezone('Asia/Jakarta')
    now_str = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        conn.execute("DELETE FROM pending_verifications WHERE expires_at < ?", (now_str,))
        conn.commit()

        row = cur.execute("""
            SELECT discord_id, tiktok_user, code, method, created_at, expires_at 
            FROM pending_verifications 
            WHERE discord_id = ?
        """, (discord_id,)).fetchone()
        if row:
            return {
                "discord_id": row[0],
                "tiktok_user": row[1],
                "code": row[2],
                "method": row[3],
                "created_at": row[4],
                "expires_at": row[5]
            }
        return None
    finally:
        conn.close()

def find_pending_live_verification(tiktok_user: str, comment_text: str) -> dict:
    """
    Mencari apakah ada verifikasi live pending untuk username ini yang kodenya terkandung di comment_text.
    """
    u_clean = tiktok_user.lower().lstrip("@")
    wib = pytz.timezone('Asia/Jakarta')
    now_str = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        conn.execute("DELETE FROM pending_verifications WHERE expires_at < ?", (now_str,))
        conn.commit()

        rows = cur.execute("""
            SELECT discord_id, tiktok_user, code, method 
            FROM pending_verifications 
            WHERE LOWER(tiktok_user) = ?
        """, (u_clean,)).fetchall()

        for r in rows:
            code = r[2]
            if code.lower() in comment_text.lower():
                return {
                    "discord_id": r[0],
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
        cur = conn.cursor()
        conn.execute("DELETE FROM pending_verifications WHERE expires_at < ?", (now_str,))
        conn.commit()

        row = cur.execute("""
            SELECT discord_id, tiktok_user, code, method
            FROM pending_verifications
            WHERE LOWER(tiktok_user) = ? AND method = 'live_greeting'
            ORDER BY created_at ASC
            LIMIT 1
        """, (u_clean,)).fetchone()
        if row:
            return {
                "discord_id": row[0],
                "tiktok_user": row[1],
                "code": row[2],
                "method": row[3]
            }
        return None
    finally:
        conn.close()

def cancel_pending_verification(discord_id: int):
    """Membatalkan verifikasi yang sedang pending."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM pending_verifications WHERE discord_id = ?", (discord_id,))
        conn.commit()
    finally:
        conn.close()

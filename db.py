import os
import json
import re
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

def check_database_health() -> bool:
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT 1")
        row = cur.fetchone()
        conn.close()
        return bool(row and row[0] == 1)
    except Exception as e:
        print(f"[HealthCheck Error] {e}")
        return False

VALID_TARGET_ACCOUNTS = [
    (1, "georgevilora", "George Vilora"),
    (2, "clarissadreams", "Jeremy Jean"),
    (3, "juliannedreams", "Julianne"),
    (4, "franklyndreams", "Frank Lynn"),
    (5, "stefannyalisha", "Steffanny Alisha"),
    (6, "dummydreamsentertaiment", "Dummy Dreams Entertaiment")
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

def sync_valid_hosts():
    """Merapikan ID (1-6) dan sinkronisasi 6 akun target resmi di database."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Bersihkan tabel hosts agar ID 1-6 dapat diisi rapi berurutan
        cur.execute("DELETE FROM hosts")
        for host_id, uname, dname in VALID_TARGET_ACCOUNTS:
            cur.execute("""
                INSERT INTO hosts (host_id, tiktok_username, display_name, is_active)
                VALUES (?, ?, ?, 1)
            """, (host_id, uname, dname))
        conn.commit()
    except Exception as e:
        print(f"[Hosts Sync Error] {e}")
    finally:
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
    return ["George Vilora", "Jeremy Jean", "Julianne", "Frank Lynn", "Steffanny Alisha", "Dummy Dreams Entertaiment", "SOLEN", "ZIZE", "YOSA", "IELYA", "YUCA", "SINTA", "COLE", "LUNA"]

def get_active_hosts() -> list:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute("SELECT tiktok_username FROM hosts WHERE is_active = 1 ORDER BY host_id ASC").fetchall()
        hosts = [r[0] for r in rows if r[0]]
        if not hosts:
            hosts = [t[1] for t in VALID_TARGET_ACCOUNTS]
        return hosts
    finally:
        conn.close()

def get_active_live_announcement(tiktok_username: str):
    """Mengambil sesi pengumuman live yang tersimpan untuk satu akun TikTok.

    Sesi ini menyimpan channel & message Discord yang sudah dikirim, sehingga
    notifikasi berikutnya cukup di-EDIT (tidak mengirim pesan baru).
    """
    u_clean = tiktok_username.lower().lstrip("@")
    conn = get_db_connection()
    try:
        row = conn.execute(
            """
            SELECT tiktok_username, channel_id, message_id, host_name, created_at
            FROM active_live_announcements
            WHERE tiktok_username = ?
            """,
            (u_clean,),
        ).fetchone()
        if not row:
            return None
        return {
            "tiktok_username": row[0],
            "channel_id": str(row[1]) if row[1] is not None else None,
            "message_id": str(row[2]) if row[2] is not None else None,
            "host_name": row[3] if len(row) > 3 else None,
            "created_at": row[4] if len(row) > 4 else None,
        }
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
        return [
            {
                "tiktok_username": r[0],
                "channel_id": str(r[1]) if r[1] is not None else None,
                "message_id": str(r[2]) if r[2] is not None else None,
                "host_name": r[3] if len(r) > 3 else None,
                "created_at": r[4] if len(r) > 4 else None,
            }
            for r in rows
        ]
    finally:
        conn.close()

def save_active_live_announcement(tiktok_username: str, channel_id, message_id, host_name: str = None):
    """Menyimpan / memperbarui sesi pengumuman live (1 pesan per akun TikTok)."""
    u_clean = tiktok_username.lower().lstrip("@")
    conn = get_db_connection()
    try:
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
    finally:
        conn.close()

def update_active_live_announcement_host(tiktok_username: str, host_name: str):
    """Memperbarui nama host pada sesi pengumuman live (mis. saat jadwal/host berubah)."""
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

def delete_active_live_announcement(tiktok_username: str):
    """Menghapus sesi pengumuman live setelah live berakhir."""
    u_clean = tiktok_username.lower().lstrip("@")
    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM active_live_announcements WHERE tiktok_username = ?",
            (u_clean,),
        )
        conn.commit()
    finally:
        conn.close()

def init_db():
    conn = get_db_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tiktok_gifts (
                tiktok_user TEXT PRIMARY KEY,
                display_name TEXT,
                total_coins INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_mappings (
                discord_id INTEGER PRIMARY KEY,
                tiktok_user TEXT UNIQUE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS active_live_announcements (
                tiktok_username TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                host_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_tiktok_gifts (
                tiktok_user TEXT,
                display_name TEXT,
                coins INTEGER DEFAULT 0,
                date TEXT,
                PRIMARY KEY (tiktok_user, date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hosts (
                host_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tiktok_username TEXT UNIQUE NOT NULL,
                display_name TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tiktok_username TEXT NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                duration_seconds INTEGER DEFAULT 0,
                total_coins INTEGER DEFAULT 0,
                scheduled_host TEXT
            )
        """)
        
        # Tabel log gift diperbarui sesuai kolom spesifik yang diminta
        conn.execute("""
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
        """)

                # Hapus tabel shift lama karena beralih ke dynamic welcome message tracking
        conn.execute("DROP TABLE IF EXISTS host_schedules")
        conn.execute("DROP TABLE IF EXISTS temp_schedules")
        conn.execute("DROP TABLE IF EXISTS host_offlines")

        

        

        # Tabel Log Activity untuk event koneksi, diskoneksi, dan gift
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                tiktok_username TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        # Tabel Verifikasi Akun TikTok Pending
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_verifications (
                discord_id INTEGER PRIMARY KEY,
                tiktok_user TEXT NOT NULL,
                code TEXT NOT NULL,
                method TEXT DEFAULT 'bio',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            )
        """)

        # Tabel Pengaturan Bot & Role
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        # Migrasi kolom jika tabel lama sudah ada
        for col, dtype in [
            ("log_date", "TEXT"),
            ("log_time", "TEXT"),
            ("log_datetime", "TEXT"),
            ("target_username", "TEXT"),
            ("scheduled_host", "TEXT")
        ]:
            try:
                conn.execute(f"ALTER TABLE gift_logs ADD COLUMN {col} {dtype}")
            except Exception:
                pass

        # Migrasi kolom host_name pada sesi pengumuman live (database lama)
        try:
            conn.execute("ALTER TABLE active_live_announcements ADD COLUMN host_name TEXT")
        except Exception:
            pass

        cur = conn.cursor()
        
        # Target Hosts Default:
        # @clarissadreams (Jeremy Jean)
        # @georgevilora (George Vilora)
        # @stefannyalisha (Steffany Alisha)
        # @julliannedreams (Julianne)
        # @dummydreamsentertaiment (Dummy Dreams Entertaiment)
        # @franklyndreams (Frank Lynn)
        # Sinkronisasi 6 akun target resmi dengan ID 1-6
        sync_valid_hosts()

        conn.commit()
    finally:
        conn.close()

def log_activity_event(event_type: str, message: str, tiktok_username: str = ""):
    """Mencatat aktivitas koneksi / diskoneksi / gift ke tabel activity_logs."""
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    user_clean = tiktok_username.lower().lstrip("@")
    
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT INTO activity_logs (event_type, tiktok_username, message, created_at)
            VALUES (?, ?, ?, ?)
        """, (event_type, user_clean, message, now_str))
        conn.commit()
    except Exception as e:
        print(f"[ActivityLog Error] Failed to log event: {e}")
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
            SELECT COALESCE(total_coins, 0) FROM tiktok_gifts 
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


HOST_ALIASES = {
    # Sinta / Mommy
    "mommy": "Sinta",
    "momy": "Sinta",
    "sinta": "Sinta",
    "shinta": "Sinta",
    # Zize
    "zize": "Zize",
    "zeze": "Zize",
    # Solen
    "solen": "Solen",
    # Yosa
    "yosa": "Yosa",
    # Ielya
    "ielya": "Ielya",
    "ilya": "Ielya",
    # Yuca
    "yuca": "Yuca",
    "yuc": "Yuca",
    # Cole
    "cole": "Cole",
    # Luna
    "luna": "Luna",
    # Target Accounts Aliases
    "george": "George Vilora",
    "vilora": "George Vilora",
    "jeremy": "Jeremy Jean",
    "jean": "Jeremy Jean",
    "clarissa": "Jeremy Jean",
    "stefanny": "Steffanny Alisha",
    "steffany": "Steffanny Alisha",
    "alisha": "Steffanny Alisha",
    "julianne": "Julianne",
    "julian": "Julianne",
    "frank": "Frank Lynn",
    "franklyn": "Frank Lynn",
    "dummy": "Dummy Dreams Entertaiment",
    "dummydreams": "Dummy Dreams Entertaiment",
}

def detect_host_from_welcome_message(text: str):
    """
    Mendeteksi nama host dari welcome message / chat / intro di TikTok.
    Case-insensitive dan membaca di sela-sela teks (misal: ZIZE's here -> Zize).
    Keyword 'Mommy' adalah alias dari Sinta.
    """
    if not text:
        return None
    for kw, host_name in HOST_ALIASES.items():
        pattern = rf"(?:^|[\s\W_]){re.escape(kw)}(?:[\x27\u2019]s|s)?(?:$|[\s\W_])"
        if re.search(pattern, text, re.IGNORECASE):
            return host_name
    return None

def get_current_host_for_account(tiktok_username: str) -> str:
    """Mengambil nama host yang sedang atau terakhir terdeteksi untuk akun TikTok ini."""
    u_clean = tiktok_username.lower().lstrip("@")
    val = get_setting(f"current_host_{u_clean}")
    if val:
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

        h_row = cur.execute("SELECT display_name FROM hosts WHERE tiktok_username = ?", (u_clean,)).fetchone()
        if h_row and h_row[0]:
            return h_row[0]
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
    current_host = initial_host or get_current_host_for_account(user_clean)

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        active_row = cur.execute("""
            SELECT session_id FROM live_sessions 
            WHERE tiktok_username = ? AND end_time IS NULL
            ORDER BY session_id DESC LIMIT 1
        """, (user_clean,)).fetchone()

        if active_row:
            if initial_host and initial_host != "Host":
                cur.execute("UPDATE live_sessions SET scheduled_host = ? WHERE session_id = ?", (initial_host, active_row[0]))
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

def log_gift_transaction(session_id: int, target_user: str, viewer_user: str, viewer_display: str, gift_name: str, gift_count: int, coins: int):
    """Mencatat log gift secara terperinci sesuai waktu WIB & host terjadwal."""
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
        cur.execute("""
            INSERT INTO gift_logs (
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
                scheduled_host
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            session_id, 
            viewer_user.lower(), 
            viewer_display, 
            gift_name, 
            gift_count, 
            coins, 
            log_date, 
            log_time, 
            log_datetime, 
            target_clean, 
            scheduled_host
        ))
        
        if session_id:
            cur.execute("UPDATE live_sessions SET total_coins = total_coins + ? WHERE session_id = ?", (coins, session_id))
        conn.commit()
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

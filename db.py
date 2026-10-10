import os
import libsql_experimental as libsql
from datetime import datetime
import pytz

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

def get_db_connection():
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)

def init_db():
    conn = get_db_connection()
    try:
        # 1. Tabel Lama (Dipertahankan)
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

        # 2. Tabel Baru untuk Fitur Tracking & Manajemen
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
                total_coins INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gift_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                tiktok_username TEXT NOT NULL,
                viewer_username TEXT NOT NULL,
                viewer_display_name TEXT,
                gift_name TEXT NOT NULL,
                gift_count INTEGER DEFAULT 1,
                coins INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS host_schedules (
                schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tiktok_username TEXT NOT NULL,
                shift_date TEXT NOT NULL,
                shift_start_time TEXT,
                shift_end_time TEXT,
                status TEXT DEFAULT 'WORK',
                PRIMARY KEY (tiktok_username, shift_date)
            )
        """)
        conn.commit()
    finally:
        conn.close()

# --- Fungsi Tracking Sesi Live & Gift ---

def start_live_session(tiktok_username: str) -> int:
    wib = pytz.timezone('Asia/Jakarta')
    now = datetime.now(wib).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO live_sessions (tiktok_username, start_time) VALUES (?, ?)",
            (tiktok_username.lower(), now)
        )
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
            start_dt = datetime.strptime(row[0], '%Y-%m-%d %H:%M:%S')
            duration_sec = int((now - start_dt).total_seconds())

        cur.execute("""
            UPDATE live_sessions 
            SET end_time = ?, duration_seconds = ?
            WHERE session_id = ?
        """, (now_str, duration_sec, session_id))
        conn.commit()
    finally:
        conn.close()

def log_gift_transaction(session_id: int, host_user: str, viewer_user: str, viewer_display: str, gift_name: str, gift_count: int, coins: int):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Log transaksi detail
        cur.execute("""
            INSERT INTO gift_logs (session_id, tiktok_username, viewer_username, viewer_display_name, gift_name, gift_count, coins)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (session_id, host_user.lower(), viewer_user.lower(), viewer_display, gift_name, gift_count, coins))
        
        # Tambah koin ke sesi live
        if session_id:
            cur.execute("""
                UPDATE live_sessions SET total_coins = total_coins + ? WHERE session_id = ?
            """, (coins, session_id))
            
        conn.commit()
    finally:
        conn.close()

# --- Helper Query Agregasi Lama ---

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

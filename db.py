import os
import json
import libsql_experimental as libsql
from datetime import datetime, timedelta
import pytz

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

def get_db_connection():
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)

def get_shift_hours(shift_num: int):
    shifts = {
        1: (1, 7),
        2: (7, 13),
        3: (13, 19),
        4: (19, 1)
    }
    return shifts.get(shift_num, (1, 7))

def get_shift_name(start_hour: int, end_hour: int) -> str:
    if start_hour == 1 and end_hour == 7:
        return "Shift 1 (01:00 - 07:00)"
    elif start_hour == 7 and end_hour == 13:
        return "Shift 2 (07:00 - 13:00)"
    elif start_hour == 13 and end_hour == 19:
        return "Shift 3 (13:00 - 19:00)"
    elif start_hour == 19 and end_hour == 1:
        return "Shift 4 (19:00 - 01:00)"
    return f"Custom ({start_hour:02d}:00 - {end_hour:02d}:00)"

def get_all_host_names() -> list:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute("SELECT DISTINCT host_name FROM host_schedules ORDER BY host_name ASC").fetchall()
        hosts = [r[0] for r in rows if r[0]]
        if not hosts:
            hosts = ["SOLEN", "ZIZE", "YOSA", "IELYA", "YUCA", "SINTA", "COLE", "LUNA"]
        return hosts
    finally:
        conn.close()

def get_active_hosts() -> list:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute("SELECT tiktok_username FROM hosts WHERE is_active = 1").fetchall()
        return [r[0] for r in rows]
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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS host_schedules (
                schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
                tiktok_username TEXT NOT NULL,
                account_name TEXT,
                host_name TEXT NOT NULL,
                start_hour INTEGER NOT NULL,
                end_hour INTEGER NOT NULL,
                UNIQUE(tiktok_username, start_hour)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS temp_schedules (
                temp_id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_date TEXT NOT NULL,
                tiktok_username TEXT NOT NULL,
                account_name TEXT,
                host_name TEXT NOT NULL,
                shift_num INTEGER NOT NULL,
                UNIQUE(schedule_date, tiktok_username, shift_num)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS host_offlines (
                offline_id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_name TEXT NOT NULL,
                offline_type TEXT NOT NULL,
                offline_date TEXT,
                offline_day TEXT,
                note TEXT
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

        cur = conn.cursor()
        
        cur.execute("SELECT COUNT(*) FROM hosts")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO hosts (tiktok_username, display_name) VALUES ('clarissadreams', 'Jeremy Jeans')")
            cur.execute("INSERT INTO hosts (tiktok_username, display_name) VALUES ('georgevilora', 'George Vilora')")

        conn.commit()
    finally:
        conn.close()

def clean_expired_schedules_and_offlines():
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    today_str = now_dt.strftime('%Y-%m-%d')
    current_hour = now_dt.hour

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM temp_schedules WHERE schedule_date < ?", (today_str,))
        
        temp_today = cur.execute("SELECT temp_id, shift_num FROM temp_schedules WHERE schedule_date = ?", (today_str,)).fetchall()
        for temp_id, shift_num in temp_today:
            start_h, end_h = get_shift_hours(shift_num)
            if start_h < end_h:
                if current_hour >= end_h:
                    cur.execute("DELETE FROM temp_schedules WHERE temp_id = ?", (temp_id,))
            else:
                if 1 <= current_hour < 19:
                    cur.execute("DELETE FROM temp_schedules WHERE temp_id = ?", (temp_id,))

        cur.execute("DELETE FROM host_offlines WHERE offline_type = 'temporary' AND offline_date < ?", (today_str,))
        cur.execute("DELETE FROM live_sessions WHERE end_time IS NULL AND total_coins = 0 AND datetime(start_time) < datetime('now', '-30 minutes')")
        
        conn.commit()
    finally:
        conn.close()

def get_scheduled_host_name(tiktok_username: str, dt_obj: datetime = None) -> str:
    clean_expired_schedules_and_offlines()

    if not dt_obj:
        wib = pytz.timezone('Asia/Jakarta')
        dt_obj = datetime.now(wib)
    
    current_hour = dt_obj.hour
    today_str = dt_obj.strftime('%Y-%m-%d')
    day_name = dt_obj.strftime('%A')
    user_clean = tiktok_username.lower()

    current_shift = 1
    if 1 <= current_hour < 7: current_shift = 1
    elif 7 <= current_hour < 13: current_shift = 2
    elif 13 <= current_hour < 19: current_shift = 3
    else: current_shift = 4

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        temp_row = cur.execute("""
            SELECT host_name FROM temp_schedules 
            WHERE tiktok_username = ? AND schedule_date = ? AND shift_num = ?
        """, (user_clean, today_str, current_shift)).fetchone()

        candidate_host = temp_row[0] if temp_row else None

        if not candidate_host:
            default_rows = cur.execute("""
                SELECT host_name, start_hour, end_hour 
                FROM host_schedules 
                WHERE tiktok_username = ?
            """, (user_clean,)).fetchall()

            for h_name, start_h, end_h in default_rows:
                if start_h < end_h:
                    if start_h <= current_hour < end_h:
                        candidate_host = h_name
                        break
                else:
                    if current_hour >= start_h or current_hour < end_h:
                        candidate_host = h_name
                        break

        if not candidate_host:
            return "Host"

        off_row = cur.execute("""
            SELECT note FROM host_offlines 
            WHERE UPPER(host_name) = UPPER(?) AND (
                (offline_type = 'temporary' AND offline_date = ?) OR
                (offline_type = 'routine' AND UPPER(offline_day) = UPPER(?))
            )
        """, (candidate_host, today_str, day_name)).fetchone()

        if off_row:
            return f"{candidate_host} (OFFLINE)"

        return candidate_host
    finally:
        conn.close()

def start_live_session(tiktok_username: str) -> int:
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    user_clean = tiktok_username.lower()
    scheduled_host = get_scheduled_host_name(user_clean, now_dt)

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        active_row = cur.execute("""
            SELECT session_id FROM live_sessions 
            WHERE tiktok_username = ? AND scheduled_host = ? AND end_time IS NULL
            ORDER BY session_id DESC LIMIT 1
        """, (user_clean, scheduled_host)).fetchone()

        if active_row:
            return active_row[0]

        recent_row = cur.execute("""
            SELECT session_id FROM live_sessions 
            WHERE tiktok_username = ? AND scheduled_host = ? AND end_time IS NOT NULL 
            AND datetime(end_time) >= datetime('now', '-1 hour')
            ORDER BY session_id DESC LIMIT 1
        """, (user_clean, scheduled_host)).fetchone()

        if recent_row:
            recent_id = recent_row[0]
            cur.execute("""
                UPDATE live_sessions 
                SET end_time = NULL, duration_seconds = 0 
                WHERE session_id = ?
            """, (recent_id,))
            conn.commit()
            return recent_id

        cur.execute("""
            INSERT INTO live_sessions (tiktok_username, start_time, scheduled_host) 
            VALUES (?, ?, ?)
        """, (user_clean, now_str, scheduled_host))
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

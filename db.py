import os
import json
import libsql_experimental as libsql
from datetime import datetime
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

        # 1. Tabel Jadwal Shift Default
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

        # 2. Tabel Jadwal Sementara
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

        # 3. Tabel Offline Host
        conn.execute("""
            CREATE TABLE IF NOT EXISTS host_offlines (
                offline_id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_name TEXT NOT NULL,
                offline_type TEXT NOT NULL, -- 'temporary' atau 'routine'
                offline_date TEXT,          -- YYYY-MM-DD jika temporary
                offline_day TEXT,           -- 'Monday', 'Tuesday', dll jika routine
                note TEXT
            )
        """)

        cur = conn.cursor()
        
        # Inisialisasi Akun TikTok Target
        cur.execute("SELECT COUNT(*) FROM hosts")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO hosts (tiktok_username, display_name) VALUES ('clarissadreams', 'Jeremy Jeans')")
            cur.execute("INSERT INTO hosts (tiktok_username, display_name) VALUES ('georgevilora', 'George Vilora')")

        # Inisialisasi Otomatis Jadwal Shift
        cur.execute("SELECT COUNT(*) FROM host_schedules")
        if cur.fetchone()[0] == 0:
            schedule_path = os.path.join("config", "schedule.json")
            if not os.path.exists(schedule_path):
                schedule_path = "schedule.json"

            loaded_from_json = False
            if os.path.exists(schedule_path):
                try:
                    with open(schedule_path, "r") as f:
                        sched_data = json.load(f)
                    for account_key, info in sched_data.items():
                        acc_name = info.get("account_name", account_key)
                        for h in info.get("hosts", []):
                            cur.execute("""
                                INSERT OR IGNORE INTO host_schedules (tiktok_username, account_name, host_name, start_hour, end_hour)
                                VALUES (?, ?, ?, ?, ?)
                            """, (account_key.lower(), acc_name, h["name"], h["start_hour"], h["end_hour"]))
                    loaded_from_json = True
                except Exception:
                    pass

            if not loaded_from_json:
                default_schedules = [
                    ("clarissadreams", "Jeremy Jeans", "SOLEN", 1, 7),
                    ("clarissadreams", "Jeremy Jeans", "ZIZE", 7, 13),
                    ("clarissadreams", "Jeremy Jeans", "YOSA", 13, 19),
                    ("clarissadreams", "Jeremy Jeans", "IELYA", 19, 1),
                    ("georgevilora", "George Vilora", "YUCA", 1, 7),
                    ("georgevilora", "George Vilora", "SINTA", 7, 13),
                    ("georgevilora", "George Vilora", "COLE", 13, 19),
                    ("georgevilora", "George Vilora", "LUNA", 19, 1),
                ]
                for username, acc_name, host_name, start_h, end_h in default_schedules:
                    cur.execute("""
                        INSERT OR IGNORE INTO host_schedules (tiktok_username, account_name, host_name, start_hour, end_hour)
                        VALUES (?, ?, ?, ?, ?)
                    """, (username, acc_name, host_name, start_h, end_h))

        conn.commit()
    finally:
        conn.close()

def clean_expired_schedules_and_offlines():
    """Menghapus otomatis jadwal sementara & offline sementara yang sudah kadaluwarsa."""
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    today_str = now_dt.strftime('%Y-%m-%d')
    current_hour = now_dt.hour

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # 1. Hapus jadwal sementara jika tanggal < hari ini
        cur.execute("DELETE FROM temp_schedules WHERE schedule_date < ?", (today_str,))
        
        # 2. Hapus jadwal sementara hari ini jika shift sudah berlalu
        temp_today = cur.execute("SELECT temp_id, shift_num FROM temp_schedules WHERE schedule_date = ?", (today_str,)).fetchall()
        for temp_id, shift_num in temp_today:
            start_h, end_h = get_shift_hours(shift_num)
            if start_h < end_h:
                if current_hour >= end_h:
                    cur.execute("DELETE FROM temp_schedules WHERE temp_id = ?", (temp_id,))
            else: # Shift malam (misal 19-01)
                if 1 <= current_hour < 19:
                    cur.execute("DELETE FROM temp_schedules WHERE temp_id = ?", (temp_id,))

        # 3. Hapus status offline sementara jika tanggal < hari ini
        cur.execute("DELETE FROM host_offlines WHERE offline_type = 'temporary' AND offline_date < ?", (today_str,))
        
        conn.commit()
    finally:
        conn.close()

def get_scheduled_host_name(tiktok_username: str, dt_obj: datetime = None) -> str:
    """Mendapatkan nama host bertugas dengan mengecek: Offline -> Temp Schedule -> Default Schedule."""
    clean_expired_schedules_and_offlines()

    if not dt_obj:
        wib = pytz.timezone('Asia/Jakarta')
        dt_obj = datetime.now(wib)
    
    current_hour = dt_obj.hour
    today_str = dt_obj.strftime('%Y-%m-%d')
    day_name = dt_obj.strftime('%A')
    user_clean = tiktok_username.lower()

    # Tentukan Shift Ke berapa saat ini
    current_shift = 1
    if 1 <= current_hour < 7: current_shift = 1
    elif 7 <= current_hour < 13: current_shift = 2
    elif 13 <= current_hour < 19: current_shift = 3
    else: current_shift = 4

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 1. Cek Jadwal Sementara (Temporary Schedule Override)
        temp_row = cur.execute("""
            SELECT host_name FROM temp_schedules 
            WHERE tiktok_username = ? AND schedule_date = ? AND shift_num = ?
        """, (user_clean, today_str, current_shift)).fetchone()

        candidate_host = temp_row[0] if temp_row else None

        # 2. Jika tidak ada jadwal sementara, ambil dari Default Schedule
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

        # 3. Cek apakah Host Candidate sedang Offline (Temporary / Routine)
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

def get_active_hosts() -> list:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        rows = cur.execute("SELECT tiktok_username FROM hosts WHERE is_active = 1").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()

def start_live_session(tiktok_username: str) -> int:
    wib = pytz.timezone('Asia/Jakarta')
    now_dt = datetime.now(wib)
    now_str = now_dt.strftime('%Y-%m-%d %H:%M:%S')
    scheduled_host = get_scheduled_host_name(tiktok_username, now_dt)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO live_sessions (tiktok_username, start_time, scheduled_host) 
            VALUES (?, ?, ?)
        """, (tiktok_username.lower(), now_str, scheduled_host))
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

def log_gift_transaction(session_id: int, host_user: str, viewer_user: str, viewer_display: str, gift_name: str, gift_count: int, coins: int):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO gift_logs (session_id, tiktok_username, viewer_username, viewer_display_name, gift_name, gift_count, coins)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (session_id, host_user.lower(), viewer_user.lower(), viewer_display, gift_name, gift_count, coins))
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

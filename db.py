import os
import libsql_experimental as libsql
from datetime import datetime
import pytz

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

def get_db_connection():
    # Perbaikan: gunakan url=TURSO_URL, bukan database=TURSO_URL
    return libsql.connect(url=TURSO_URL, auth_token=TURSO_TOKEN)

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
            CREATE TABLE IF NOT EXISTS active_summary_reports (
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

        result = conn.execute(
            "SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = ?",
            (username.lower(),)
        )
        row = result.fetchone()
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
        result = conn.execute(
            "SELECT discord_id FROM user_mappings WHERE tiktok_user = ?",
            (tiktok_user.lower(),)
        )
        row = result.fetchone()
        return row[0] if row else None
    finally:
        conn.close()

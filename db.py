import os
import libsql_experimental as libsql

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

def get_db_connection():
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tiktok_gifts (
            tiktok_user TEXT PRIMARY KEY,
            total_coins INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_mappings (
            discord_id INTEGER PRIMARY KEY,
            tiktok_user TEXT UNIQUE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS active_live_announcements (
            tiktok_username TEXT PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

def save_gift_data(username: str, coins: int) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO tiktok_gifts (tiktok_user, total_coins)
        VALUES (?, ?)
        ON CONFLICT(tiktok_user) DO UPDATE SET
        total_coins = total_coins + excluded.total_coins
    """, (username.lower(), coins))
    conn.commit()

    cursor.execute("SELECT total_coins FROM tiktok_gifts WHERE tiktok_user = ?", (username.lower(),))
    total = cursor.fetchone()[0]
    return total

def get_discord_id_from_tiktok(tiktok_user: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT discord_id FROM user_mappings WHERE tiktok_user = ?", (tiktok_user.lower(),))
    row = cursor.fetchone()
    return row[0] if row else None
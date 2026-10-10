import json
import os
from db import get_db_connection, get_scheduled_host_name

def get_schedule_info(tiktok_username: str):
    u_clean = tiktok_username.lower().lstrip("@")
    host_name = get_scheduled_host_name(u_clean)
    
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        row = cur.execute("SELECT display_name FROM hosts WHERE tiktok_username = ?", (u_clean,)).fetchone()
        account_name = row[0] if row and row[0] else u_clean
        return host_name, account_name
    finally:
        conn.close()

def load_role_config():
    if os.path.exists("config/roles.json"):
        try:
            with open("config/roles.json", "r") as f:
                data = json.load(f)
                return data.get("role_tiers", {}), data.get("stack_roles", False)
        except Exception:
            pass
    return {}, False

def load_summary_config():
    if os.path.exists("config/config.json"):
        try:
            with open("config/config.json", "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

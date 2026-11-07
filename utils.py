import json
import os
from db import get_db_connection, get_current_host_for_account, get_channel_display_name

def get_schedule_info(tiktok_username: str):
    """Mengembalikan nama host (berbasis welcome message) dan nama channel resmi."""
    u_clean = tiktok_username.lower().lstrip("@")
    host_name = get_current_host_for_account(u_clean)
    account_name = get_channel_display_name(u_clean)
    return host_name, account_name

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

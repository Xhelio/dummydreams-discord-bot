import json
from datetime import datetime
import pytz

def load_config() -> dict:
    try:
        with open("config/config.json", "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Config Error] Failed to read config.json: {e}")
        return {"summary_channel_id": 0, "overwrite_summary": True}

def get_schedule_info(username: str = "georgevilora") -> tuple[str, str]:
    """
    Returns tuple: (host_name, account_name) berdasarkan username akun TikTok
    """
    user_key = username.lower()
    default_host = "Host"
    account_name = "TikTok Live"

    try:
        with open("config/schedule.json", "r") as f:
            data = json.load(f)

            # Ambil data spesifik berdasarkan username akun
            account_data = data.get(user_key, {})
            hosts = account_data.get("hosts", [])
            account_name = account_data.get("account_name", "TikTok Live")
            default_host = account_data.get("default_name", "Host")
    except Exception as e:
        print(f"[Schedule Error] Failed to read schedule.json: {e}")
        return default_host, account_name

    wib = pytz.timezone('Asia/Jakarta')
    current_hour = datetime.now(wib).hour

    for host in hosts:
        name = host.get("name")
        start = host.get("start_hour")
        end = host.get("end_hour")

        if start < end:
            if start <= current_hour < end:
                return name, account_name
        else:
            if current_hour >= start or current_hour < end:
                return name, account_name

    return default_host, account_name

def load_role_config() -> tuple[dict[int, list[tuple[int, int]]], bool]:
    try:
        with open("config/roles.json", "r") as f:
            data = json.load(f)
            raw_tiers = data.get("role_tiers", {})
            stack_roles = data.get("stack_roles", False)

            role_tiers = {}
            for guild_id_str, tiers in raw_tiers.items():
                guild_id = int(guild_id_str)
                tier_list = [(t["min_coins"], int(t["role_id"])) for t in tiers]
                tier_list.sort(key=lambda x: x[0])
                role_tiers[guild_id] = tier_list

            return role_tiers, stack_roles
    except Exception as e:
        print(f"[Config Error] Failed to read roles.json: {e}")
        return {}, False

def load_summary_config() -> tuple[int, bool]:
    """
    Membaca summary_channel_id dan overwrite_summary dari config.json
    """
    config = load_config()
    return int(config.get("summary_channel_id", 0)), config.get("overwrite_summary", True)
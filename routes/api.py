from flask import Blueprint, request, jsonify
import os
import json
from db import check_database_health, get_bool_setting
from runtime_status import get_bot_status, get_live_snapshot, get_observer_diag

api_bp = Blueprint('api', __name__, url_prefix='/api')
async_announcement_handler = None

def set_announcement_handler(handler):
    global async_announcement_handler
    async_announcement_handler = handler

@api_bp.route('/health', methods=['GET'])
def health_check():
    """Health status endpoint for the website, TikTok live and Discord bot pills."""
    db_ok = check_database_health()
    bot = get_bot_status()
    live_accounts = get_live_snapshot()
    payload = {
        "status": "ok" if db_ok else "warning",
        "message": "Website Stable" if db_ok else "Website Connected (Database Reconnecting)",
        "database": "connected" if db_ok else "error",
        "website": True,
        "discord_bot": {
            "online": bool(bot.get("online")),
            "user": bot.get("user"),
            "ready_at": bot.get("ready_at"),
        },
        "tiktok_live": {
            "count": len(live_accounts),
            "accounts": live_accounts,
            "observer": get_observer_diag(),
        },
        "master_enabled": get_bool_setting("master_enabled", True),
    }
    return jsonify(payload), 200

@api_bp.route('/config', methods=['GET'])
def get_bot_config():
    secret_key = request.headers.get("X-API-KEY")
    expected_key = os.environ.get("API_SECRET_KEY")

    if expected_key and secret_key != expected_key:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    target_username = request.args.get("username", "").lower()

    try:
        with open("config/config.json", "r") as f:
            config_data = json.load(f)

        accounts = config_data.get("accounts", [])

        if target_username:
            matched_acc = next((acc for acc in accounts if acc["tiktok_username"].lower() == target_username), None)
            if matched_acc:
                return jsonify({
                    "account_name": matched_acc["account_name"],
                    "tiktok_username": matched_acc["tiktok_username"],
                    "default_name": config_data.get("default_name", "Host"),
                    "summary_channel_id": config_data.get("summary_channel_id", 0),
                    "overwrite_summary": config_data.get("overwrite_summary", True)
                }), 200
            return jsonify({"status": "error", "message": "Account not found"}), 404

        return jsonify(config_data), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@api_bp.route('/heartbeat', methods=['POST'])
def handle_heartbeat():
    secret_key = request.headers.get("X-API-KEY")
    expected_key = os.environ.get("API_SECRET_KEY")

    if expected_key and secret_key != expected_key:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    data = request.get_json() or {}
    username = data.get("username", "unknown")
    print(f"[API] Received heartbeat from worker for @{username}")
    return jsonify({"status": "ok"}), 200

@api_bp.route('/announcement', methods=['POST'])
def trigger_announcement():
    data = request.get_json() or {}
    secret_key = request.headers.get("X-API-KEY")
    expected_key = os.environ.get("API_SECRET_KEY")

    if expected_key and secret_key != expected_key:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username = data.get("username")
    is_live = data.get("is_live")

    if not username or is_live is None:
        return jsonify({"status": "error", "message": "Missing username or is_live status"}), 400

    if async_announcement_handler:
        async_announcement_handler(username, is_live)
        return jsonify({"status": "success", "message": f"Announcement triggered for @{username}"}), 200

    return jsonify({"status": "error", "message": "Bot handler not ready"}), 500

from flask import Blueprint, request, jsonify
from werkzeug.exceptions import RequestEntityTooLarge
import os
import json
import logging
import hmac
import hashlib
import time
import asyncio
import pytz
from datetime import datetime
from db import (
    check_database_health,
    get_bool_setting,
    start_live_session,
    end_live_session,
    get_open_live_session_id,
    get_current_host_for_account,
    touch_live_session,
    log_activity_event,
)
from runtime_status import (
    get_bot_status,
    get_live_snapshot,
    get_observer_diag,
    get_bot_instance,
    set_live,
    touch_live,
    clear_live,
    update_observer_diag,
)

api_bp = Blueprint('api', __name__, url_prefix='/api')
logger = logging.getLogger(__name__)
MAX_EULER_WEBHOOK_BATCH_SIZE = 100

# Bound memory and parsing work for EulerStream webhook requests, including
# chunked bodies that do not provide a trustworthy Content-Length header.
MAX_EULER_WEBHOOK_BODY_BYTES = 1024 * 1024


@api_bp.before_request
def _limit_euler_webhook_body():
    if request.path not in (
        "/api/euler-webhook",
        "/api/webhooks/eulerstream",
    ):
        return None

    request.max_content_length = MAX_EULER_WEBHOOK_BODY_BYTES
    if request.content_length is not None and request.content_length > MAX_EULER_WEBHOOK_BODY_BYTES:
        return jsonify({
            "ok": False,
            "error": "Webhook request body exceeds maximum size",
            "max_bytes": MAX_EULER_WEBHOOK_BODY_BYTES,
        }), 413

    # Force a bounded read before authentication or route logic can return
    # early. This also enforces the limit when Content-Length is unavailable.
    try:
        request.get_data(cache=True)
    except RequestEntityTooLarge:
        return jsonify({
            "ok": False,
            "error": "Webhook request body exceeds maximum size",
            "max_bytes": MAX_EULER_WEBHOOK_BODY_BYTES,
        }), 413
    return None


@api_bp.errorhandler(RequestEntityTooLarge)
def _handle_oversized_euler_webhook(error):
    return jsonify({
        "ok": False,
        "error": "Webhook request body exceeds maximum size",
        "max_bytes": MAX_EULER_WEBHOOK_BODY_BYTES,
    }), 413

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
        "signer": {
            "api_key_configured": bool(
                (os.environ.get("SIGN_API_KEY") or os.environ.get("TIKTOK_SIGN_API_KEY") or "").strip()
            ),
        },
    }
    return jsonify(payload), 200

def _api_key_valid(secret_key: str) -> bool:
    expected_key = (os.environ.get("API_SECRET_KEY") or "").strip()
    supplied_key = (secret_key or "").strip()
    if not expected_key or not supplied_key:
        return False
    return hmac.compare_digest(supplied_key, expected_key)


def _api_auth_error():
    """Fail closed when API authentication is missing or not configured."""
    if not (os.environ.get("API_SECRET_KEY") or "").strip():
        return jsonify({
            "status": "error",
            "message": "API authentication is not configured"
        }), 503
    if _api_key_valid(request.headers.get("X-API-KEY")):
        return None
    return jsonify({"status": "error", "message": "Unauthorized"}), 401


@api_bp.route('/config', methods=['GET'])
def get_bot_config():
    auth_error = _api_auth_error()
    if auth_error is not None:
        return auth_error

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
    except Exception:
        logger.exception("Failed to read API configuration")
        return jsonify({"status": "error", "message": "Unable to read configuration"}), 500

@api_bp.route('/heartbeat', methods=['POST'])
def handle_heartbeat():
    auth_error = _api_auth_error()
    if auth_error is not None:
        return auth_error

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "JSON object payload required"}), 400

    username_value = data.get("username")
    if username_value is None:
        return jsonify({"status": "error", "message": "Missing username"}), 400
    if not isinstance(username_value, str):
        return jsonify({"status": "error", "message": "username must be a string"}), 400
    username = username_value.strip().lower().lstrip("@")
    print(f"[API] Received heartbeat from worker for @{username or 'unknown'}")
    if not username:
        return jsonify({"status": "error", "message": "Missing username"}), 400
    # A webhook-created live state has no websocket heartbeat of its own: keep
    # the runtime badge and the open session alive while the worker reports in.
    try:
        touch_live(username)
        session_id = get_open_live_session_id(username)
        if session_id:
            touch_live_session(session_id)
    except Exception as e:
        print(f"[API] Heartbeat state update failed for @{username}: {e}")
    return jsonify({"status": "ok"}), 200

@api_bp.route('/announcement', methods=['POST'])
def trigger_announcement():
    auth_error = _api_auth_error()
    if auth_error is not None:
        return auth_error

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "JSON object payload required"}), 400

    username = data.get("username")
    is_live = data.get("is_live")

    if not isinstance(username, str) or not username.strip():
        return jsonify({"status": "error", "message": "username must be a non-empty string"}), 400
    if not isinstance(is_live, bool):
        return jsonify({"status": "error", "message": "is_live must be a boolean"}), 400
    username = username.strip().lstrip("@").lower()
    if not username:
        return jsonify({"status": "error", "message": "username must be a non-empty string"}), 400

    if async_announcement_handler:
        async_announcement_handler(username, is_live)
        return jsonify({"status": "success", "message": f"Announcement triggered for @{username}"}), 200

    return jsonify({"status": "error", "message": "Bot handler not ready"}), 500


def _euler_webhook_secret() -> str:
    return (
        os.environ.get("EULER_WEBHOOK_SECRET")
        or os.environ.get("EULER_WEBHOOK_SIGNING_SECRET")
        or ""
    ).strip()


def _verify_euler_webhook_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify EulerStream webhook signature (HMAC-SHA256 hex of raw body)."""
    if not secret or not signature_header:
        return False
    sig = (signature_header or "").strip()
    # Be tolerant to formats like "sha256=<hex>" if ever used.
    if "=" in sig and not all(c in "0123456789abcdefABCDEF" for c in sig):
        sig = sig.split("=", 1)[-1].strip()
    try:
        expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


def _extract_euler_username(payload):
    """Best-effort extraction of TikTok username/uniqueId from webhook payload."""
    if not isinstance(payload, dict):
        return ""
    candidates = []

    def add(val):
        if isinstance(val, str):
            v = val.strip().lstrip("@")
            if v:
                candidates.append(v)

    # Common flat keys
    for key in ("uniqueId", "unique_id", "username", "tiktok_username", "user", "account"):
        val = payload.get(key)
        if isinstance(val, str):
            add(val)
        elif isinstance(val, dict):
            for k2 in ("uniqueId", "unique_id", "username", "id", "name"):
                add(val.get(k2))

    # Nested data/room/user structures
    for container_key in ("data", "room", "roomInfo", "room_info", "creator", "stream", "event", "payload"):
        obj = payload.get(container_key)
        if isinstance(obj, dict):
            for k2 in ("uniqueId", "unique_id", "username", "tiktok_username"):
                add(obj.get(k2))
            user = obj.get("user") or obj.get("owner") or obj.get("author")
            if isinstance(user, dict):
                for k2 in ("uniqueId", "unique_id", "username", "displayId", "display_id"):
                    add(user.get(k2))
            elif isinstance(user, str):
                add(user)

    # Fallback: scan shallow string values of likely keys
    for k, v in payload.items():
        lk = str(k).lower()
        if "unique" in lk or lk in ("username", "user", "account"):
            add(v if isinstance(v, str) else None)

    # Return first plausible candidate (prefer values that look like usernames)
    for c in candidates:
        if c:
            return c.lower()
    return ""


def _infer_euler_live_state(payload):
    """Infer live state from webhook payload: True=live, False=offline, None=unknown."""
    if not isinstance(payload, dict):
        return None

    # Explicit booleans
    for key in ("is_live", "isLive", "live", "online", "is_online", "isOnline"):
        if key in payload:
            val = payload.get(key)
            if isinstance(val, bool):
                # "live" key can be a bool or an object; bool means state.
                return val
            if isinstance(val, str):
                v = val.strip().lower()
                if v in ("true", "1", "yes", "live", "online", "on"):
                    return True
                if v in ("false", "0", "no", "offline", "ended", "off"):
                    return False

    # Event/type/status strings
    text_fields = []
    for key in ("event", "type", "status", "state", "action", "event_type", "eventType"):
        val = payload.get(key)
        if isinstance(val, str):
            text_fields.append(val.lower())
    nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for key in ("event", "type", "status", "state", "action"):
        val = nested.get(key)
        if isinstance(val, str):
            text_fields.append(val.lower())

    blob = " ".join(text_fields)
    if blob:
        # Order matters: detect offline/end first to avoid matching "live_end" as live.
        offline_tokens = ("offline", "ended", "end", "stop", "stopped", "disconnect", "closed", "finish", "finished", "not_live")
        live_tokens = ("live", "online", "start", "started", "streaming", "broadcast")
        if any(t in blob for t in offline_tokens):
            # avoid false positive like "live" inside other words? tokens are indicative enough
            # but ensure it's not "live_start" misread; "start/live" would also appear, so be specific
            if any(t in blob for t in ("end", "ended", "offline", "stop", "stopped", "closed", "finish", "finished", "not_live")):
                return False
        if any(t in blob for t in live_tokens):
            return True

    # Room status codes sometimes used
    for key in ("room_status", "roomStatus", "status_code", "statusCode"):
        val = payload.get(key)
        try:
            ival = int(val)
            # common: 2 = live, 4/0 = offline (heuristic)
            if ival == 2:
                return True
            if ival in (0, 4):
                return False
        except Exception:
            pass

    return None


def _trigger_announcement_async(username: str, is_live: bool, host_name: str = None):
    handler = async_announcement_handler
    if not handler:
        return False

    # Try to run in the bot's event loop if available; otherwise best-effort call.
    bot_loop = None
    try:
        bot = get_bot_instance()
        bot_loop = getattr(bot, "loop", None) if bot else None
    except Exception:
        bot_loop = None

    async def _run():
        try:
            if asyncio.iscoroutinefunction(handler):
                try:
                    await handler(username, is_live, host_name)
                except TypeError:
                    await handler(username, is_live)
            else:
                try:
                    handler(username, is_live, host_name)
                except TypeError:
                    handler(username, is_live)
        except Exception as e:
            print(f"[Webhook] Announcement handler failed for @{username}: {e}")

    try:
        if bot_loop and bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(_run(), bot_loop)
            return True
    except Exception:
        pass
    # Fallback: run synchronously if handler is sync
    try:
        if not asyncio.iscoroutinefunction(handler):
            try:
                handler(username, is_live, host_name)
            except TypeError:
                handler(username, is_live)
            return True
    except Exception as e:
        print(f"[Webhook] Sync announcement failed for @{username}: {e}")
    return False


def _handle_euler_live_event(username: str, is_live: bool, source_payload=None):
    """Apply a webhook live/offline signal as the primary detection source."""
    username = (username or "").lower().lstrip("@")
    if not username:
        return {"ok": False, "error": "missing username"}

    now_wib = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")

    if is_live:
        try:
            session_id = start_live_session(username)
        except Exception as e:
            return {"ok": False, "error": f"start_live_session failed: {e}"}

        try:
            host = get_current_host_for_account(username, fresh_only=True) or "Host"
        except Exception:
            host = "Host"

        try:
            set_live(username, host, source="webhook")
        except Exception:
            pass
        try:
            update_observer_diag(
                username,
                status="webhook_live",
                last_error=None,
                webhook_live=True,
                webhook_event_at=now_wib,
            )
        except Exception:
            pass

        # Nudge observer to connect quickly so it can receive LiveIntro/gifts.
        # Do NOT force host reconfirm here: this webhook is the primary live
        # signal and a reconnect must not wipe a host just detected.
        try:
            from observer import request_tiktok_reconnect
            request_tiktok_reconnect(username, reconfirm_host=False)
        except Exception:
            pass

        try:
            log_activity_event("webhook", f"EulerStream webhook: @{username} is LIVE (primary signal).", username)
        except Exception:
            pass

        _trigger_announcement_async(username, True, host)
        return {"ok": True, "username": username, "is_live": True, "session_id": session_id, "host": host}

    # Offline
    session_id = None
    try:
        session_id = get_open_live_session_id(username)
        if session_id:
            end_live_session(session_id)
    except Exception as e:
        return {"ok": False, "error": f"end_live_session failed: {e}"}

    try:
        clear_live(username)
    except Exception:
        pass
    try:
        update_observer_diag(
            username,
            status="webhook_offline",
            webhook_live=False,
            webhook_event_at=now_wib,
        )
    except Exception:
        pass

    try:
        log_activity_event("webhook", f"EulerStream webhook: @{username} is OFFLINE (primary signal).", username)
    except Exception:
        pass

    _trigger_announcement_async(username, False, None)
    return {"ok": True, "username": username, "is_live": False, "session_id": session_id}


@api_bp.route('/euler-webhook', methods=['POST'])
@api_bp.route('/webhooks/eulerstream', methods=['POST'])
def euler_live_webhook():
    """Receive EulerStream TikTok live alerts (webhook primary live detection).

    Verifies X-Webhook-Signature (HMAC-SHA256 of raw body) with EULER_WEBHOOK_SECRET.
    """
    secret = _euler_webhook_secret()
    if not secret:
        return jsonify({"ok": False, "error": "EULER_WEBHOOK_SECRET not configured"}), 503

    raw_body = request.get_data(cache=True, as_text=False) or b""
    signature = (
        request.headers.get("X-Webhook-Signature")
        or request.headers.get("X-Euler-Signature")
        or request.headers.get("X-Signature")
        or ""
    )
    if not _verify_euler_webhook_signature(raw_body, signature, secret):
        try:
            log_activity_event("webhook", "Rejected EulerStream webhook: invalid signature.", "")
        except Exception:
            pass
        return jsonify({"ok": False, "error": "Invalid webhook signature"}), 401

    try:
        payload = request.get_json(silent=True, force=False)
    except Exception:
        payload = None
    if payload is None:
        # Try to parse manually in case content-type is odd
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except Exception:
            payload = {}

    # Some providers send an array/batch or wrap events in common containers.
    events = []
    if isinstance(payload, list):
        events = payload
    elif isinstance(payload, dict):
        for key in ("events", "alerts", "data", "items"):
            val = payload.get(key)
            if isinstance(val, list):
                events = val
                break
        if not events:
            events = [payload]
    else:
        events = []

    if len(events) > MAX_EULER_WEBHOOK_BATCH_SIZE:
        return jsonify({
            "ok": False,
            "error": "Webhook batch exceeds maximum event count",
            "max_events": MAX_EULER_WEBHOOK_BATCH_SIZE,
        }), 413

    results = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        # Some payloads nest the actual event under "data"/"event"/"payload"
        candidate = ev
        if _extract_euler_username(candidate) == "" or _infer_euler_live_state(candidate) is None:
            for key in ("data", "event", "payload", "alert", "live"):
                nested = ev.get(key)
                if isinstance(nested, dict):
                    if _extract_euler_username(nested) or _infer_euler_live_state(nested) is not None:
                        # Merge: nested fields take precedence but keep top-level context
                        merged = dict(ev)
                        merged.update(nested)
                        candidate = merged
                        break

        username = _extract_euler_username(candidate)
        state = _infer_euler_live_state(candidate)
        if not username or state is None:
            results.append({"username": username or None, "handled": False, "reason": "unrecognized event"})
            continue
        results.append(_handle_euler_live_event(username, bool(state), candidate))

    handled = sum(1 for r in results if r.get("ok"))
    status = 200 if handled > 0 or not results else 202
    return jsonify({"ok": True, "handled": handled, "results": results}), status

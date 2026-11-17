from flask import Flask, redirect, request, Response, jsonify, g
from threading import Thread
from werkzeug.exceptions import HTTPException
import logging
import os
import hmac
import time
import uuid
from routes.admin import admin_bp
from routes.api import api_bp, set_announcement_handler
from routes.settings import settings_bp
import log_setup

app = Flask(__name__)
log = log_setup.get_logger("bot.web")

# Optional basic auth for admin panel (recommended for production/public hosting).
# Set ADMIN_USERNAME and ADMIN_PASSWORD to enable protection for /admin routes.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def _is_admin_auth_enabled() -> bool:
    return bool(ADMIN_USERNAME and ADMIN_PASSWORD)


def _check_admin_auth(auth) -> bool:
    if not auth or not auth.username:
        return False
    user_ok = hmac.compare_digest(str(auth.username), str(ADMIN_USERNAME))
    pass_ok = hmac.compare_digest(str(auth.password or ""), str(ADMIN_PASSWORD))
    return user_ok and pass_ok


@app.before_request
def _log_request_start():
    g.log_request_id = uuid.uuid4().hex[:12]
    g.log_request_started = time.perf_counter()
    try:
        log.info(
            "HTTP -> %s %s ip=%s ua=%r",
            request.method,
            log_setup.redact(request.full_path),
            request.remote_addr or "-",
            (request.user_agent.string or "")[:120],
        )
    except Exception:
        pass
    return None


@app.after_request
def _log_request_end(response):
    try:
        request_id = getattr(g, "log_request_id", "-")
        started = getattr(g, "log_request_started", None)
        duration = (time.perf_counter() - started) * 1000 if started else -1
        status = response.status_code
        if status >= 500:
            level = logging.ERROR
        elif status >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO
        log.log(
            level,
            "HTTP <- %s %s status=%s duration=%.1fms bytes=%s ip=%s req=%s",
            request.method,
            log_setup.redact(request.path),
            status,
            duration,
            response.calculate_content_length() or 0,
            request.remote_addr or "-",
            request_id,
        )
        response.headers.setdefault("X-Request-Id", request_id)
    except Exception:
        pass
    return response


@app.errorhandler(Exception)
def _handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        return error.get_response()
    request_id = getattr(g, "log_request_id", "-")
    log.error(
        "HTTP 500 %s %s req=%s ip=%s: %s",
        request.method,
        log_setup.redact(request.path),
        request_id,
        request.remote_addr or "-",
        error,
        exc_info=(type(error), error, error.__traceback__),
    )
    return jsonify({"ok": False, "error": "Internal server error", "request_id": request_id}), 500


@app.before_request
def _protect_admin_routes():
    if not _is_admin_auth_enabled():
        return None
    path = request.path or ""
    if path == "/admin" or path.startswith("/admin/"):
        if not _check_admin_auth(request.authorization):
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Admin Panel"'},
            )
    return None


# Register Blueprints
app.register_blueprint(admin_bp)
app.register_blueprint(api_bp)
app.register_blueprint(settings_bp)


@app.route('/')
def home():
    return redirect('/admin/data')


@app.route('/health')
def health():
    # Lightweight health/readiness endpoint for hosting monitors.
    # `ok` reflects the database only (stable contract for uptime monitors);
    # `bot_ready` additionally reflects the Discord gateway connection.
    try:
        from runtime_status import get_live_snapshot, get_bot_status
        live = get_live_snapshot()
        live_count = len(live) if isinstance(live, dict) else 0
        bot_status = get_bot_status()
        bot_ready = bool(bot_status.get("online"))
        bot_user = bot_status.get("user")
    except Exception:
        live_count = 0
        bot_ready = False
        bot_user = None
    try:
        from db import get_db_connection
        conn = get_db_connection()
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False
    env = (os.environ.get("BOT_ENV") or os.environ.get("APP_ENV") or "stable").strip().lower()
    if env in ("prod", "production", "live"):
        env = "stable"
    elif env in ("testing", "staging", "dev", "development"):
        env = "test"
    version = (
        os.environ.get("PUSH_CODE")
        or os.environ.get("BOT_VERSION")
        or (os.environ.get("RENDER_GIT_COMMIT") or "")[:7]
        or "unknown"
    )
    return jsonify({
        "ok": db_ok,
        "db": db_ok,
        "bot_ready": bot_ready,
        "bot_user": bot_user,
        "env": env or "stable",
        "version": version,
        "live_tracked": live_count,
    }), (200 if db_ok else 503)


def run():
    port = int(os.environ.get("PORT", 8080))
    # Prefer a production WSGI server when available.
    try:
        from waitress import serve
        log.info("[Web] Serving admin panel with waitress on 0.0.0.0:%s (threads=8).", port)
        serve(app, host='0.0.0.0', port=port, threads=8)
        return
    except Exception as exc:
        log.warning("[Web] waitress unavailable/failed (%s); falling back to Flask dev server.", exc)
    log.info("[Web] Serving admin panel with Flask dev server on 0.0.0.0:%s.", port)
    app.run(host='0.0.0.0', port=port, threaded=True)


def keep_alive(handler_callback=None):
    if handler_callback:
        set_announcement_handler(handler_callback)
    t = Thread(target=run, daemon=True)
    t.start()

from flask import Flask, redirect, request, Response, jsonify, g, send_from_directory
from threading import Thread
from urllib.parse import urlparse
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
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
log = log_setup.get_logger("bot.web")

# Identifies the running process, so a redeploy can be told apart from a restart
# (and a stale deploy can be told apart from a crashed one) without server logs.
BOOT_TS = time.time()
BOOT_ID = uuid.uuid4().hex[:8]


def _build_version():
    """Short build id shown in the UI and /health (7 chars when available)."""
    return (
        os.environ.get("PUSH_CODE")
        or os.environ.get("BOT_VERSION")
        or (os.environ.get("RENDER_GIT_COMMIT") or "")[:7]
        or "unknown"
    )

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


if not _is_admin_auth_enabled():
    log.warning(
        "[Security] Admin panel has NO basic auth (ADMIN_USERNAME/ADMIN_PASSWORD not set). "
        "Set both to protect /admin/* on public deployments."
    )


@app.before_request
def _log_request_start():
    g.log_request_id = uuid.uuid4().hex[:12]
    g.log_request_started = time.perf_counter()
    try:
        auth = request.authorization
        web_user = f"admin:{auth.username}" if auth and getattr(auth, "username", None) else None
        log_setup.replace_log_context(
            method=request.method,
            path=log_setup.redact(request.path),
            ip=request.remote_addr or "-",
            web_user=web_user,
            request_id=g.log_request_id,
        )
    except Exception:
        pass
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
        # Security headers: reduce browser trust issues and harden the admin origin.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.is_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
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


@app.before_request
def _reject_cross_origin_admin_mutations():
    """Defense-in-depth against CSRF on browser-based admin actions.

    Rejects unsafe cross-site requests to /admin/* when an Origin/Referer header
    is present and does not match this host. Requests without either header are
    allowed (e.g. CLI/monitor probes) and still rely on admin auth when enabled.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    path = request.path or ""
    if not (path == "/admin" or path.startswith("/admin/")):
        return None
    origin = (request.headers.get("Origin") or "").strip()
    source = origin or (request.headers.get("Referer") or "").strip()
    if not source:
        return None
    try:
        src_host = (urlparse(source).hostname or "").lower()
        req_host = (urlparse(request.host_url).hostname or request.host.split(":")[0] or "").lower()
    except Exception:
        return jsonify({"ok": False, "error": "Invalid request origin"}), 403
    if not src_host or not req_host or src_host != req_host:
        return jsonify({"ok": False, "error": "Cross-origin request blocked"}), 403
    return None


@app.before_request
def _fast_head_probe():
    """Answer HEAD probes to the panel without rendering the dashboard.

    Uptime monitors that point at ``/`` follow the redirect to ``/admin/data``
    and previously made the bot render the full admin page (1.7-2.4s of DB
    work) on every check. HEAD needs no body, so acknowledge it immediately.
    Monitors should use ``/health`` instead.
    """
    if request.method == "HEAD" and (request.path or "") in ("/", "/admin", "/admin/data"):
        return Response("", 200)
    return None



# PWA assets (served without admin auth)
@app.route('/manifest.webmanifest')
def pwa_manifest():
    return send_from_directory(STATIC_DIR, 'manifest.webmanifest', mimetype='application/manifest+json')


@app.route('/sw.js')
def pwa_service_worker():
    resp = send_from_directory(STATIC_DIR, 'sw.js', mimetype='application/javascript')
    # Allow the service worker to control the full origin scope.
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/static/<path:filename>')
def pwa_static(filename):
    resp = send_from_directory(STATIC_DIR, filename)
    # Cache static assets, but not the service worker/manifest.
    if filename.endswith('sw.js') or filename.endswith('manifest.webmanifest'):
        resp.headers['Cache-Control'] = 'no-cache'
    else:
        resp.headers.setdefault('Cache-Control', 'public, max-age=604800')
    return resp


@app.route('/robots.txt')
def robots_txt():
    body = "User-agent: *\nDisallow: /admin/\nDisallow: /api/\nDisallow: /health\nDisallow: /version\n"
    return Response(body, mimetype='text/plain')


@app.route('/security.txt')
@app.route('/.well-known/security.txt')
def security_txt():
    contact = (os.environ.get('SECURITY_CONTACT') or 'mailto:security@example.com').strip()
    body = (
        "Contact: " + contact + "\n"
        "Canonical: https://dummydreams-discord-bot.onrender.com/.well-known/security.txt\n"
        "Preferred-Languages: en, id\n"
        "Expires: 2027-12-31T00:00:00Z\n"
    )
    return Response(body, mimetype='text/plain')


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
    version = _build_version()
    return jsonify({
        "ok": db_ok,
        "db": db_ok,
        "bot_ready": bot_ready,
        "bot_user": bot_user,
        "env": env or "stable",
        "version": version,
        "live_tracked": live_count,
        "boot_id": BOOT_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(BOOT_TS)),
        "admin_auth": _is_admin_auth_enabled(),
    }), (200 if db_ok else 503)


@app.route('/version')
def version_info():
    """Which code is actually running right now.

    Deploy checks compare ``boot_id``/``started_at`` before and after a deploy:
    a real redeploy changes both. ``version`` comes from the platform's commit
    env var, so it can lag behind (or be pinned by PUSH_CODE).
    """
    import platform
    import sys
    return jsonify({
        "version": _build_version(),
        "commit": os.environ.get("RENDER_GIT_COMMIT", ""),
        "env": os.environ.get("BOT_ENV", ""),
        "boot_id": BOOT_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(BOOT_TS)),
        "uptime_seconds": int(time.time() - BOOT_TS),
        "pid": os.getpid(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "admin_auth": _is_admin_auth_enabled(),
    })


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

from flask import Flask, redirect, request, Response, jsonify, g, send_from_directory, session
from threading import Thread
from urllib.parse import urlparse
from werkzeug.exceptions import HTTPException
import logging
import os
import hmac
import time
import uuid
import secrets
from routes.admin import admin_bp
from routes.api import api_bp, set_announcement_handler
from routes.settings import settings_bp
import log_setup

app = Flask(__name__)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
log = log_setup.get_logger("bot.web")

# Secret key is required for signed sessions and CSRF tokens.
# Prefer an explicit SECRET_KEY; fall back to API_SECRET_KEY for continuity.
_secret_key = (os.environ.get("SECRET_KEY") or os.environ.get("API_SECRET_KEY") or "").strip()
if _secret_key:
    app.secret_key = _secret_key
else:
    app.secret_key = secrets.token_hex(32)
    log.warning(
        "[Security] SECRET_KEY/API_SECRET_KEY not set; using ephemeral secret key. "
        "CSRF/session tokens will be invalidated on restart."
    )
app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
# Secure cookie in production when behind HTTPS/proxy; still works on local HTTP.
if (os.environ.get("BOT_ENV") or "").strip().lower() in ("stable", "prod", "production", "live"):
    app.config.setdefault("SESSION_COOKIE_SECURE", True)

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
    path = request.path or ""
    if not (path == "/admin" or path.startswith("/admin/")):
        return None

    # Fail closed: never expose admin routes without configured credentials.
    if not _is_admin_auth_enabled():
        return jsonify({
            "ok": False,
            "error": "Admin authentication is not configured"
        }), 503

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
    indicates a different site. Requests without usable origin info are allowed
    (e.g. CLI/monitor probes) and still rely on admin auth when enabled.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    path = request.path or ""
    if not (path == "/admin" or path.startswith("/admin/")):
        return None

    origin = (request.headers.get("Origin") or "").strip()
    referer = (request.headers.get("Referer") or "").strip()
    sec_fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()

    # Modern browsers send Sec-Fetch-Site; same-origin/same-site are safe here.
    if sec_fetch_site in ("same-origin", "same-site", "none"):
        return None

    # Collect candidate source hosts (prefer Origin, fall back to Referer).
    src_hosts = []
    for candidate in (origin, referer):
        if not candidate or candidate.lower() == "null":
            continue
        try:
            host = (urlparse(candidate).hostname or "").lower()
        except Exception:
            host = ""
        if host:
            src_hosts.append(host)

    # No usable browser origin info -> allow (auth still applies when enabled).
    if not src_hosts:
        return None

    try:
        allowed = set()
        for cand in (
            request.host,
            request.headers.get("Host"),
            request.headers.get("X-Forwarded-Host"),
            request.headers.get("X-Forwarded-Server"),
            urlparse(request.host_url).hostname,
        ):
            if not cand:
                continue
            for part in str(cand).split(","):
                part = part.strip().lower()
                if not part:
                    continue
                # Accept full URL values defensively.
                if "://" in part:
                    try:
                        part = urlparse(part).hostname or part
                    except Exception:
                        pass
                host_only = str(part).split(":", 1)[0].strip().lower()
                if host_only:
                    allowed.add(host_only)

        # If we cannot determine our own host, do not block legitimate requests.
        if not allowed:
            return None
        if any(h in allowed for h in src_hosts):
            return None
        return jsonify({"ok": False, "error": "Cross-origin request blocked"}), 403
    except Exception:
        # Fail open on validation errors to avoid breaking legitimate admin use.
        return None
    return None


def _get_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def _is_valid_csrf_token(token: str) -> bool:
    expected = session.get("_csrf_token")
    if not expected or not token:
        return False
    try:
        return hmac.compare_digest(str(expected), str(token))
    except Exception:
        return False


@app.context_processor
def _inject_csrf_token():
    # Lazily create/read session token so templates can render {{ csrf_token }}.
    return {"csrf_token": lambda: _get_csrf_token()}


@app.before_request
def _validate_csrf_admin_mutations():
    """Require a valid CSRF token for state-changing admin actions."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    path = request.path or ""
    if not (path == "/admin" or path.startswith("/admin/")):
        return None

    # Accept token from form field or explicit header (for future JS POSTs).
    supplied = (request.form.get("csrf_token") or "").strip()
    if not supplied:
        supplied = (request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken") or "").strip()

    if not _is_valid_csrf_token(supplied):
        wants_json = (
            request.is_json
            or "application/json" in (request.headers.get("Accept") or "")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        if wants_json:
            return jsonify({"ok": False, "error": "Invalid or missing CSRF token"}), 403
        return Response("Invalid or missing CSRF token.", 403, mimetype="text/plain")

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
        from db import check_database_health
        db_ok = check_database_health()
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

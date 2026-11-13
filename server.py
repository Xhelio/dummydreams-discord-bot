from flask import Flask, redirect, request, Response, jsonify
from threading import Thread
import os
import hmac
from routes.admin import admin_bp
from routes.api import api_bp, set_announcement_handler
from routes.settings import settings_bp

app = Flask(__name__)

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
    try:
        from runtime_status import get_live_snapshot
        live = get_live_snapshot()
        live_count = len(live) if isinstance(live, dict) else 0
    except Exception:
        live_count = 0
    try:
        from db import get_db_connection
        conn = get_db_connection()
        conn.execute("SELECT 1")
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify({
        "ok": db_ok,
        "db": db_ok,
        "live_tracked": live_count,
    }), (200 if db_ok else 503)


def run():
    port = int(os.environ.get("PORT", 8080))
    # Prefer a production WSGI server when available.
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=port, threads=8)
        return
    except Exception:
        pass
    app.run(host='0.0.0.0', port=port, threaded=True)


def keep_alive(handler_callback=None):
    if handler_callback:
        set_announcement_handler(handler_callback)
    t = Thread(target=run, daemon=True)
    t.start()

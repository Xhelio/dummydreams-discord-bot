from flask import Blueprint, render_template_string, request, redirect
import os
try:
    import libsql_experimental as libsql
except ImportError:
    import sqlite3 as libsql
import pytz
from datetime import datetime
from db import (
    get_db_connection, 
    get_all_host_names, 
    get_activity_logs, 
    clear_activity_logs, 
    get_active_hosts,
    get_setting,
    set_setting,
    get_recent_gift_activities,
    get_channel_display_name,
    sync_valid_hosts
)
from runtime_status import get_live_snapshot, clear_all_live
from verification import fetch_tiktok_display_name
from observer import request_tiktok_reconnect
from db import reset_live_state
from styles import BASE_CSS, NAV_HTML, AUTO_REFRESH_JS

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

@admin_bp.route('/data', methods=['GET'])
def view_data():
    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Database Admin Panel</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>🛡️ Database Admin Panel</h1>
        </div>
        NAV_HTML_PLACEHOLDER
        
        <div class="card">
            <h2>📌 Admin Management Menu</h2>
            <p style="color: #94a3b8; font-size: 0.95rem; margin-bottom: 16px;">
                Use the modules above to manage viewer and coin data, monitor realtime live sessions, or review the activity log.
            </p>
        </div>

        <div class="card" style="border-color: #ef4444;">
            <h2 style="color: #ef4444;">⚠️ Danger Zone</h2>
            <form action="/admin/reset-db" method="POST" onsubmit="return confirm('DELETE ALL GIFT AND LIVE SESSION DATA?')">
                <button type="submit" class="btn-danger">🔥 Reset Gift & Live Session Data</button>
            </form>
        </div>
        AUTO_REFRESH_JS_PLACEHOLDER
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)
    return render_template_string(html)

@admin_bp.route('/host/<host_name>', methods=['GET'])
def view_host_detail(host_name):
    host_name_upper = host_name.strip().upper()
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        stats = cur.execute("""
            SELECT 
                COUNT(session_id) as total_sessions,
                COALESCE(SUM(total_coins), 0) as grand_total_coins,
                COALESCE(SUM(duration_seconds), 0) as total_duration
            FROM live_sessions 
            WHERE UPPER(scheduled_host) = ?
        """, (host_name_upper,)).fetchone()

        total_sessions = stats[0] if stats else 0
        total_coins = stats[1] if stats else 0
        total_duration_sec = stats[2] if stats else 0

        sessions = cur.execute("""
            SELECT 
                session_id, 
                tiktok_username, 
                start_time, 
                end_time, 
                duration_seconds, 
                total_coins
            FROM live_sessions 
            WHERE UPPER(scheduled_host) = ?
            ORDER BY start_time DESC
        """, (host_name_upper,)).fetchall()

        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Host Shift History: {{ host_name }}</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>👤 Shift History Detail: <span style="color: #38bdf8;">{{ host_name }}</span></h1>
            <a href="/admin/schedule" class="nav-btn">⬅ Back to Schedule</a>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="stat-grid">
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">TOTAL SHIFT CONSISTENCY</div>
                <div class="stat-number">{{ total_sessions }} Sessions</div>
            </div>
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">TOTAL COINS EARNED</div>
                <div class="stat-number" style="color: #eab308;">{{ total_coins }} c</div>
            </div>
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">TOTAL STREAMING DURATION</div>
                <div class="stat-number" style="color: #22c55e;">
                    {{ total_duration_sec // 3600 }}h {{ (total_duration_sec % 3600) // 60 }}m
                </div>
            </div>
        </div>

        <div class="card">
            <h2>📜 Live Session History for Host {{ host_name }}</h2>
            <table>
                <tr>
                    <th>Channel (@)</th>
                    <th>Start Time</th>
                    <th>End Time</th>
                    <th>Live Duration</th>
                    <th>Coins Earned</th>
                </tr>
                {% for s in sessions %}
                <tr>
                    <td>@{{ s[1] }}</td>
                    <td>{{ s[2] }}</td>
                    <td>{{ s[3] or '🔴 Still Live' }}</td>
                    <td>
                        {% if s[4] > 0 %}
                            {{ s[4] // 3600 }}h {{ (s[4] % 3600) // 60 }}m {{ s[4] % 60 }}s
                        {% else %}
                            -
                        {% endif %}
                    </td>
                    <td><span class="badge" style="background: #ca8a04;">{{ s[5] }} c</span></td>
                </tr>
                {% else %}
                <tr><td colspan="5">No live shift history for this host yet.</td></tr>
                {% endfor %}
            </table>
        </div>
        AUTO_REFRESH_JS_PLACEHOLDER
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)

    return render_template_string(
        html, 
        host_name=host_name_upper, 
        total_sessions=total_sessions, 
        total_coins=total_coins, 
        total_duration_sec=total_duration_sec, 
        sessions=sessions
    )

@admin_bp.route('/viewers', methods=['GET'])
def view_viewers():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        viewers = cur.execute("""
            SELECT 
                g.tiktok_user, 
                g.display_name, 
                g.total_coins,
                COALESCE(a.tiktok_username, s.tiktok_username) as active_channel
            FROM tiktok_gifts g
            LEFT JOIN active_live_announcements a ON LOWER(g.tiktok_user) = LOWER(a.tiktok_username)
            LEFT JOIN live_sessions s ON LOWER(g.tiktok_user) = LOWER(s.tiktok_username) AND s.end_time IS NULL
            ORDER BY g.total_coins DESC
        """).fetchall()
        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Viewer & Coin List</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>👥 Viewer & Coin List</h1>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="card">
            <h2>Viewer List</h2>
            <table>
                <tr>
                    <th>Username (@)</th>
                    <th>Viewer Name</th>
                    <th>Total Coins</th>
                    <th>Realtime Live Status</th>
                    <th>Edit Coins</th>
                </tr>
                {% for v in viewers %}
                <tr>
                    <td>
                        <a href="/admin/viewer/{{ v[0] }}" class="viewer-link" title="Click to view coin details">@{{ v[0] }} ↗</a>
                    </td>
                    <td>
                        <a href="/admin/viewer/{{ v[0] }}" class="viewer-link" title="Click to view coin details">{{ v[1] or v[0] }} ↗</a>
                    </td>
                    <td><span class="badge" style="background: #ca8a04;">{{ v[2] }} c</span></td>
                    <td>
                        {% if v[3] %}
                        <a href="https://www.tiktok.com/@{{ v[3] }}/live" target="_blank" class="badge badge-active">🔴 Live Now ↗</a>
                        {% else %}
                        <span class="badge badge-inactive">Offline</span>
                        {% endif %}
                    </td>
                    <td>
                        <form action="/admin/edit-coin" method="POST" class="edit-form">
                            <input type="hidden" name="tiktok_user" value="{{ v[0] }}">
                            <input type="number" name="new_coins" value="{{ v[2] }}" required>
                            <button type="submit">Save</button>
                        </form>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="5">No viewer data recorded yet.</td></tr>
                {% endfor %}
            </table>
        </div>
        AUTO_REFRESH_JS_PLACEHOLDER
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)
    return render_template_string(html, viewers=viewers)

@admin_bp.route('/viewer/<tiktok_user>', methods=['GET'])
def view_viewer_detail(tiktok_user):
    user_clean = tiktok_user.strip().lower().lstrip("@")
    wib = pytz.timezone('Asia/Jakarta')
    today_str = datetime.now(wib).strftime('%Y-%m-%d')

    try:
        conn = get_db_connection()
        cur = conn.cursor()

        viewer_row = cur.execute("""
            SELECT display_name, total_coins 
            FROM tiktok_gifts 
            WHERE LOWER(tiktok_user) = ?
        """, (user_clean,)).fetchone()

        display_name = viewer_row[0] if viewer_row and viewer_row[0] else user_clean
        grand_total_coins = viewer_row[1] if viewer_row else 0

        today_row = cur.execute("""
            SELECT coins 
            FROM daily_tiktok_gifts 
            WHERE LOWER(tiktok_user) = ? AND date = ?
        """, (user_clean, today_str)).fetchone()

        today_coins = today_row[0] if today_row else 0

        daily_history = cur.execute("""
            SELECT date, coins 
            FROM daily_tiktok_gifts 
            WHERE LOWER(tiktok_user) = ?
            ORDER BY date DESC
        """, (user_clean,)).fetchall()

        gift_logs = cur.execute("""
            SELECT 
                COALESCE(log_date, created_at) as log_date,
                COALESCE(log_time, '') as log_time,
                gift_name, 
                gift_count, 
                coins, 
                COALESCE(target_username, '-') as target_username, 
                COALESCE(scheduled_host, '-') as scheduled_host
            FROM gift_logs
            WHERE LOWER(viewer_username) = ?
            ORDER BY log_datetime DESC, created_at DESC
            LIMIT 50
        """, (user_clean,)).fetchall()

        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Viewer Detail: @{{ username }}</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>👤 Viewer Detail: <span style="color: #38bdf8;">{{ display_name }}</span> (@{{ username }})</h1>
            <a href="/admin/viewers" class="nav-btn">⬅ Back to Viewer List</a>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="stat-grid">
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">TOTAL COINS</div>
                <div class="stat-number" style="color: #eab308;">{{ grand_total_coins }} c</div>
            </div>
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">COINS TODAY ({{ today_str }})</div>
                <div class="stat-number" style="color: #22c55e;">{{ today_coins }} c</div>
            </div>
        </div>

        <div class="card">
            <h2>📅 Daily Coin History</h2>
            <table>
                <tr>
                    <th>Date</th>
                    <th>Coin Amount</th>
                </tr>
                {% for d in daily_history %}
                <tr>
                    <td><strong>{{ d[0] }}</strong></td>
                    <td><span class="badge" style="background: #ca8a04;">{{ d[1] }} c</span></td>
                </tr>
                {% else %}
                <tr><td colspan="2">No daily coin records yet.</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>🎁 Recent Gift Log</h2>
            <table>
                <tr>
                    <th>Date & Time</th>
                    <th>Gift Name</th>
                    <th>Amount</th>
                    <th>Coins</th>
                    <th>Target Account Spent</th>
                    <th>Detected Host</th>
                </tr>
                {% for g in gift_logs %}
                <tr>
                    <td>{{ g[0] }} {{ g[1] }}</td>
                    <td><strong>{{ g[2] }}</strong></td>
                    <td>{{ g[3] }}x</td>
                    <td><span class="badge">{{ g[4] }} c</span></td>
                    <td>@{{ g[5] }}</td>
                    <td>
                        {% if g[6] != '-' and g[6] != 'Host' %}
                            <a href="/admin/host/{{ g[6] }}" class="host-link" title="Click to view shift history">{{ g[6] }} ↗</a>
                        {% else %}
                            <strong>{{ g[6] }}</strong>
                        {% endif %}
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="6">No gift history recorded yet.</td></tr>
                {% endfor %}
            </table>
        </div>
        AUTO_REFRESH_JS_PLACEHOLDER
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)

    return render_template_string(
        html,
        username=user_clean,
        display_name=display_name,
        grand_total_coins=grand_total_coins,
        today_coins=today_coins,
        today_str=today_str,
        daily_history=daily_history,
        gift_logs=gift_logs
    )

@admin_bp.route('/monitor', methods=['GET'])
def view_monitor():
    live_snapshot = get_live_snapshot()
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        host_rows = cur.execute("""
            SELECT h.host_id, h.tiktok_username, h.display_name, h.is_active
            FROM hosts h
            ORDER BY h.host_id ASC
        """).fetchall()

        session_rows = cur.execute("""
            SELECT 
                s.session_id, 
                s.tiktok_username, 
                s.start_time, 
                s.end_time, 
                s.duration_seconds, 
                s.total_coins, 
                s.scheduled_host
            FROM live_sessions s
            ORDER BY s.start_time DESC
        """).fetchall()

        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    # Current host from the most recent open session (used only as a fallback).
    open_hosts = {}
    for s in session_rows:
        uname = (s[1] or "").lower()
        if s[3] is None and uname not in open_hosts and s[6] and s[6] != "Host":
            open_hosts[uname] = s[6]

    hosts = []
    for h in host_rows:
        uname = (h[1] or "").lower()
        live_item = live_snapshot.get(uname)
        hosts.append({
            "host_id": h[0],
            "username": h[1],
            "display_name": h[2] or h[1],
            "is_active": bool(h[3]),
            "is_live": bool(live_item),
            "host_name": (live_item or {}).get("host") or open_hosts.get(uname) or h[2] or h[1],
        })

    # Observer/connection diagnostics (rate limits, offline polling, cooldowns...).
    observer_diag = {}
    try:
        from runtime_status import get_observer_diag
        observer_diag = get_observer_diag() or {}
    except Exception:
        observer_diag = {}
    signer_configured = bool((os.environ.get("SIGN_API_KEY") or os.environ.get("TIKTOK_SIGN_API_KEY") or "").strip())
    conn_states = []
    rate_limited_any = False
    for h in hosts:
        d = observer_diag.get((h["username"] or "").lower(), {}) or {}
        status = (d.get("status") or "unknown").lower()
        wait_seconds = int(d.get("wait_seconds") or 0)
        if status.startswith("rate_limited"):
            rate_limited_any = True
        if status in ("rate_limited_cooldown", "rate_limited_backoff"):
            label = f"⏳ Rate limited — retry in {max(1, wait_seconds // 60)}m"
        elif status == "online_waiting_websocket":
            label = "🟡 Online (probe) — waiting websocket for LiveIntro"
        elif status in ("offline",):
            label = "⚪ Offline (polling)"
        elif status in ("connected", "connected_with_handler_error"):
            label = "🟢 Connected (websocket)"
        elif status == "non_priority_off":
            label = "💤 Paused (non-priority off)"
        elif status in ("error", "error_json_response"):
            label = "🔴 Connection error"
        else:
            label = status.replace("_", " ").capitalize() if status else "Unknown"
        conn_states.append({
            "username": h["username"],
            "label": label,
            "status": status,
            "last_error": (d.get("last_error") or "")[:180],
            "connect_mode": d.get("connect_mode") or "normal",
            "intro_preview": (d.get("last_intro_preview") or "")[:200],
            "intro_at": d.get("last_intro_at") or "",
            "intro_source": d.get("last_intro_source") or "",
        })

    sessions = []
    for s in session_rows:
        uname = (s[1] or "").lower()
        sessions.append({
            "session_id": s[0],
            "username": s[1],
            "start_time": s[2],
            "end_time": s[3],
            "duration_seconds": s[4] or 0,
            "total_coins": s[5] or 0,
            "scheduled_host": s[6],
            "is_live_now": uname in live_snapshot,
        })

    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Realtime Live Monitor</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>📹 Realtime Live Monitor</h1>
        </div>
        NAV_HTML_PLACEHOLDER

        {% if rate_limited_any %}
        <div class="notice-warning">
            ⚠️ Satu atau lebih akun sedang kena <strong>rate limit sign-server</strong> (EulerStream), jadi websocket
            tidak bisa connect dan <code>LiveIntroEvent</code> tidak diterima. Tambahkan API key di
            <a href="https://www.eulerstream.com/pricing" target="_blank" rel="noopener">eulerstream.com/pricing</a>
            atau tunggu cooldown selesai. Tombol <em>Refresh/Reset</em> akan mencoba ulang sekarang (memakai kuota).
        </div>
        {% endif %}

        <div class="card">
            <h2>🔌 Connection Health</h2>
            <div class="section-note">
                Sign API key (EulerStream):
                {% if signer_configured %}
                <span class="badge badge-active">Configured</span>
                {% else %}
                <span class="badge badge-inactive">Not configured</span>
                — set the <code>SIGN_API_KEY</code> environment variable to raise per-account daily limits.
                {% endif %}
            </div>
            <div class="table-scroll">
            <table>
                <tr><th>Account</th><th>Mode</th><th>Status</th><th>Last Live Intro (host detection source)</th><th>Last Error</th></tr>
                {% for c in conn_states %}
                <tr>
                    <td>@{{ c.username }}</td>
                    <td>{{ c.connect_mode }}</td>
                    <td>{{ c.label }}</td>
                    <td style="max-width: 360px; white-space: normal; word-break: break-word;">
                        {% if c.intro_preview %}
                        <span style="color:#e2e8f0;">{{ c.intro_preview }}</span>
                        <div class="field-help">{{ c.intro_at }} · {{ c.intro_source }}</div>
                        {% else %}
                        <span class="field-help">No LiveIntroEvent output yet</span>
                        {% endif %}
                    </td>
                    <td style="max-width: 320px; white-space: normal; word-break: break-word; color: #94a3b8;">{{ c.last_error or '-' }}</td>
                </tr>
                {% else %}
                <tr><td colspan="5">No observer data yet.</td></tr>
                {% endfor %}
            </table>
            </div>
        </div>

        <div class="card">
            <h2>🎙️ TikTok Target Accounts</h2>
            <div style="display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap;">
                <form action="/admin/sync-hosts" method="POST" onsubmit="return confirm('Reorganize and sync the 6 official target accounts with IDs 1-6?');" style="margin: 0;">
                    <button type="submit" style="background: #15803d; padding: 8px 14px;">🔄 Sync 6 Official Accounts (IDs 1-6)</button>
                </form>
                <form action="/admin/tiktok-reconnect" method="POST" onsubmit="return confirm('Disconnect and reconnect TikTok livestream observers now?');" style="margin: 0;">
                    <button type="submit" style="background: #7c3aed; padding: 8px 14px;">🔌 Refresh TikTok Connection</button>
                </form>
                <form action="/admin/reset-live-state" method="POST" onsubmit="return confirm('Clear live sessions/announcements/host state and activity logs, then reconnect observers from scratch? Gift totals are not deleted.');" style="margin: 0;">
                    <button type="submit" style="background: #b45309; padding: 8px 14px;">🧹 Reset Live State & Reconnect</button>
                </form>
            </div>
            <form action="/admin/add-host" method="POST" class="form-grid" style="grid-template-columns: 1fr auto;">
                <input type="text" name="tiktok_username" placeholder="TikTok username (@...) — channel name is fetched automatically" required>
                <button type="submit">+ Add Target Host</button>
            </form>
            <br>
            <table>
                <tr><th>ID</th><th>Username TikTok</th><th>Channel Name</th><th>Live Host</th><th>Realtime Status</th><th>Action</th></tr>
                {% for h in hosts %}
                <tr>
                    <td>{{ h.host_id }}</td>
                    <td>@{{ h.username }}</td>
                    <td>{{ h.display_name }}</td>
                    <td><strong>{{ h.host_name }}</strong></td>
                    <td>
                        {% if h.is_live %}
                        <a href="https://www.tiktok.com/@{{ h.username }}/live" target="_blank" class="badge badge-active" title="Click to watch the live">🔴 Live Now ↗</a>
                        {% else %}
                        <span class="badge badge-inactive">Offline</span>
                        {% endif %}
                    </td>
                    <td>
                        <a href="/admin/toggle-host/{{ h.host_id }}" class="btn-action btn-toggle">Toggle Active Status</a>
                        <a href="/admin/delete-host/{{ h.host_id }}" class="btn-action btn-del" onclick="return confirm('Delete this target?')">Delete</a>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="6">No targets registered yet.</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>📹 Live Session History</h2>
            <table>
                <tr>
                    <th>Username (@)</th>
                    <th>Start</th>
                    <th>End</th>
                    <th>Duration</th>
                    <th>Coins</th>
                    <th>Detected Host</th>
                </tr>
                {% for s in sessions %}
                <tr>
                    <td>
                        <a href="https://www.tiktok.com/@{{ s.username }}" target="_blank" class="viewer-link">@{{ s.username }} ↗</a>
                    </td>
                    <td>{{ s.start_time }}</td>
                    <td>
                        {% if s.end_time %}
                            {{ s.end_time }}
                        {% elif s.is_live_now %}
                            <span class="badge badge-active">🔴 Live Now</span>
                        {% else %}
                            <span style="color: #94a3b8; font-style: italic;">No data</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if s.duration_seconds > 0 %}
                            {{ s.duration_seconds // 3600 }}h {{ (s.duration_seconds % 3600) // 60 }}m {{ s.duration_seconds % 60 }}s
                        {% else %}
                            -
                        {% endif %}
                    </td>
                    <td><span class="badge" style="background: #ca8a04;">{{ s.total_coins }} c</span></td>
                    <td>
                        {% if s.scheduled_host and s.scheduled_host != '-' and s.scheduled_host != 'Host' %}
                            <a href="/admin/host/{{ s.scheduled_host }}" class="host-link" title="Click to view shift history">{{ s.scheduled_host }} ↗</a>
                        {% else %}
                            <strong>{{ s.scheduled_host or '-' }}</strong>
                        {% endif %}
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="6">No live session history yet.</td></tr>
                {% endfor %}
            </table>
        </div>
        AUTO_REFRESH_JS_PLACEHOLDER
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)
    return render_template_string(html, hosts=hosts, sessions=sessions, conn_states=conn_states, rate_limited_any=rate_limited_any, signer_configured=signer_configured)

# Handlers Form Admin
@admin_bp.route('/add-host', methods=['POST'])
def add_host():
    username = request.form.get("tiktok_username", "").strip().lower().lstrip("@")
    if username:
        display_name = fetch_tiktok_display_name(username)
        if not display_name or display_name == username:
            display_name = get_channel_display_name(username) or username
        conn = get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO hosts (tiktok_username, display_name, is_active) VALUES (?, ?, 1)",
            (username, display_name),
        )
        conn.commit()
        conn.close()
    return redirect('/admin/monitor?msg=' + quote_plus('Host ditambahkan/diperbarui.') + '&msg_type=success')

@admin_bp.route('/toggle-host/<int:host_id>')
def toggle_host(host_id):
    conn = get_db_connection()
    conn.execute("UPDATE hosts SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE host_id = ?", (host_id,))
    conn.commit()
    conn.close()
    return redirect('/admin/monitor?msg=' + quote_plus('Status aktif host diperbarui.') + '&msg_type=success')

@admin_bp.route('/delete-host/<int:host_id>')
def delete_host(host_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM hosts WHERE host_id = ?", (host_id,))
    conn.commit()
    conn.close()
    return redirect('/admin/monitor?msg=' + quote_plus('Host dihapus.') + '&msg_type=success')

@admin_bp.route('/edit-coin', methods=['POST'])
def edit_coin():
    username = request.form.get("tiktok_user", "").strip().lower().lstrip("@")
    new_coins = int(request.form.get("new_coins", 0))
    if username:
        conn = get_db_connection()
        conn.execute("UPDATE tiktok_gifts SET total_coins = ? WHERE LOWER(tiktok_user) = ?", (new_coins, username))
        conn.commit()
        conn.close()
    return redirect('/admin/viewers?msg=' + quote_plus('Coins viewer diperbarui.') + '&msg_type=success')

@admin_bp.route('/sync-hosts', methods=['POST'])
def sync_hosts_action():
    sync_valid_hosts()
    return redirect('/admin/monitor?msg=' + quote_plus('Sinkronisasi host selesai.') + '&msg_type=success')

@admin_bp.route('/activity', methods=['GET'])
def view_activity():
    channel_filter = request.args.get("channel", "").strip().lower().lstrip("@")
    logs = get_activity_logs(limit=250, channel_filter=channel_filter)
    gift_activities = get_recent_gift_activities(limit=100, channel_filter=channel_filter)
    all_hosts = get_active_hosts()
    if not all_hosts:
        all_hosts = ['georgevilora', 'clarissadreams', 'juliannedreams', 'franklyndreams', 'stefannyalisha', 'dummydreamsentertaiment']

    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Log Activity Realtime</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>📊 Realtime Activity Log</h1>
            <div style="display: flex; gap: 10px; align-items: center;">
                <form action="/admin/clear-activity-logs" method="POST" onsubmit="return confirm('Clear the entire activity log?');" style="margin: 0;">
                    <button type="submit" class="btn-danger" style="padding: 6px 12px; font-size: 0.8rem;">🗑️ Clear Log</button>
                </form>
            </div>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="card" style="margin-bottom: 16px; padding: 14px 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                <form method="GET" action="/admin/activity" style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                    <label for="channel_filter" style="font-size: 0.875rem; color: #94a3b8; font-weight: 500;">Channel Account Filter:</label>
                    <select name="channel" id="channel_filter" onchange="this.form.submit()" style="padding: 6px 10px; min-width: 240px;">
                        <option value="" {% if not current_filter %}selected{% endif %}>🌐 All Accounts (6 Official Targets)</option>
                        {% for h in all_hosts %}
                        <option value="{{ h }}" {% if current_filter == h %}selected{% endif %}>@{{ h }}</option>
                        {% endfor %}
                    </select>
                    {% if current_filter %}
                    <a href="/admin/activity" class="btn-action" style="background: #334155; color: white;">Reset Filter</a>
                    {% endif %}
                </form>
                <div style="color: #94a3b8; font-size: 0.85rem;">
                    Showing <strong>{{ gift_activities|length }}</strong> gifts & <strong>{{ logs|length }}</strong> log feed lines
                </div>
            </div>
        </div>

        <!-- RECENT GIFT ACTIVITY TABLE -->
        <div class="card">
            <h2>🎁 Recent Incoming Gifts</h2>
            <p style="color: #94a3b8; font-size: 0.85rem; margin-bottom: 14px;">
                Realtime gift history: date &amp; time, target account name, user/donor, gift name, coin amount, and the user's current total coins.
            </p>
            <table>
                <tr>
                    <th style="width: 50px;">NO</th>
                    <th>DATE & TIME (WIB)</th>
                    <th>ACCOUNT NAME (CHANNEL)</th>
                    <th>USER (DONOR)</th>
                    <th>GIFT NAME</th>
                    <th>COIN AMOUNT</th>
                    <th>CURRENT TOTAL COINS</th>
                </tr>
                {% for g in gift_activities %}
                <tr>
                    <td>{{ loop.index }}</td>
                    <td><span style="color: #94a3b8; font-family: monospace;">{{ g[1] }}</span></td>
                    <td>
                        <strong>{{ g[3] }}</strong>
                        <span style="color: #38bdf8; font-size: 0.8rem; display: block;">@{{ g[2] }}</span>
                    </td>
                    <td>
                        <strong style="color: #f8fafc;">{{ g[5] }}</strong>
                        <span style="color: #94a3b8; font-size: 0.8rem; display: block;">@{{ g[4] }}</span>
                    </td>
                    <td>
                        <span class="badge" style="background: #0284c7;">{{ g[6] }} {% if g[7] > 1 %}(x{{ g[7] }}){% endif %}</span>
                    </td>
                    <td>
                        <span class="badge" style="background: #ca8a04; font-weight: bold;">+{{ g[8] }} c</span>
                    </td>
                    <td>
                        <span class="badge" style="background: #15803d; font-weight: bold;">{{ g[9] }} c</span>
                    </td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="7" style="text-align: center; color: #64748b; padding: 20px;">
                        No gift activity recorded for this filter yet.
                    </td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <!-- LIVE STREAM FEED TERMINAL -->
        <div class="card">
            <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom: 8px;">
                <h2 style="margin:0;">📜 Live Activity Feed (Stream Console)</h2>
                <button type="button" class="btn" id="copy-all-log-btn" onclick="copyAllActivityLog()" style="background:#334155; border:1px solid #475569; padding:8px 12px;">📋 Copy All Log</button>
            </div>
            <div class="terminal-container" id="terminal-feed">
                {% for log in logs %}
                <div class="terminal-row" data-log-ts="{{ log[4] }}" data-log-type="{{ log[1] }}" data-log-msg="{{ log[3]|e }}">
                    <span style="color: #64748b; font-size: 0.75rem; min-width: 140px;">[{{ log[4] }}]</span>
                    {% if log[1] == 'connect' %}
                        <span class="log-tag-connect">CONNECT</span>
                        <span style="color: #4ade80; font-weight: 600;">{{ log[3] }}</span>
                    {% elif log[1] == 'disconnect' %}
                        <span class="log-tag-disconnect">DISCONNECT</span>
                        <span style="color: #f87171; font-weight: 600;">{{ log[3] }}</span>
                    {% elif log[1] == 'host_detected' %}
                        <span class="log-tag-gift" style="color: #38bdf8; background: rgba(56, 189, 248, 0.1);">HOST</span>
                        <span style="color: #38bdf8; font-weight: 600;">{{ log[3] }}</span>
                    {% elif log[1] == 'host_check' %}
                        <span class="log-tag-gift" style="color: #a78bfa; background: rgba(167, 139, 250, 0.12);">HOST CHECK</span>
                        <span style="color: #ddd6fe;">{{ log[3] }}</span>
                    {% elif log[1] == 'gift' %}
                        <span class="log-tag-gift">🎁 GIFT</span>
                        <span style="color: #f8fafc;">{{ log[3] }}</span>
                    {% elif log[1] == 'reconnect' %}
                        <span class="log-tag-gift" style="color: #f59e0b; background: rgba(245, 158, 11, 0.12);">RECONNECT</span>
                        <span style="color: #fde68a; font-weight: 600;">{{ log[3] }}</span>
                    {% elif log[1] == 'system' %}
                        <span class="log-tag-gift" style="color: #94a3b8; background: rgba(148, 163, 184, 0.12);">SYSTEM</span>
                        <span style="color: #cbd5e1;">{{ log[3] }}</span>
                    {% elif log[1] == 'tier_reset' %}
                        <span class="log-tag-gift" style="color: #f472b6; background: rgba(244, 114, 182, 0.12);">TIER</span>
                        <span style="color: #fbcfe8;">{{ log[3] }}</span>
                    {% elif log[1] == 'error' %}
                        <span class="log-tag-disconnect">ERROR</span>
                        <span style="color: #fca5a5; font-weight: 600;">{{ log[3] }}</span>
                    {% else %}
                        <span class="log-tag-gift" style="color: #e2e8f0; background: rgba(226, 232, 240, 0.08);">{{ log[1]|upper }}</span>
                        <span style="color: #e2e8f0;">{{ log[3] }}</span>
                    {% endif %}
                </div>
                {% else %}
                <div style="color: #64748b; font-style: italic; padding: 24px; text-align: center;">
                    No activity recorded yet. The system is standing by and monitoring the 6 official target accounts.
                </div>
                {% endfor %}
            </div>
        </div>

        <script>
        function collectActivityLogText() {
            const rows = Array.from(document.querySelectorAll('#terminal-feed .terminal-row'));
            let text = rows.map(row => {
                const ts = row.getAttribute('data-log-ts') || '';
                const type = (row.getAttribute('data-log-type') || '').toUpperCase();
                const msg = row.getAttribute('data-log-msg') || '';
                return `[${ts}] [${type}] ${msg}`.trim();
            }).filter(Boolean).join('\n');
            if (!text) text = 'No activity log lines available.';
            return text;
        }

        function showCopyLogModal(text) {
            const existing = document.getElementById('copy-log-modal');
            if (existing) existing.remove();
            const overlay = document.createElement('div');
            overlay.id = 'copy-log-modal';
            overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px;';
            overlay.innerHTML = `
                <div style="background:#0f172a;border:1px solid #334155;border-radius:12px;max-width:1000px;width:100%;padding:16px;color:#e2e8f0;">
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;">
                        <strong>Copy All Log</strong>
                        <button type="button" id="close-copy-log-modal" class="btn" style="background:#334155;border:1px solid #475569;padding:6px 10px;">Close</button>
                    </div>
                    <p style="margin:0 0 8px;color:#94a3b8;font-size:0.85rem;">Teks log sudah diseleksi. Jika tidak otomatis tercopy, tekan Ctrl+C / Cmd+C lalu paste.</p>
                    <textarea id="copy-log-textarea" readonly style="width:100%;height:60vh;background:#020617;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:12px;font-family:monospace;font-size:12px;white-space:pre;"></textarea>
                </div>`;
            document.body.appendChild(overlay);
            const ta = overlay.querySelector('#copy-log-textarea');
            ta.value = text;
            ta.focus();
            ta.select();
            overlay.querySelector('#close-copy-log-modal').onclick = () => overlay.remove();
            overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
        }

        async function copyAllActivityLog() {
            const text = collectActivityLogText();
            const btn = document.getElementById('copy-all-log-btn');
            const original = btn ? btn.textContent : '';
            let copied = false;
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(text);
                    copied = true;
                }
            } catch (e) { copied = false; }
            if (!copied) {
                try {
                    const ta = document.createElement('textarea');
                    ta.value = text;
                    ta.setAttribute('readonly', '');
                    ta.style.position = 'fixed';
                    ta.style.top = '-9999px';
                    document.body.appendChild(ta);
                    ta.select();
                    copied = document.execCommand('copy');
                    document.body.removeChild(ta);
                } catch (e) { copied = false; }
            }
            if (btn) {
                btn.textContent = copied ? '✅ Copied' : '📋 Manual Copy';
                setTimeout(() => btn.textContent = original, 1500);
            }
            showCopyLogModal(text);
        }
        </script>

        AUTO_REFRESH_JS_PLACEHOLDER
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)
    return render_template_string(html, logs=logs, gift_activities=gift_activities, all_hosts=all_hosts, current_filter=channel_filter)

@admin_bp.route('/verifications', methods=['GET'])
def view_verifications():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        users = cur.execute("""
            SELECT u.discord_id, u.tiktok_user, COALESCE(g.total_coins, 0), g.display_name
            FROM user_mappings u
            LEFT JOIN tiktok_gifts g ON LOWER(u.tiktok_user) = LOWER(g.tiktok_user)
            ORDER BY g.total_coins DESC
        """).fetchall()

        pending = cur.execute("""
            SELECT discord_id, tiktok_user, code, method, created_at, expires_at 
            FROM pending_verifications 
            ORDER BY created_at DESC
        """).fetchall()

        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    verified_role_id = get_setting("verified_role_id", "")
    verification_channel_id = get_setting("verification_channel_id", "")

    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Manage TikTok Account Verification</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>🔐 TikTok & Discord Account Verification Management</h1>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="card">
            <h2>⚙️ Verification Configuration</h2>
            <p class="muted" style="margin-bottom: 10px;">
                Verified role and verification channel settings are managed in <strong>Bot Settings</strong> to keep all bot controls in one place.
            </p>
            <table>
                <tr><th>Setting</th><th>Value</th></tr>
                <tr><td>Verified Role ID</td><td><code>{{ verified_role_id or '-' }}</code></td></tr>
                <tr><td>Verification Channel ID</td><td><code>{{ verification_channel_id or '-' }}</code></td></tr>
            </table>
            <p style="margin-top: 12px;">
                <a href="/admin/settings" class="nav-btn" style="display:inline-flex;">⚙️ Open Bot Settings</a>
            </p>
        </div>

        <div class="card">
            <h2>✅ Verified Accounts (Discord &lt;-&gt; TikTok)</h2>
            <p style="color: #94a3b8; font-size: 0.85rem; margin-bottom: 12px;">
                Discord accounts successfully linked to a TikTok account. Each account can only be claimed by one user.
            </p>
            <table>
                <tr>
                    <th>Discord ID</th>
                    <th>TikTok Username</th>
                    <th>Display Name</th>
                    <th>Total Coins</th>
                    <th>Action</th>
                </tr>
                {% for u in users %}
                <tr>
                    <td><code>{{ u[0] }}</code></td>
                    <td><strong>@{{ u[1] }}</strong></td>
                    <td>{{ u[3] or '-' }}</td>
                    <td><span class="badge">{{ u[2] }} c</span></td>
                    <td>
                        <form action="/admin/unclaim-user/{{ u[0] }}" method="POST" onsubmit="return confirm('Remove the account link for Discord ID {{ u[0] }}?');" style="display:inline;">
                            <button type="submit" class="btn-action btn-del">Unclaim</button>
                        </form>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="5" style="text-align:center; color:#64748b;">No verified accounts yet.</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>⏳ Pending Verifications (In Progress)</h2>
            <table>
                <tr>
                    <th>Discord ID</th>
                    <th>TikTok Username</th>
                    <th>Verification Code</th>
                    <th>Method</th>
                    <th>Created At</th>
                    <th>Expires At (WIB)</th>
                </tr>
                {% for p in pending %}
                <tr>
                    <td><code>{{ p[0] }}</code></td>
                    <td><strong>@{{ p[1] }}</strong></td>
                    <td><span class="badge" style="background:#f59e0b; color:black;">{{ p[2] }}</span></td>
                    <td><span class="badge">{{ p[3]|upper }}</span></td>
                    <td>{{ p[4] }}</td>
                    <td>{{ p[5] }}</td>
                </tr>
                {% else %}
                <tr><td colspan="6" style="text-align:center; color:#64748b;">No pending verifications right now.</td></tr>
                {% endfor %}
            </table>
        </div>

        AUTO_REFRESH_JS_PLACEHOLDER
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)
    return render_template_string(html, users=users, pending=pending, verified_role_id=verified_role_id, verification_channel_id=verification_channel_id)

@admin_bp.route('/save-verification-settings', methods=['POST'])
def save_verification_settings():
    # Consolidated into /admin/settings to avoid duplicate/conflicting config.
    return redirect('/admin/settings')

@admin_bp.route('/unclaim-user/<int:discord_id>', methods=['POST'])
def unclaim_user_admin(discord_id):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM user_mappings WHERE discord_id = ?", (discord_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect('/admin/verifications')

@admin_bp.route('/clear-activity-logs', methods=['POST'])
def clear_activity():
    clear_activity_logs()
    return redirect('/admin/activity?msg=' + quote_plus('Activity log dibersihkan.') + '&msg_type=success')

@admin_bp.route('/reset-db', methods=['POST'])
def reset_db():
    conn = get_db_connection()
    conn.execute("DELETE FROM gift_logs")
    conn.execute("DELETE FROM live_sessions")
    conn.execute("DELETE FROM tiktok_gifts")
    conn.execute("DELETE FROM daily_tiktok_gifts")
    conn.execute("DELETE FROM active_live_announcements")
    conn.execute("DELETE FROM activity_logs")
    conn.commit()
    conn.close()
    return redirect('/admin/data?msg=' + quote_plus('Database live/gift/log di-reset.') + '&msg_type=warning')

@admin_bp.route('/tiktok-reconnect', methods=['POST'])
def tiktok_reconnect():
    try:
        targets = request_tiktok_reconnect()
        print(f"[Admin] TikTok reconnect requested for: {targets}")
    except Exception as e:
        print(f"[Admin] Failed to request TikTok reconnect: {e}")
    return redirect('/admin/monitor?msg=' + quote_plus('Reconnect observer diminta.') + '&msg_type=info')


@admin_bp.route('/reset-live-state', methods=['POST'])
def reset_live_state_route():
    try:
        # Log intent before clearing activity logs so admin action is visible in console/history up to reset point.
        try:
            from db import log_activity_event
            log_activity_event("system", "Admin requested reset live state and reconnect.", "")
        except Exception:
            pass
        reset_live_state(clear_logs=True)
        try:
            clear_all_live()
        except Exception:
            pass
        targets = request_tiktok_reconnect()
        try:
            from db import log_activity_event
            log_activity_event("system", f"Live state reset completed; reconnect requested for: {targets}", "")
        except Exception:
            pass
        print(f"[Admin] Live state reset and TikTok reconnect requested for: {targets}")
    except Exception as e:
        print(f"[Admin] Failed to reset live state/reconnect: {e}")
    return redirect('/admin/monitor?msg=' + quote_plus('Live state di-reset dan reconnect diminta.') + '&msg_type=warning')

from flask import Blueprint, render_template_string, request, redirect
from urllib.parse import quote_plus
import asyncio
import os
try:
    import libsql_experimental as libsql
except ImportError:
    import sqlite3 as libsql
import pytz
from datetime import datetime, timedelta
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
    sync_valid_hosts,
    get_coin_integrity,
    recalculate_coin_totals,
    get_all_verification_status,
    save_verification_status,
    delete_verification_status,
    get_analytics_summary,
    get_analytics_sessions,
    get_analytics_session_breakdown,
    get_analytics_timeseries,
    get_analytics_gift_by_host,
    get_analytics_gift_by_account,
    get_analytics_viewer_ranking,
    get_analytics_viewer_host_spend,
    get_analytics_host_options,
    get_analytics_account_options,
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
    integrity = get_coin_integrity()
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

        <div class="card">
            <h2>🪙 Coin Integrity</h2>
            {% if integrity.error %}
            <div class="notice-danger">Gagal membaca data koin: {{ integrity.error }}</div>
            {% else %}
            <div class="stat-grid">
                <div class="stat-box">
                    <div class="muted">Total koin tersimpan (viewer)</div>
                    <div class="stat-number">{{ integrity.stored_total }} c</div>
                </div>
                <div class="stat-box">
                    <div class="muted">Total dari gift log (detail)</div>
                    <div class="stat-number">{{ integrity.logged_total }} c</div>
                </div>
                <div class="stat-box">
                    <div class="muted">Koin hari ini (WIB)</div>
                    <div class="stat-number">{{ integrity.today_total }} c</div>
                </div>
                <div class="stat-box">
                    <div class="muted">Baris gift log</div>
                    <div class="stat-number">{{ integrity.gift_log_rows }}</div>
                </div>
            </div>
            {% if integrity.stored_total != integrity.logged_total %}
            <div class="notice-warning">
                Ada selisih {{ integrity.stored_total - integrity.logged_total }} c antara total tersimpan dan gift log.
                Klik <strong>Recalculate</strong> untuk menyamakan semua total dari gift log.
            </div>
            {% else %}
            <div class="notice-success">✅ Total koin konsisten dengan gift log.</div>
            {% endif %}
            {% if integrity.mismatches %}
            <div class="table-scroll">
            <table>
                <tr><th>Viewer</th><th>Tersimpan</th><th>Di gift log</th><th>Selisih</th></tr>
                {% for m in integrity.mismatches %}
                <tr>
                    <td><a href="/admin/viewer/{{ m.viewer }}" class="viewer-link">@{{ m.viewer }}</a></td>
                    <td>{{ m.stored }} c</td>
                    <td>{{ m.logged }} c</td>
                    <td>{{ m.stored - m.logged }} c</td>
                </tr>
                {% endfor %}
            </table>
            </div>
            {% endif %}
            <form action="/admin/recalculate-coins" method="POST" onsubmit="return confirm('Hitung ulang SEMUA total koin dari gift log? Total yang diedit manual tanpa log akan hilang.');" style="margin-top: 12px;">
                <button type="submit" class="btn-save" style="background:#0e7490;">🔄 Recalculate Coin Totals dari Gift Log</button>
            </form>
            {% endif %}
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
    return render_template_string(html, integrity=integrity)

@admin_bp.route('/recalculate-coins', methods=['POST'])
def recalculate_coins():
    result = recalculate_coin_totals()
    if result.get("error"):
        msg = "Gagal recalculate: " + str(result["error"])
        return redirect("/admin/data?msg=" + quote_plus(msg) + "&msg_type=danger")
    try:
        from db import log_activity_event
        log_activity_event(
            "system",
            f"Admin recalculated coin totals from gift log (viewers={result.get('viewers')}, total={result.get('total_coins')}c, today={result.get('today_coins')}c).",
            "",
        )
    except Exception:
        pass
    msg = f"Coin totals dihitung ulang: {result.get('viewers')} viewer, total {result.get('total_coins')} c, hari ini {result.get('today_coins')} c."
    return redirect("/admin/data?msg=" + quote_plus(msg) + "&msg_type=success")

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
    wib = pytz.timezone('Asia/Jakarta')
    today_str = datetime.now(wib).strftime('%Y-%m-%d')
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        viewers = cur.execute("""
            SELECT 
                g.tiktok_user, 
                g.display_name, 
                g.total_coins,
                COALESCE(a.tiktok_username, s.tiktok_username) as active_channel,
                COALESCE(d.coins, 0) as today_coins
            FROM tiktok_gifts g
            LEFT JOIN active_live_announcements a ON LOWER(g.tiktok_user) = LOWER(a.tiktok_username)
            LEFT JOIN live_sessions s ON LOWER(g.tiktok_user) = LOWER(s.tiktok_username) AND s.end_time IS NULL
            LEFT JOIN daily_tiktok_gifts d ON LOWER(d.tiktok_user) = LOWER(g.tiktok_user) AND d.date = ?
            ORDER BY g.total_coins DESC
        """, (today_str,)).fetchall()
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
                    <th>Hari Ini (WIB)</th>
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
                    <td>{{ v[4] }} c</td>
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
                <tr><td colspan="6">No viewer data recorded yet.</td></tr>
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
            SELECT MAX(display_name), COALESCE(SUM(total_coins), 0)
            FROM tiktok_gifts 
            WHERE LOWER(tiktok_user) = ?
        """, (user_clean,)).fetchone()

        display_name = viewer_row[0] if viewer_row and viewer_row[0] else user_clean
        grand_total_coins = int(viewer_row[1] or 0) if viewer_row else 0

        logged_row = cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(coins), 0)
            FROM gift_logs
            WHERE LOWER(viewer_username) = ?
        """, (user_clean,)).fetchone()
        logged_gift_count = int(logged_row[0] or 0) if logged_row else 0
        logged_total_coins = int(logged_row[1] or 0) if logged_row else 0

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
                COALESCE(NULLIF(host_name, ''), NULLIF(scheduled_host, ''), '-') as scheduled_host
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
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">DARI GIFT LOG ({{ logged_gift_count }} gift)</div>
                <div class="stat-number">{{ logged_total_coins }} c</div>
            </div>
        </div>

        {% if grand_total_coins != logged_total_coins %}
        <div class="notice-warning">
            ⚠️ Total tersimpan ({{ grand_total_coins }} c) berbeda dengan jumlah gift log ({{ logged_total_coins }} c).
            Jalankan <a href="/admin/data">Recalculate Coin Totals</a> untuk menyamakan.
        </div>
        {% endif %}

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
        logged_total_coins=logged_total_coins,
        logged_gift_count=logged_gift_count,
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
    raw_coins = request.form.get("new_coins", "").strip()
    try:
        new_coins = max(0, int(raw_coins or 0))
    except (TypeError, ValueError):
        return redirect('/admin/viewers?msg=' + quote_plus('Nilai koin tidak valid (harus angka).') + '&msg_type=danger')
    if not username:
        return redirect('/admin/viewers?msg=' + quote_plus('Username viewer kosong.') + '&msg_type=danger')
    old_coins = 0
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(display_name), ?), COALESCE(SUM(total_coins), 0) "
            "FROM tiktok_gifts WHERE LOWER(tiktok_user) = ?",
            (username, username),
        ).fetchone()
        display = (row[0] if row and row[0] else username)
        old_coins = int(row[1] or 0) if row else 0
        # Keep a single, lowercase row per viewer.
        conn.execute("DELETE FROM tiktok_gifts WHERE LOWER(tiktok_user) = ?", (username,))
        conn.execute(
            "INSERT INTO tiktok_gifts (tiktok_user, display_name, total_coins) VALUES (?, ?, ?)",
            (username, display, new_coins),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        from db import log_activity_event
        log_activity_event(
            "system",
            f"Admin set coins for @{username}: {old_coins}c -> {new_coins}c (all-time; gift log unchanged).",
            username,
        )
    except Exception:
        pass
    return redirect('/admin/viewers?msg=' + quote_plus(f'Coins @{username} diperbarui: {old_coins} c -> {new_coins} c.') + '&msg_type=success')

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
                    <th>HOST</th>
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
                        <span class="badge" style="background: #7c3aed;">{{ g[10] or 'Host' }}</span>
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
                    <td colspan="8" style="text-align: center; color: #64748b; padding: 20px;">
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

def _fmt_duration(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "-"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}j {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


ANALYTICS_PRESETS = [
    ("today", "Hari ini"),
    ("yesterday", "Kemarin"),
    ("7d", "7 hari terakhir"),
    ("30d", "30 hari terakhir"),
    ("90d", "90 hari terakhir"),
    ("this_week", "Minggu ini"),
    ("this_month", "Bulan ini"),
    ("last_month", "Bulan lalu"),
    ("all", "Semua waktu"),
    ("custom", "Kustom"),
]


def _parse_iso_date(value, fallback):
    try:
        return datetime.strptime(str(value).strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return fallback


@admin_bp.route('/analytics', methods=['GET'])
def view_analytics():
    wib = pytz.timezone('Asia/Jakarta')
    today = datetime.now(wib).date()

    preset_values = {p[0] for p in ANALYTICS_PRESETS}
    preset = (request.args.get('preset') or '7d').strip()
    if preset not in preset_values:
        preset = '7d'

    if preset == 'custom':
        start_d = _parse_iso_date(request.args.get('start'), today - timedelta(days=6))
        end_d = _parse_iso_date(request.args.get('end'), today)
        if start_d > end_d:
            start_d, end_d = end_d, start_d
    elif preset == 'today':
        start_d = end_d = today
    elif preset == 'yesterday':
        start_d = end_d = today - timedelta(days=1)
    elif preset == '7d':
        start_d, end_d = today - timedelta(days=6), today
    elif preset == '30d':
        start_d, end_d = today - timedelta(days=29), today
    elif preset == '90d':
        start_d, end_d = today - timedelta(days=89), today
    elif preset == 'this_week':
        start_d, end_d = today - timedelta(days=today.weekday()), today
    elif preset == 'this_month':
        start_d, end_d = today.replace(day=1), today
    elif preset == 'last_month':
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        start_d, end_d = last_prev.replace(day=1), last_prev
    elif preset == 'all':
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT MIN(log_date) FROM gift_logs WHERE log_date IS NOT NULL AND log_date != ''"
            ).fetchone()
        finally:
            conn.close()
        start_d = _parse_iso_date(row[0], today - timedelta(days=29)) if row and row[0] else today - timedelta(days=29)
        end_d = today
    else:
        start_d, end_d = today - timedelta(days=6), today

    start_str = start_d.strftime('%Y-%m-%d')
    end_str = end_d.strftime('%Y-%m-%d')

    host_filter = (request.args.get('host') or '').strip()
    account_filter = (request.args.get('account') or '').strip().lstrip('@')
    viewer_filter = (request.args.get('viewer') or '').strip().lstrip('@')

    days = (end_d - start_d).days + 1
    group_by = (request.args.get('group') or '').strip()
    if group_by not in ('day', 'week', 'month'):
        group_by = 'day' if days <= 45 else ('week' if days <= 200 else 'month')

    chart_type = (request.args.get('chart') or 'bar').strip()
    if chart_type not in ('bar', 'line', 'area'):
        chart_type = 'bar'
    metric = (request.args.get('metric') or 'coins').strip()
    if metric not in ('coins', 'gifts', 'viewers', 'sessions'):
        metric = 'coins'

    host_arg = host_filter or None
    account_arg = account_filter or None
    viewer_arg = viewer_filter or None

    summary = get_analytics_summary(start_str, end_str, host_arg, account_arg, viewer_arg)
    session_summary = get_analytics_sessions(start_str, end_str, host_arg, account_arg)
    session_break = get_analytics_session_breakdown(start_str, end_str, host_arg, account_arg)
    series = get_analytics_timeseries(start_str, end_str, group_by, host_arg, account_arg, viewer_arg)
    gift_by_host = get_analytics_gift_by_host(start_str, end_str, account_arg, viewer_arg)
    gift_by_account = get_analytics_gift_by_account(start_str, end_str, host_arg, viewer_arg)
    viewer_rows_raw = get_analytics_viewer_ranking(start_str, end_str, host_arg, account_arg, 100)
    spend_rows_raw = get_analytics_viewer_host_spend(start_str, end_str, viewer_arg, host_arg, account_arg, 200)
    host_options = get_analytics_host_options()
    account_options = get_analytics_account_options()

    total_coins = summary.get('coins', 0) or 0

    def _pct(part):
        return round((part / total_coins * 100), 1) if total_coins else 0.0

    host_rows = []
    seen_hosts = set()
    for key, label, coins, gifts, viewers, accounts in gift_by_host:
        k = (key or '').lower()
        sessions, duration = session_break['by_host'].get(k, (0, 0))
        coins = int(coins or 0)
        host_rows.append({
            'key': k,
            'name': label or key or 'Host',
            'coins': coins,
            'gifts': int(gifts or 0),
            'viewers': int(viewers or 0),
            'accounts': int(accounts or 0),
            'sessions': int(sessions or 0),
            'duration': _fmt_duration(duration),
            'pct': _pct(coins),
        })
        seen_hosts.add(k)
    for k, (sessions, duration) in session_break['by_host'].items():
        if k in seen_hosts:
            continue
        host_rows.append({
            'key': k, 'name': k or 'Host', 'coins': 0, 'gifts': 0, 'viewers': 0,
            'accounts': 0, 'sessions': int(sessions or 0), 'duration': _fmt_duration(duration), 'pct': 0.0,
        })
    host_rows.sort(key=lambda r: (r['coins'], r['sessions']), reverse=True)

    account_rows = []
    seen_accounts = set()
    for key, label, coins, gifts, viewers, hosts_count in gift_by_account:
        k = (key or '').lower()
        sessions, duration = session_break['by_account'].get(k, (0, 0))
        coins = int(coins or 0)
        account_rows.append({
            'key': k,
            'name': label or key or k,
            'coins': coins,
            'gifts': int(gifts or 0),
            'viewers': int(viewers or 0),
            'hosts': int(hosts_count or 0),
            'sessions': int(sessions or 0),
            'duration': _fmt_duration(duration),
            'pct': _pct(coins),
        })
        seen_accounts.add(k)
    for k, (sessions, duration) in session_break['by_account'].items():
        if k in seen_accounts:
            continue
        account_rows.append({
            'key': k, 'name': k, 'coins': 0, 'gifts': 0, 'viewers': 0, 'hosts': 0,
            'sessions': int(sessions or 0), 'duration': _fmt_duration(duration), 'pct': 0.0,
        })
    account_rows.sort(key=lambda r: (r['coins'], r['sessions']), reverse=True)

    viewer_rows = [{
        'key': (r[0] or '').lower(),
        'name': r[1] or r[0],
        'coins': int(r[2] or 0),
        'gifts': int(r[3] or 0),
        'hosts': int(r[4] or 0),
        'last_at': r[5] or '-',
        'pct': _pct(int(r[2] or 0)),
    } for r in viewer_rows_raw]

    spend_rows = [{
        'viewer': (r[0] or '').lower(),
        'display': r[1] or r[0],
        'host': r[2] or '-',
        'coins': int(r[3] or 0),
        'gifts': int(r[4] or 0),
        'pct': _pct(int(r[3] or 0)),
    } for r in spend_rows_raw]

    host_max = max([r['coins'] for r in host_rows] + [0])
    account_max = max([r['coins'] for r in account_rows] + [0])

    chart_payload = {
        'labels': [b['label'] for b in series],
        'keys': [b['key'] for b in series],
        'coins': [b['coins'] for b in series],
        'gifts': [b['gifts'] for b in series],
        'viewers': [b['viewers'] for b in series],
        'sessions': [b['sessions'] for b in series],
    }

    def _link(**overrides):
        params = {
            'preset': preset,
            'start': start_str,
            'end': end_str,
            'host': host_filter,
            'account': account_filter,
            'viewer': viewer_filter,
            'group': group_by,
        }
        params.update(overrides)
        query = '&'.join(f"{k}={quote_plus(str(v))}" for k, v in params.items() if v)
        return '/admin/analytics' + ('?' + query if query else '')

    html = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Analytics</title>
        <style>BASE_CSS_PLACEHOLDER</style>
        <style>
            .analytics-form label { display: flex; flex-direction: column; gap: 4px; font-size: 0.78rem; color: #94a3b8; font-weight: 600; }
            .analytics-form select, .analytics-form input { width: 100%; }
            .analytics-chips { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
            .analytics-chips .nav-btn { padding: 6px 10px; font-size: 0.78rem; }
            .chart-wrap { background: #0b1220; border: 1px solid var(--border); border-radius: 10px; padding: 10px; overflow: hidden; }
            .chart-toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 10px; }
            .chart-toolbar label { color: #94a3b8; font-size: 0.78rem; font-weight: 600; display: flex; align-items: center; gap: 6px; }
            .rank-bar { height: 6px; background: #0b1220; border-radius: 999px; overflow: hidden; margin-top: 5px; min-width: 60px; }
            .rank-bar > span { display: block; height: 100%; background: linear-gradient(90deg, #0284c7, #38bdf8); }
            .analytics-scroll { max-height: 420px; overflow-y: auto; }
            .muted { color: #94a3b8; }
            .share-pill { color: #7dd3fc; font-weight: 700; }
        </style>
    </head>
    <body>
        <div class="header">
            <div>
                <h1>📈 Analytics</h1>
                <p class="page-subtitle">{{ start_str }} s/d {{ end_str }} (WIB) · {{ days }} hari · grup: {{ group_by }}</p>
            </div>
            <a class="nav-btn" href="/admin/analytics?preset=7d">🔄 Reset Filter</a>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="card">
            <div class="card-header">
                <h2>🔎 Filter &amp; Rentang</h2>
                <span class="muted" style="font-size:0.8rem;">Semua metrik di bawah mengikuti filter ini</span>
            </div>
            <div class="analytics-chips">
                <button type="button" class="nav-btn" onclick="applyPreset('today')">Hari ini</button>
                <button type="button" class="nav-btn" onclick="applyPreset('7d')">7 hari</button>
                <button type="button" class="nav-btn" onclick="applyPreset('30d')">30 hari</button>
                <button type="button" class="nav-btn" onclick="applyPreset('this_month')">Bulan ini</button>
                <button type="button" class="nav-btn" onclick="applyPreset('last_month')">Bulan lalu</button>
                <button type="button" class="nav-btn" onclick="applyPreset('all')">Semua</button>
            </div>
            <form method="GET" action="/admin/analytics" class="analytics-form" id="analytics-form">
                <div class="form-grid">
                    <label>Rentang
                        <select name="preset" id="preset-select">
                            {% for value, label in presets %}
                            <option value="{{ value }}" {% if preset == value %}selected{% endif %}>{{ label }}</option>
                            {% endfor %}
                        </select>
                    </label>
                    <label>Dari tanggal
                        <input type="date" name="start" value="{{ start_str }}">
                    </label>
                    <label>Sampai tanggal
                        <input type="date" name="end" value="{{ end_str }}">
                    </label>
                    <label>Host
                        <select name="host">
                            <option value="">Semua host</option>
                            {% for h in host_options %}
                            <option value="{{ h }}" {% if host_filter|lower == h|lower %}selected{% endif %}>{{ h }}</option>
                            {% endfor %}
                        </select>
                    </label>
                    <label>Akun TikTok
                        <select name="account">
                            <option value="">Semua akun</option>
                            {% for a in account_options %}
                            <option value="{{ a }}" {% if account_filter|lower == a|lower %}selected{% endif %}>@{{ a }}</option>
                            {% endfor %}
                        </select>
                    </label>
                    <label>Viewer (opsional)
                        <input type="text" name="viewer" value="{{ viewer_filter }}" placeholder="username TikTok">
                    </label>
                    <label>Group grafik
                        <select name="group">
                            <option value="day" {% if group_by == 'day' %}selected{% endif %}>Per hari</option>
                            <option value="week" {% if group_by == 'week' %}selected{% endif %}>Per minggu</option>
                            <option value="month" {% if group_by == 'month' %}selected{% endif %}>Per bulan</option>
                        </select>
                    </label>
                    <label>&nbsp;
                        <button type="submit" class="btn-save" style="background:#0e7490;">Terapkan Filter</button>
                    </label>
                </div>
                <p class="muted" style="font-size:0.76rem; margin-top:8px;">Preset "Kustom" memakai tanggal Dari/Sampai. Filter viewer dipakai untuk analisa spent viewer &amp; grafik pribadinya.</p>
            </form>
        </div>

        <div class="stat-grid">
            <div class="stat-box">
                <div class="muted" style="font-size:0.82rem;">TOTAL KOIN</div>
                <div class="stat-number" style="color:#eab308;">{{ summary.coins }} c</div>
            </div>
            <div class="stat-box">
                <div class="muted" style="font-size:0.82rem;">GIFT MASUK</div>
                <div class="stat-number">{{ summary.gifts }}</div>
                <div class="muted" style="font-size:0.75rem;">{{ summary.gift_rows }} transaksi log</div>
            </div>
            <div class="stat-box">
                <div class="muted" style="font-size:0.82rem;">VIEWER UNIK</div>
                <div class="stat-number" style="color:#22c55e;">{{ summary.viewers }}</div>
            </div>
            <div class="stat-box">
                <div class="muted" style="font-size:0.82rem;">HOST AKTIF</div>
                <div class="stat-number">{{ summary.hosts }}</div>
            </div>
            <div class="stat-box">
                <div class="muted" style="font-size:0.82rem;">AKUN LIVE</div>
                <div class="stat-number">{{ summary.accounts }}</div>
            </div>
            <div class="stat-box">
                <div class="muted" style="font-size:0.82rem;">SESI LIVE</div>
                <div class="stat-number">{{ session_summary.sessions }}</div>
                <div class="muted" style="font-size:0.75rem;">rata-rata {{ session_summary.avg_duration // 60 }} mnt</div>
            </div>
            <div class="stat-box">
                <div class="muted" style="font-size:0.82rem;">DURASI LIVE</div>
                <div class="stat-number" style="color:#f472b6;">{{ fmt_duration(session_summary.duration) }}</div>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <h2>📊 Tren Metrik</h2>
                <div class="chart-toolbar">
                    <label>Metrik
                        <select id="chart-metric">
                            <option value="coins">Koin</option>
                            <option value="gifts">Gift</option>
                            <option value="viewers">Viewer unik</option>
                            <option value="sessions">Sesi live</option>
                        </select>
                    </label>
                    <label>Format
                        <select id="chart-type">
                            <option value="bar">Bar</option>
                            <option value="line">Line</option>
                            <option value="area">Area</option>
                        </select>
                    </label>
                </div>
            </div>
            <div class="chart-wrap">
                <div id="chart-svg"></div>
            </div>
            <div class="table-scroll analytics-scroll" style="margin-top:12px;">
                <table>
                    <tr><th>Periode</th><th>Koin</th><th>Gift</th><th>Viewer unik</th><th>Sesi live</th></tr>
                    {% for b in series %}
                    <tr>
                        <td><strong>{{ b.label }}</strong></td>
                        <td>{{ b.coins }} c</td>
                        <td>{{ b.gifts }}</td>
                        <td>{{ b.viewers }}</td>
                        <td>{{ b.sessions }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="5" class="muted">Tidak ada data pada rentang ini.</td></tr>
                    {% endfor %}
                </table>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <h2>🏆 Ranking Host</h2>
                <span class="muted" style="font-size:0.8rem;">Klik nama host untuk melihat koin host tersebut di rentang tanggal ini</span>
            </div>
            <div class="table-scroll analytics-scroll">
                <table>
                    <tr><th>#</th><th>Host</th><th>Koin</th><th>Share</th><th>Gift</th><th>Viewer</th><th>Akun</th><th>Sesi</th><th>Durasi live</th></tr>
                    {% for h in host_rows %}
                    <tr>
                        <td>{{ loop.index }}</td>
                        <td>
                            <a href="{{ _link(preset='custom', host=h.name) }}" class="host-link">{{ h.name }}</a>
                            <div class="rank-bar"><span style="width: {{ (h.coins / host_max * 100) if host_max else 0 }}%;"></span></div>
                        </td>
                        <td><span class="badge" style="background:#ca8a04;">{{ h.coins }} c</span></td>
                        <td class="share-pill">{{ h.pct }}%</td>
                        <td>{{ h.gifts }}</td>
                        <td>{{ h.viewers }}</td>
                        <td>{{ h.accounts }}</td>
                        <td>{{ h.sessions }}</td>
                        <td>{{ h.duration }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="9" class="muted">Belum ada data host pada rentang ini.</td></tr>
                    {% endfor %}
                </table>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <h2>📡 Traffic &amp; Ranking Akun</h2>
                <span class="muted" style="font-size:0.8rem;">Koin, sesi live, durasi, viewer, dan jumlah host per akun</span>
            </div>
            <div class="table-scroll analytics-scroll">
                <table>
                    <tr><th>#</th><th>Akun TikTok</th><th>Koin</th><th>Share</th><th>Gift</th><th>Viewer</th><th>Host</th><th>Sesi</th><th>Durasi live</th></tr>
                    {% for a in account_rows %}
                    <tr>
                        <td>{{ loop.index }}</td>
                        <td>
                            <a href="{{ _link(preset='custom', account=a.name) }}" class="viewer-link">@{{ a.name }}</a>
                            <div class="rank-bar"><span style="width: {{ (a.coins / account_max * 100) if account_max else 0 }}%;"></span></div>
                        </td>
                        <td><span class="badge" style="background:#ca8a04;">{{ a.coins }} c</span></td>
                        <td class="share-pill">{{ a.pct }}%</td>
                        <td>{{ a.gifts }}</td>
                        <td>{{ a.viewers }}</td>
                        <td>{{ a.hosts }}</td>
                        <td>{{ a.sessions }}</td>
                        <td>{{ a.duration }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="9" class="muted">Belum ada data akun pada rentang ini.</td></tr>
                    {% endfor %}
                </table>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <h2>💎 Ranking Viewer (Spent Koin)</h2>
                <span class="muted" style="font-size:0.8rem;">Klik nama viewer untuk analisa spent-nya per host</span>
            </div>
            <div class="table-scroll analytics-scroll">
                <table>
                    <tr><th>#</th><th>Viewer</th><th>Koin</th><th>Share</th><th>Gift</th><th>Host berbeda</th><th>Terakhir gift</th></tr>
                    {% for v in viewer_rows %}
                    <tr>
                        <td>{{ loop.index }}</td>
                        <td>
                            <a href="{{ _link(preset='custom', viewer=v.key) }}" class="viewer-link">{{ v.name }}</a>
                            <span class="muted" style="font-size:0.75rem; display:block;">@{{ v.key }}</span>
                        </td>
                        <td><span class="badge" style="background:#ca8a04;">{{ v.coins }} c</span></td>
                        <td class="share-pill">{{ v.pct }}%</td>
                        <td>{{ v.gifts }}</td>
                        <td>{{ v.hosts }}</td>
                        <td class="muted" style="font-size:0.78rem;">{{ v.last_at }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="7" class="muted">Belum ada data viewer pada rentang ini.</td></tr>
                    {% endfor %}
                </table>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <h2>🎯 Spent Viewer ke Host{% if viewer_filter %} — @{{ viewer_filter }}{% endif %}</h2>
                <span class="muted" style="font-size:0.8rem;">Berapa koin yang dibelanjakan ke masing-masing host dalam rentang ini</span>
            </div>
            <div class="table-scroll analytics-scroll">
                <table>
                    <tr><th>#</th><th>Viewer</th><th>Host</th><th>Koin</th><th>Share</th><th>Gift</th></tr>
                    {% for s in spend_rows %}
                    <tr>
                        <td>{{ loop.index }}</td>
                        <td><strong>{{ s.display }}</strong><span class="muted" style="font-size:0.75rem; display:block;">@{{ s.viewer }}</span></td>
                        <td><span class="badge" style="background:#7c3aed;">{{ s.host }}</span></td>
                        <td><span class="badge" style="background:#ca8a04;">{{ s.coins }} c</span></td>
                        <td class="share-pill">{{ s.pct }}%</td>
                        <td>{{ s.gifts }}</td>
                    </tr>
                    {% else %}
                    <tr><td colspan="6" class="muted">Belum ada data spent viewer ke host pada rentang ini.</td></tr>
                    {% endfor %}
                </table>
            </div>
        </div>

        AUTO_REFRESH_JS_PLACEHOLDER
        <script>
        (function () {
            var DATA = {{ chart_payload|tojson }};
            var state = { metric: {{ metric|tojson }}, type: {{ chart_type|tojson }} };
            var metricSelect = document.getElementById('chart-metric');
            var typeSelect = document.getElementById('chart-type');
            if (metricSelect) { metricSelect.value = state.metric; metricSelect.addEventListener('change', function () { state.metric = this.value; draw(); }); }
            if (typeSelect) { typeSelect.value = state.type; typeSelect.addEventListener('change', function () { state.type = this.value; draw(); }); }

            function fmt(n) { return (Math.round(n || 0)).toLocaleString('id-ID'); }
            function niceCeil(v) {
                if (!v || v <= 0) return 1;
                var exp = Math.pow(10, Math.floor(Math.log10(v)));
                var f = v / exp;
                var nf = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
                return nf * exp;
            }
            function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

            function draw() {
                var wrap = document.getElementById('chart-svg');
                if (!wrap) return;
                var labels = DATA.labels || [];
                var values = DATA[state.metric] || [];
                if (!labels.length) {
                    wrap.innerHTML = '<div class="muted" style="padding:40px;text-align:center;">Tidak ada data untuk filter ini.</div>';
                    return;
                }
                var width = Math.max(320, wrap.clientWidth || 800);
                var height = 300;
                var pad = { l: 58, r: 18, t: 22, b: 62 };
                var pw = width - pad.l - pad.r;
                var ph = height - pad.t - pad.b;
                var maxV = Math.max.apply(null, values.concat([0]));
                var top = niceCeil(maxV);
                var parts = [];
                parts.push('<svg viewBox="0 0 ' + width + ' ' + height + '" width="100%" height="' + height + '" role="img" aria-label="Grafik metrik">');
                parts.push('<defs><linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#38bdf8" stop-opacity="0.45"/><stop offset="100%" stop-color="#38bdf8" stop-opacity="0.02"/></linearGradient></defs>');
                for (var t = 0; t <= 4; t++) {
                    var y = pad.t + ph - (ph * t / 4);
                    var val = top * t / 4;
                    parts.push('<line x1="' + pad.l + '" y1="' + y + '" x2="' + (pad.l + pw) + '" y2="' + y + '" stroke="#1e293b" stroke-width="1"/>');
                    parts.push('<text x="' + (pad.l - 8) + '" y="' + (y + 4) + '" text-anchor="end" fill="#64748b" font-size="10">' + fmt(val) + '</text>');
                }
                var n = values.length;
                var slot = pw / n;
                if (state.type === 'bar') {
                    var bw = Math.max(2, Math.min(44, slot * 0.62));
                    for (var i = 0; i < n; i++) {
                        var bh = ph * (values[i] / top);
                        var bx = pad.l + slot * i + (slot - bw) / 2;
                        var by = pad.t + ph - bh;
                        parts.push('<rect x="' + bx + '" y="' + by + '" width="' + bw + '" height="' + Math.max(0, bh) + '" rx="3" fill="#38bdf8"><title>' + esc(labels[i]) + ': ' + fmt(values[i]) + '</title></rect>');
                    }
                } else {
                    var pts = [];
                    for (var j = 0; j < n; j++) {
                        pts.push([pad.l + slot * (j + 0.5), pad.t + ph - ph * (values[j] / top)]);
                    }
                    if (state.type === 'area') {
                        var area = 'M' + pts[0][0] + ',' + (pad.t + ph) + ' L' + pts.map(function (p) { return p[0] + ',' + p[1]; }).join(' L') + ' L' + pts[n - 1][0] + ',' + (pad.t + ph) + ' Z';
                        parts.push('<path d="' + area + '" fill="url(#areaGrad)" stroke="none"/>');
                    }
                    parts.push('<polyline fill="none" stroke="#38bdf8" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" points="' + pts.map(function (p) { return p[0] + ',' + p[1]; }).join(' ') + '"/>');
                    for (var k = 0; k < n; k++) {
                        parts.push('<circle cx="' + pts[k][0] + '" cy="' + pts[k][1] + '" r="3.5" fill="#0b1220" stroke="#38bdf8" stroke-width="2"><title>' + esc(labels[k]) + ': ' + fmt(values[k]) + '</title></circle>');
                    }
                }
                var step = Math.max(1, Math.ceil(n / 12));
                for (var x = 0; x < n; x += step) {
                    var cx = pad.l + slot * (x + 0.5);
                    parts.push('<text x="' + cx + '" y="' + (pad.t + ph + 18) + '" text-anchor="end" fill="#94a3b8" font-size="10" transform="rotate(-28 ' + cx + ' ' + (pad.t + ph + 18) + ')">' + esc(labels[x]) + '</text>');
                }
                parts.push('</svg>');
                wrap.innerHTML = parts.join('');
            }
            window.addEventListener('resize', function () { draw(); });
            draw();
        })();

        function applyPreset(value) {
            var form = document.getElementById('analytics-form');
            var select = document.getElementById('preset-select');
            if (!form || !select) return;
            select.value = value;
            form.submit();
        }
        </script>
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)

    return render_template_string(
        html,
        summary=summary,
        session_summary=session_summary,
        series=series,
        host_rows=host_rows,
        account_rows=account_rows,
        viewer_rows=viewer_rows,
        spend_rows=spend_rows,
        chart_payload=chart_payload,
        start_str=start_str,
        end_str=end_str,
        days=days,
        preset=preset,
        presets=ANALYTICS_PRESETS,
        group_by=group_by,
        host_filter=host_filter,
        account_filter=account_filter,
        viewer_filter=viewer_filter,
        host_options=host_options,
        account_options=account_options,
        host_max=host_max,
        account_max=account_max,
        metric=metric,
        chart_type=chart_type,
        fmt_duration=_fmt_duration,
        _link=_link,
    )


def _get_bot_instance():
    """Ambil instance bot Discord dari modul main (proses yang sama dengan Flask)."""
    try:
        import main as bot_module
        return getattr(bot_module, "bot", None)
    except Exception:
        return None


async def _fetch_member_from_guilds(bot, discord_id: int):
    for guild in list(getattr(bot, "guilds", []) or []):
        try:
            member = await guild.fetch_member(discord_id)
            return member, guild
        except Exception:
            continue
    return None, None


def check_discord_verification(discord_id: int, use_api: bool = True) -> dict:
    """Cek apakah member Discord benar-benar punya role verifikasi.

    use_api=True  -> boleh fetch live ke Discord kalau member tidak ada di cache.
    use_api=False -> hanya baca cache bot (cepat, untuk render halaman).
    """
    info = {
        "discord_name": "",
        "discord_handle": "",
        "guild_name": "",
        "has_role": None,
        "role_name": "",
        "error": "",
    }
    try:
        discord_id = int(discord_id)
    except (TypeError, ValueError):
        info["error"] = "Discord ID tidak valid."
        return info

    bot = _get_bot_instance()
    if bot is None or not getattr(bot, "is_ready", lambda: False)():
        info["error"] = "Bot Discord belum siap/berjalan."
        return info

    member = None
    guild = None
    for g in list(getattr(bot, "guilds", []) or []):
        m = g.get_member(discord_id)
        if m:
            member, guild = m, g
            break

    if member is None and use_api:
        loop = getattr(bot, "loop", None)
        if loop and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    _fetch_member_from_guilds(bot, discord_id), loop
                )
                member, guild = future.result(timeout=10)
            except Exception as e:
                info["error"] = f"Gagal cek ke Discord: {e}"
        else:
            info["error"] = "Event loop bot tidak berjalan."

    if member is None:
        # Tetap tampilkan nama Discord kalau user masih ada di cache bot.
        try:
            user = bot.get_user(discord_id)
        except Exception:
            user = None
        if user is not None:
            info["discord_name"] = getattr(user, "display_name", None) or getattr(user, "name", "") or ""
            info["discord_handle"] = str(user)
        if not info["error"]:
            info["error"] = (
                "Member tidak ditemukan di server (mungkin sudah keluar)."
                if use_api else "Belum dicek (member tidak ada di cache bot)."
            )
        return info

    info["discord_name"] = getattr(member, "display_name", None) or getattr(member, "name", "") or ""
    info["discord_handle"] = str(member)
    info["guild_name"] = getattr(guild, "name", "") or ""

    role = None
    try:
        import main as bot_module
        role = bot_module.get_verified_role(guild)
    except Exception as e:
        info["error"] = f"Gagal membaca role verifikasi: {e}"
        return info

    if role is None:
        info["error"] = "Role verifikasi belum diatur di Bot Settings."
        return info

    info["role_name"] = getattr(role, "name", "") or ""
    info["has_role"] = bool(role in member.roles)
    return info


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

    # Isi otomatis baris yang belum pernah dicek, hanya dari cache bot (tanpa API).
    statuses = get_all_verification_status()
    if any(u[0] not in statuses for u in users):
        bot = _get_bot_instance()
        if bot is not None and getattr(bot, "is_ready", lambda: False)():
            for u in users:
                if u[0] in statuses:
                    continue
                info = check_discord_verification(u[0], use_api=False)
                if info.get("discord_name") or info.get("error"):
                    save_verification_status(u[0], **info)
        statuses = get_all_verification_status()

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
                Kolom <strong>Role Status</strong> mengecek langsung ke Discord apakah member benar-benar masih punya role verifikasi.
            </p>
            <form action="/admin/reverify-all" method="POST" onsubmit="return confirm('Verifikasi ulang role semua akun terverifikasi?');" style="margin-bottom: 12px;">
                <button type="submit" class="btn-save" style="background:#0e7490;">🔁 Verifikasi Ulang Semua</button>
            </form>
            <div class="table-scroll">
            <table>
                <tr>
                    <th>Discord ID</th>
                    <th>Discord Name</th>
                    <th>TikTok Username</th>
                    <th>Display Name</th>
                    <th>Total Coins</th>
                    <th>Role Status</th>
                    <th>Action</th>
                </tr>
                {% for u in users %}
                {% set st = statuses.get(u[0]) %}
                <tr>
                    <td><code>{{ u[0] }}</code></td>
                    <td>
                        {% if st and st[1] %}
                        <strong>{{ st[1] }}</strong><br>
                        <span class="muted" style="font-size: 0.78rem;">{{ st[2] }}{% if st[3] %} · {{ st[3] }}{% endif %}</span>
                        {% else %}
                        <span class="muted">-</span>
                        {% endif %}
                    </td>
                    <td><strong>@{{ u[1] }}</strong></td>
                    <td>{{ u[3] or '-' }}</td>
                    <td><span class="badge">{{ u[2] }} c</span></td>
                    <td>
                        {% if not st %}
                        <span class="badge" style="background:#475569;">Belum dicek</span>
                        {% elif st[4] == 1 %}
                        <span class="badge" style="background:#15803d;">✅ Punya role{% if st[5] %} ({{ st[5] }}){% endif %}</span>
                        {% elif st[4] == 0 %}
                        <span class="badge" style="background:#b91c1c;">❌ Belum punya role</span>
                        {% else %}
                        <span class="badge" style="background:#a16207;">⚠️ Tidak bisa dicek</span>
                        {% endif %}
                        {% if st and st[7] %}
                        <br><span class="muted" style="font-size: 0.75rem;">{{ st[7] }}</span>
                        {% endif %}
                        {% if st and st[6] %}
                        <br><span class="muted" style="font-size: 0.72rem;">dicek: {{ st[6] }} WIB</span>
                        {% endif %}
                    </td>
                    <td>
                        <form action="/admin/reverify-user/{{ u[0] }}" method="POST" style="display:inline;">
                            <button type="submit" class="btn-action" style="background:#0e7490;">🔁 Verifikasi Ulang</button>
                        </form>
                        <form action="/admin/unclaim-user/{{ u[0] }}" method="POST" onsubmit="return confirm('Remove the account link for Discord ID {{ u[0] }}?');" style="display:inline; margin-left: 6px;">
                            <button type="submit" class="btn-action btn-del">Unclaim</button>
                        </form>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="7" style="text-align:center; color:#64748b;">No verified accounts yet.</td></tr>
                {% endfor %}
            </table>
            </div>
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
    return render_template_string(html, users=users, pending=pending, verified_role_id=verified_role_id, verification_channel_id=verification_channel_id, statuses=statuses)

@admin_bp.route('/save-verification-settings', methods=['POST'])
def save_verification_settings():
    # Consolidated into /admin/settings to avoid duplicate/conflicting config.
    return redirect('/admin/settings')

@admin_bp.route('/reverify-user/<int:discord_id>', methods=['POST'])
def reverify_user(discord_id):
    info = check_discord_verification(discord_id, use_api=True)
    save_verification_status(discord_id, **info)
    try:
        from db import log_activity_event
        log_activity_event(
            "system",
            f"Admin re-checked verification role for Discord ID {discord_id}: "
            f"has_role={info.get('has_role')} role={info.get('role_name') or '-'} "
            f"error={info.get('error') or '-'}.",
            str(discord_id),
        )
    except Exception:
        pass
    if info.get("has_role") is True:
        msg = f"✅ Member {info.get('discord_name') or discord_id} sudah punya role {info.get('role_name') or ''}.".strip()
        return redirect('/admin/verifications?msg=' + quote_plus(msg) + '&msg_type=success')
    if info.get("has_role") is False:
        msg = f"❌ Member {info.get('discord_name') or discord_id} BELUM punya role {info.get('role_name') or 'verifikasi'}. Link TikTok tetap ada, tapi role tidak ditemukan."
        return redirect('/admin/verifications?msg=' + quote_plus(msg) + '&msg_type=danger')
    msg = f"⚠️ Tidak bisa mengecek {discord_id}: {info.get('error') or 'unknown error'}"
    return redirect('/admin/verifications?msg=' + quote_plus(msg) + '&msg_type=warning')

@admin_bp.route('/reverify-all', methods=['POST'])
def reverify_all():
    try:
        conn = get_db_connection()
        rows = conn.execute("SELECT discord_id FROM user_mappings").fetchall()
        conn.close()
    except Exception as e:
        return redirect('/admin/verifications?msg=' + quote_plus(f'Gagal membaca data verifikasi: {e}') + '&msg_type=danger')

    total = 0
    ok = 0
    missing = 0
    unknown = 0
    for (discord_id,) in rows:
        info = check_discord_verification(discord_id, use_api=True)
        save_verification_status(discord_id, **info)
        total += 1
        if info.get("has_role") is True:
            ok += 1
        elif info.get("has_role") is False:
            missing += 1
        else:
            unknown += 1

    try:
        from db import log_activity_event
        log_activity_event(
            "system",
            f"Admin re-checked verification roles for all verified accounts: total={total}, has_role={ok}, missing={missing}, unchecked={unknown}.",
            "",
        )
    except Exception:
        pass

    msg = f"Verifikasi ulang selesai: {total} akun dicek, {ok} punya role, {missing} belum punya role, {unknown} tidak bisa dicek."
    msg_type = "success" if missing == 0 and unknown == 0 else ("warning" if ok else "danger")
    return redirect('/admin/verifications?msg=' + quote_plus(msg) + '&msg_type=' + msg_type)

@admin_bp.route('/unclaim-user/<int:discord_id>', methods=['POST'])
def unclaim_user_admin(discord_id):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM user_mappings WHERE discord_id = ?", (discord_id,))
        conn.commit()
    finally:
        conn.close()
    delete_verification_status(discord_id)
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

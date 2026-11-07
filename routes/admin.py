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
from styles import BASE_CSS, NAV_HTML, AUTO_REFRESH_JS

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

@admin_bp.route('/data', methods=['GET'])
def view_data():
    html = """
    <!DOCTYPE html>
    <html lang="id">
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
            <h2>📌 Menu Pengelolaan Admin</h2>
            <p style="color: #94a3b8; font-size: 0.95rem; margin-bottom: 16px;">
                Pilih modul di atas untuk mengelola data viewer & koin, memantau sesi live realtime, atau memeriksa log activity.
            </p>
        </div>

        <div class="card" style="border-color: #ef4444;">
            <h2 style="color: #ef4444;">⚠️ Danger Zone</h2>
            <form action="/admin/reset-db" method="POST" onsubmit="return confirm('HAPUS SELURUH DATA GIFT DAN SESI LIVE?')">
                <button type="submit" class="btn-danger">🔥 Reset Data Gift & Sesi Live</button>
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
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Riwayat Shift Host: {{ host_name }}</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>👤 Detail Riwayat Shift: <span style="color: #38bdf8;">{{ host_name }}</span></h1>
            <a href="/admin/schedule" class="nav-btn">⬅ Kembali ke Jadwal</a>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="stat-grid">
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">TOTAL KONSISTENSI SHIFT</div>
                <div class="stat-number">{{ total_sessions }} Sesi</div>
            </div>
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">TOTAL KOIN DIPEROLEH</div>
                <div class="stat-number" style="color: #eab308;">{{ total_coins }} c</div>
            </div>
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">TOTAL DURASI STREAMING</div>
                <div class="stat-number" style="color: #22c55e;">
                    {{ total_duration_sec // 3600 }}j {{ (total_duration_sec % 3600) // 60 }}m
                </div>
            </div>
        </div>

        <div class="card">
            <h2>📜 Histori Sesi Live Host {{ host_name }}</h2>
            <table>
                <tr>
                    <th>Channel (@)</th>
                    <th>Waktu Mulai</th>
                    <th>Waktu Selesai</th>
                    <th>Durasi Live</th>
                    <th>Koin Didapat</th>
                </tr>
                {% for s in sessions %}
                <tr>
                    <td>@{{ s[1] }}</td>
                    <td>{{ s[2] }}</td>
                    <td>{{ s[3] or '🔴 Masih Live' }}</td>
                    <td>
                        {% if s[4] > 0 %}
                            {{ s[4] // 3600 }}j {{ (s[4] % 3600) // 60 }}m {{ s[4] % 60 }}d
                        {% else %}
                            -
                        {% endif %}
                    </td>
                    <td><span class="badge" style="background: #ca8a04;">{{ s[5] }} c</span></td>
                </tr>
                {% else %}
                <tr><td colspan="5">Belum ada data riwayat shift live untuk host ini.</td></tr>
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
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daftar Viewer & Koin</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>👥 Daftar Viewer & Koin</h1>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="card">
            <h2>Daftar Viewer</h2>
            <table>
                <tr>
                    <th>Username (@)</th>
                    <th>Nama Viewer</th>
                    <th>Total Koin</th>
                    <th>Status Live Realtime</th>
                    <th>Edit Koin</th>
                </tr>
                {% for v in viewers %}
                <tr>
                    <td>
                        <a href="/admin/viewer/{{ v[0] }}" class="viewer-link" title="Klik untuk lihat detail perolehan koin">@{{ v[0] }} ↗</a>
                    </td>
                    <td>
                        <a href="/admin/viewer/{{ v[0] }}" class="viewer-link" title="Klik untuk lihat detail perolehan koin">{{ v[1] or v[0] }} ↗</a>
                    </td>
                    <td><span class="badge" style="background: #ca8a04;">{{ v[2] }} c</span></td>
                    <td>
                        {% if v[3] %}
                        <a href="https://www.tiktok.com/@{{ v[3] }}/live" target="_blank" class="badge badge-active">🔴 Sedang Live ↗</a>
                        {% else %}
                        <span class="badge badge-inactive">Offline</span>
                        {% endif %}
                    </td>
                    <td>
                        <form action="/admin/edit-coin" method="POST" class="edit-form">
                            <input type="hidden" name="tiktok_user" value="{{ v[0] }}">
                            <input type="number" name="new_coins" value="{{ v[2] }}" required>
                            <button type="submit">Simpan</button>
                        </form>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="5">Belum ada data viewer tercatat.</td></tr>
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
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Detail Viewer: @{{ username }}</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>👤 Detail Viewer: <span style="color: #38bdf8;">{{ display_name }}</span> (@{{ username }})</h1>
            <a href="/admin/viewers" class="nav-btn">⬅ Kembali ke Daftar Viewer</a>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="stat-grid">
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">TOTAL KOIN KESELURUHAN</div>
                <div class="stat-number" style="color: #eab308;">{{ grand_total_coins }} c</div>
            </div>
            <div class="stat-box">
                <div style="color: #94a3b8; font-size: 0.85rem;">KOIN HARI INI ({{ today_str }})</div>
                <div class="stat-number" style="color: #22c55e;">{{ today_coins }} c</div>
            </div>
        </div>

        <div class="card">
            <h2>📅 Riwayat Koin Per Hari</h2>
            <table>
                <tr>
                    <th>Tanggal</th>
                    <th>Jumlah Koin</th>
                </tr>
                {% for d in daily_history %}
                <tr>
                    <td><strong>{{ d[0] }}</strong></td>
                    <td><span class="badge" style="background: #ca8a04;">{{ d[1] }} c</span></td>
                </tr>
                {% else %}
                <tr><td colspan="2">Belum ada catatan koin harian.</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>🎁 Log Kiriman Gift Terbaru</h2>
            <table>
                <tr>
                    <th>Tanggal & Waktu</th>
                    <th>Nama Gift</th>
                    <th>Jumlah</th>
                    <th>Koin</th>
                    <th>Akun Target Spent</th>
                    <th>Host Terdeteksi</th>
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
                            <a href="/admin/host/{{ g[6] }}" class="host-link" title="Klik untuk lihat riwayat shift">{{ g[6] }} ↗</a>
                        {% else %}
                            <strong>{{ g[6] }}</strong>
                        {% endif %}
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="6">Belum ada histori gift tercatat.</td></tr>
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
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        hosts = cur.execute("""
            SELECT 
                h.host_id, 
                h.tiktok_username, 
                h.display_name,
                h.is_active,
                CASE 
                    WHEN a.tiktok_username IS NOT NULL OR s.session_id IS NOT NULL THEN 1 
                    ELSE 0 
                END as is_currently_live
            FROM hosts h
            LEFT JOIN active_live_announcements a ON LOWER(h.tiktok_username) = LOWER(a.tiktok_username)
            LEFT JOIN live_sessions s ON LOWER(h.tiktok_username) = LOWER(s.tiktok_username) AND s.end_time IS NULL
            ORDER BY h.host_id ASC
        """).fetchall()

        sessions = cur.execute("""
            SELECT 
                s.session_id, 
                s.tiktok_username, 
                s.start_time, 
                s.end_time, 
                s.duration_seconds, 
                s.total_coins, 
                s.scheduled_host,
                CASE 
                    WHEN a.tiktok_username IS NOT NULL OR s.end_time IS NULL THEN 1 
                    ELSE 0 
                END as is_live_now
            FROM live_sessions s
            LEFT JOIN active_live_announcements a ON LOWER(s.tiktok_username) = LOWER(a.tiktok_username)
            ORDER BY s.start_time DESC
        """).fetchall()

        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    html = """
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Monitor Live Realtime</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>📹 Monitor Live Realtime</h1>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="card">
            <h2>🎙️ Akun Target TikTok</h2>
            <div style="display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap;">
                <form action="/admin/sync-hosts" method="POST" onsubmit="return confirm('Merapikan dan sinkronisasi 6 akun target resmi dengan ID 1-6?');" style="margin: 0;">
                    <button type="submit" style="background: #15803d; padding: 8px 14px;">🔄 Rapikan & Sinkron 6 Akun Resmi (ID 1-6)</button>
                </form>
            </div>
            <form action="/admin/add-host" method="POST" class="form-grid">
                <input type="text" name="tiktok_username" placeholder="Username TikTok (@...)" required>
                <input type="text" name="display_name" placeholder="Nama Channel (misal: George Vilora)" required>
                <button type="submit">+ Tambah Target Host</button>
            </form>
            <br>
            <table>
                <tr><th>ID</th><th>Username TikTok</th><th>Nama Channel</th><th>Status Realtime</th><th>Aksi</th></tr>
                {% for h in hosts %}
                <tr>
                    <td>{{ h[0] }}</td>
                    <td>@{{ h[1] }}</td>
                    <td>{{ h[2] or h[1] }}</td>
                    <td>
                        {% if h[4] == 1 %}
                        <a href="https://www.tiktok.com/@{{ h[1] }}/live" target="_blank" class="badge badge-active" title="Klik untuk nonton Live">🔴 Sedang Live ↗</a>
                        {% else %}
                        <span class="badge badge-inactive">Offline</span>
                        {% endif %}
                    </td>
                    <td>
                        <a href="/admin/toggle-host/{{ h[0] }}" class="btn-action btn-toggle">Ubah Status Active</a>
                        <a href="/admin/delete-host/{{ h[0] }}" class="btn-action btn-del" onclick="return confirm('Hapus target ini?')">Hapus</a>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="5">Belum ada target terdaftar.</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>📹 Riwayat Sesi Live</h2>
            <table>
                <tr>
                    <th>Username (@)</th>
                    <th>Mulai</th>
                    <th>Selesai</th>
                    <th>Durasi</th>
                    <th>Koin</th>
                    <th>Host Terdeteksi</th>
                </tr>
                {% for s in sessions %}
                <tr>
                    <td>
                        <a href="https://www.tiktok.com/@{{ s[1] }}" target="_blank" class="viewer-link">@{{ s[1] }} ↗</a>
                    </td>
                    <td>{{ s[2] }}</td>
                    <td>
                        {% if s[3] %}
                            {{ s[3] }}
                        {% elif s[7] == 1 %}
                            <span class="badge badge-active">🔴 Sedang Live</span>
                        {% else %}
                            <span style="color: #94a3b8; font-style: italic;">Tidak ada data</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if s[4] > 0 %}
                            {{ s[4] // 3600 }}j {{ (s[4] % 3600) // 60 }}m {{ s[4] % 60 }}d
                        {% else %}
                            -
                        {% endif %}
                    </td>
                    <td><span class="badge" style="background: #ca8a04;">{{ s[5] }} c</span></td>
                    <td>
                        {% if s[6] and s[6] != '-' and s[6] != 'Host' %}
                            <a href="/admin/host/{{ s[6] }}" class="host-link" title="Klik untuk lihat riwayat shift">{{ s[6] }} ↗</a>
                        {% else %}
                            <strong>{{ s[6] or '-' }}</strong>
                        {% endif %}
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="6">Belum ada histori sesi live.</td></tr>
                {% endfor %}
            </table>
        </div>
        AUTO_REFRESH_JS_PLACEHOLDER
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)
    return render_template_string(html, hosts=hosts, sessions=sessions)

# Handlers Form Admin
@admin_bp.route('/add-host', methods=['POST'])
def add_host():
    username = request.form.get("tiktok_username", "").strip().lower().lstrip("@")
    display_name = request.form.get("display_name", username).strip()
    if username:
        conn = get_db_connection()
        conn.execute("INSERT OR REPLACE INTO hosts (tiktok_username, display_name, is_active) VALUES (?, ?, 1)", (username, display_name))
        conn.commit()
        conn.close()
    return redirect('/admin/monitor')

@admin_bp.route('/toggle-host/<int:host_id>')
def toggle_host(host_id):
    conn = get_db_connection()
    conn.execute("UPDATE hosts SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE host_id = ?", (host_id,))
    conn.commit()
    conn.close()
    return redirect('/admin/monitor')

@admin_bp.route('/delete-host/<int:host_id>')
def delete_host(host_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM hosts WHERE host_id = ?", (host_id,))
    conn.commit()
    conn.close()
    return redirect('/admin/monitor')

@admin_bp.route('/edit-coin', methods=['POST'])
def edit_coin():
    username = request.form.get("tiktok_user", "").strip().lower().lstrip("@")
    new_coins = int(request.form.get("new_coins", 0))
    if username:
        conn = get_db_connection()
        conn.execute("UPDATE tiktok_gifts SET total_coins = ? WHERE LOWER(tiktok_user) = ?", (new_coins, username))
        conn.commit()
        conn.close()
    return redirect('/admin/viewers')

@admin_bp.route('/sync-hosts', methods=['POST'])
def sync_hosts_action():
    sync_valid_hosts()
    return redirect('/admin/monitor')

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
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Log Activity Realtime</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>📊 Log Activity Realtime</h1>
            <div style="display: flex; gap: 10px; align-items: center;">
                <form action="/admin/clear-activity-logs" method="POST" onsubmit="return confirm('Bersihkan seluruh log activity?');" style="margin: 0;">
                    <button type="submit" class="btn-danger" style="padding: 6px 12px; font-size: 0.8rem;">🗑️ Bersihkan Log</button>
                </form>
            </div>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="card" style="margin-bottom: 16px; padding: 14px 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                <form method="GET" action="/admin/activity" style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
                    <label for="channel_filter" style="font-size: 0.875rem; color: #94a3b8; font-weight: 500;">Filter Akun Channel:</label>
                    <select name="channel" id="channel_filter" onchange="this.form.submit()" style="padding: 6px 10px; min-width: 240px;">
                        <option value="" {% if not current_filter %}selected{% endif %}>🌐 Semua Akun (6 Target Resmi)</option>
                        {% for h in all_hosts %}
                        <option value="{{ h }}" {% if current_filter == h %}selected{% endif %}>@{{ h }}</option>
                        {% endfor %}
                    </select>
                    {% if current_filter %}
                    <a href="/admin/activity" class="btn-action" style="background: #334155; color: white;">Reset Filter</a>
                    {% endif %}
                </form>
                <div style="color: #94a3b8; font-size: 0.85rem;">
                    Menampilkan <strong>{{ gift_activities|length }}</strong> gift & <strong>{{ logs|length }}</strong> baris log feed
                </div>
            </div>
        </div>

        <!-- TABEL AKTIVITAS GIFT TERBARU (Terstruktur Sesuai Permintaan) -->
        <div class="card">
            <h2>🎁 Aktivitas Gift Masuk Terbaru</h2>
            <p style="color: #94a3b8; font-size: 0.85rem; margin-bottom: 14px;">
                Memuat riwayat gift realtime: Tanggal & Waktu, Nama Akun Target, User/Donatur, Nama Hadiah, Jumlah Koin, dan Total Koin Pengguna Saat Ini.
            </p>
            <table>
                <tr>
                    <th style="width: 50px;">NO</th>
                    <th>TANGGAL & WAKTU (WIB)</th>
                    <th>NAMA AKUN (CHANNEL)</th>
                    <th>USER (DONATUR)</th>
                    <th>NAMA GIFT</th>
                    <th>JUMLAH KOIN</th>
                    <th>TOTAL KOIN SAAT INI</th>
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
                        Belum ada aktivitas gift yang tercatat untuk filter ini.
                    </td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <!-- LIVE STREAM FEED TERMINAL -->
        <div class="card">
            <h2>📜 Live Activity Feed (Konsol Stream)</h2>
            <div class="terminal-container" id="terminal-feed">
                {% for log in logs %}
                <div class="terminal-row">
                    <span style="color: #64748b; font-size: 0.75rem; min-width: 140px;">[{{ log[4] }}]</span>
                    {% if log[1] == 'connect' %}
                        <span class="log-tag-connect">CONNECT</span>
                        <span style="color: #4ade80; font-weight: 600;">{{ log[3] }}</span>
                    {% elif log[1] == 'disconnect' %}
                        <span class="log-tag-disconnect">DISCONNECT</span>
                        <span style="color: #f87171; font-weight: 600;">{{ log[3] }}</span>
                    {% elif log[1] == 'host_detected' %}
                        <span class="log-tag-gift" style="color: #38bdf8; background: rgba(56, 189, 248, 0.1); padding: 2px 6px; border-radius: 4px;">HOST</span>
                        <span style="color: #38bdf8; font-weight: 600;">{{ log[3] }}</span>
                    {% else %}
                        <span class="log-tag-gift">🎁 GIFT</span>
                        <span style="color: #f8fafc;">{{ log[3] }}</span>
                    {% endif %}
                </div>
                {% else %}
                <div style="color: #64748b; font-style: italic; padding: 24px; text-align: center;">
                    Belum ada aktivitas yang tercatat. Sistem standby memantau 6 akun target resmi.
                </div>
                {% endfor %}
            </div>
        </div>

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
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Kelola Verifikasi Akun TikTok</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>🔐 Pengelolaan Verifikasi Akun TikTok & Discord</h1>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="card">
            <h2>⚙️ Konfigurasi Role & Channel Verifikasi</h2>
            <form action="/admin/save-verification-settings" method="POST" class="form-grid" style="grid-template-columns: 1fr 1fr auto; align-items: end;">
                <div>
                    <label style="font-size: 0.8rem; color: #94a3b8;">Verified Role ID (Discord Role ID):</label>
                    <input type="text" name="verified_role_id" value="{{ verified_role_id }}" placeholder="Contoh: 1554883561759187105" required>
                </div>
                <div>
                    <label style="font-size: 0.8rem; color: #94a3b8;">Verification Channel ID:</label>
                    <input type="text" name="verification_channel_id" value="{{ verification_channel_id }}" placeholder="Contoh: 123456789012345678">
                </div>
                <button type="submit">💾 Simpan Konfigurasi</button>
            </form>
        </div>

        <div class="card">
            <h2>✅ Akun Terverifikasi (Discord &lt;-&gt; TikTok)</h2>
            <p style="color: #94a3b8; font-size: 0.85rem; margin-bottom: 12px;">
                Daftar akun Discord yang telah sukses di-binding ke akun TikTok. Satu akun hanya dapat diklaim oleh satu pengguna.
            </p>
            <table>
                <tr>
                    <th>Discord ID</th>
                    <th>TikTok Username</th>
                    <th>Display Name</th>
                    <th>Total Koin</th>
                    <th>Aksi</th>
                </tr>
                {% for u in users %}
                <tr>
                    <td><code>{{ u[0] }}</code></td>
                    <td><strong>@{{ u[1] }}</strong></td>
                    <td>{{ u[3] or '-' }}</td>
                    <td><span class="badge">{{ u[2] }} c</span></td>
                    <td>
                        <form action="/admin/unclaim-user/{{ u[0] }}" method="POST" onsubmit="return confirm('Batalkan ikatan akun untuk Discord ID {{ u[0] }}?');" style="display:inline;">
                            <button type="submit" class="btn-action btn-del">Batal Claim</button>
                        </form>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="5" style="text-align:center; color:#64748b;">Belum ada akun yang terverifikasi.</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>⏳ Verifikasi Pending (Sedang Berjalan)</h2>
            <table>
                <tr>
                    <th>Discord ID</th>
                    <th>TikTok Username</th>
                    <th>Kode Verifikasi</th>
                    <th>Metode</th>
                    <th>Waktu Dibuat</th>
                    <th>Batas Waktu (WIB)</th>
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
                <tr><td colspan="6" style="text-align:center; color:#64748b;">Tidak ada verifikasi yang sedang pending saat ini.</td></tr>
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
    v_role = request.form.get('verified_role_id', '').strip()
    v_chan = request.form.get('verification_channel_id', '').strip()
    if v_role:
        set_setting('verified_role_id', v_role)
    if v_chan:
        set_setting('verification_channel_id', v_chan)
    return redirect('/admin/verifications')

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
    return redirect('/admin/activity')

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
    return redirect('/admin/data')

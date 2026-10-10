from flask import Flask, render_template_string, request, jsonify, redirect
from threading import Thread
import os
import json
import libsql_experimental as libsql

app = Flask('')

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

async_announcement_handler = None

BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 30px; background: #0f172a; color: #f8fafc; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid #334155; padding-bottom: 16px; }
h1 { font-size: 1.5rem; color: #f8fafc; }
.nav-links { display: flex; gap: 10px; margin-bottom: 24px; flex-wrap: wrap; }
.nav-btn { background: #1e293b; color: #38bdf8; border: 1px solid #334155; text-decoration: none; padding: 8px 16px; border-radius: 8px; font-size: 0.875rem; font-weight: 600; transition: all 0.2s; }
.nav-btn:hover, .nav-btn.active { background: #0284c7; color: white; border-color: #0284c7; }
.card { background: #1e293b; border-radius: 10px; border: 1px solid #334155; padding: 20px; margin-bottom: 24px; }
h2 { color: #38bdf8; font-size: 1.1rem; margin-bottom: 16px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 10px; }
th, td { border-bottom: 1px solid #334155; padding: 10px 14px; text-align: left; font-size: 0.875rem; }
th { background-color: #0284c7; color: white; text-transform: uppercase; font-size: 0.75rem; }
tr:nth-child(even) { background-color: #162032; }
.badge { background: #0369a1; color: #e0f2fe; padding: 3px 8px; border-radius: 4px; font-weight: 600; }
.badge-active { background: #15803d; color: #dcfce7; }
.badge-inactive { background: #475569; color: #cbd5e1; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-top: 10px; }
input, select, button { padding: 8px 12px; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: white; font-size: 0.875rem; }
button { background: #0284c7; cursor: pointer; font-weight: 600; border: none; }
button:hover { background: #0369a1; }
.btn-danger { background: #ef4444; }
.btn-action { padding: 4px 8px; font-size: 0.75rem; text-decoration: none; border-radius: 4px; display: inline-block; }
.btn-toggle { background: #ca8a04; color: white; }
.btn-del { background: #dc2626; color: white; }
.edit-form { display: flex; gap: 6px; }
.edit-form input { width: 90px; padding: 4px 6px; }
.edit-form button { padding: 4px 8px; font-size: 0.75rem; }
"""

NAV_HTML = """
<div class="nav-links">
    <a href="/admin/data" class="nav-btn">🏠 Main Menu</a>
    <a href="/admin/schedule" class="nav-btn">📅 Jadwal Shift</a>
    <a href="/admin/viewers" class="nav-btn">👥 Viewer & Koin</a>
    <a href="/admin/monitor" class="nav-btn">📹 Monitor Live</a>
</div>
"""

@app.route('/')
def home():
    return redirect('/admin/data')

# 1. Dashboard Navigasi Utama
@app.route('/admin/data', methods=['GET'])
def view_data():
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Database Admin Panel</title>
        <style>{BASE_CSS}</style>
    </head>
    <body>
        <div class="header">
            <h1>🛡️ Database Admin Panel</h1>
        </div>
        {NAV_HTML}
        
        <div class="card">
            <h2>📌 Menu Pengelolaan Admin</h2>
            <p style="color: #94a3b8; font-size: 0.95rem; margin-bottom: 16px;">
                Pilih modul di atas untuk mengelola jadwal shift, data viewer, atau memantau sesi live realtime.
            </p>
        </div>

        <div class="card" style="border-color: #ef4444;">
            <h2 style="color: #ef4444;">⚠️ Danger Zone</h2>
            <form action="/admin/reset-db" method="POST" onsubmit="return confirm('HAPUS SELURUH DATA GIFT DAN SESI LIVE?')">
                <button type="submit" class="btn-danger">🔥 Reset Data Gift & Sesi Live</button>
            </form>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

# 2. Halaman Jadwal Shift Host
@app.route('/admin/schedule', methods=['GET'])
def view_schedule():
    try:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        cur = conn.cursor()
        schedules = cur.execute("""
            SELECT tiktok_username, account_name, host_name, start_hour, end_hour 
            FROM host_schedules 
            ORDER BY tiktok_username, start_hour
        """).fetchall()
        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Jadwal Shift Host</title>
        <style>{BASE_CSS}</style>
    </head>
    <body>
        <div class="header">
            <h1>📅 Jadwal Shift Host</h1>
        </div>
        {NAV_HTML}

        <div class="card">
            <h2>+ Tambah / Update Shift Host</h2>
            <form action="/admin/add-schedule" method="POST" class="form-grid">
                <input type="text" name="tiktok_username" placeholder="Username TikTok (@...)" required>
                <input type="text" name="account_name" placeholder="Nama Channel" required>
                <input type="text" name="host_name" placeholder="Nama Host (misal: SOLEN)" required>
                <input type="number" name="start_hour" placeholder="Jam Mulai (0-23)" min="0" max="23" required>
                <input type="number" name="end_hour" placeholder="Jam Selesai (0-23)" min="0" max="23" required>
                <button type="submit">Simpan Shift</button>
            </form>
            <br>
            <table>
                <tr>
                    <th>Username Target</th>
                    <th>Nama Channel</th>
                    <th>Nama Host Shift</th>
                    <th>Jam Shift</th>
                </tr>
                {{% for sc in schedules %}}
                <tr>
                    <td>@{{ sc[0] }}</td>
                    <td>@{{ sc[0] }}</td>
                    <td><strong>{{ sc[2] }}</strong></td>
                    <td>{{ "%02d:00"|format(sc[3]) }} - {{ "%02d:00"|format(sc[4]) }} WIB</td>
                </tr>
                {{% else %}}
                <tr><td colspan="4">Belum ada data jadwal shift host.</td></tr>
                {{% endfor %}}
            </table>
        </div>
    </body>
    </html>
    """
    return render_template_string(html, schedules=schedules)

# 3. Halaman Viewer & Edit Koin
@app.route('/admin/viewers', methods=['GET'])
def view_viewers():
    try:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        cur = conn.cursor()
        viewers = cur.execute("""
            SELECT 
                g.tiktok_user, 
                g.display_name, 
                g.total_coins,
                a.tiktok_username as active_channel
            FROM tiktok_gifts g
            LEFT JOIN active_live_announcements a ON g.tiktok_user = a.tiktok_username
            ORDER BY g.total_coins DESC
        """).fetchall()
        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daftar Viewer & Koin</title>
        <style>{BASE_CSS}</style>
    </head>
    <body>
        <div class="header">
            <h1>👥 Daftar Viewer & Koin</h1>
        </div>
        {NAV_HTML}

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
                {{% for v in viewers %}}
                <tr>
                    <td>@{{ v[0] }}</td>
                    <td>{{ v[1] or '-' }}</td>
                    <td><span class="badge">{{ v[2] }} c</span></td>
                    <td>
                        {{% if v[3] %}}
                        <span class="badge badge-active">🔴 Live di @{{ v[3] }}</span>
                        {{% else %}}
                        <span class="badge badge-inactive">Offline</span>
                        {{% endif %}}
                    </td>
                    <td>
                        <form action="/admin/edit-coin" method="POST" class="edit-form">
                            <input type="hidden" name="tiktok_user" value="{{ v[0] }}">
                            <input type="number" name="new_coins" value="{{ v[2] }}" required>
                            <button type="submit">Simpan</button>
                        </form>
                    </td>
                </tr>
                {{% else %}}
                <tr><td colspan="5">Belum ada data viewer tercatat.</td></tr>
                {{% endfor %}}
            </table>
        </div>
    </body>
    </html>
    """
    return render_template_string(html, viewers=viewers)

# 4. Halaman Monitoring Sesi Live & Target Host
@app.route('/admin/monitor', methods=['GET'])
def view_monitor():
    try:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        cur = conn.cursor()

        hosts = cur.execute("""
            SELECT 
                h.host_id, 
                h.tiktok_username, 
                h.is_active,
                CASE WHEN a.tiktok_username IS NOT NULL THEN 1 ELSE 0 END as is_currently_live
            FROM hosts h
            LEFT JOIN active_live_announcements a ON h.tiktok_username = a.tiktok_username
            ORDER BY h.host_id ASC
        """).fetchall()

        sessions = cur.execute("""
            SELECT session_id, tiktok_username, start_time, end_time, duration_seconds, total_coins, scheduled_host 
            FROM live_sessions 
            ORDER BY start_time DESC
        """).fetchall()

        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Monitor Live Realtime</title>
        <style>{BASE_CSS}</style>
    </head>
    <body>
        <div class="header">
            <h1>📹 Monitor Live Realtime</h1>
        </div>
        {NAV_HTML}

        <!-- Target Host -->
        <div class="card">
            <h2>🎙️ Akun Target TikTok</h2>
            <form action="/admin/add-host" method="POST" class="form-grid">
                <input type="text" name="tiktok_username" placeholder="Username TikTok (@...)" required>
                <button type="submit">+ Tambah Target Host</button>
            </form>
            <br>
            <table>
                <tr><th>ID</th><th>Username TikTok</th><th>Nama Channel</th><th>Status Realtime</th><th>Aksi</th></tr>
                {{% for h in hosts %}}
                <tr>
                    <td>{{ h[0] }}</td>
                    <td>@{{ h[1] }}</td>
                    <td>@{{ h[1] }}</td>
                    <td>
                        {{% if h[3] == 1 %}}
                        <span class="badge badge-active">🔴 Sedang Live</span>
                        {{% else %}}
                        <span class="badge badge-inactive">Offline</span>
                        {{% endif %}}
                    </td>
                    <td>
                        <a href="/admin/toggle-host/{{ h[0] }}" class="btn-action btn-toggle">Ubah Status Active</a>
                        <a href="/admin/delete-host/{{ h[0] }}" class="btn-action btn-del" onclick="return confirm('Hapus target ini?')">Hapus</a>
                    </td>
                </tr>
                {{% else %}}
                <tr><td colspan="5">Belum ada target terdaftar.</td></tr>
                {{% endfor %}}
            </table>
        </div>

        <!-- Sesi Live -->
        <div class="card">
            <h2>📹 Riwayat Sesi Live</h2>
            <table>
                <tr>
                    <th>Username (@)</th>
                    <th>Mulai</th>
                    <th>Selesai</th>
                    <th>Durasi</th>
                    <th>Koin</th>
                    <th>Host Terjadwal</th>
                </tr>
                {{% for s in sessions %}}
                <tr>
                    <td>@{{ s[1] }}</td>
                    <td>{{ s[2] }}</td>
                    <td>{{ s[3] or '🔴 Sedang Live' }}</td>
                    <td>
                        {{% if s[4] > 0 %}}
                        {{ s[4] // 3600 }}j {{ (s[4] % 3600) // 60 }}m {{ s[4] % 60 }}d
                        {{% else %}}
                        -
                        {{% endif %}}
                    </td>
                    <td><span class="badge">{{ s[5] }} c</span></td>
                    <td><strong>{{ s[6] or '-' }}</strong></td>
                </tr>
                {{% else %}}
                <tr><td colspan="6">Belum ada histori sesi live.</td></tr>
                {{% endfor %}}
            </table>
        </div>
    </body>
    </html>
    """
    return render_template_string(html, hosts=hosts, sessions=sessions)

# --- Action Handlers ---

@app.route('/admin/add-host', methods=['POST'])
def add_host():
    username = request.form.get("tiktok_username", "").strip().lower().lstrip("@")
    if username:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        conn.execute("INSERT OR REPLACE INTO hosts (tiktok_username, display_name, is_active) VALUES (?, ?, 1)", (username, username))
        conn.commit()
        conn.close()
    return redirect('/admin/monitor')

@app.route('/admin/toggle-host/<int:host_id>')
def toggle_host(host_id):
    conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    conn.execute("UPDATE hosts SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE host_id = ?", (host_id,))
    conn.commit()
    conn.close()
    return redirect('/admin/monitor')

@app.route('/admin/delete-host/<int:host_id>')
def delete_host(host_id):
    conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    conn.execute("DELETE FROM hosts WHERE host_id = ?", (host_id,))
    conn.commit()
    conn.close()
    return redirect('/admin/monitor')

@app.route('/admin/edit-coin', methods=['POST'])
def edit_coin():
    username = request.form.get("tiktok_user", "").strip().lower().lstrip("@")
    new_coins = int(request.form.get("new_coins", 0))
    if username:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        conn.execute("UPDATE tiktok_gifts SET total_coins = ? WHERE tiktok_user = ?", (new_coins, username))
        conn.commit()
        conn.close()
    return redirect('/admin/viewers')

@app.route('/admin/add-schedule', methods=['POST'])
def add_schedule():
    username = request.form.get("tiktok_username", "").strip().lower().lstrip("@")
    acc_name = request.form.get("account_name", username).strip()
    host_name = request.form.get("host_name", "").strip().upper()
    try:
        start_h = int(request.form.get("start_hour", 0))
        end_h = int(request.form.get("end_hour", 0))
    except ValueError:
        return redirect('/admin/schedule')

    if username and host_name:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        conn.execute("""
            INSERT OR REPLACE INTO host_schedules (tiktok_username, account_name, host_name, start_hour, end_hour)
            VALUES (?, ?, ?, ?, ?)
        """, (username, acc_name, host_name, start_h, end_h))
        conn.commit()
        conn.close()
    return redirect('/admin/schedule')

@app.route('/admin/reset-db', methods=['POST'])
def reset_db():
    conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    conn.execute("DELETE FROM gift_logs")
    conn.execute("DELETE FROM live_sessions")
    conn.execute("DELETE FROM tiktok_gifts")
    conn.execute("DELETE FROM daily_tiktok_gifts")
    conn.commit()
    conn.close()
    return redirect('/admin/data')

# API Config & Heartbeat Handlers
@app.route('/api/config', methods=['GET'])
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

@app.route('/api/heartbeat', methods=['POST'])
def handle_heartbeat():
    secret_key = request.headers.get("X-API-KEY")
    expected_key = os.environ.get("API_SECRET_KEY")

    if expected_key and secret_key != expected_key:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    data = request.get_json() or {}
    username = data.get("username", "unknown")
    print(f"[API] Received heartbeat from worker for @{username}")
    return jsonify({"status": "ok"}), 200

@app.route('/api/announcement', methods=['POST'])
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

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, threaded=True)

def keep_alive(handler_callback=None):
    global async_announcement_handler
    async_announcement_handler = handler_callback
    t = Thread(target=run, daemon=True)
    t.start()

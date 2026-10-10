from flask import Flask, render_template_string, request, jsonify
from threading import Thread
import os
import json
import libsql_experimental as libsql

app = Flask('')

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

async_announcement_handler = None

@app.route('/')
def home():
    html = """
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>DummyDreams Bot Dashboard</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f172a; color: #f8fafc; min-height: 100vh; display: flex; justify-content: center; align-items: center; padding: 20px; }
            .container { background: #1e293b; border-radius: 16px; border: 1px solid #334155; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5); width: 100%; max-width: 550px; padding: 32px; text-align: center; }
            .status-badge { display: inline-flex; align-items: center; gap: 8px; background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.3); color: #4ade80; padding: 6px 16px; border-radius: 9999px; font-size: 0.875rem; font-weight: 600; margin-bottom: 20px; }
            .pulse { width: 8px; height: 8px; background-color: #22c55e; border-radius: 50%; box-shadow: 0 0 10px #22c55e; animation: pulse-animation 2s infinite; }
            @keyframes pulse-animation { 0% { box-shadow: 0 0 0 0px rgba(34, 197, 94, 0.7); } 100% { box-shadow: 0 0 0 10px rgba(34, 197, 94, 0); } }
            h1 { font-size: 1.875rem; font-weight: 700; color: #f8fafc; margin-bottom: 10px; }
            p.desc { color: #94a3b8; font-size: 0.95rem; line-height: 1.5; margin-bottom: 28px; }
            .btn { display: inline-block; background: linear-gradient(135deg, #0284c7, #0369a1); color: #ffffff; text-decoration: none; padding: 12px 24px; border-radius: 10px; font-weight: 600; font-size: 0.95rem; transition: all 0.2s ease; width: 100%; box-shadow: 0 4px 6px -1px rgba(2, 132, 199, 0.3); }
            .btn:hover { background: linear-gradient(135deg, #0369a1, #075985); transform: translateY(-1px); }
            .info-box { margin-top: 28px; padding-top: 20px; border-top: 1px solid #334155; display: grid; grid-template-columns: 1fr 1fr; gap: 12px; text-align: left; }
            .info-item { background: #0f172a; padding: 12px; border-radius: 8px; border: 1px solid #1e293b; }
            .info-label { font-size: 0.75rem; color: #64748b; font-weight: 600; text-transform: uppercase; }
            .info-val { font-size: 0.875rem; color: #cbd5e1; font-weight: 500; margin-top: 2px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="status-badge">
                <span class="pulse"></span> Server Active 24/7
            </div>
            <h1>DummyDreams Bot</h1>
            <p class="desc">Discord Bot & TikTok Live Observer Service sedang berjalan normal di latar belakang.</p>
            <a href="/admin/data" class="btn">🛡️ Buka Database Admin Panel</a>
            
            <div class="info-box">
                <div class="info-item">
                    <div class="info-label">Environment</div>
                    <div class="info-val">Production (Render)</div>
                </div>
                <div class="info-item">
                    <div class="info-label">Database</div>
                    <div class="info-val">Turso (LibSQL)</div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html)

@app.route('/admin/data', methods=['GET'])
def view_data():
    try:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        cur = conn.cursor()

        gifts = cur.execute("SELECT tiktok_user, display_name, total_coins FROM tiktok_gifts ORDER BY total_coins DESC").fetchall()
        users = cur.execute("SELECT discord_id, tiktok_user FROM user_mappings").fetchall()
        announcements = cur.execute("SELECT tiktok_username, channel_id, message_id FROM active_live_announcements").fetchall()

        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    html = """
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Turso DB Admin Panel</title>
        <style>
            * { box-sizing: border-box; margin: 0; padding: 0; }
            body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 30px; background: #0f172a; color: #f8fafc; }
            .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; border-bottom: 1px solid #334155; padding-bottom: 20px; }
            h1 { font-size: 1.5rem; color: #f8fafc; display: flex; align-items: center; gap: 10px; }
            .btn-back { background: #334155; color: #f8fafc; text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 0.875rem; font-weight: 500; transition: background 0.2s; }
            .btn-back:hover { background: #475569; }
            h2 { color: #38bdf8; font-size: 1.1rem; padding-bottom: 8px; margin-top: 30px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
            table { border-collapse: collapse; width: 100%; margin-bottom: 20px; background: #1e293b; border-radius: 10px; overflow: hidden; border: 1px solid #334155; }
            th, td { border-bottom: 1px solid #334155; padding: 12px 16px; text-align: left; font-size: 0.9rem; }
            th { background-color: #0284c7; color: white; font-weight: 600; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em; }
            tr:last-child td { border-bottom: none; }
            tr:nth-child(even) { background-color: #162032; }
            tr:hover { background-color: #26354a; }
            .badge { background: #0369a1; color: #e0f2fe; padding: 4px 10px; border-radius: 6px; font-weight: 600; font-size: 0.85rem; }
            .empty { color: #64748b; font-style: italic; text-align: center; }
        </style>
    </head>
    <body>
        <div class="header">
            <h1>🛡️ Turso DB Admin Panel</h1>
            <a href="/" class="btn-back">← Kembali ke Utama</a>
        </div>

        <h2>📊 Supporter Coins (tiktok_gifts)</h2>
        <table>
            <tr><th>TikTok Username</th><th>Display Name</th><th>Total Coins</th></tr>
            {% for row in gifts %}
            <tr><td>@{{ row[0] }}</td><td>{{ row[1] or '-' }}</td><td><span class="badge">{{ row[2] }} c</span></td></tr>
            {% else %}
            <tr><td colspan="3" class="empty">Belum ada data koin.</td></tr>
            {% endfor %}
        </table>

        <h2>🔗 Discord User Mappings (user_mappings)</h2>
        <table>
            <tr><th>Discord ID</th><th>TikTok Username</th></tr>
            {% for row in users %}
            <tr><td>{{ row[0] }}</td><td>@{{ row[1] }}</td></tr>
            {% else %}
            <tr><td colspan="2" class="empty">Belum ada data klaim akun.</td></tr>
            {% endfor %}
        </table>

        <h2>📢 Active Announcements (active_live_announcements)</h2>
        <table>
            <tr><th>TikTok Username</th><th>Channel ID</th><th>Message ID</th></tr>
            {% for row in announcements %}
            <tr><td>@{{ row[0] }}</td><td>{{ row[1] }}</td><td>{{ row[2] }}</td></tr>
            {% else %}
            <tr><td colspan="3" class="empty">Belum ada pengumuman aktif.</td></tr>
            {% endfor %}
        </table>
    </body>
    </html>
    """
    return render_template_string(html, gifts=gifts, users=users, announcements=announcements)

# Endpoint baru untuk memberikan config ke Worker
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
    print(f"[API Request] Menerima akses dari Worker dengan Headers: {dict(request.headers)}")
    data = request.get_json() or {}
    secret_key = request.headers.get("X-API-KEY")
    expected_key = os.environ.get("API_SECRET_KEY")

    if expected_key and secret_key != expected_key:
        print(f"[API Warning] Akses ditolak: API Key salah atau tidak cocok.")
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

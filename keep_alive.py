from flask import Flask, render_template_string, request, jsonify
from threading import Thread
import os
from db import get_db_connection

app = Flask('')

TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")

# Global handler untuk meneruskan request API ke bot Discord
async_announcement_handler = None

@app.route('/')
def home():
    return "Bot Server is Alive!"

# Endpoint Admin Panel
@app.route('/admin/data', methods=['GET'])
def view_data():
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        gifts = cur.execute("SELECT tiktok_user, display_name, total_coins FROM tiktok_gifts ORDER BY total_coins DESC").fetchall()
        users = cur.execute("SELECT discord_id, tiktok_user FROM user_mappings").fetchall()
        announcements = cur.execute("SELECT tiktok_username, channel_id, message_id FROM active_live_announcements").fetchall()

        conn.close()
    except Exception as e:
        return f"<h3>Database Error: {e}</h3>", 500

    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Turso DB Admin Panel</title>
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 30px; background: #0f172a; color: #f8fafc; }
            h2 { color: #38bdf8; border-bottom: 2px solid #0284c7; padding-bottom: 8px; margin-top: 30px; }
            table { border-collapse: collapse; width: 100%; margin-bottom: 20px; background: #1e293b; border-radius: 8px; overflow: hidden; }
            th, td { border: 1px solid #334155; padding: 12px; text-align: left; }
            th { background-color: #0284c7; color: white; text-transform: uppercase; font-size: 13px; }
            tr:nth-child(even) { background-color: #1a2234; }
            tr:hover { background-color: #334155; }
            .badge { background: #0369a1; padding: 4px 8px; border-radius: 4px; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>🛡️ Replit 1 - Database Admin Panel</h1>

        <h2>📊 Supporter Coins (tiktok_gifts)</h2>
        <table>
            <tr><th>TikTok Username</th><th>Display Name</th><th>Total Coins</th></tr>
            {% for row in gifts %}
            <tr><td>@{{ row[0] }}</td><td>{{ row[1] or '-' }}</td><td><span class="badge">{{ row[2] }} c</span></td></tr>
            {% else %}
            <tr><td colspan="3">No coin data yet.</td></tr>
            {% endfor %}
        </table>

        <h2>🔗 Discord User Mappings (user_mappings)</h2>
        <table>
            <tr><th>Discord ID</th><th>TikTok Username</th></tr>
            {% for row in users %}
            <tr><td>{{ row[0] }}</td><td>@{{ row[1] }}</td></tr>
            {% else %}
            <tr><td colspan="2">No account claims yet.</td></tr>
            {% endfor %}
        </table>

        <h2>📢 Active Announcements (active_live_announcements)</h2>
        <table>
            <tr><th>TikTok Username</th><th>Channel ID</th><th>Message ID</th></tr>
            {% for row in announcements %}
            <tr><td>@{{ row[0] }}</td><td>{{ row[1] }}</td><td>{{ row[2] }}</td></tr>
            {% else %}
            <tr><td colspan="3">No active announcements.</td></tr>
            {% endfor %}
        </table>
    </body>
    </html>
    """
    return render_template_string(html, gifts=gifts, users=users, announcements=announcements)

# Endpoint API dari Replit 2
@app.route('/api/announcement', methods=['POST'])
def trigger_announcement():
    # Notifikasi konsol bahwa Replit 2 mengakses endpoint ini
    print(f"[API Request] Received access from Replit 2 with headers: {dict(request.headers)}")

    data = request.get_json() or {}
    secret_key = request.headers.get("X-API-KEY")
    expected_key = os.environ.get("API_SECRET_KEY")

    if expected_key and secret_key != expected_key:
        print(f"[API Warning] Access denied: wrong or mismatched API Key.")
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    username = data.get("username")
    is_live = data.get("is_live")
    print(f"[API Data] Successfully processed data for @{username}, Live Status: {is_live}")

    if not username or is_live is None:
        return jsonify({"status": "error", "message": "Missing username or is_live status"}), 400

    if async_announcement_handler:
        async_announcement_handler(username, is_live)
        return jsonify({"status": "success", "message": f"Announcement triggered for @{username}"}), 200

    return jsonify({"status": "error", "message": "Bot handler not ready"}), 500

def run():
    # Mengambil PORT dari environment Replit, dengan fallback ke 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, threaded=True)

def keep_alive(handler_callback=None):
    global async_announcement_handler
    async_announcement_handler = handler_callback
    # Menggunakan daemon=True agar thread berhenti sempurna saat skrip mati
    t = Thread(target=run, daemon=True)
    t.start()
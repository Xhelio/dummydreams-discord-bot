from flask import Flask, render_template_string, request, jsonify, redirect
from threading import Thread
import os
import json
import libsql_experimental as libsql
import pytz
from datetime import datetime
from db import get_shift_name, clean_expired_schedules_and_offlines, get_all_host_names

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
.badge { background: #0369a1; color: #e0f2fe; padding: 3px 8px; border-radius: 4px; font-weight: 600; text-decoration: none; display: inline-block; }
.badge-active { background: #15803d; color: #dcfce7; border: 1px solid #22c55e; transition: all 0.2s; }
.badge-active:hover { background: #166534; transform: translateY(-1px); }
.badge-inactive { background: #475569; color: #cbd5e1; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; margin-top: 10px; }
input, select, button { padding: 8px 12px; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: white; font-size: 0.875rem; }
button { background: #0284c7; cursor: pointer; font-weight: 600; border: none; }
button:hover { background: #0369a1; }
.btn-danger { background: #ef4444; }
.btn-action { padding: 4px 8px; font-size: 0.75rem; text-decoration: none; border-radius: 4px; display: inline-block; }
.btn-toggle { background: #ca8a04; color: white; }
.btn-del { background: #dc2626; color: white; }
.edit-form { display: flex; gap: 6px; align-items: center; }
.edit-form input, .edit-form select { width: auto; padding: 4px 6px; }
.edit-form button { padding: 4px 8px; font-size: 0.75rem; }
.host-link, .viewer-link { color: #38bdf8; text-decoration: underline; font-weight: bold; }
.host-link:hover, .viewer-link:hover { color: #7dd3fc; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 20px; }
.stat-box { background: #0f172a; border: 1px solid #334155; border-radius: 8px; padding: 16px; text-align: center; }
.stat-number { font-size: 1.5rem; font-weight: bold; color: #38bdf8; margin-top: 6px; }
"""

NAV_HTML = """
<div class="nav-links">
    <a href="/admin/data" class="nav-btn">🏠 Main Menu</a>
    <a href="/admin/schedule" class="nav-btn">📅 Jadwal Shift</a>
    <a href="/admin/viewers" class="nav-btn">👥 Viewer & Koin</a>
    <a href="/admin/monitor" class="nav-btn">📹 Monitor Live</a>
</div>
"""

AUTO_REFRESH_JS = """
<script>
    setInterval(function() {
        fetch(window.location.href)
            .then(response => response.text())
            .then(html => {
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');
                const currentCards = document.querySelectorAll('.card');
                const newCards = doc.querySelectorAll('.card');
                
                currentCards.forEach((card, index) => {
                    if (newCards[index] && !card.querySelector('form[action="/admin/reset-db"]')) {
                        const currentTable = card.querySelector('table');
                        const newTable = newCards[index].querySelector('table');
                        if (currentTable && newTable) {
                            currentTable.innerHTML = newTable.innerHTML;
                        }
                    }
                });
            })
            .catch(err => console.log('Realtime sync error:', err));
    }, 5000);
</script>
"""

@app.route('/')
def home():
    return redirect('/admin/data')

@app.route('/admin/data', methods=['GET'])
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
                Pilih modul di atas untuk mengelola jadwal shift, data viewer, atau memantau sesi live realtime.
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

@app.route('/admin/schedule', methods=['GET'])
def view_schedule():
    clean_expired_schedules_and_offlines()
    host_vars = get_all_host_names()

    try:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        cur = conn.cursor()

        schedules_raw = cur.execute("""
            SELECT schedule_id, tiktok_username, account_name, host_name, start_hour, end_hour 
            FROM host_schedules 
            ORDER BY tiktok_username, start_hour
        """).fetchall()

        schedules = []
        for s in schedules_raw:
            schedules.append({
                "id": s[0],
                "username": s[1],
                "account_name": s[2],
                "host_name": s[3],
                "start_hour": s[4],
                "end_hour": s[5],
                "shift_label": get_shift_name(s[4], s[5])
            })

        temp_schedules = cur.execute("""
            SELECT temp_id, schedule_date, tiktok_username, account_name, host_name, shift_num 
            FROM temp_schedules 
            ORDER BY schedule_date ASC, shift_num ASC
        """).fetchall()

        offlines = cur.execute("""
            SELECT offline_id, host_name, offline_type, offline_date, offline_day, note 
            FROM host_offlines 
            ORDER BY offline_id DESC
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
        <title>Jadwal Shift Host</title>
        <style>BASE_CSS_PLACEHOLDER</style>
    </head>
    <body>
        <div class="header">
            <h1>📅 Jadwal Shift Host</h1>
        </div>
        NAV_HTML_PLACEHOLDER

        <div class="card">
            <h2>📌 Jadwal Shift Default (Statis)</h2>
            <form action="/admin/add-schedule" method="POST" class="form-grid">
                <input type="text" name="tiktok_username" placeholder="Username TikTok (@...)" required>
                <input type="text" name="account_name" placeholder="Nama Channel" required>
                <select name="host_name" required>
                    <option value="" disabled selected>-- Pilih Variabel Host --</option>
                    {% for hvar in host_vars %}
                    <option value="{{ hvar }}">{{ hvar }}</option>
                    {% endfor %}
                </select>
                <select name="shift_num" required>
                    <option value="1">Shift 1 (01:00 - 07:00 WIB)</option>
                    <option value="2">Shift 2 (07:00 - 13:00 WIB)</option>
                    <option value="3">Shift 3 (13:00 - 19:00 WIB)</option>
                    <option value="4">Shift 4 (19:00 - 01:00 WIB)</option>
                </select>
                <button type="submit">Simpan Shift</button>
            </form>
            <br>
            <table>
                <tr>
                    <th>Username Target</th>
                    <th>Nama Channel</th>
                    <th>Nama Host Shift</th>
                    <th>Kelompok Shift & Jam Shift</th>
                    <th>Aksi</th>
                </tr>
                {% for sc in schedules %}
                <tr>
                    <td>@{{ sc.username }}</td>
                    <td>{{ sc.account_name or '@' ~ sc.username }}</td>
                    <td>
                        <a href="/admin/host/{{ sc.host_name }}" class="host-link" title="Klik untuk lihat riwayat shift">{{ sc.host_name }} ↗</a>
                    </td>
                    <td><span class="badge">{{ sc.shift_label }}</span></td>
                    <td>
                        <form action="/admin/edit-schedule/{{ sc.id }}" method="POST" class="edit-form">
                            <select name="host_name" required>
                                {% for hvar in host_vars %}
                                <option value="{{ hvar }}" {% if hvar == sc.host_name %}selected{% endif %}>{{ hvar }}</option>
                                {% endfor %}
                            </select>
                            <select name="shift_num" required>
                                <option value="1" {% if sc.start_hour == 1 %}selected{% endif %}>Shift 1</option>
                                <option value="2" {% if sc.start_hour == 7 %}selected{% endif %}>Shift 2</option>
                                <option value="3" {% if sc.start_hour == 13 %}selected{% endif %}>Shift 3</option>
                                <option value="4" {% if sc.start_hour == 19 %}selected{% endif %}>Shift 4</option>
                            </select>
                            <button type="submit">Edit</button>
                            <a href="/admin/delete-schedule/{{ sc.id }}" class="btn-action btn-del" onclick="return confirm('Hapus shift ini?')">Hapus</a>
                        </form>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="5">Belum ada data jadwal shift host.</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>⏳ Jadwal Sementara (Penimpa Durasi Shift)</h2>
            <form action="/admin/add-temp-schedule" method="POST" class="form-grid">
                <input type="date" name="schedule_date" required>
                <input type="text" name="tiktok_username" placeholder="Username TikTok (@...)" required>
                <input type="text" name="account_name" placeholder="Nama Channel" required>
                <select name="host_name" required>
                    <option value="" disabled selected>-- Pilih Host Pengganti --</option>
                    {% for hvar in host_vars %}
                    <option value="{{ hvar }}">{{ hvar }}</option>
                    {% endfor %}
                </select>
                <select name="shift_num" required>
                    <option value="1">Shift 1 (01:00 - 07:00 WIB)</option>
                    <option value="2">Shift 2 (07:00 - 13:00 WIB)</option>
                    <option value="3">Shift 3 (13:00 - 19:00 WIB)</option>
                    <option value="4">Shift 4 (19:00 - 01:00 WIB)</option>
                </select>
                <button type="submit">Simpan Jadwal Sementara</button>
            </form>
            <br>
            <table>
                <tr>
                    <th>Tanggal</th>
                    <th>Channel</th>
                    <th>Host Pengganti</th>
                    <th>Shift</th>
                    <th>Aksi</th>
                </tr>
                {% for ts in temp_schedules %}
                <tr>
                    <td>{{ ts[1] }}</td>
                    <td>{{ ts[3] or '@' ~ ts[2] }}</td>
                    <td>
                        <a href="/admin/host/{{ ts[4] }}" class="host-link" title="Klik untuk lihat riwayat shift">{{ ts[4] }} ↗</a>
                    </td>
                    <td><span class="badge">Shift {{ ts[5] }}</span></td>
                    <td>
                        <a href="/admin/delete-temp-schedule/{{ ts[0] }}" class="btn-action btn-del" onclick="return confirm('Hapus jadwal sementara ini?')">Hapus</a>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="5">Belum ada jadwal sementara aktif.</td></tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h2>🚫 Status Offline Host</h2>
            <form action="/admin/add-offline" method="POST" class="form-grid">
                <select name="host_name" required>
                    <option value="" disabled selected>-- Pilih Host Offline --</option>
                    {% for hvar in host_vars %}
                    <option value="{{ hvar }}">{{ hvar }}</option>
                    {% endfor %}
                </select>
                <select name="offline_type" id="offType" onchange="toggleOffInputs()" required>
                    <option value="temporary">Sementara (Tanggal Tertentu)</option>
                    <option value="routine">Rutin (Seminggu Sekali)</option>
                </select>
                <input type="date" name="offline_date" id="offDate">
                <select name="offline_day" id="offDay" style="display: none;">
                    <option value="Monday">Senin</option>
                    <option value="Tuesday">Selasa</option>
                    <option value="Wednesday">Rabu</option>
                    <option value="Thursday">Kamis</option>
                    <option value="Friday">Jumat</option>
                    <option value="Saturday">Sabtu</option>
                    <option value="Sunday">Minggu</option>
                </select>
                <input type="text" name="note" placeholder="Keterangan (misal: Sakit/Izin)">
                <button type="submit">Simpan Status Offline</button>
            </form>
            <br>
            <table>
                <tr>
                    <th>Nama Host</th>
                    <th>Tipe Offline</th>
                    <th>Waktu Offline</th>
                    <th>Keterangan</th>
                    <th>Aksi</th>
                </tr>
                {% for off in offlines %}
                <tr>
                    <td>
                        <a href="/admin/host/{{ off[1] }}" class="host-link" title="Klik untuk lihat riwayat shift">{{ off[1] }} ↗</a>
                    </td>
                    <td><span class="badge">{{ 'Sementara' if off[2] == 'temporary' else 'Rutin Mingguan' }}</span></td>
                    <td>{{ off[3] if off[2] == 'temporary' else off[4] }}</td>
                    <td>{{ off[5] or '-' }}</td>
                    <td>
                        <a href="/admin/delete-offline/{{ off[0] }}" class="btn-action btn-del" onclick="return confirm('Hapus status offline ini?')">Hapus</a>
                    </td>
                </tr>
                {% else %}
                <tr><td colspan="5">Belum ada catatan host offline.</td></tr>
                {% endfor %}
            </table>
        </div>

        <script>
            function toggleOffInputs() {
                var type = document.getElementById("offType").value;
                if (type === "temporary") {
                    document.getElementById("offDate").style.display = "block";
                    document.getElementById("offDay").style.display = "none";
                } else {
                    document.getElementById("offDate").style.display = "none";
                    document.getElementById("offDay").style.display = "block";
                }
            }
            toggleOffInputs();
        </script>
        AUTO_REFRESH_JS_PLACEHOLDER
    </body>
    </html>
    """.replace("BASE_CSS_PLACEHOLDER", BASE_CSS).replace("NAV_HTML_PLACEHOLDER", NAV_HTML).replace("AUTO_REFRESH_JS_PLACEHOLDER", AUTO_REFRESH_JS)
    
    return render_template_string(html, schedules=schedules, temp_schedules=temp_schedules, offlines=offlines, host_vars=host_vars)

# Route Detail Host
@app.route('/admin/host/<host_name>', methods=['GET'])
def view_host_detail(host_name):
    host_name_upper = host_name.strip().upper()
    try:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
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

# 3. Halaman Viewer & Edit Koin (Dengan Link Klik Viewer)
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

# Route Baru: Detail Koin Viewer (Total Koin, Koin Hari Ini, Histori Per Hari)
@app.route('/admin/viewer/<tiktok_user>', methods=['GET'])
def view_viewer_detail(tiktok_user):
    user_clean = tiktok_user.strip().lower().lstrip("@")
    wib = pytz.timezone('Asia/Jakarta')
    today_str = datetime.now(wib).strftime('%Y-%m-%d')

    try:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        cur = conn.cursor()

        # 1. Total Koin Keseluruhan & Display Name
        viewer_row = cur.execute("""
            SELECT display_name, total_coins 
            FROM tiktok_gifts 
            WHERE tiktok_user = ?
        """, (user_clean,)).fetchone()

        display_name = viewer_row[0] if viewer_row and viewer_row[0] else user_clean
        grand_total_coins = viewer_row[1] if viewer_row else 0

        # 2. Total Koin Hari Ini
        today_row = cur.execute("""
            SELECT coins 
            FROM daily_tiktok_gifts 
            WHERE tiktok_user = ? AND date = ?
        """, (user_clean, today_str)).fetchone()

        today_coins = today_row[0] if today_row else 0

        # 3. Riwayat Koin Per Hari (Histori Harian)
        daily_history = cur.execute("""
            SELECT date, coins 
            FROM daily_tiktok_gifts 
            WHERE tiktok_user = ?
            ORDER BY date DESC
        """, (user_clean,)).fetchall()

        # 4. Log Transaksi Gift Lengkap
        gift_logs = cur.execute("""
            SELECT gift_name, gift_count, coins, created_at, tiktok_username
            FROM gift_logs
            WHERE tiktok_username = ? OR viewer_username = ?
            ORDER BY created_at DESC
            LIMIT 50
        """, (user_clean, user_clean)).fetchall()

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
                    <th>Waktu</th>
                    <th>Nama Gift</th>
                    <th>Jumlah</th>
                    <th>Koin</th>
                    <th>Channel Live</th>
                </tr>
                {% for g in gift_logs %}
                <tr>
                    <td>{{ g[3] }}</td>
                    <td><strong>{{ g[0] }}</strong></td>
                    <td>{{ g[1] }}x</td>
                    <td><span class="badge">{{ g[2] }} c</span></td>
                    <td>@{{ g[4] }}</td>
                </tr>
                {% else %}
                <tr><td colspan="5">Belum ada histori gift tercatat.</td></tr>
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
                h.display_name,
                h.is_active,
                CASE WHEN a.tiktok_username IS NOT NULL THEN 1 ELSE 0 END as is_currently_live
            FROM hosts h
            LEFT JOIN active_live_announcements a ON h.tiktok_username = a.tiktok_username
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
                CASE WHEN a.tiktok_username IS NOT NULL THEN 1 ELSE 0 END as is_live_now
            FROM live_sessions s
            LEFT JOIN active_live_announcements a ON s.tiktok_username = a.tiktok_username
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
                    <th>Host Terjadwal</th>
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

@app.route('/admin/add-host', methods=['POST'])
def add_host():
    username = request.form.get("tiktok_username", "").strip().lower().lstrip("@")
    display_name = request.form.get("display_name", username).strip()
    if username:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        conn.execute("INSERT OR REPLACE INTO hosts (tiktok_username, display_name, is_active) VALUES (?, ?, 1)", (username, display_name))
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
    shift_num = int(request.form.get("shift_num", 1))

    shifts_map = {1: (1, 7), 2: (7, 13), 3: (13, 19), 4: (19, 1)}
    start_h, end_h = shifts_map.get(shift_num, (1, 7))

    if username and host_name:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        conn.execute("""
            INSERT OR REPLACE INTO host_schedules (tiktok_username, account_name, host_name, start_hour, end_hour)
            VALUES (?, ?, ?, ?, ?)
        """, (username, acc_name, host_name, start_h, end_h))
        conn.commit()
        conn.close()
    return redirect('/admin/schedule')

@app.route('/admin/edit-schedule/<int:sched_id>', methods=['POST'])
def edit_schedule(sched_id):
    host_name = request.form.get("host_name", "").strip().upper()
    shift_num = int(request.form.get("shift_num", 1))

    shifts_map = {1: (1, 7), 2: (7, 13), 3: (13, 19), 4: (19, 1)}
    start_h, end_h = shifts_map.get(shift_num, (1, 7))

    if host_name:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        conn.execute("""
            UPDATE host_schedules 
            SET host_name = ?, start_hour = ?, end_hour = ?
            WHERE schedule_id = ?
        """, (host_name, start_h, end_h, sched_id))
        conn.commit()
        conn.close()
    return redirect('/admin/schedule')

@app.route('/admin/delete-schedule/<int:sched_id>')
def delete_schedule(sched_id):
    conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    conn.execute("DELETE FROM host_schedules WHERE schedule_id = ?", (sched_id,))
    conn.commit()
    conn.close()
    return redirect('/admin/schedule')

@app.route('/admin/add-temp-schedule', methods=['POST'])
def add_temp_schedule():
    sched_date = request.form.get("schedule_date", "").strip()
    username = request.form.get("tiktok_username", "").strip().lower().lstrip("@")
    acc_name = request.form.get("account_name", username).strip()
    host_name = request.form.get("host_name", "").strip().upper()
    shift_num = int(request.form.get("shift_num", 1))

    if sched_date and username and host_name:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        conn.execute("""
            INSERT OR REPLACE INTO temp_schedules (schedule_date, tiktok_username, account_name, host_name, shift_num)
            VALUES (?, ?, ?, ?, ?)
        """, (sched_date, username, acc_name, host_name, shift_num))
        conn.commit()
        conn.close()
    return redirect('/admin/schedule')

@app.route('/admin/delete-temp-schedule/<int:temp_id>')
def delete_temp_schedule(temp_id):
    conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    conn.execute("DELETE FROM temp_schedules WHERE temp_id = ?", (temp_id,))
    conn.commit()
    conn.close()
    return redirect('/admin/schedule')

@app.route('/admin/add-offline', methods=['POST'])
def add_offline():
    host_name = request.form.get("host_name", "").strip().upper()
    off_type = request.form.get("offline_type", "temporary")
    off_date = request.form.get("offline_date", "").strip()
    off_day = request.form.get("offline_day", "Monday").strip()
    note = request.form.get("note", "").strip()

    if host_name:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        conn.execute("""
            INSERT INTO host_offlines (host_name, offline_type, offline_date, offline_day, note)
            VALUES (?, ?, ?, ?, ?)
        """, (host_name, off_type, off_date if off_type == 'temporary' else None, off_day if off_type == 'routine' else None, note))
        conn.commit()
        conn.close()
    return redirect('/admin/schedule')

@app.route('/admin/delete-offline/<int:off_id>')
def delete_offline(off_id):
    conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
    conn.execute("DELETE FROM host_offlines WHERE offline_id = ?", (off_id,))
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
    conn.execute("DELETE FROM active_live_announcements")
    conn.execute("DELETE FROM temp_schedules")
    conn.execute("DELETE FROM host_offlines")
    conn.commit()
    conn.close()
    return redirect('/admin/data')

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

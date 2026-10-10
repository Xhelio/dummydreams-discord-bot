from flask import Flask, render_template_string, request, jsonify, redirect
from threading import Thread
import os
import json
import libsql_experimental as libsql
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

# 2. Halaman Jadwal Shift Host (Default, Sementara, Offline)
@app.route('/admin/schedule', methods=['GET'])
def view_schedule():
    clean_expired_schedules_and_offlines()
    host_vars = get_all_host_names()

    try:
        conn = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        cur = conn.cursor()

        # Data Default Schedules
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

        # Data Temp Schedules
        temp_schedules = cur.execute("""
            SELECT temp_id, schedule_date, tiktok_username, account_name, host_name, shift_num 
            FROM temp_schedules 
            ORDER BY schedule_date ASC, shift_num ASC
        """).fetchall()

        # Data Host Offlines
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

        <!-- Segmen 1: Jadwal Shift Default -->
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
                    <td><strong>{{ sc.host_name }}</strong></td>
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

        <!-- Segmen 2: Jadwal Sementara -->
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
                    <td><strong>{{ ts[4] }}</strong></td>
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

        <!-- Segmen 3: Offline Host -->
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
                    <td><strong>{{ off[1] }}</strong></td>
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

# --- Action Handlers untuk Shift & Offline ---

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
                    <td>@{{ v[0] }}</td>
                    <td>{{ v[1] or '-' }}</td>
                    <td><span class="badge">{{ v[2] }} c</span></td>
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
                        <a href="https://www.tiktok.com/@{{ h[1] }}/live" target="_blank" class="badge badge-active" title="Klik untuk nonton Live">

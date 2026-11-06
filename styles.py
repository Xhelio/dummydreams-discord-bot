BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 30px; background: #0f172a; color: #f8fafc; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid #334155; padding-bottom: 16px; flex-wrap: wrap; gap: 12px; }
h1 { font-size: 1.5rem; color: #f8fafc; display: flex; align-items: center; gap: 8px; }
.nav-bar-container { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; flex-wrap: wrap; gap: 14px; }
.nav-links { display: flex; gap: 10px; flex-wrap: wrap; }
.nav-btn { background: #1e293b; color: #38bdf8; border: 1px solid #334155; text-decoration: none; padding: 8px 16px; border-radius: 8px; font-size: 0.875rem; font-weight: 600; transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px; }
.nav-btn:hover, .nav-btn.active { background: #0284c7; color: white; border-color: #0284c7; }

/* Status Indikator Website Stabil */
.status-indicator-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: #0f172a;
    border: 1px solid #10b981;
    padding: 6px 14px;
    border-radius: 9999px;
    font-size: 0.825rem;
    font-weight: 600;
    color: #34d399;
    box-shadow: 0 0 12px rgba(16, 185, 129, 0.15);
    transition: all 0.3s ease;
}
.status-pulse-dot {
    width: 9px;
    height: 9px;
    background-color: #10b981;
    border-radius: 50%;
    display: inline-block;
    animation: statusPulse 2s infinite ease-in-out;
}
.ping-badge {
    color: #94a3b8;
    font-weight: 400;
    font-size: 0.75rem;
}
@keyframes statusPulse {
    0% { transform: scale(0.9); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
    70% { transform: scale(1.1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
    100% { transform: scale(0.9); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
}

.card { background: #1e293b; border-radius: 10px; border: 1px solid #334155; padding: 20px; margin-bottom: 24px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); }
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

/* Log Terminal & Activity Styling */
.terminal-container {
    background: #020617;
    border: 1px solid #1e293b;
    border-radius: 8px;
    padding: 16px;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 0.875rem;
    line-height: 1.6;
    max-height: 600px;
    overflow-y: auto;
    box-shadow: inset 0 2px 8px rgba(0,0,0,0.4);
}
.terminal-row {
    padding: 6px 10px;
    border-bottom: 1px solid #0f172a;
    display: flex;
    align-items: baseline;
    gap: 8px;
    word-break: break-word;
}
.terminal-row:hover {
    background-color: #0b1329;
}
.log-tag-connect {
    color: #22c55e;
    font-weight: bold;
    background: rgba(34, 197, 94, 0.1);
    padding: 2px 6px;
    border-radius: 4px;
}
.log-tag-disconnect {
    color: #ef4444;
    font-weight: bold;
    background: rgba(239, 68, 68, 0.1);
    padding: 2px 6px;
    border-radius: 4px;
}
.log-tag-gift {
    color: #fbbf24;
    font-weight: 500;
}
"""

NAV_HTML = """
<div class="nav-bar-container">
    <div class="nav-links">
        <a href="/admin/data" class="nav-btn">🏠 Main Menu</a>
        <a href="/admin/schedule" class="nav-btn">📅 Jadwal Shift</a>
        <a href="/admin/viewers" class="nav-btn">👥 Viewer & Koin</a>
        <a href="/admin/monitor" class="nav-btn">📹 Monitor Live</a>
        <a href="/admin/activity" class="nav-btn">📊 Log Activity</a>
        <a href="/admin/verifications" class="nav-btn">🔐 Verifikasi Akun</a>
    </div>
    <div id="website-status-box" class="status-indicator-pill" title="Indikator Sistem Realtime">
        <span class="status-pulse-dot" id="status-pulse-dot"></span>
        <span id="website-status-text">Website Stabil</span>
        <span id="website-ping-badge" class="ping-badge">&bull; Memeriksa...</span>
    </div>
</div>
"""

AUTO_REFRESH_JS = """
<script>
    // Realtime Health & Ping Checker untuk Indikator Website Stabil
    function updateWebsiteStatus() {
        const startTime = Date.now();
        fetch('/api/health')
            .then(res => res.json())
            .then(data => {
                const latency = Date.now() - startTime;
                const statusPill = document.getElementById('website-status-box');
                const statusDot = document.getElementById('status-pulse-dot');
                const statusText = document.getElementById('website-status-text');
                const pingBadge = document.getElementById('website-ping-badge');

                if (statusText && data.status === 'ok') {
                    statusText.innerText = 'Website Stabil';
                    pingBadge.innerText = '• ' + latency + 'ms';
                    if (statusPill) {
                        statusPill.style.borderColor = '#10b981';
                        statusPill.style.color = '#34d399';
                    }
                    if (statusDot) {
                        statusDot.style.backgroundColor = '#10b981';
                    }
                }
            })
            .catch(err => {
                const statusPill = document.getElementById('website-status-box');
                const statusDot = document.getElementById('status-pulse-dot');
                const statusText = document.getElementById('website-status-text');
                const pingBadge = document.getElementById('website-ping-badge');

                if (statusText) {
                    statusText.innerText = 'Website Gangguan';
                    pingBadge.innerText = '• Offline';
                    if (statusPill) {
                        statusPill.style.borderColor = '#ef4444';
                        statusPill.style.color = '#f87171';
                    }
                    if (statusDot) {
                        statusDot.style.backgroundColor = '#ef4444';
                    }
                }
            });
    }

    // Jalankan cek pertama langsung dan ulangi tiap 4 detik
    updateWebsiteStatus();
    setInterval(updateWebsiteStatus, 4000);

    // Sinkronisasi Tabel Realtime
    setInterval(function() {
        fetch(window.location.href)
            .then(response => response.text())
            .then(html => {
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');
                const currentCards = document.querySelectorAll('.card');
                const newCards = doc.querySelectorAll('.card');
                
                currentCards.forEach((card, index) => {
                    if (newCards[index] && !card.querySelector('form[action="/admin/reset-db"]') && !card.querySelector('form[action="/admin/clear-activity-logs"]')) {
                        const currentTable = card.querySelector('table');
                        const newTable = newCards[index].querySelector('table');
                        if (currentTable && newTable) {
                            currentTable.innerHTML = newTable.innerHTML;
                        }
                        const currentTerminal = card.querySelector('.terminal-container');
                        const newTerminal = newCards[index].querySelector('.terminal-container');
                        if (currentTerminal && newTerminal) {
                            currentTerminal.innerHTML = newTerminal.innerHTML;
                        }
                    }
                });
            })
            .catch(err => console.log('Realtime sync error:', err));
    }, 4000);
</script>
"""

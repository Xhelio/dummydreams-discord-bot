BASE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 30px; background: #0f172a; color: #f8fafc; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid #334155; padding-bottom: 16px; flex-wrap: wrap; gap: 12px; }
h1 { font-size: 1.5rem; color: #f8fafc; display: flex; align-items: center; gap: 8px; }
.nav-bar-container { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; flex-wrap: wrap; gap: 14px; }
.nav-links { display: flex; gap: 10px; flex-wrap: wrap; }
.nav-btn { background: #1e293b; color: #38bdf8; border: 1px solid #334155; text-decoration: none; padding: 8px 16px; border-radius: 8px; font-size: 0.875rem; font-weight: 600; transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px; }
.nav-btn:hover, .nav-btn.active { background: #0284c7; color: white; border-color: #0284c7; }

/* Website Status Indicator */
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

/* Three live indicators */
.status-cluster { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.status-indicator-pill.status-neutral { border-color: #475569; color: #cbd5e1; box-shadow: none; }
.status-indicator-pill.status-neutral .status-pulse-dot { background-color: #64748b; animation: none; }
.status-indicator-pill.status-warning { border-color: #f59e0b; color: #fbbf24; box-shadow: 0 0 12px rgba(245, 158, 11, 0.15); }
.status-indicator-pill.status-warning .status-pulse-dot { background-color: #f59e0b; }
.status-indicator-pill.status-offline { border-color: #ef4444; color: #f87171; box-shadow: 0 0 12px rgba(239, 68, 68, 0.12); }
.status-indicator-pill.status-offline .status-pulse-dot { background-color: #ef4444; animation: none; }

/* Settings page */
.notice-success { background: rgba(16, 185, 129, 0.12); border: 1px solid #10b981; color: #6ee7b7; border-radius: 8px; padding: 12px 16px; margin-bottom: 20px; font-weight: 600; }
.muted { color: #94a3b8; font-size: 0.9rem; margin-bottom: 12px; }
.switch-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding: 12px 0; border-bottom: 1px solid #334155; }
.switch-row:last-child { border-bottom: none; }
.switch-label { display: flex; align-items: center; gap: 10px; font-weight: 600; cursor: pointer; }
.switch-label input[type="checkbox"], .inline-check input[type="checkbox"] { width: 18px; height: 18px; accent-color: #0284c7; }
.switch-help { color: #94a3b8; font-size: 0.82rem; max-width: 480px; text-align: right; }
.master-row { background: rgba(2, 132, 199, 0.08); border-radius: 8px; padding: 14px; margin-bottom: 8px; }
.field-label { display: block; color: #cbd5e1; font-size: 0.8rem; font-weight: 700; margin: 10px 0 6px; text-transform: uppercase; letter-spacing: 0.03em; }
.field-help { display: block; color: #64748b; font-size: 0.78rem; margin-top: 4px; }
.form-grid.two-col { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.full-col { grid-column: 1 / -1; }
.inline-check { display: flex; align-items: center; gap: 8px; font-size: 0.875rem; margin: 8px 0; color: #e2e8f0; }
.inline-fields { display: flex; gap: 8px; }
.inline-fields input { width: 100%; }
.inline-form { display: inline-block; margin: 0; }
.btn-save { width: 100%; padding: 14px; font-size: 1rem; background: #15803d; border-radius: 8px; margin-bottom: 24px; }
.btn-save:hover { background: #166534; }
.table-scroll { width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
.tier-form, .host-form, .alias-form { grid-template-columns: minmax(130px, 1fr) minmax(120px, 1fr) auto; align-items: end; }
textarea { width: 100%; padding: 10px 12px; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: white; font-size: 0.875rem; resize: vertical; font-family: inherit; }

/* Responsive dashboard */
@media (max-width: 760px) {
    body { padding: 14px; }
    h1 { font-size: 1.12rem; gap: 6px; }
    h2 { font-size: 1rem; }
    .header { margin-bottom: 16px; padding-bottom: 12px; }
    .nav-bar-container { flex-direction: column; align-items: stretch; gap: 10px; margin-bottom: 16px; }
    .nav-links { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; width: 100%; }
    .nav-btn { justify-content: center; padding: 9px 8px; font-size: 0.78rem; text-align: center; }
    .status-cluster { flex-direction: column; width: 100%; }
    .status-indicator-pill { width: 100%; justify-content: center; }
    .card { padding: 14px; border-radius: 8px; margin-bottom: 16px; overflow-x: auto; -webkit-overflow-scrolling: touch; }
    .stat-grid { grid-template-columns: 1fr; gap: 10px; }
    .form-grid, .form-grid.two-col, .tier-form, .host-form, .alias-form { grid-template-columns: 1fr; }
    .switch-row { flex-direction: column; gap: 4px; }
    .switch-help { text-align: left; max-width: none; }
    .inline-fields { flex-direction: column; }
    input, select, button, textarea { width: 100%; font-size: 0.9rem; }
    .btn-action { padding: 6px 8px; }
    th, td { padding: 8px 10px; font-size: 0.8rem; white-space: nowrap; }
    table { min-width: 560px; }
    .terminal-row { flex-direction: column; gap: 2px; }
    .edit-form { flex-wrap: wrap; }
    .edit-form input, .edit-form select { width: 100%; }
    .log-tag-connect, .log-tag-disconnect, .log-tag-gift { align-self: flex-start; }
}
"""

NAV_HTML = """
<div class="nav-bar-container">
    <div class="nav-links">
        <a href="/admin/data" class="nav-btn">🏠 Main Menu</a>
        <a href="/admin/viewers" class="nav-btn">👥 Viewers & Coins</a>
        <a href="/admin/monitor" class="nav-btn">📹 Live Monitor</a>
        <a href="/admin/activity" class="nav-btn">📊 Activity Log</a>
        <a href="/admin/verifications" class="nav-btn">🔐 Account Verification</a>
        <a href="/admin/settings" class="nav-btn">⚙️ Bot Settings</a>
    </div>
    <div class="status-cluster" title="Realtime System Indicators">
        <div id="website-status-box" class="status-indicator-pill status-neutral">
            <span class="status-pulse-dot" id="website-status-dot"></span>
            <span id="website-status-text">Website</span>
            <span id="website-ping-badge" class="ping-badge">&bull; Checking...</span>
        </div>
        <div id="tiktok-status-box" class="status-indicator-pill status-neutral">
            <span class="status-pulse-dot" id="tiktok-status-dot"></span>
            <span id="tiktok-status-text">TikTok Live</span>
            <span id="tiktok-live-badge" class="ping-badge">&bull; Checking...</span>
        </div>
        <div id="discord-status-box" class="status-indicator-pill status-neutral">
            <span class="status-pulse-dot" id="discord-status-dot"></span>
            <span id="discord-status-text">Discord Bot</span>
            <span id="discord-ping-badge" class="ping-badge">&bull; Checking...</span>
        </div>
    </div>
</div>
"""

AUTO_REFRESH_JS = """
<script>
    function setStatusPill(prefix, state, text, detail) {
        const pill = document.getElementById(prefix + '-status-box');
        const dot = document.getElementById(prefix + '-status-dot');
        const label = document.getElementById(prefix + '-status-text');
        const badge = document.getElementById(prefix + '-ping-badge');
        if (!pill) return;
        pill.classList.remove('status-neutral', 'status-warning', 'status-offline');
        if (state === 'ok') {
            pill.style.borderColor = '#10b981';
            pill.style.color = '#34d399';
            if (dot) dot.style.backgroundColor = '#10b981';
        } else if (state === 'neutral') {
            pill.classList.add('status-neutral');
            pill.style.borderColor = '#475569';
            pill.style.color = '#cbd5e1';
            if (dot) dot.style.backgroundColor = '#64748b';
        } else if (state === 'warning') {
            pill.classList.add('status-warning');
            pill.style.borderColor = '#f59e0b';
            pill.style.color = '#fbbf24';
            if (dot) dot.style.backgroundColor = '#f59e0b';
        } else {
            pill.classList.add('status-offline');
            pill.style.borderColor = '#ef4444';
            pill.style.color = '#f87171';
            if (dot) dot.style.backgroundColor = '#ef4444';
        }
        if (label) label.innerText = text;
        if (badge) badge.innerText = detail || '';
    }

    function updateSystemStatus() {
        const startTime = Date.now();
        fetch('/api/health', { cache: 'no-store' })
            .then(res => res.json())
            .then(data => {
                const latency = Date.now() - startTime;
                const websiteOk = data.website === true && data.database === 'connected';
                setStatusPill(
                    'website',
                    websiteOk ? 'ok' : 'warning',
                    websiteOk ? 'Website Stable' : 'Website Issue',
                    '• ' + latency + 'ms'
                );

                const bot = data.discord_bot || {};
                setStatusPill(
                    'discord',
                    bot.online ? 'ok' : 'offline',
                    bot.online ? 'Discord Online' : 'Discord Offline',
                    bot.online ? ('• ' + (bot.user || 'ready')) : '• offline'
                );

                const tiktok = data.tiktok_live || {};
                const count = tiktok.count || 0;
                const names = Object.keys(tiktok.accounts || {});
                setStatusPill(
                    'tiktok',
                    count > 0 ? 'ok' : 'neutral',
                    count > 0 ? 'TikTok Live' : 'No Live Now',
                    count > 0 ? ('• ' + count + ' live') : '• 0 live'
                );
                const tiktokPill = document.getElementById('tiktok-status-box');
                if (tiktokPill) {
                    tiktokPill.title = names.length ? ('Live: ' + names.join(', ')) : 'No target account is live';
                }
            })
            .catch(() => {
                setStatusPill('website', 'offline', 'Website Offline', '• Offline');
                setStatusPill('discord', 'offline', 'Discord Unknown', '• no data');
                setStatusPill('tiktok', 'offline', 'TikTok Unknown', '• no data');
            });
    }

    updateSystemStatus();
    setInterval(updateSystemStatus, 4000);

    // Realtime table synchronization (safe: input forms are never replaced)
    setInterval(function() {
        if (window.location.pathname === '/admin/settings') return;
        fetch(window.location.href, { cache: 'no-store' })
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

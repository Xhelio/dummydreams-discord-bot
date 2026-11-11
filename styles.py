BASE_CSS = """
:root {
  --bg: #0b1220; --bg-2: #0f172a; --card: #111827; --card-2: #1e293b;
  --border: #334155; --text: #e5e7eb; --muted: #94a3b8; --primary: #0284c7;
  --primary-2: #38bdf8; --success: #16a34a; --warning: #ca8a04; --danger: #dc2626;
  --focus: #7dd3fc;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body { font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 28px; background: linear-gradient(180deg, #0b1220 0%, #0f172a 100%); color: var(--text); line-height: 1.45; }
a { color: var(--primary-2); }
a:focus-visible, button:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 14px; flex-wrap: wrap; gap: 12px; }
h1 { font-size: 1.45rem; color: #f8fafc; display: flex; align-items: center; gap: 8px; line-height: 1.2; }
h2 { color: #7dd3fc; font-size: 1.08rem; margin-bottom: 12px; }
h3 { color: #e2e8f0; font-size: 0.98rem; margin-bottom: 8px; }
.page-subtitle { color: var(--muted); font-size: 0.9rem; margin-top: 4px; }
.nav-bar-container { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; position: sticky; top: 0; z-index: 20; background: rgba(11,18,32,0.85); backdrop-filter: blur(6px); padding: 8px 0; }
.nav-links { display: flex; gap: 8px; flex-wrap: wrap; }
.nav-btn { background: var(--card-2); color: #7dd3fc; border: 1px solid var(--border); text-decoration: none; padding: 8px 14px; border-radius: 8px; font-size: 0.84rem; font-weight: 600; transition: all 0.15s ease; display: inline-flex; align-items: center; gap: 6px; }
.nav-btn:hover { background: #243244; color: #fff; border-color: #475569; transform: translateY(-1px); }
.nav-btn.active { background: var(--primary); color: #fff; border-color: var(--primary); box-shadow: 0 0 0 1px rgba(56,189,248,0.15) inset; }

/* Status pills */
.status-indicator-pill { display: inline-flex; align-items: center; gap: 8px; background: #0f172a; border: 1px solid #10b981; padding: 6px 12px; border-radius: 9999px; font-size: 0.8rem; font-weight: 600; color: #34d399; box-shadow: 0 0 12px rgba(16,185,129,0.12); transition: all 0.2s ease; white-space: nowrap; }
.status-pulse-dot { width: 9px; height: 9px; background-color: #10b981; border-radius: 50%; display: inline-block; animation: statusPulse 2s infinite ease-in-out; }
.ping-badge { color: #94a3b8; font-weight: 500; font-size: 0.74rem; }
@keyframes statusPulse { 0% { transform: scale(0.9); box-shadow: 0 0 0 0 rgba(16,185,129,0.6);} 70% { transform: scale(1.1); box-shadow: 0 0 0 6px rgba(16,185,129,0);} 100% { transform: scale(0.9); box-shadow: 0 0 0 0 rgba(16,185,129,0);} }

.card { background: rgba(30,41,59,0.95); border-radius: 12px; border: 1px solid var(--border); padding: 18px; margin-bottom: 18px; box-shadow: 0 8px 24px rgba(2,6,23,0.25); }
.card-header { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom: 10px; }
.card-header h2 { margin-bottom: 0; }
.section-note { color: var(--muted); font-size: 0.86rem; margin-bottom: 12px; }
table { border-collapse: collapse; width: 100%; margin-bottom: 8px; }
th, td { border-bottom: 1px solid #334155; padding: 10px 12px; text-align: left; font-size: 0.86rem; vertical-align: middle; }
th { background: #0369a1; color: #fff; text-transform: uppercase; font-size: 0.72rem; letter-spacing: 0.04em; position: sticky; top: 0; z-index: 1; }
tr:hover td { background: rgba(148,163,184,0.04); }
tr:nth-child(even) td { background-color: rgba(15,23,42,0.35); }
.badge { background: #0369a1; color: #e0f2fe; padding: 3px 8px; border-radius: 999px; font-weight: 600; text-decoration: none; display: inline-block; font-size: 0.75rem; }
.badge-active { background: #166534; color: #dcfce7; border: 1px solid #22c55e; }
.badge-inactive { background: #475569; color: #e2e8f0; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-top: 10px; }
input, select, button, textarea { padding: 9px 12px; border-radius: 8px; border: 1px solid var(--border); background: #0b1220; color: #f8fafc; font-size: 0.87rem; }
input::placeholder, textarea::placeholder { color: #64748b; }
input[type="checkbox"], input[type="radio"] { width: 18px; height: 18px; padding: 0; accent-color: #0284c7; cursor: pointer; }
button, .btn, .btn-action { background: var(--primary); cursor: pointer; font-weight: 600; border: none; color: #fff; transition: all 0.15s ease; }
button:hover, .btn:hover, .btn-action:hover { background: #0369a1; transform: translateY(-1px); }
button:disabled, .btn:disabled { opacity: 0.55; cursor: not-allowed; transform: none; }
.btn-danger, .btn-del { background: var(--danger); color: #fff; }
.btn-danger:hover, .btn-del:hover { background: #b91c1c; }
.btn-secondary { background: #334155; border: 1px solid #475569; }
.btn-secondary:hover { background: #475569; }
.btn-action { padding: 5px 9px; font-size: 0.74rem; text-decoration: none; border-radius: 6px; display: inline-flex; align-items: center; gap: 4px; }
.btn-toggle { background: #a16207; color: #fff; }
.btn-toggle:hover { background: #854d0e; }
.edit-form { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.edit-form input, .edit-form select { width: auto; min-width: 90px; padding: 6px 8px; }
.edit-form button { padding: 6px 8px; font-size: 0.74rem; }
.host-link, .viewer-link { color: #7dd3fc; text-decoration: underline; font-weight: 700; }
.host-link:hover, .viewer-link:hover { color: #bae6fd; }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 18px; }
.stat-box { background: #0b1220; border: 1px solid var(--border); border-radius: 10px; padding: 14px; text-align: center; }
.stat-number { font-size: 1.45rem; font-weight: 800; color: #7dd3fc; margin-top: 6px; }

/* Terminal & logs */
.terminal-container { background: #020617; border: 1px solid #1e293b; border-radius: 10px; padding: 14px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, 'Courier New', monospace; font-size: 0.84rem; line-height: 1.55; max-height: 620px; overflow-y: auto; box-shadow: inset 0 2px 10px rgba(0,0,0,0.35); }
.terminal-row { padding: 7px 8px; border-bottom: 1px solid #0f172a; display: flex; align-items: flex-start; gap: 8px; word-break: break-word; }
.terminal-row:hover { background-color: #0b1329; }
.log-tag-connect { color: #22c55e; font-weight: 700; background: rgba(34,197,94,0.12); padding: 2px 7px; border-radius: 999px; white-space: nowrap; }
.log-tag-disconnect { color: #f87171; font-weight: 700; background: rgba(239,68,68,0.12); padding: 2px 7px; border-radius: 999px; white-space: nowrap; }
.log-tag-gift { color: #fbbf24; font-weight: 600; background: rgba(251,191,36,0.12); padding: 2px 7px; border-radius: 999px; white-space: nowrap; }

/* Status cluster states */
.status-cluster { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; justify-content: flex-end; }
.status-indicator-pill.status-neutral { border-color: #475569; color: #cbd5e1; box-shadow: none; }
.status-indicator-pill.status-neutral .status-pulse-dot { background-color: #64748b; animation: none; }
.status-indicator-pill.status-warning { border-color: #f59e0b; color: #fbbf24; box-shadow: 0 0 12px rgba(245,158,11,0.15); }
.status-indicator-pill.status-warning .status-pulse-dot { background-color: #f59e0b; }
.status-indicator-pill.status-offline { border-color: #ef4444; color: #f87171; box-shadow: 0 0 12px rgba(239,68,68,0.12); }
.status-indicator-pill.status-offline .status-pulse-dot { background-color: #ef4444; animation: none; }

/* Notices & helpers */
.notice-success, .notice-info, .notice-warning, .notice-danger { border-radius: 10px; padding: 12px 14px; margin-bottom: 16px; font-weight: 600; border: 1px solid; }
.notice-success { background: rgba(22,163,74,0.12); border-color: #16a34a; color: #86efac; }
.notice-info { background: rgba(2,132,199,0.12); border-color: #0284c7; color: #7dd3fc; }
.notice-warning { background: rgba(202,138,4,0.12); border-color: #ca8a04; color: #fde68a; }
.notice-danger { background: rgba(220,38,38,0.12); border-color: #dc2626; color: #fca5a5; }
.muted { color: var(--muted); font-size: 0.88rem; margin-bottom: 10px; }
.switch-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding: 12px 0; border-bottom: 1px solid #334155; }
.switch-row:last-child { border-bottom: none; }
.switch-label { display: flex; align-items: center; gap: 10px; font-weight: 600; cursor: pointer; }
.switch-help { color: #94a3b8; font-size: 0.82rem; max-width: 520px; text-align: right; line-height: 1.4; }
.master-row { background: rgba(2,132,199,0.08); border: 1px solid rgba(56,189,248,0.15); border-radius: 10px; padding: 14px; margin-bottom: 8px; }
.field-label { display: block; color: #cbd5e1; font-size: 0.78rem; font-weight: 700; margin: 10px 0 6px; text-transform: uppercase; letter-spacing: 0.04em; }
.field-help { display: block; color: #64748b; font-size: 0.77rem; margin-top: 4px; }
.form-grid.two-col { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.full-col { grid-column: 1 / -1; }
.inline-check { display: flex; align-items: center; gap: 8px; font-size: 0.87rem; margin: 8px 0; color: #e2e8f0; }
.inline-fields { display: flex; gap: 8px; }
.inline-fields input { width: 100%; }
.inline-form { display: inline-block; margin: 0; }
.btn-save { width: 100%; padding: 13px 16px; font-size: 0.98rem; background: #15803d; border-radius: 10px; margin-bottom: 18px; }
.btn-save:hover { background: #166534; }
.table-scroll { width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 8px; }
.tier-form, .host-form, .alias-form { grid-template-columns: minmax(130px, 1fr) minmax(120px, 1fr) auto; align-items: end; }
textarea { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border); background: #0b1220; color: white; font-size: 0.87rem; resize: vertical; font-family: inherit; }
.toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom: 12px; }
.toolbar > * { flex: 0 0 auto; }
.empty-state { color: #64748b; font-style: italic; padding: 20px; text-align: center; }
.sticky-actions { position: sticky; bottom: 12px; z-index: 15; }

/* Responsive */
@media (max-width: 760px) {
  body { padding: 14px; }
  h1 { font-size: 1.1rem; }
  h2 { font-size: 0.98rem; }
  .header { margin-bottom: 14px; padding-bottom: 10px; }
  .nav-bar-container { position: static; flex-direction: column; align-items: stretch; gap: 10px; margin-bottom: 14px; padding: 0; background: transparent; backdrop-filter: none; }
  .nav-links { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; width: 100%; }
  .nav-btn { justify-content: center; padding: 9px 8px; font-size: 0.78rem; text-align: center; }
  .status-cluster { flex-direction: column; width: 100%; }
  .status-indicator-pill { width: 100%; justify-content: center; }
  .card { padding: 14px; border-radius: 10px; margin-bottom: 14px; }
  .stat-grid { grid-template-columns: 1fr; gap: 10px; }
  .form-grid, .form-grid.two-col, .tier-form, .host-form, .alias-form { grid-template-columns: 1fr; }
  .switch-row { flex-direction: column; gap: 6px; }
  .switch-help { text-align: left; max-width: none; }
  .inline-fields { flex-direction: column; }
  input, select, button, textarea { width: 100%; font-size: 0.92rem; }
  .btn-action { padding: 7px 9px; }
  th, td { padding: 8px 9px; font-size: 0.79rem; white-space: nowrap; }
  table { min-width: 560px; }
  .terminal-row { flex-direction: column; gap: 4px; }
  .edit-form { flex-wrap: wrap; }
  .edit-form input, .edit-form select { width: 100%; }
}
"""

NAV_HTML = """
<div class="nav-bar-container">
    <nav class="nav-links" aria-label="Admin navigation">
        <a href="/admin/data" class="nav-btn" data-path="/admin/data">🏠 Main Menu</a>
        <a href="/admin/viewers" class="nav-btn" data-path="/admin/viewers">👥 Viewers & Coins</a>
        <a href="/admin/monitor" class="nav-btn" data-path="/admin/monitor">📹 Live Monitor</a>
        <a href="/admin/activity" class="nav-btn" data-path="/admin/activity">📊 Activity Log</a>
        <a href="/admin/analytics" class="nav-btn" data-path="/admin/analytics">📈 Analytics</a>
        <a href="/admin/verifications" class="nav-btn" data-path="/admin/verifications">🔐 Verification</a>
        <a href="/admin/settings" class="nav-btn" data-path="/admin/settings">⚙️ Settings</a>
    </nav>
    <div class="status-cluster" title="Realtime System Indicators">
        <div id="website-status-box" class="status-indicator-pill status-neutral">
            <span class="status-pulse-dot" id="website-status-dot"></span>
            <span id="website-status-text">Website</span>
            <span id="website-ping-badge" class="ping-badge">&bull; Checking...</span>
        </div>
        <div id="tiktok-status-box" class="status-indicator-pill status-neutral">
            <span class="status-pulse-dot" id="tiktok-status-dot"></span>
            <span id="tiktok-status-text">Live: -</span>
            <span id="tiktok-live-badge" class="ping-badge">&bull; -</span>
        </div>
        <div id="discord-status-box" class="status-indicator-pill status-neutral">
            <span class="status-pulse-dot" id="discord-status-dot"></span>
            <span id="discord-status-text">Discord Bot</span>
            <span id="discord-ping-badge" class="ping-badge">&bull; Checking...</span>
        </div>
    </div>
</div>
<script>
(function(){
  try {
    var p = window.location.pathname || '';
    document.querySelectorAll('.nav-links .nav-btn').forEach(function(a){
      var d = a.getAttribute('data-path') || '';
      if (p === d || (d !== '/admin/data' && p.indexOf(d) === 0)) a.classList.add('active');
    });
    var params = new URLSearchParams(window.location.search);
    var msg = params.get('msg');
    var type = params.get('msg_type') || 'success';
    if (msg) {
      var allowed = ['success','info','warning','danger'];
      if (allowed.indexOf(type) === -1) type = 'success';
      var banner = document.createElement('div');
      banner.className = 'notice-' + type;
      banner.style.position = 'relative';
      banner.style.margin = '0 0 16px 0';
      banner.textContent = msg;
      var close = document.createElement('button');
      close.type = 'button';
      close.textContent = '×';
      close.setAttribute('aria-label','Close');
      close.style.cssText = 'position:absolute;right:10px;top:8px;background:transparent;border:none;color:inherit;font-size:18px;cursor:pointer;padding:0 4px;width:auto;';
      close.onclick = function(){ banner.remove(); };
      banner.appendChild(close);
      var anchor = document.querySelector('.nav-bar-container');
      if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(banner, anchor.nextSibling);
      else document.body.insertBefore(banner, document.body.firstChild);
      params.delete('msg'); params.delete('msg_type');
      var qs = params.toString();
      var newUrl = window.location.pathname + (qs ? ('?' + qs) : '');
      window.history.replaceState({}, '', newUrl);
    }
  } catch(e){}
})();
</script>
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
                const accounts = tiktok.accounts || {};
                const names = Object.keys(accounts);
                const liveHosts = names.map(u => {
                    const info = accounts[u] || {};
                    return info.host || u || 'Host';
                });
                const uniqueHosts = [...new Set(liveHosts.filter(Boolean))];
                const liveText = uniqueHosts.length ? ('Live: ' + uniqueHosts.join(', ')) : 'Live: tidak ada';
                setStatusPill(
                    'tiktok',
                    count > 0 ? 'ok' : 'neutral',
                    liveText,
                    count > 0 ? ('• ' + count + ' akun') : '• 0 akun'
                );
                const tiktokPill = document.getElementById('tiktok-status-box');
                if (tiktokPill) {
                    tiktokPill.title = names.length ? ('Akun live: ' + names.join(', ')) : 'Tidak ada akun live';
                }
            })
            .catch(() => {
                setStatusPill('website', 'offline', 'Website Offline', '• Offline');
                setStatusPill('discord', 'offline', 'Discord Unknown', '• no data');
                setStatusPill('tiktok', 'offline', 'TikTok Unknown', '• no data');
            });
    }

    updateSystemStatus();
    setInterval(updateSystemStatus, 15000);

    // Realtime table/log synchronization (safe: avoid disrupting user edits)
    setInterval(function() {
        if (document.hidden) return;
        if (window.location.pathname.indexOf('/admin/settings') === 0) return;
        if (document.getElementById('copy-log-modal')) return;
        const ae = document.activeElement;
        if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT' || ae.isContentEditable)) return;
        fetch(window.location.href, { cache: 'no-store' })
            .then(response => response.text())
            .then(html => {
                const parser = new DOMParser();
                const doc = parser.parseFromString(html, 'text/html');
                const currentCards = document.querySelectorAll('.card');
                const newCards = doc.querySelectorAll('.card');

                currentCards.forEach((card, index) => {
                    if (!newCards[index]) return;
                    if (card.querySelector('form')) return; // don't replace cards containing forms
                    const currentTable = card.querySelector('table');
                    const newTable = newCards[index].querySelector('table');
                    if (currentTable && newTable) {
                        currentTable.innerHTML = newTable.innerHTML;
                    }
                    const currentTerminal = card.querySelector('.terminal-container');
                    const newTerminal = newCards[index].querySelector('.terminal-container');
                    if (currentTerminal && newTerminal) {
                        const nearBottom = (currentTerminal.scrollHeight - currentTerminal.scrollTop - currentTerminal.clientHeight) < 40;
                        currentTerminal.innerHTML = newTerminal.innerHTML;
                        if (nearBottom) currentTerminal.scrollTop = currentTerminal.scrollHeight;
                    }
                });
            })
            .catch(err => console.log('Realtime sync error:', err));
    }, 15000);
</script>
"""

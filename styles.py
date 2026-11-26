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
.table-sortable { cursor: pointer; user-select: none; position: relative; }
.table-sortable::after { content: '↕'; opacity: .45; margin-left: 5px; font-size: .8em; }
.table-sortable[data-sort-direction='asc']::after { content: '↑'; opacity: .95; }
.table-sortable[data-sort-direction='desc']::after { content: '↓'; opacity: .95; }
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
table { border-collapse: collapse; width: 100%; margin-bottom: 8px; font-size: 0.84rem; }
th, td { border-bottom: 1px solid #334155; padding: 8px 10px; text-align: left; vertical-align: middle; font-size: 0.84rem; line-height: 1.35; }
th { background: #0369a1; color: #fff; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.04em; position: sticky; top: 0; z-index: 1; }
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
.edit-form { display: flex; gap: 6px; align-items: center; flex-wrap: nowrap; }
.edit-form input, .edit-form select { width: auto; min-width: 90px; padding: 6px 8px; }
.edit-form button { padding: 6px 8px; font-size: 0.74rem; }
.icon-btn { width: auto !important; min-width: 32px; padding: 6px 8px; font-size: 0.95rem; line-height: 1; display: inline-flex; align-items: center; justify-content: center; }
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
.table-scroll { width: 100%; max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 8px; overscroll-behavior-x: contain; }
/* Always-visible scrollbars: desktop Chrome/Android can hide overlay bars,
   which makes wide tables look unscrollable. */
html { scrollbar-color: #475569 #0b1220; scrollbar-width: thin; }
html::-webkit-scrollbar, body::-webkit-scrollbar, .table-scroll::-webkit-scrollbar, .terminal-container::-webkit-scrollbar { width: 11px; height: 11px; }
html::-webkit-scrollbar-track, body::-webkit-scrollbar-track, .table-scroll::-webkit-scrollbar-track, .terminal-container::-webkit-scrollbar-track { background: #0b1220; }
html::-webkit-scrollbar-thumb, body::-webkit-scrollbar-thumb, .table-scroll::-webkit-scrollbar-thumb, .terminal-container::-webkit-scrollbar-thumb { background: #475569; border-radius: 8px; border: 2px solid #0b1220; }
html::-webkit-scrollbar-thumb:hover, body::-webkit-scrollbar-thumb:hover, .table-scroll::-webkit-scrollbar-thumb:hover, .terminal-container::-webkit-scrollbar-thumb:hover { background: #64748b; }
.table-scroll { scrollbar-width: thin; scrollbar-color: #475569 #0b1220; }
.tt-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; justify-content: flex-end; margin: 0 0 8px; font-size: 0.78rem; color: #94a3b8; }
.tt-bar .tt-count { margin-right: auto; color: #94a3b8; }
.tt-bar .tt-pager { display: inline-flex; align-items: center; gap: 6px; }
.tt-bar button { padding: 4px 9px; font-size: 0.76rem; border-radius: 6px; background: #334155; border: 1px solid #475569; color: #e2e8f0; width: auto; }
.tt-bar button:hover { background: #475569; transform: none; }
.tt-bar button:disabled { opacity: 0.45; cursor: not-allowed; }
.tt-bar select { width: auto; padding: 4px 8px; font-size: 0.76rem; background: #0b1220; }
.tt-bar label { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.tt-page-label { min-width: 54px; text-align: center; color: #cbd5e1; font-variant-numeric: tabular-nums; }
th.tt-sortable { cursor: pointer; user-select: none; white-space: nowrap; }
th.tt-sortable:hover { background: #075985; }
th.tt-sortable::after { content: ' \21C5'; opacity: 0.45; font-size: 0.72em; }
th.tt-sortable[data-tt-dir="asc"]::after { content: ' \25B2'; opacity: 0.95; }
th.tt-sortable[data-tt-dir="desc"]::after { content: ' \25BC'; opacity: 0.95; }
tr.tt-hidden { display: none !important; }
.tier-form, .host-form, .alias-form { grid-template-columns: minmax(130px, 1fr) minmax(120px, 1fr) auto; align-items: end; }
.tier-list { display: flex; flex-direction: column; gap: 10px; }
.tier-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; padding: 12px; background: rgba(2, 6, 23, 0.35); border: 1px solid #334155; border-radius: 10px; }
.tier-meta { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.tier-name { color: #e2e8f0; }
.tier-role { color: #94a3b8; font-size: 0.76rem; overflow-wrap: anywhere; }
.tier-actions { display: flex; align-items: flex-end; gap: 8px; flex-wrap: wrap; }
.tier-coins-form { display: flex; align-items: flex-end; gap: 6px; margin: 0; }
.tier-coins-field { display: flex; flex-direction: column; gap: 4px; }
.tier-coins-field span { color: #94a3b8; font-size: 0.68rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap; }
.tier-coins-field input { width: 120px; }
.tier-delete-form { margin: 0; }
textarea { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border); background: #0b1220; color: white; font-size: 0.87rem; resize: vertical; font-family: inherit; }
.toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom: 12px; }
.toolbar > * { flex: 0 0 auto; }
.empty-state { color: #64748b; font-style: italic; padding: 20px; text-align: center; }
.sticky-actions { position: sticky; bottom: 12px; z-index: 15; }

/* Responsive */
@media (max-width: 760px) {
  body { padding: 12px; }
  h1 { font-size: 1.05rem; }
  h2 { font-size: 0.95rem; margin-bottom: 8px; }
  .tier-row { grid-template-columns: 1fr; align-items: stretch; }
  .tier-actions { align-items: flex-end; }
  .tier-coins-form { flex: 1 1 auto; }
  .tier-coins-field { flex: 1 1 auto; min-width: 0; }
  .tier-coins-field input { width: 100%; }
  h3 { font-size: 0.88rem; }
  .header { margin-bottom: 12px; padding-bottom: 8px; gap: 8px; }
  .nav-bar-container { position: static; flex-direction: column; align-items: stretch; gap: 8px; margin-bottom: 12px; padding: 0; background: transparent; backdrop-filter: none; }
  .nav-links { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; width: 100%; }
  .nav-btn { justify-content: center; padding: 7px 6px; font-size: 0.75rem; text-align: center; }
  .status-cluster { flex-direction: row; width: 100%; gap: 6px; justify-content: flex-start; }
  .status-indicator-pill { width: auto; max-width: 100%; flex: 0 0 auto; justify-content: center; padding: 4px 8px; font-size: 0.7rem; gap: 6px; }
  .ping-badge { font-size: 0.65rem; }
  .card { padding: 12px; border-radius: 10px; margin-bottom: 12px; }
  .card-header { gap: 8px; margin-bottom: 8px; }
  .section-note { font-size: 0.8rem; margin-bottom: 8px; }
  .muted { font-size: 0.82rem; margin-bottom: 8px; }
  .field-label { margin: 8px 0 4px; font-size: 0.72rem; }
  .field-help { font-size: 0.72rem; }
  .notice-success, .notice-info, .notice-warning, .notice-danger { padding: 9px 11px; margin-bottom: 10px; font-size: 0.84rem; }
  .empty-state { padding: 12px; }
  .stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; margin-bottom: 12px; }
  .stat-box { padding: 9px 6px; }
  .stat-number { font-size: 1.08rem; margin-top: 1px; word-break: break-word; }
  .form-grid, .form-grid.two-col, .tier-form, .host-form, .alias-form { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }
  .switch-row { flex-direction: column; gap: 4px; padding: 8px 0; }
  .switch-help { text-align: left; max-width: none; font-size: 0.76rem; }
  .inline-fields { flex-wrap: wrap; }
  input, select, button, textarea { width: 100%; font-size: 0.85rem; padding: 7px 9px; }
  .btn-action { padding: 6px 8px; }
  .tier-actions { flex-wrap: wrap; }
  .tier-delete-form { width: 100%; }
  .tier-delete-form button { width: 100%; }
  th, td { padding: 6px 8px; font-size: 0.78rem; white-space: nowrap; }
  th { font-size: 0.64rem; }
  table { min-width: 100%; }
  .tt-bar { justify-content: space-between; margin-bottom: 6px; }
  .tt-bar .tt-count { width: 100%; }
  #summary-table th, #summary-table td { white-space: normal; }
  .terminal-row { flex-direction: column; gap: 4px; }
  .edit-form { flex-wrap: nowrap; }
  .edit-form input, .edit-form select { width: auto; min-width: 72px; flex: 1 1 auto; }
  .edit-form .icon-btn { width: auto; flex: 0 0 auto; }
}
@media (min-width: 520px) and (max-width: 760px) {
  .stat-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

/* Info icon: hover/focus shows a short explanation. */
.info-i { display: inline-flex; align-items: center; justify-content: center; width: 16px; height: 16px; border-radius: 50%; border: 1px solid #475569; color: #94a3b8; font-size: 10px; font-weight: 800; cursor: help; position: relative; vertical-align: middle; margin-left: 4px; flex: 0 0 auto; }
.info-i:hover, .info-i:focus { border-color: #38bdf8; color: #7dd3fc; }
.info-i::after { content: attr(data-tip); position: absolute; left: 50%; bottom: calc(100% + 7px); transform: translateX(-50%) scale(0.98); background: #0f172a; border: 1px solid #334155; color: #cbd5e1; padding: 6px 9px; border-radius: 7px; font-size: 0.72rem; font-weight: 500; line-height: 1.35; width: max-content; max-width: 240px; white-space: normal; text-align: left; z-index: 60; opacity: 0; pointer-events: none; transition: opacity 0.12s ease; box-shadow: 0 8px 20px rgba(2,6,23,0.5); }
.info-i:hover::after, .info-i:focus::after { opacity: 1; }
/* Admin overview and safer destructive actions */
.dashboard-links {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}
.dashboard-link {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: #0f172a;
  color: var(--text);
  text-decoration: none;
  transition: border-color 0.15s ease, background 0.15s ease, transform 0.15s ease;
}
.dashboard-link strong { color: #7dd3fc; font-size: 0.9rem; }
.dashboard-link span { color: var(--muted); font-size: 0.78rem; line-height: 1.4; }
.dashboard-link:hover { background: #17243a; border-color: #0ea5e9; transform: translateY(-1px); }
.danger-zone {
  border: 1px solid rgba(239,68,68,0.45);
  border-radius: 10px;
  padding: 14px;
  background: rgba(127,29,29,0.10);
}
.danger-zone > summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  cursor: pointer;
  color: #fca5a5;
  font-weight: 700;
  list-style: none;
}
.danger-zone > summary::-webkit-details-marker { display: none; }
.danger-zone > summary::after { content: '+'; font-size: 1.15rem; line-height: 1; }
.danger-zone[open] > summary::after { content: '−'; }
.danger-zone-hint { color: #94a3b8; font-size: 0.78rem; font-weight: 500; }
.danger-zone-content { margin-top: 12px; padding-top: 12px; border-top: 1px solid rgba(239,68,68,0.25); }
@media (max-width: 760px) {
  .dashboard-links { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .dashboard-link { padding: 11px; }
  .danger-zone > summary { align-items: flex-start; flex-direction: column; gap: 4px; }
}

"""

NAV_HTML = """
<div class="nav-bar-container">
    <nav class="nav-links" aria-label="Admin navigation">
        <a href="/admin/data" class="nav-btn" data-path="/admin/data">🏠 Admin Overview</a>
        <a href="/admin/viewers" class="nav-btn" data-path="/admin/viewers">👥 Viewers & Coins</a>
        <a href="/admin/monitor" class="nav-btn" data-path="/admin/monitor">📹 Live Monitor</a>
        <a href="/admin/activity" class="nav-btn" data-path="/admin/activity">📊 Activity Log</a>
        <a href="/admin/analytics" class="nav-btn" data-path="/admin/analytics">📈 Analytics</a>
        <a href="/admin/monthly-archive" class="nav-btn" data-path="/admin/monthly-archive">📊 Monthly Analytics</a>
        <a href="/admin/timeline" class="nav-btn" data-path="/admin/timeline">🗓️ Timeline</a>
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
        <div id="build-status-box" class="status-indicator-pill status-neutral" title="Running Git build code">
            <span id="build-status-text">Build</span>
            <span id="build-badge" class="ping-badge">&bull; ...</span>
        </div>
    </div>
</div>
<script>
(function(){
  try {
    var p = window.location.pathname || '';
    document.querySelectorAll('.nav-links .nav-btn').forEach(function(a){
      var d = a.getAttribute('data-path') || '';
      var active = (p === d) || (d !== '/admin/data' && p.indexOf(d) === 0);
      if (!active && p.indexOf('/admin/viewer') === 0 && d === '/admin/viewers') active = true;
      if (!active && p.indexOf('/admin/host') === 0 && d === '/admin/monitor') active = true;
      if (active) a.classList.add('active');
    });
    var params = new URLSearchParams(window.location.search);
    var msg = params.get('msg');
    var type = params.get('msg_type') || 'success';
    if (msg) {
      if (document.querySelector('.flash-banner-msg')) {
        params.delete('msg'); params.delete('msg_type');
        var qs0 = params.toString();
        window.history.replaceState({}, '', window.location.pathname + (qs0 ? ('?' + qs0) : ''));
        return;
      }
      var allowed = ['success','info','warning','danger'];
      if (allowed.indexOf(type) === -1) type = 'success';
      var banner = document.createElement('div');
      banner.className = 'notice-' + type + ' flash-banner-msg';
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
    try {
      fetch('/admin/build-info', { cache: 'no-store' })
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(function(info){
          if (!info) return;
          var code = info.commit || info.version || info.env || 'unknown';
          var el = document.getElementById('build-badge');
          var box = document.getElementById('build-status-box');
          if (el) el.textContent = code;
          if (box) box.title = 'Build: ' + code + (info.env ? (' (' + info.env + ')') : '');
        })
        .catch(function(){});
    } catch(e){}
  } catch(e){}
})();
</script>
<script>
(function(){
  function parseValue(text){
    var s=(text||'').trim().replace(/\s+/g,' ');
    if(!s) return {type:'empty',value:''};
    var date=Date.parse(s);
    if(/[0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2}|[0-9]{1,2}:[0-9]{2}/.test(s)&&date) return {type:'date',value:date};
    var n=s.replace(/[.,](?=\d{3}(\D|$))/g,'').replace(/[^0-9.-]/g,'');
    if(n && /^-?\d+(\.\d+)?$/.test(n)) return {type:'number',value:Number(n)};
    return {type:'text',value:s.toLocaleLowerCase()};
  }
  document.querySelectorAll('table').forEach(function(table){
    var header=Array.from(table.rows).find(function(r){return r.querySelector('th');});
    if(!header) return;
    Array.from(header.cells).forEach(function(th,index){
      if(th.tagName!=='TH') return;
      th.classList.add('table-sortable');
      th.setAttribute('tabindex','0');
      th.setAttribute('role','button');
      th.setAttribute('aria-label',(th.textContent.trim()||'Column')+'; click to sort');
      th.title='Click to sort';
      var direction=1;
      function sort(){
        var rows=Array.from(table.rows).filter(function(r){return r!==header && !r.querySelector('th');});
        var first=rows.map(function(r){return parseValue(r.cells[index] ? r.cells[index].innerText : '');});
        var nonEmpty=first.filter(function(v){return v.type!=='empty';});
        var numeric=nonEmpty.length && nonEmpty.every(function(v){return v.type==='number';});
        var dates=nonEmpty.length && nonEmpty.every(function(v){return v.type==='date';});
        rows.sort(function(a,b){
          var av=parseValue(a.cells[index]?a.cells[index].innerText:'');
          var bv=parseValue(b.cells[index]?b.cells[index].innerText:'');
          if(av.type==='empty') return 1; if(bv.type==='empty') return -1;
          var aa= numeric||dates ? av.value : av.value;
          var bb= numeric||dates ? bv.value : bv.value;
          return (aa<bb?-1:aa>bb?1:0)*direction;
        });
        var parent=header.parentNode;
        rows.forEach(function(row){parent.appendChild(row);});
        direction*=-1;
        Array.from(header.cells).forEach(function(c){c.removeAttribute('data-sort-direction');});
        th.setAttribute('data-sort-direction',direction===-1?'asc':'desc');
      }
      th.addEventListener('click',sort);
      th.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();sort();}});
    });
  });
})();
</script>

AUTO_REFRESH_JS = r"""
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
                    // Show the account when the host is not detected yet instead
                    // of an empty/"Host" label.
                    return (info.host && info.host !== 'Host') ? info.host : ('@' + u);
                });
                const uniqueHosts = [...new Set(liveHosts.filter(Boolean))];
                const liveText = uniqueHosts.length ? ('Live: ' + uniqueHosts.join(', ')) : 'Live: none';
                setStatusPill(
                    'tiktok',
                    count > 0 ? 'ok' : 'neutral',
                    liveText,
                    count > 0 ? ('• ' + count + ' accounts') : '• 0 accounts'
                );
                const tiktokPill = document.getElementById('tiktok-status-box');
                if (tiktokPill) {
                    tiktokPill.title = names.length ? ('Live accounts: ' + names.join(', ')) : 'No live accounts';
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
        if (window.location.pathname.indexOf('/admin/timeline') === 0) return;
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
                    const currentTable = card.querySelector('table');
                    const newTable = newCards[index].querySelector('table');
                    // The refresh cycle is paused whenever an input/select/textarea
                    // is focused (guard above), so tables containing action forms can
                    // also be refreshed. Skipping those tables left Live Monitor host
                    // names stale because its rows contain resolve/delete controls.
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
                // Keep asynchronously fetched LIVE thumbnail previews in sync
                // without replacing forms, tables, or the whole page.
                const currentPreviews = document.querySelectorAll('.live-thumbnail-grid');
                const newPreviews = doc.querySelectorAll('.live-thumbnail-grid');
                currentPreviews.forEach((preview, index) => {
                    if (newPreviews[index]) preview.innerHTML = newPreviews[index].innerHTML;
                });
            })
            .catch(err => console.log('Realtime sync error:', err));
    }, 15000);

    // ---------------------------------------------------------------
    // Table tools: sortable columns + page size (10 / 50 / 100 / All).
    // Safe with live refresh: re-applied when a table's content changes.
    // ---------------------------------------------------------------
    (function () {
        if (window.__ttReady) return;
        window.__ttReady = true;
        var SIZES = ['10', '50', '100', 'all'];
        var states = new WeakMap();
        var seq = 0;

        function headerRow(table) {
            if (table.tHead && table.tHead.rows.length) return table.tHead.rows[0];
            var rows = table.rows || [];
            for (var i = 0; i < rows.length; i++) {
                var cells = rows[i].cells ? Array.prototype.slice.call(rows[i].cells) : [];
                if (cells.length && cells.every(function (c) { return c.tagName === 'TH'; })) return rows[i];
            }
            return null;
        }

        function bodyRows(table, head) {
            var rows = table.rows || [];
            var out = [];
            for (var i = 0; i < rows.length; i++) {
                var r = rows[i];
                if (r === head) continue;
                if (r.cells && r.cells.length === 1 && r.cells[0].colSpan > 1) continue;
                out.push(r);
            }
            return out;
        }

        function cellValue(cell) {
            var raw = (cell && (cell.getAttribute('data-sort') || cell.textContent) || '').trim();
            var compact = raw.replace(/\s+/g, '');
            if (/^[+-]?\d+(?:[.,]\d+)*$/.test(compact)) {
                var n = parseFloat(compact.replace(/,/g, ''));
                if (!isNaN(n)) return n;
            }
            return raw.toLowerCase();
        }

        function applySort(table, st) {
            if (st.col < 0) return;
            var head = headerRow(table);
            if (!head) return;
            var rows = bodyRows(table, head);
            if (rows.length < 2) return;
            var dir = st.dir;
            var sorted = rows.slice().sort(function (a, b) {
                var av = cellValue(a.cells[st.col]);
                var bv = cellValue(b.cells[st.col]);
                if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * dir;
                return String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: 'base' }) * dir;
            });
            var changed = false;
            for (var i = 0; i < sorted.length; i++) { if (sorted[i] !== rows[i]) { changed = true; break; } }
            if (!changed) return;
            var parent = rows[0].parentNode;
            var frag = document.createDocumentFragment();
            sorted.forEach(function (r) { frag.appendChild(r); });
            parent.appendChild(frag);
        }

        function applyPage(table, st) {
            var head = headerRow(table);
            if (!head) return;
            var rows = bodyRows(table, head);
            var size = st.size === 'all' ? (rows.length || 1) : (parseInt(st.size, 10) || 10);
            var pages = Math.max(1, Math.ceil(rows.length / size));
            if (st.page > pages - 1) st.page = pages - 1;
            if (st.page < 0) st.page = 0;
            rows.forEach(function (r, i) {
                var hide = st.size !== 'all' && (i < st.page * size || i >= (st.page + 1) * size);
                if (hide) r.classList.add('tt-hidden');
                else r.classList.remove('tt-hidden');
            });
            updateBar(st, rows.length, pages);
        }

        function updateBar(st, count, pages) {
            if (!st.bar) return;
            st.bar.style.display = '';
            var countEl = st.bar.querySelector('.tt-count');
            var pager = st.bar.querySelector('.tt-pager');
            var label = st.bar.querySelector('.tt-page-label');
            var prev = st.bar.querySelector('[data-tt-prev]');
            var next = st.bar.querySelector('[data-tt-next]');
            if (countEl) countEl.textContent = count + ' baris';
            if (!pager) return;
            if (st.size === 'all' || pages <= 1) { pager.style.display = 'none'; return; }
            pager.style.display = '';
            if (label) label.textContent = (st.page + 1) + ' / ' + pages;
            if (prev) prev.disabled = st.page <= 0;
            if (next) next.disabled = st.page >= pages - 1;
        }

        function ensureBar(table, st) {
            if (st.bar && st.bar.isConnected) return;
            var wrap = table.closest ? table.closest('.table-scroll') : null;
            if (!wrap) {
                wrap = document.createElement('div');
                wrap.className = 'table-scroll';
                table.parentNode.insertBefore(wrap, table);
                wrap.appendChild(table);
            }
            var bar = document.createElement('div');
            bar.className = 'tt-bar';
            bar.innerHTML = '<span class="tt-count"></span>'
                + '<label>Rows <select></select></label>'
                + '<span class="tt-pager"><button type="button" data-tt-prev title="Previous">\u2039</button>'
                + '<span class="tt-page-label"></span>'
                + '<button type="button" data-tt-next title="Next">\u203A</button></span>';
            var sel = bar.querySelector('select');
            SIZES.forEach(function (s) {
                var opt = document.createElement('option');
                opt.value = s;
                opt.textContent = (s === 'all') ? 'All' : s;
                sel.appendChild(opt);
            });
            sel.value = st.size;
            sel.addEventListener('change', function () {
                st.size = sel.value;
                st.page = 0;
                applyPage(table, st);
            });
            bar.querySelector('[data-tt-prev]').addEventListener('click', function () {
                st.page = Math.max(0, st.page - 1);
                applyPage(table, st);
            });
            bar.querySelector('[data-tt-next]').addEventListener('click', function () {
                st.page = st.page + 1;
                applyPage(table, st);
            });
            wrap.parentNode.insertBefore(bar, wrap);
            st.bar = bar;
        }

        function attach(table) {
            if (!table || table.tagName !== 'TABLE') return;
            if (table.classList && table.classList.contains('no-table-tools')) return;
            if (table.closest && table.closest('.tl-detail')) return;
            var st = states.get(table);
            if (!st) { st = { page: 0, size: '10', col: -1, dir: 1, id: ++seq }; states.set(table, st); }
            var head = headerRow(table);
            if (!head) return;
            var rows = bodyRows(table, head);
            if (!rows.length) { if (st.bar) st.bar.style.display = 'none'; return; }
            ensureBar(table, st);
            var ths = Array.prototype.slice.call(head.cells);
            ths.forEach(function (th, idx) {
                if (!th.textContent.trim()) return;
                th.classList.add('tt-sortable');
                if (th.dataset.ttBound === '1') return;
                th.dataset.ttBound = '1';
                th.addEventListener('click', function () {
                    if (st.col === idx) st.dir = (st.dir === 1 ? -1 : 1);
                    else { st.col = idx; st.dir = 1; }
                    ths.forEach(function (h) { delete h.dataset.ttDir; });
                    th.dataset.ttDir = st.dir === 1 ? 'asc' : 'desc';
                    st.page = 0;
                    applySort(table, st);
                    applyPage(table, st);
                });
            });
            if (st.col >= 0 && ths[st.col]) ths[st.col].dataset.ttDir = (st.dir === 1 ? 'asc' : 'desc');
            applySort(table, st);
            applyPage(table, st);
        }

        function attachAll() {
            var tables = document.querySelectorAll('table');
            for (var i = 0; i < tables.length; i++) {
                try { attach(tables[i]); } catch (e) {}
            }
        }

        var timer = null;
        var obs = new MutationObserver(function () {
            if (timer) return;
            timer = setTimeout(function () { timer = null; attachAll(); }, 120);
        });
        function start() {
            attachAll();
            obs.observe(document.body, { childList: true, subtree: true });
        }
        if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
        else start();
    })();
</script>
"""

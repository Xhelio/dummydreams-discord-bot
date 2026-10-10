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

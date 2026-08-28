// Self-contained dashboard page - no external CDN dependencies (fonts,
// scripts, or styles), so it works reliably and never depends on a
// third-party host being up. Talks to the /dashboard/api/* endpoints on the
// same origin, which the browser's cached Basic-Auth credential already
// covers once the page itself has loaded.
export const DASHBOARD_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mercury Dashboard</title>
<style>
  :root {
    --bg: #0b0f14;
    --panel: #131a22;
    --panel-2: #1a232e;
    --border: #263241;
    --text: #e6edf3;
    --muted: #8b98a5;
    --accent: #4da3ff;
    --good: #2ea043;
    --good-bg: rgba(46, 160, 67, 0.12);
    --warn: #d29922;
    --warn-bg: rgba(210, 153, 34, 0.12);
    --bad: #f85149;
    --bad-bg: rgba(248, 81, 73, 0.12);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.4;
  }
  header {
    padding: 20px 28px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
  }
  header h1 {
    font-size: 20px;
    margin: 0;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  header h1 .dot {
    width: 9px; height: 9px; border-radius: 50%;
    background: var(--good);
    box-shadow: 0 0 0 4px var(--good-bg);
  }
  header .sub { color: var(--muted); font-size: 13px; }
  main { padding: 24px 28px 60px; max-width: 1200px; margin: 0 auto; }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 14px;
    margin-bottom: 28px;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
  }
  .card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .card .value { font-size: 28px; font-weight: 650; margin-top: 6px; }
  .card.bad .value { color: var(--bad); }
  .card.warn .value { color: var(--warn); }
  .card.good .value { color: var(--good); }
  section { margin-bottom: 32px; }
  section h2 { font-size: 15px; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: .04em; margin: 0 0 12px; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  .tabs { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
  .tabs button {
    background: var(--panel);
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 13px;
    cursor: pointer;
  }
  .tabs button.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: .03em; }
  tr:last-child td { border-bottom: none; }
  tr.disp-250 { background: transparent; }
  tr.disp-421 { background: var(--warn-bg); }
  tr.disp-550 { background: var(--bad-bg); }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 20px; font-size: 11px; font-weight: 600; }
  .pill.disp-250 { background: var(--good-bg); color: var(--good); }
  .pill.disp-421 { background: var(--warn-bg); color: var(--warn); }
  .pill.disp-550 { background: var(--bad-bg); color: var(--bad); }
  .pill.alert-URGENT { background: var(--bad-bg); color: var(--bad); }
  .pill.alert-STANDARD { background: var(--warn-bg); color: var(--warn); }
  .muted { color: var(--muted); }
  .bars { display: flex; flex-direction: column; gap: 8px; }
  .bar-row { display: flex; align-items: center; gap: 10px; font-size: 13px; }
  .bar-row .bar-label { width: 150px; flex-shrink: 0; color: var(--muted); }
  .bar-row .bar-track { flex: 1; background: var(--panel-2); border-radius: 4px; height: 10px; overflow: hidden; }
  .bar-row .bar-fill { height: 100%; background: var(--accent); border-radius: 4px; }
  .bar-row .bar-count { width: 36px; text-align: right; }
  .empty { padding: 32px; text-align: center; color: var(--muted); }
  .scroll-x { overflow-x: auto; }
  .subject-cell { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  @media (max-width: 640px) {
    main { padding: 16px; }
    header { padding: 16px; }
    .bar-row .bar-label { width: 90px; }
  }
</style>
</head>
<body>
<header>
  <h1><span class="dot"></span> Mercury Dashboard</h1>
  <div class="sub" id="asof">loading...</div>
</header>
<main>
  <div class="cards" id="cards"></div>

  <section>
    <h2>Category breakdown (last 7 days)</h2>
    <div class="panel" style="padding: 18px 20px;">
      <div class="bars" id="categoryBars"><div class="empty">Loading...</div></div>
    </div>
  </section>

  <section>
    <h2>Recent activity</h2>
    <div class="tabs" id="tabs">
      <button data-filter="" class="active">All</button>
      <button data-filter="250">Accepted</button>
      <button data-filter="421">Soft-deferred</button>
      <button data-filter="550">Hard-bounced</button>
    </div>
    <div class="panel scroll-x">
      <table id="messagesTable">
        <thead>
          <tr><th>Time</th><th>From</th><th>Subject</th><th>Category</th><th>Verdict</th><th>Disposition</th><th>Alert</th></tr>
        </thead>
        <tbody><tr><td colspan="7" class="empty">Loading...</td></tr></tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>Recent rule changes</h2>
    <div class="panel scroll-x">
      <table id="rulesTable">
        <thead><tr><th>Time</th><th>Action</th><th>Rule</th><th>Source</th></tr></thead>
        <tbody><tr><td colspan="4" class="empty">Loading...</td></tr></tbody>
      </table>
    </div>
  </section>

  <section>
    <h2>Recent actions</h2>
    <div class="panel scroll-x">
      <table id="actionsTable">
        <thead><tr><th>Time</th><th>Kind</th><th>Domain</th><th>Result</th><th>Details</th></tr></thead>
        <tbody><tr><td colspan="5" class="empty">Loading...</td></tr></tbody>
      </table>
    </div>
  </section>
</main>

<script>
function esc(s) {
  return (s ?? '').toString().replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

async function loadSummary() {
  const res = await fetch('/dashboard/api/summary');
  const data = await res.json();
  document.getElementById('asof').textContent = 'as of ' + new Date().toLocaleString();

  const cards = [
    { label: 'Messages (24h)', value: data.last24h.total, cls: '' },
    { label: 'Hard bounces (24h)', value: data.last24h.hardBounces, cls: data.last24h.hardBounces > 0 ? 'bad' : 'good' },
    { label: 'Urgent alerts (24h)', value: data.last24h.urgent, cls: data.last24h.urgent > 0 ? 'bad' : 'good' },
    { label: 'Actions taken (24h)', value: data.last24h.actions, cls: '' },
    { label: 'Rule changes (7d)', value: data.last7d.ruleChanges, cls: '' },
    { label: 'Standing rules', value: data.ruleCount, cls: '' },
  ];
  document.getElementById('cards').innerHTML = cards.map(c =>
    \`<div class="card \${c.cls}"><div class="label">\${esc(c.label)}</div><div class="value">\${c.value}</div></div>\`
  ).join('');

  const maxCount = Math.max(1, ...data.categories.map(c => c.count));
  document.getElementById('categoryBars').innerHTML = data.categories.length
    ? data.categories.map(c => \`
        <div class="bar-row">
          <div class="bar-label">\${esc(c.category || 'UNKNOWN')}</div>
          <div class="bar-track"><div class="bar-fill" style="width:\${(c.count / maxCount * 100).toFixed(0)}%"></div></div>
          <div class="bar-count">\${c.count}</div>
        </div>\`).join('')
    : '<div class="empty">No data yet.</div>';
}

async function loadMessages(filter) {
  const url = '/dashboard/api/messages' + (filter ? '?disposition=' + filter : '');
  const res = await fetch(url);
  const rows = await res.json();
  const tbody = document.querySelector('#messagesTable tbody');
  tbody.innerHTML = rows.length ? rows.map(r => \`
    <tr class="disp-\${esc(r.enforced_disposition)}">
      <td>\${esc(fmtTime(r.received_at))}</td>
      <td>\${esc(r.from_domain)}</td>
      <td class="subject-cell" title="\${esc(r.subject)}">\${esc(r.subject)}</td>
      <td>\${esc(r.category)}</td>
      <td>\${esc(r.verdict)}</td>
      <td><span class="pill disp-\${esc(r.enforced_disposition)}">\${esc(r.enforced_disposition)}</span></td>
      <td>\${r.alert_level && r.alert_level !== 'NONE' ? \`<span class="pill alert-\${esc(r.alert_level)}">\${esc(r.alert_level)}</span>\` : '<span class="muted">-</span>'}</td>
    </tr>\`).join('') : '<tr><td colspan="7" class="empty">Nothing here yet.</td></tr>';
}

async function loadRules() {
  const res = await fetch('/dashboard/api/rules');
  const rows = await res.json();
  document.querySelector('#rulesTable tbody').innerHTML = rows.length ? rows.map(r => \`
    <tr>
      <td>\${esc(fmtTime(r.changed_at))}</td>
      <td>\${esc(r.action)}</td>
      <td>\${esc(r.rule_text)}</td>
      <td class="muted">\${esc(r.source)}</td>
    </tr>\`).join('') : '<tr><td colspan="4" class="empty">No rule changes yet.</td></tr>';
}

async function loadActions() {
  const res = await fetch('/dashboard/api/actions');
  const rows = await res.json();
  document.querySelector('#actionsTable tbody').innerHTML = rows.length ? rows.map(r => \`
    <tr>
      <td>\${esc(fmtTime(r.executed_at))}</td>
      <td>\${esc(r.kind)}</td>
      <td>\${esc(r.domain) || '<span class="muted">-</span>'}</td>
      <td>\${esc(r.result) || '<span class="muted">-</span>'}</td>
      <td class="subject-cell" title="\${esc(r.outcome_summary)}">\${esc(r.outcome_summary)}</td>
    </tr>\`).join('') : '<tr><td colspan="5" class="empty">No actions yet.</td></tr>';
}

document.getElementById('tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  document.querySelectorAll('#tabs button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadMessages(btn.dataset.filter);
});

loadSummary();
loadMessages('');
loadRules();
loadActions();
</script>
</body>
</html>
`;

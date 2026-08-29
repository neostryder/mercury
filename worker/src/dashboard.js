// Self-contained dashboard page - no external CDN dependencies (fonts,
// scripts, or styles), so it works reliably and never depends on a
// third-party host being up. Talks to the /dashboard/api/* endpoints on the
// same origin, which the browser's cached Basic-Auth credential already
// covers once the page itself has loaded.

// Duplicated from the page's own CSS custom properties (:root, below)
// rather than referenced via var() - these values are baked into SVG
// presentation attributes generated here on the Worker, outside the
// stylesheet's cascade, so there is nothing for var() to resolve against.
const CHART_COLORS = {
  border: '#263241',
  muted: '#8b98a5',
  accent: '#4da3ff',
  good: '#2ea043',
  warn: '#d29922',
  bad: '#f85149',
  extra: ['#a371f7', '#39c5cf', '#e3b341', '#ff9bce', '#7ee787', '#ffa657'],
};

function escSvg(s) {
  return (s ?? '').toString().replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function isoDate(d) {
  return d.toISOString().slice(0, 10);
}

// Dense ascending list of the last `days` UTC dates, so a chart has one bar
// per day even for a day D1's GROUP BY produced no row for at all.
function lastNDays(days) {
  const out = [];
  // Phoenix is a fixed UTC-7 offset (no DST) - shift before reading the UTC
  // calendar fields so "today" lines up with the same day the D1 queries
  // bucket by (see the matching `date(received_at, '-7 hours')` in index.js).
  const now = new Date(Date.now() - 7 * 60 * 60 * 1000);
  for (let i = days - 1; i >= 0; i--) {
    out.push(isoDate(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i))));
  }
  return out;
}

function shortDay(iso) {
  return new Date(iso + 'T00:00:00Z').toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

// Renders a stacked bar chart as raw SVG markup, built entirely from D1
// query results on the Worker itself - no client-side charting library, no
// canvas. `days` is an ascending array of 'YYYY-MM-DD' strings; `series` is
// [{ key, label, color }]; `byDay` maps a date to { [key]: count }, missing
// keys treated as 0.
function renderStackedBarSVG(days, series, byDay, { width = 760, height = 220 } = {}) {
  const padL = 34, padR = 12, padT = 10, padB = 30;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const n = Math.max(1, days.length);
  const barGap = 2;
  const barW = Math.max(1, plotW / n - barGap);

  const totals = days.map((d) => {
    const row = byDay[d] || {};
    return series.reduce((sum, s) => sum + (row[s.key] || 0), 0);
  });
  const maxTotal = Math.max(1, ...totals);

  const gridLines = [0, 0.25, 0.5, 0.75, 1].map((frac) => {
    const y = padT + plotH * (1 - frac);
    return `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${width - padR}" y2="${y.toFixed(1)}" stroke="${CHART_COLORS.border}" stroke-width="1" />` +
      `<text x="${(padL - 6).toFixed(1)}" y="${(y + 3).toFixed(1)}" text-anchor="end" font-size="9" fill="${CHART_COLORS.muted}">${Math.round(maxTotal * frac)}</text>`;
  }).join('');

  const bars = days.map((d, i) => {
    const row = byDay[d] || {};
    const x = padL + i * (plotW / n);
    let yCursor = padT + plotH;
    return series.map((s) => {
      const v = row[s.key] || 0;
      if (v <= 0) return '';
      const segH = (v / maxTotal) * plotH;
      yCursor -= segH;
      return `<rect x="${x.toFixed(1)}" y="${yCursor.toFixed(1)}" width="${barW.toFixed(1)}" height="${segH.toFixed(1)}" fill="${s.color}"><title>${escSvg(shortDay(d))} - ${escSvg(s.label)}: ${v}</title></rect>`;
    }).join('');
  }).join('');

  const labelEvery = Math.max(1, Math.ceil(n / 8));
  const xLabels = days.map((d, i) => {
    if (i % labelEvery !== 0) return '';
    const x = padL + i * (plotW / n) + barW / 2;
    return `<text x="${x.toFixed(1)}" y="${height - 8}" text-anchor="middle" font-size="9" fill="${CHART_COLORS.muted}">${escSvg(shortDay(d))}</text>`;
  }).join('');

  const legend = series.map((s, i) => `<g transform="translate(${padL + i * 108}, ${(height - padB + 20).toFixed(1)})"><rect width="9" height="9" fill="${s.color}" /><text x="14" y="8.5" font-size="10" fill="${CHART_COLORS.muted}">${escSvg(s.label)}</text></g>`).join('');

  return `<svg viewBox="0 0 ${width} ${height + 20}" xmlns="http://www.w3.org/2000/svg">${gridLines}${bars}${xLabels}${legend}</svg>`;
}

export { CHART_COLORS, lastNDays, renderStackedBarSVG };

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
  .chart-title { font-size: 12px; color: var(--muted); margin-bottom: 8px; }
  .chart-wrap svg { width: 100%; height: auto; display: block; }
  .chart-panel { margin-bottom: 14px; }
  tr.bounce-row { cursor: pointer; }
  tr.bounce-row .caret { display: inline-block; transition: transform .1s; color: var(--muted); }
  tr.bounce-row.open .caret { transform: rotate(90deg); }
  tr.bounce-detail td { background: var(--panel-2); }
  .detail-box { white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; max-height: 320px; overflow-y: auto; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 4px; }
  .detail-block { margin-bottom: 14px; }
  .btn { background: var(--accent); color: #fff; border: none; border-radius: 6px; padding: 6px 12px; font-size: 12px; cursor: pointer; }
  .btn.danger { background: var(--bad); }
  .btn:disabled { opacity: .5; cursor: default; }
  .reverse-status { font-size: 12px; color: var(--muted); margin-left: 8px; }
  .action-item-row { display: flex; align-items: flex-start; gap: 10px; padding: 12px 18px; border-bottom: 1px solid var(--border); }
  .action-item-row:last-child { border-bottom: none; }
  .action-item-row.done { opacity: .45; }
  .action-item-row.done .action-item-summary { text-decoration: line-through; }
  .action-item-row input[type="checkbox"] { width: 16px; height: 16px; margin-top: 2px; flex-shrink: 0; }
  .action-item-meta { font-size: 11px; color: var(--muted); margin-top: 2px; }
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
    <h2>Trends (last 30 days)</h2>
    <div class="panel chart-panel" style="padding: 18px 20px;">
      <div class="chart-title">Message volume by disposition</div>
      <div class="chart-wrap" id="volumeTrend"><div class="empty">Loading...</div></div>
    </div>
    <div class="panel" style="padding: 18px 20px;">
      <div class="chart-title">Category volume</div>
      <div class="chart-wrap" id="categoryTrend"><div class="empty">Loading...</div></div>
    </div>
  </section>

  <section>
    <h2>Action items</h2>
    <div class="panel" id="actionItemsPanel"><div class="empty">Loading...</div></div>
  </section>

  <section>
    <h2>Hard bounces</h2>
    <div class="panel scroll-x">
      <table id="bouncesTable">
        <thead><tr><th></th><th>Time</th><th>From</th><th>Subject</th><th>Category</th><th>Rule</th></tr></thead>
        <tbody><tr><td colspan="6" class="empty">Loading...</td></tr></tbody>
      </table>
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

async function loadTrends() {
  const res = await fetch('/dashboard/api/trends');
  const data = await res.json();
  document.getElementById('volumeTrend').innerHTML = data.volumeSvg || '<div class="empty">No data yet.</div>';
  document.getElementById('categoryTrend').innerHTML = data.categorySvg || '<div class="empty">No data yet.</div>';
}

const bounceDetailCache = {};

async function loadHardBounces() {
  const res = await fetch('/dashboard/api/hard-bounces');
  const rows = await res.json();
  const tbody = document.querySelector('#bouncesTable tbody');
  tbody.innerHTML = rows.length ? rows.map(r => \`
    <tr class="bounce-row" data-id="\${r.id}">
      <td><span class="caret">&#9656;</span></td>
      <td>\${esc(fmtTime(r.received_at))}</td>
      <td>\${esc(r.from_domain)}</td>
      <td class="subject-cell" title="\${esc(r.subject)}">\${esc(r.subject)}</td>
      <td>\${esc(r.category)}</td>
      <td>\${r.triggered_rule ? '<span class="pill disp-421">rule</span>' : '<span class="muted">-</span>'}</td>
    </tr>
    <tr class="bounce-detail" data-detail-for="\${r.id}" style="display:none;"><td colspan="6"></td></tr>\`).join('')
    : '<tr><td colspan="6" class="empty">No hard bounces yet.</td></tr>';
}

async function toggleBounceDetail(row) {
  const id = row.dataset.id;
  const detailRow = document.querySelector(\`tr.bounce-detail[data-detail-for="\${id}"]\`);
  const wasOpen = row.classList.contains('open');
  document.querySelectorAll('tr.bounce-row.open').forEach(r => r.classList.remove('open'));
  document.querySelectorAll('tr.bounce-detail').forEach(d => d.style.display = 'none');
  if (wasOpen) return;

  row.classList.add('open');
  const cell = detailRow.querySelector('td');
  detailRow.style.display = '';
  cell.innerHTML = '<div class="empty">Loading...</div>';

  let detail = bounceDetailCache[id];
  if (!detail) {
    const res = await fetch('/dashboard/api/hard-bounces/' + id);
    if (!res.ok) {
      cell.innerHTML = '<div class="empty">Could not load this message.</div>';
      return;
    }
    detail = await res.json();
    bounceDetailCache[id] = detail;
  }

  cell.innerHTML = \`
    <div style="padding: 14px 4px;">
      <div class="detail-block">
        <strong>Judge reasoning</strong>
        <div class="detail-box">\${esc(detail.reasoning || detail.analysis || '(none saved)')}</div>
      </div>
      <div class="detail-block">
        <strong>Full saved message</strong>
        <div class="detail-box">\${esc(detail.full_content || '(none saved)')}</div>
      </div>
      \${detail.triggered_rule ? \`
      <div class="detail-block">
        <strong>Rule that triggered this bounce</strong>
        <div class="detail-box">\${esc(detail.triggered_rule)}</div>
        <button class="btn danger" data-reverse-rule="\${esc(detail.triggered_rule)}">Reverse this rule</button>
        <span class="reverse-status"></span>
      </div>\` : '<div class="muted">No specific standing rule was identified for this disposition.</div>'}
    </div>\`;
}

async function reverseRule(btn) {
  const rule = btn.dataset.reverseRule;
  if (!window.confirm('Remove this rule from the standing rules ledger?')) return;
  btn.disabled = true;
  const status = btn.nextElementSibling;
  status.textContent = 'Reversing...';
  try {
    const res = await fetch('/dashboard/api/rules/reverse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rule }),
    });
    const data = await res.json();
    if (res.ok && data.ok !== false) {
      status.textContent = 'Reversed.';
    } else {
      status.textContent = 'Failed: ' + (data.error || res.status);
      btn.disabled = false;
    }
  } catch (err) {
    status.textContent = 'Failed: ' + err;
    btn.disabled = false;
  }
}

document.getElementById('bouncesTable').addEventListener('click', (e) => {
  const reverseBtn = e.target.closest('[data-reverse-rule]');
  if (reverseBtn) {
    reverseRule(reverseBtn);
    return;
  }
  const row = e.target.closest('tr.bounce-row');
  if (row) toggleBounceDetail(row);
});

async function loadActionItems() {
  const res = await fetch('/dashboard/api/action-items');
  const rows = await res.json();
  const panel = document.getElementById('actionItemsPanel');
  panel.innerHTML = rows.length ? rows.map(r => \`
    <div class="action-item-row" data-id="\${r.id}">
      <input type="checkbox" data-complete-id="\${r.id}">
      <div>
        <div class="action-item-summary">\${esc(r.summary)}</div>
        <div class="action-item-meta">\${esc(r.kind)} - \${esc(fmtTime(r.created_at))}\${r.related_message_id ? ' - message #' + r.related_message_id : ''}</div>
      </div>
    </div>\`).join('') : '<div class="empty">No open action items.</div>';
}

document.getElementById('actionItemsPanel').addEventListener('change', async (e) => {
  const box = e.target.closest('[data-complete-id]');
  if (!box || !box.checked) return;
  const id = box.dataset.completeId;
  const row = box.closest('.action-item-row');
  box.disabled = true;
  try {
    const res = await fetch('/dashboard/api/action-items/' + id + '/complete', { method: 'POST' });
    const data = await res.json();
    if (data.ok && data.completed) {
      row.classList.add('done');
    } else {
      box.checked = false;
      box.disabled = false;
    }
  } catch (err) {
    box.checked = false;
    box.disabled = false;
  }
});

loadSummary();
loadMessages('');
loadRules();
loadActions();
loadTrends();
loadHardBounces();
loadActionItems();
</script>
</body>
</html>
`;

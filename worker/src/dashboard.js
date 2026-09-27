// Self-contained dashboard page - no external CDN dependencies (fonts,
// scripts, or styles), so it works reliably and never depends on a
// third-party host being up. Talks to the /dashboard/api/* endpoints on the
// same origin, all of which sit behind the same Cloudflare Access application
// as the page itself.
//
// The page script below lives inside a template literal, so it uses no
// backticks, no dollar-brace sequences and no backslashes.

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

// Each category keeps one color class wherever it appears, so its color does
// not change when its rank in the chart does.
const CATEGORY_CLASSES = {
  NEWSLETTER: 'cat-1', PROMOTIONAL: 'cat-2', TRANSACTIONAL: 'cat-3', SHIPPING_DELIVERY: 'cat-4',
  ACCOUNT_SECURITY: 'cat-5', PERSONAL: 'cat-6', SOCIAL: 'cat-7', FINANCIAL: 'cat-8',
  POLITICAL_FUNDRAISING: 'cat-9', PHISHING: 'cat-10', SCAM: 'cat-11', MALWARE: 'cat-12',
};
function categoryClass(category) {
  return CATEGORY_CLASSES[category] || 'cat-other';
}

// Renders a stacked bar chart as raw SVG markup, built entirely from D1
// query results on the Worker itself - no client-side charting library, no
// canvas. `days` is an ascending array of 'YYYY-MM-DD' strings; `series` is
// [{ key, label, cls }]; `byDay` maps a date to { [key]: count }, missing
// keys treated as 0. Colors come from the page stylesheet through the class
// names, since the SVG is inserted into the page's own DOM, so both themes
// apply to it. The legend is drawn by the page, next to the chart.
function renderStackedBarSVG(days, series, byDay, { width = 1300, height = 240, label = 'Chart' } = {}) {
  const padL = 40, padR = 12, padT = 12, padB = 28;
  const plotW = width - padL - padR;
  const plotH = height - padT - padB;
  const n = Math.max(1, days.length);
  const barGap = 4;
  const barW = Math.max(1, plotW / n - barGap);

  const totals = days.map((d) => {
    const row = byDay[d] || {};
    return series.reduce((sum, s) => sum + (row[s.key] || 0), 0);
  });
  const maxTotal = Math.max(1, ...totals);

  const gridLines = [0, 0.25, 0.5, 0.75, 1].map((frac) => {
    const y = padT + plotH * (1 - frac);
    return `<line class="grid" x1="${padL}" y1="${y.toFixed(1)}" x2="${width - padR}" y2="${y.toFixed(1)}" />` +
      `<text class="axis" x="${(padL - 6).toFixed(1)}" y="${(y + 3).toFixed(1)}" text-anchor="end">${Math.round(maxTotal * frac)}</text>`;
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
      return `<rect class="seg ${escSvg(s.cls)}" x="${x.toFixed(1)}" y="${yCursor.toFixed(1)}" width="${barW.toFixed(1)}" height="${segH.toFixed(1)}"><title>${escSvg(shortDay(d))} - ${escSvg(s.label)}: ${v}</title></rect>`;
    }).join('');
  }).join('');

  const labelEvery = Math.max(1, Math.ceil(n / 8));
  const xLabels = days.map((d, i) => {
    if (i % labelEvery !== 0) return '';
    const x = padL + i * (plotW / n) + barW / 2;
    return `<text class="axis" x="${x.toFixed(1)}" y="${height - 8}" text-anchor="middle">${escSvg(shortDay(d))}</text>`;
  }).join('');

  return `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="${escSvg(label)}">${gridLines}${bars}${xLabels}</svg>`;
}

export { lastNDays, renderStackedBarSVG, categoryClass };

export const DASHBOARD_HTML = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Mercury</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: light-dark(#f6f8fa, #0d1117);
    --panel: light-dark(#ffffff, #151b23);
    --panel-2: light-dark(#eef1f5, #1c2330);
    --border: light-dark(#d0d7de, #2d3440);
    --control-border: light-dark(#8c959f, #6e7681);
    --text: light-dark(#1f2328, #e6edf3);
    --muted: light-dark(#57606a, #9aa5b1);
    --accent: light-dark(#0969da, #4493f8);
    --accent-fill: light-dark(#0969da, #1f6feb);
    --accent-text: light-dark(#0550ae, #79c0ff);
    --good: light-dark(#1a7f37, #3fb950);
    --good-text: light-dark(#116329, #56d364);
    --warn: light-dark(#9a6700, #d29922);
    --warn-text: light-dark(#7d4e00, #e3b341);
    --bad: light-dark(#cf222e, #f85149);
    --bad-text: light-dark(#a40e26, #ff7b72);
    --bad-fill: light-dark(#cf222e, #b62324);
    --focus: light-dark(#0969da, #79c0ff);
    --tint: 12%;
    --cat-1: light-dark(#8250df, #a371f7);
    --cat-2: light-dark(#bf3989, #f778ba);
    --cat-3: light-dark(#0969da, #58a6ff);
    --cat-4: light-dark(#9a6700, #d29922);
    --cat-5: light-dark(#cf222e, #ff7b72);
    --cat-6: light-dark(#1a7f37, #3fb950);
    --cat-7: light-dark(#1b7c83, #39c5cf);
    --cat-8: light-dark(#6639ba, #d2a8ff);
    --cat-9: light-dark(#953800, #ffa657);
    --cat-10: light-dark(#a40e26, #ffa198);
    --cat-11: light-dark(#7d4e00, #e3b341);
    --cat-12: light-dark(#4d2d00, #f0883e);
    --cat-other: light-dark(#8c959f, #6e7681);
  }
  :root[data-theme="light"] { color-scheme: light; }
  :root[data-theme="dark"] { color-scheme: dark; }
  * { box-sizing: border-box; }
  html { scrollbar-gutter: stable; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 14px;
    line-height: 1.45;
  }
  h1, h2, h3 { text-wrap: balance; }
  p { text-wrap: pretty; }
  a { color: var(--accent-text); }
  :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 4px; }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
  .skip { position: absolute; left: -9999px; }
  .skip:focus { left: 16px; top: 8px; z-index: 10; background: var(--panel); padding: 8px 12px; }
  .num, td time, .kpi .value { font-variant-numeric: tabular-nums; }

  .appbar {
    position: sticky; top: 0; z-index: 5;
    background: color-mix(in srgb, var(--bg) 92%, transparent);
    backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border);
  }
  .appbar-inner { max-width: 1400px; margin: 0 auto; padding: 10px 24px; display: flex; flex-wrap: wrap; align-items: center; gap: 12px 20px; }
  .brand { display: flex; align-items: center; gap: 10px; font-size: 17px; font-weight: 650; margin: 0; }
  .health { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 999px; background: var(--panel-2); color: var(--muted); }
  .health::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
  .health.ok { color: var(--good-text); background: color-mix(in srgb, var(--good) var(--tint), var(--panel)); }
  .health.warn { color: var(--warn-text); background: color-mix(in srgb, var(--warn) var(--tint), var(--panel)); }
  .health.bad { color: var(--bad-text); background: color-mix(in srgb, var(--bad) var(--tint), var(--panel)); }
  nav ul { list-style: none; display: flex; gap: 2px; margin: 0; padding: 0; flex-wrap: wrap; }
  nav a { display: block; padding: 7px 12px; border-radius: 8px; color: var(--muted); text-decoration: none; font-weight: 550; min-height: 32px; }
  nav a:hover { background: var(--panel-2); color: var(--text); }
  nav a[aria-current="page"] { background: var(--panel-2); color: var(--text); box-shadow: inset 0 -2px 0 var(--accent); }
  .appbar-tools { margin-left: auto; display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--muted); }
  select, input, textarea, button { font: inherit; }
  select, input:not([type="checkbox"]), textarea {
    background: var(--panel); color: var(--text); border: 1px solid var(--control-border);
    border-radius: 6px; padding: 6px 9px; min-height: 32px;
  }
  textarea { width: 100%; }
  @supports (field-sizing: content) { textarea { field-sizing: content; min-block-size: 3lh; max-block-size: 12lh; } }
  button { cursor: pointer; }
  .btn { background: var(--accent-fill); color: #fff; border: 1px solid transparent; border-radius: 6px; padding: 6px 12px; min-height: 32px; font-weight: 550; }
  .btn.secondary { background: var(--panel-2); color: var(--text); border-color: var(--control-border); }
  .btn.danger { background: var(--bad-fill); }
  .btn.small { padding: 3px 9px; min-height: 28px; font-size: 12px; }
  .btn:disabled { opacity: .55; cursor: default; }
  input[type="checkbox"] { accent-color: var(--accent); width: 18px; height: 18px; }

  main { max-width: 1400px; margin: 0 auto; padding: 20px 24px 60px; }
  [data-view][hidden] { display: none; }
  .view-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; margin: 4px 0 16px; }
  .view-head h2 { font-size: 20px; margin: 0; }
  .view-head p { margin: 0; color: var(--muted); }
  section { scroll-margin-top: 80px; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; }
  .panel > h3, .panel-title { font-size: 13px; font-weight: 650; margin: 0; padding: 12px 16px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .panel-body { padding: 14px 16px; }
  .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 440px), 1fr)); gap: 16px; margin-bottom: 16px; align-items: start; }
  .stack { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
  .chart-panel { margin-bottom: 16px; }
  .muted { color: var(--muted); }
  .empty { padding: 28px 16px; text-align: center; color: var(--muted); }
  .load-error { color: var(--bad-text); }

  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 160px), 1fr)); gap: 12px; margin-bottom: 16px; }
  .kpi { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; text-decoration: none; color: inherit; display: block; }
  a.kpi:hover { border-color: var(--control-border); }
  .kpi .label { color: var(--muted); font-size: 12px; font-weight: 600; }
  .kpi .value { font-size: 26px; font-weight: 650; margin-top: 2px; }
  .kpi .detail { font-size: 12px; color: var(--muted); }
  .kpi.attention { border-color: color-mix(in srgb, var(--warn) 60%, var(--border)); }
  .kpi.attention .value { color: var(--warn-text); }
  .kpi.alarm { border-color: color-mix(in srgb, var(--bad) 60%, var(--border)); }
  .kpi.alarm .value { color: var(--bad-text); }

  .status { display: inline-flex; align-items: center; gap: 5px; padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; white-space: nowrap; }
  .status::before { font-weight: 700; }
  .status.s-250 { color: var(--good-text); background: color-mix(in srgb, var(--good) var(--tint), var(--panel)); }
  .status.s-250::before { content: "+"; }
  .status.s-421 { color: var(--warn-text); background: color-mix(in srgb, var(--warn) var(--tint), var(--panel)); }
  .status.s-421::before { content: "~"; }
  .status.s-550 { color: var(--bad-text); background: color-mix(in srgb, var(--bad) var(--tint), var(--panel)); }
  .status.s-550::before { content: "x"; }
  .tag { display: inline-block; padding: 0 7px; border-radius: 999px; font-size: 11px; font-weight: 600; background: var(--panel-2); color: var(--muted); }
  .tag.alert-URGENT { color: var(--bad-text); background: color-mix(in srgb, var(--bad) var(--tint), var(--panel)); }
  .tag.alert-STANDARD { color: var(--warn-text); background: color-mix(in srgb, var(--warn) var(--tint), var(--panel)); }
  .tag.rule { color: var(--accent-text); background: color-mix(in srgb, var(--accent) var(--tint), var(--panel)); }

  .table-wrap { overflow-x: auto; overscroll-behavior-inline: contain; }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 9px 12px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
  thead th { position: sticky; top: 0; background: var(--panel); color: var(--muted); font-size: 12px; font-weight: 600; z-index: 1; }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr.row-open { cursor: pointer; }
  tbody tr.row-open:hover td { background: var(--panel-2); }
  .subject { max-width: 440px; }
  .linkish { all: unset; cursor: pointer; color: var(--accent-text); text-decoration: underline; text-underline-offset: 2px; }
  .linkish:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
  .policy-entry-detail.stale { color: var(--warn-text); }
  .subject button { all: unset; cursor: pointer; display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
  .subject button:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
  .sender { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; margin-bottom: 12px; }
  .toolbar search { flex: 1 1 340px; }
  .toolbar form { display: flex; gap: 8px; width: 100%; }
  .toolbar input[type="search"] { flex: 1; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { background: var(--panel); color: var(--muted); border: 1px solid var(--control-border); border-radius: 999px; padding: 4px 12px; min-height: 30px; font-size: 13px; }
  .chip[aria-pressed="true"] { background: var(--accent-fill); border-color: var(--accent-fill); color: #fff; }
  .pager { display: flex; align-items: center; gap: 8px; padding: 10px 12px; font-size: 12px; color: var(--muted); border-top: 1px solid var(--border); }
  .pager .spacer { flex: 1; }

  .activity, .recent { container-type: inline-size; }
  @container (width < 720px) {
    .activity thead { display: none; }
    .activity table, .activity tbody, .activity tr, .activity td { display: block; }
    .activity tr { padding: 10px 12px; border-bottom: 1px solid var(--border); display: grid; grid-template-columns: 1fr auto; gap: 2px 10px; }
    .activity td { border: none; padding: 0; }
    .activity td[data-col="time"] { grid-column: 1; color: var(--muted); font-size: 12px; }
    .activity td[data-col="outcome"] { grid-column: 2; grid-row: 1; }
    .activity td[data-col="sender"] { grid-column: 1 / -1; max-width: none; }
    .activity td[data-col="subject"] { grid-column: 1 / -1; max-width: none; }
    .activity td[data-secondary] { display: none; }
  }
  @container (width < 480px) {
    .recent thead { display: none; }
    .recent table, .recent tbody, .recent tr, .recent td { display: block; }
    .recent tr { padding: 10px 12px; border-bottom: 1px solid var(--border); display: grid; grid-template-columns: 1fr auto; gap: 2px 10px; }
    .recent td { border: none; padding: 0; }
    .recent td[data-col="time"] { color: var(--muted); font-size: 12px; }
    .recent td[data-col="outcome"] { grid-column: 2; grid-row: 1; }
    .recent td[data-col="sender"], .recent td[data-col="subject"] { grid-column: 1 / -1; max-width: none; }
  }
  .recent .subject { max-width: 260px; }

  .attention-list { list-style: none; margin: 0; padding: 0; }
  .attention-list li { display: flex; gap: 10px; align-items: flex-start; padding: 10px 16px; border-bottom: 1px solid var(--border); }
  .attention-list li:last-child { border-bottom: none; }
  .attention-list label { display: flex; gap: 10px; align-items: flex-start; cursor: pointer; flex: 1; min-height: 24px; }
  .attention-list li.done { opacity: .5; }
  .attention-list li.done .summary { text-decoration: line-through; }
  .meta { font-size: 12px; color: var(--muted); }

  .chart-wrap { overflow-x: auto; }
  .chart-wrap svg { width: 100%; min-width: 760px; height: auto; display: block; }
  .chart-wrap .grid { stroke: var(--border); stroke-width: 1; }
  .chart-wrap .axis { fill: var(--muted); font-size: 12px; }
  .seg { stroke: var(--panel); stroke-width: 1; }
  .seg.s-250, .sw.s-250 { fill: var(--good); background: var(--good); }
  .seg.s-421, .sw.s-421 { fill: var(--warn); background: var(--warn); }
  .seg.s-550, .sw.s-550 { fill: var(--bad); background: var(--bad); }
  .cat-1 { fill: var(--cat-1); background: var(--cat-1); } .cat-2 { fill: var(--cat-2); background: var(--cat-2); }
  .cat-3 { fill: var(--cat-3); background: var(--cat-3); } .cat-4 { fill: var(--cat-4); background: var(--cat-4); }
  .cat-5 { fill: var(--cat-5); background: var(--cat-5); } .cat-6 { fill: var(--cat-6); background: var(--cat-6); }
  .cat-7 { fill: var(--cat-7); background: var(--cat-7); } .cat-8 { fill: var(--cat-8); background: var(--cat-8); }
  .cat-9 { fill: var(--cat-9); background: var(--cat-9); } .cat-10 { fill: var(--cat-10); background: var(--cat-10); }
  .cat-11 { fill: var(--cat-11); background: var(--cat-11); } .cat-12 { fill: var(--cat-12); background: var(--cat-12); }
  .cat-other { fill: var(--cat-other); background: var(--cat-other); }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; list-style: none; margin: 10px 0 0; padding: 0; font-size: 12px; color: var(--muted); }
  .legend li { display: inline-flex; align-items: center; gap: 6px; }
  .legend .sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; }
  .bars { display: flex; flex-direction: column; gap: 7px; }
  .bar-row { display: grid; grid-template-columns: minmax(90px, 170px) 1fr 40px; align-items: center; gap: 10px; font-size: 13px; }
  .bar-track { background: var(--panel-2); border-radius: 4px; height: 10px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; }
  .bar-count { text-align: right; }

  .policy-tools { display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; margin-bottom: 14px; }
  .policy-groups { display: flex; flex-direction: column; gap: 22px; }
  .policy-group-title { font-size: 13px; font-weight: 650; margin: 0 0 8px; }
  .policy-group-note { font-size: 12px; color: var(--muted); margin: -4px 0 10px; }
  .policy-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); gap: 12px; }
  .policy-card { padding: 0; display: flex; flex-direction: column; min-width: 0; }
  .policy-card h4 { margin: 0; padding: 10px 14px; font-size: 13px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; gap: 8px; }
  .policy-entries { max-height: 320px; overflow-y: auto; padding: 0 14px; }
  .policy-entry { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; border-bottom: 1px solid var(--border); padding: 7px 0; font-size: 13px; }
  .policy-entry > div { min-width: 0; overflow-wrap: anywhere; }
  .policy-entry .btn { flex-shrink: 0; white-space: nowrap; }
  .policy-entry:last-child { border-bottom: none; }
  .policy-entry[hidden] { display: none; }
  .policy-entry-detail { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .policy-form { display: flex; flex-direction: column; gap: 6px; padding: 12px 14px; border-top: 1px solid var(--border); margin-top: auto; }
  .policy-form label { font-size: 12px; color: var(--muted); }
  .policy-form input { width: 100%; }
  .policy-form .btn { align-self: flex-start; }
  .policy-warning { border: 1px solid var(--warn); background: color-mix(in srgb, var(--warn) var(--tint), var(--panel)); color: var(--warn-text); border-radius: 8px; padding: 10px 12px; margin-bottom: 12px; font-size: 13px; }

  .review-list { display: flex; flex-direction: column; gap: 12px; }
  .review-card { padding: 12px 16px; display: grid; gap: 6px; }
  .review-card .review-head { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center; }
  .review-card .review-why { color: var(--muted); font-size: 13px; }
  .review-actions { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .review-card.done { opacity: .55; }
  output.preview { display: block; font-size: 12px; color: var(--muted); }
  output.preview:empty { display: none; }
  output.preview ul { margin: 4px 0 0; padding-left: 18px; }
  dl.facts { display: grid; grid-template-columns: minmax(110px, max-content) minmax(0, 1fr); gap: 6px 16px; margin: 0; }
  dl.facts dt { color: var(--muted); }
  dl.facts dd { margin: 0; overflow-wrap: anywhere; }

  dialog { border: 1px solid var(--border); border-radius: 12px; background: var(--panel); color: var(--text); padding: 0; width: min(760px, calc(100vw - 32px)); max-height: calc(100vh - 48px); }
  dialog::backdrop { background: rgb(0 0 0 / .45); }
  dialog.drawer { margin: 0 0 0 auto; height: 100vh; max-height: 100vh; max-width: 100vw; border-radius: 12px 0 0 12px; }
  dialog[open] { opacity: 1; transition: opacity .15s ease; }
  @starting-style { dialog[open] { opacity: 0; } }
  .dialog-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--panel); }
  .dialog-head h2 { font-size: 16px; margin: 0; }
  .dialog-body { padding: 16px 18px; }
  .dialog-actions { display: flex; gap: 8px; justify-content: flex-end; padding: 12px 18px; border-top: 1px solid var(--border); }
  .detail-block { margin-top: 16px; }
  .detail-block h3 { font-size: 13px; margin: 0 0 6px; }
  .detail-box { white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; max-height: 300px; overflow-y: auto; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; margin: 0; }
  output.toast { position: fixed; right: 16px; bottom: 16px; background: var(--panel); border: 1px solid var(--control-border); border-radius: 8px; padding: 10px 14px; box-shadow: 0 6px 24px rgb(0 0 0 / .25); max-width: min(420px, calc(100vw - 32px)); z-index: 20; }
  output.toast:empty { display: none; }

  @media (max-width: 640px) {
    .appbar-inner, main { padding-left: 16px; padding-right: 16px; }
    .appbar-tools { margin-left: 0; width: 100%; }
    dialog.drawer { border-radius: 0; width: 100vw; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { transition-duration: .001ms !important; animation-duration: .001ms !important; scroll-behavior: auto !important; }
  }
</style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="appbar">
  <div class="appbar-inner">
    <h1 class="brand">Mercury <span class="health" id="health">Checking</span></h1>
    <nav aria-label="Dashboard views">
      <ul>
        <li><a href="#overview" data-nav="overview">Overview</a></li>
        <li><a href="#activity" data-nav="activity">Activity</a></li>
        <li><a href="#policy" data-nav="policy">Policy</a></li>
        <li><a href="#audit" data-nav="audit">Audit</a></li>
        <li><a href="#review" data-nav="review">Review</a></li>
        <li><a href="#system" data-nav="system">System</a></li>
      </ul>
    </nav>
    <div class="appbar-tools">
      <span id="updated" aria-live="off">Not loaded yet</span>
      <button type="button" class="btn secondary small" id="refresh">Refresh</button>
      <label class="sr-only" for="theme">Theme</label>
      <select id="theme">
        <option value="">System theme</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
    </div>
  </div>
</header>

<main id="main">
  <section data-view="overview" aria-labelledby="h-overview">
    <div class="view-head"><h2 id="h-overview">Overview</h2><p id="overview-sub">The last 24 hours, in Phoenix time.</p></div>
    <div class="kpis" id="kpis"><div class="empty">Loading the summary...</div></div>
    <div class="grid-2 top">
      <div class="stack">
        <div class="panel">
          <h3>Needs attention <span class="tag" id="attention-count">0</span></h3>
          <ul class="attention-list" id="attention"><li class="empty">Loading...</li></ul>
        </div>
        <div class="panel">
          <h3>Categories, last 7 days</h3>
          <div class="panel-body"><div class="bars" id="categoryBars"><div class="empty">Loading...</div></div></div>
        </div>
      </div>
      <div class="panel">
        <h3>Recent decisions <a href="#activity">All activity</a></h3>
        <div class="table-wrap recent"><table>
          <caption class="sr-only">The eight newest messages</caption>
          <thead><tr><th scope="col">Time</th><th scope="col">Sender</th><th scope="col">Subject</th><th scope="col">Outcome</th></tr></thead>
          <tbody id="recent"><tr><td colspan="4" class="empty">Loading...</td></tr></tbody>
        </table></div>
      </div>
    </div>
    <div class="panel chart-panel">
      <h3>Messages per day, last 30 days</h3>
      <div class="panel-body"><div class="chart-wrap" id="volumeTrend"><div class="empty">Loading...</div></div><ul class="legend" id="volumeLegend"></ul></div>
    </div>
    <div class="panel chart-panel">
      <h3>Categories per day, last 30 days</h3>
      <div class="panel-body"><div class="chart-wrap" id="categoryTrend"><div class="empty">Loading...</div></div><ul class="legend" id="categoryLegend"></ul></div>
    </div>
  </section>

  <section data-view="activity" aria-labelledby="h-activity" hidden>
    <div class="view-head"><h2 id="h-activity">Activity</h2><p>Every message Mercury decided on, newest first. Open a row for the full decision.</p></div>
    <div class="toolbar">
      <search>
        <form id="activity-search" role="search">
          <label class="sr-only" for="q">Search sender or subject</label>
          <input type="search" id="q" name="q" placeholder="Search sender or subject (press /)" autocomplete="off">
          <button class="btn secondary" type="submit">Search</button>
        </form>
      </search>
      <div class="chips" role="group" aria-label="Filter by outcome" id="outcome-chips">
        <button type="button" class="chip" data-outcome="" aria-pressed="true">All</button>
        <button type="button" class="chip" data-outcome="250" aria-pressed="false">Accepted</button>
        <button type="button" class="chip" data-outcome="421" aria-pressed="false">Deferred</button>
        <button type="button" class="chip" data-outcome="550" aria-pressed="false">Rejected</button>
      </div>
    </div>
    <div class="panel activity">
      <div class="table-wrap"><table>
        <caption class="sr-only">Messages, newest first</caption>
        <thead><tr><th scope="col">Time</th><th scope="col">Sender</th><th scope="col">Subject</th><th scope="col">Outcome</th><th scope="col">Category</th><th scope="col">Verdict</th><th scope="col">Rule</th><th scope="col">Alert</th></tr></thead>
        <tbody id="messages"><tr><td colspan="8" class="empty">Loading...</td></tr></tbody>
      </table></div>
      <div class="pager" data-pager="messages"></div>
    </div>
  </section>

  <section data-view="policy" aria-labelledby="h-policy" hidden>
    <div class="view-head"><h2 id="h-policy">Policy</h2><p>Sender lists are checked first: an exact address, then the most specific domain, then patterns. A list match counts only when DMARC passes for the sender's domain. Everything else goes to the judge with the rules below.</p></div>
    <div class="policy-tools">
      <label for="policy-filter" class="sr-only">Find a rule</label>
      <input type="search" id="policy-filter" placeholder="Find a sender or rule">
      <span class="muted" id="policy-total"></span>
    </div>
    <div id="filteringWarnings"></div>
    <div class="policy-groups" id="filteringPolicy"><div class="empty">Loading...</div></div>
  </section>

  <section data-view="audit" aria-labelledby="h-audit" hidden>
    <div class="view-head"><h2 id="h-audit">Audit</h2><p>Changes to the policy, and the actions Mercury took on your mailbox.</p></div>
    <div class="chips" role="group" aria-label="Show" id="audit-chips" style="margin-bottom:12px">
      <button type="button" class="chip" data-audit="rules" aria-pressed="true">Rule changes</button>
      <button type="button" class="chip" data-audit="actions" aria-pressed="false">Mailbox actions</button>
    </div>
    <div class="panel" data-audit-panel="rules">
      <div class="table-wrap"><table>
        <caption class="sr-only">Rule changes, newest first</caption>
        <thead><tr><th scope="col">Time</th><th scope="col">Change</th><th scope="col">Rule</th><th scope="col">Source</th></tr></thead>
        <tbody id="rules"><tr><td colspan="4" class="empty">Loading...</td></tr></tbody>
      </table></div>
      <div class="pager" data-pager="rules"></div>
    </div>
    <div class="panel" data-audit-panel="actions" hidden>
      <div class="table-wrap"><table>
        <caption class="sr-only">Mailbox actions, newest first</caption>
        <thead><tr><th scope="col">Time</th><th scope="col">Kind</th><th scope="col">Domain</th><th scope="col">Result</th><th scope="col">Details</th></tr></thead>
        <tbody id="actions"><tr><td colspan="5" class="empty">Loading...</td></tr></tbody>
      </table></div>
      <div class="pager" data-pager="actions"></div>
    </div>
  </section>

  <section data-view="review" aria-labelledby="h-review" hidden>
    <div class="view-head"><h2 id="h-review">Review</h2><p>A random sample of recent decisions from each outcome. Marking them gives a real error rate per outcome.</p></div>
    <div class="panel" style="margin-bottom:16px"><h3>Error rates, last 90 days</h3><div class="panel-body"><dl class="facts" id="review-metrics"><dt>Loading</dt><dd>...</dd></dl></div></div>
    <div class="review-list" id="review-list"><div class="empty">Loading...</div></div>
  </section>

  <section data-view="system" aria-labelledby="h-system" hidden>
    <div class="view-head"><h2 id="h-system">System</h2><p>How recently each part of Mercury has done its job.</p></div>
    <div class="panel"><div class="panel-body"><dl class="facts" id="system-facts"><dt>Loading</dt><dd>...</dd></dl></div></div>
  </section>
</main>

<dialog id="inspector" class="drawer" aria-labelledby="inspector-title">
  <div class="dialog-head">
    <h2 id="inspector-title">Message</h2>
    <form method="dialog"><button class="btn secondary small" value="close">Close</button></form>
  </div>
  <div class="dialog-body" id="inspector-body"></div>
</dialog>

<dialog id="confirm" aria-labelledby="confirm-title">
  <form method="dialog">
    <div class="dialog-head"><h2 id="confirm-title">Confirm</h2></div>
    <div class="dialog-body" id="confirm-body"></div>
    <div class="dialog-actions">
      <button class="btn secondary" value="cancel">Cancel</button>
      <button class="btn danger" value="ok" id="confirm-ok">Remove</button>
    </div>
  </form>
</dialog>

<output class="toast" id="toast" aria-live="polite"></output>

<script>
var TZ = 'America/Phoenix';
var OUTCOME = { '250': 'Accepted', '421': 'Deferred', '550': 'Rejected' };
var RECIPIENT = {
  R: 'rpgm.tools address in To or Cc',
  F: 'Personal address in To or Cc',
  r: 'Bcc to an rpgm.tools address',
  f: 'Bcc through the personal forwarding alias',
};
var state = { view: 'overview', outcome: '', q: '', audit: 'rules', loadedAt: 0 };
var pagers = { messages: { limit: 50, offset: 0 }, rules: { limit: 50, offset: 0 }, actions: { limit: 50, offset: 0 } };
var filteringPolicy = null;
var lastSummary = null;

function esc(s) {
  return (s == null ? '' : String(s)).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; });
}
function byId(id) { return document.getElementById(id); }
async function fetchJson(url, options) {
  var res = await fetch(url, options);
  var data = null;
  try { data = await res.json(); } catch (err) { data = null; }
  if (!res.ok || data === null || data.ok === false) {
    throw new Error((data && (data.error || data.detail)) || ('HTTP ' + res.status));
  }
  return data;
}
function loadFailed(what, err) {
  var subject = what.charAt(0).toUpperCase() + what.slice(1);
  return '<div class="empty load-error">' + esc(subject) + ' did not load: ' + esc(err.message || err) + '. Refresh tries again.</div>';
}
function loadFailedRow(what, err, colspan) {
  return '<tr><td colspan="' + colspan + '">' + loadFailed(what, err) + '</td></tr>';
}
function toast(text) {
  var t = byId('toast');
  t.textContent = text;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(function () { t.textContent = ''; }, 6000);
}

var absFormat = new Intl.DateTimeFormat('en-US', { timeZone: TZ, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
var fullFormat = new Intl.DateTimeFormat('en-US', { timeZone: TZ, dateStyle: 'medium', timeStyle: 'long' });
var relFormat = new Intl.RelativeTimeFormat('en', { numeric: 'auto' });
function relative(date) {
  var secs = Math.round((date.getTime() - Date.now()) / 1000);
  var units = [['day', 86400], ['hour', 3600], ['minute', 60]];
  for (var i = 0; i < units.length; i++) {
    if (Math.abs(secs) >= units[i][1]) return relFormat.format(Math.round(secs / units[i][1]), units[i][0]);
  }
  return 'just now';
}
function timeTag(iso, mode) {
  if (!iso) return '<span class="muted">-</span>';
  var d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  var shown = mode === 'relative' ? relative(d) : absFormat.format(d);
  return '<time datetime="' + esc(d.toISOString()) + '" title="' + esc(fullFormat.format(d)) + '">' + esc(shown) + '</time>';
}
function outcomeTag(code) {
  if (!code) return '<span class="muted">-</span>';
  return '<span class="status s-' + esc(code) + '">' + esc(OUTCOME[code] || code) + '</span>';
}
function standingRuleCount(policy) {
  if (!policy) return null;
  var count = function (lists) { return Object.values(lists || {}).reduce(function (n, list) { return n + list.length; }, 0); };
  return count(policy.sender_lists) + (policy.blacklist_patterns || []).length + count(policy.semantic_rules) + (policy.custom_actions || []).length;
}

// ---- theme ----
function applyTheme(value) {
  if (value) document.documentElement.setAttribute('data-theme', value);
  else document.documentElement.removeAttribute('data-theme');
}
(function () {
  var saved = '';
  try { saved = localStorage.getItem('mercury-theme') || ''; } catch (err) { saved = ''; }
  applyTheme(saved);
  byId('theme').value = saved;
  byId('theme').addEventListener('change', function (e) {
    applyTheme(e.target.value);
    try { localStorage.setItem('mercury-theme', e.target.value); } catch (err) { /* private window */ }
  });
})();

// ---- routing: #view?key=value ----
function readHash() {
  var raw = location.hash.slice(1);
  var parts = raw.split('?');
  var view = parts[0] || 'overview';
  if (!document.querySelector('[data-view="' + view + '"]')) view = 'overview';
  var params = new URLSearchParams(parts[1] || '');
  state.view = view;
  state.outcome = params.get('outcome') || '';
  state.q = params.get('q') || '';
  state.audit = params.get('show') === 'actions' ? 'actions' : 'rules';
}
function writeHash() {
  var params = new URLSearchParams();
  if (state.view === 'activity') {
    if (state.outcome) params.set('outcome', state.outcome);
    if (state.q) params.set('q', state.q);
  }
  if (state.view === 'audit' && state.audit === 'actions') params.set('show', 'actions');
  var next = '#' + state.view + (params.toString() ? '?' + params.toString() : '');
  if (location.hash !== next) history.replaceState(null, '', next);
}
function showView() {
  document.querySelectorAll('[data-view]').forEach(function (el) { el.hidden = el.dataset.view !== state.view; });
  document.querySelectorAll('[data-nav]').forEach(function (a) {
    if (a.dataset.nav === state.view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  document.querySelectorAll('#outcome-chips .chip').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.outcome === state.outcome)); });
  document.querySelectorAll('#audit-chips .chip').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.audit === state.audit)); });
  document.querySelectorAll('[data-audit-panel]').forEach(function (p) { p.hidden = p.dataset.auditPanel !== state.audit; });
  byId('q').value = state.q;
  document.title = 'Mercury - ' + state.view.charAt(0).toUpperCase() + state.view.slice(1);
}
window.addEventListener('hashchange', function () {
  document.querySelectorAll('dialog[open]').forEach(function (d) { d.close(); });
  readHash(); showView(); loadView();
});

// ---- overview ----
async function loadSummary() {
  var data;
  try {
    data = await fetchJson('/dashboard/api/summary');
  } catch (err) {
    byId('kpis').innerHTML = loadFailed('the summary', err);
    byId('categoryBars').innerHTML = loadFailed('the category breakdown', err);
    return;
  }
  lastSummary = data;
  var d = data.last24h;
  var open = data.openActionItems || 0;
  byId('kpis').innerHTML =
    '<a class="kpi" href="#activity"><div class="label">Messages, 24 hours</div><div class="value num">' + esc(d.total) + '</div>' +
      '<div class="detail">' + esc(d.accepted) + ' accepted, ' + esc(d.deferred) + ' deferred, ' + esc(d.hardBounces) + ' rejected</div></a>' +
    '<a class="kpi' + (open ? ' attention' : '') + '" href="#overview"><div class="label">Needs attention</div><div class="value num">' + esc(open) + '</div>' +
      '<div class="detail">' + (d.urgent ? esc(d.urgent) + ' urgent alert' + (d.urgent === 1 ? '' : 's') + ' in 24 hours' : 'No urgent alerts in 24 hours') + '</div></a>' +
    '<a class="kpi' + (d.deliveryFailed ? ' alarm' : '') + '" href="#audit?show=actions"><div class="label">Delivery, 24 hours</div><div class="value num">' + esc(d.delivered) + '</div>' +
      '<div class="detail">' + (d.deliveryFailed ? esc(d.deliveryFailed) + ' failed' : 'delivered, none failed') + '</div></a>' +
    '<a class="kpi" href="#policy"><div class="label">Policy</div><div class="value num" id="ruleCountValue">' + esc(standingRuleCount(filteringPolicy) == null ? '-' : standingRuleCount(filteringPolicy)) + '</div>' +
      '<div class="detail">standing rules, ' + esc(data.last7d.ruleChanges) + ' changed this week</div></a>';
  var maxCount = Math.max.apply(null, [1].concat(data.categories.map(function (c) { return c.count; })));
  byId('categoryBars').innerHTML = data.categories.length ? data.categories.map(function (c) {
    return '<div class="bar-row"><div>' + esc(c.category || 'UNKNOWN') + '</div>' +
      '<div class="bar-track"><div class="bar-fill ' + esc(c.cls) + '" style="width:' + (c.count / maxCount * 100).toFixed(0) + '%"></div></div>' +
      '<div class="bar-count num">' + esc(c.count) + '</div></div>';
  }).join('') : '<div class="empty">No mail in the last 7 days.</div>';
}

async function loadAttention() {
  var list = byId('attention');
  var rows;
  try {
    rows = await fetchJson('/dashboard/api/action-items');
  } catch (err) {
    list.innerHTML = '<li>' + loadFailed('action items', err) + '</li>';
    return;
  }
  byId('attention-count').textContent = rows.length;
  list.innerHTML = rows.length ? rows.map(function (r) {
    return '<li data-id="' + esc(r.id) + '"><label><input type="checkbox" data-complete-id="' + esc(r.id) + '">' +
      '<span><span class="summary">' + esc(r.summary) + '</span><br><span class="meta">' + esc(r.kind) + ', ' + timeTag(r.created_at, 'relative') +
      (r.related_message_id ? ', <button type="button" class="btn secondary small" data-open-message="' + esc(r.related_message_id) + '">Open message</button>' : '') +
      '</span></span></label></li>';
  }).join('') : '<li class="empty">Nothing needs you right now.</li>';
}

byId('attention').addEventListener('change', async function (e) {
  var box = e.target.closest('[data-complete-id]');
  if (!box || !box.checked) return;
  var row = box.closest('li');
  box.disabled = true;
  try {
    var data = await fetchJson('/dashboard/api/action-items/' + box.dataset.completeId + '/complete', { method: 'POST' });
    if (!data.completed) throw new Error('it was already completed or no longer exists');
    row.classList.add('done');
    toast('Marked done.');
  } catch (err) {
    box.checked = false;
    box.disabled = false;
    toast('This item was not marked done: ' + (err.message || err) + '. Tick it again to retry.');
  }
});

function messageRow(r, compact) {
  var subject = '<button type="button" data-open-message="' + esc(r.id) + '" title="' + esc(r.subject) + '">' + (r.subject ? esc(r.subject) : '<span class="muted">(no subject)</span>') + '</button>';
  var sender = esc(r.from_domain || r.from_display || '-');
  var cells = '<td data-col="time">' + timeTag(r.received_at) + '</td>' +
    '<td data-col="sender" class="sender" title="' + esc(r.from_display) + '">' + sender + '</td>' +
    '<td data-col="subject" class="subject">' + subject + '</td>' +
    '<td data-col="outcome">' + outcomeTag(r.enforced_disposition) + '</td>';
  if (!compact) {
    cells += '<td data-secondary>' + esc(r.category) + '</td>' +
      '<td data-secondary>' + esc(r.verdict) + '</td>' +
      '<td data-secondary>' + (r.triggered_rule ? '<span class="tag rule">rule</span>' : '<span class="muted">-</span>') + '</td>' +
      '<td data-secondary>' + (r.alert_level && r.alert_level !== 'NONE' ? '<span class="tag alert-' + esc(r.alert_level) + '">' + esc(r.alert_level) + '</span>' : '<span class="muted">-</span>') + '</td>';
  }
  return '<tr class="row-open" data-row-message="' + esc(r.id) + '">' + cells + '</tr>';
}

async function loadRecent() {
  var body = byId('recent');
  try {
    var data = await fetchJson('/dashboard/api/messages?limit=20&offset=0');
    body.innerHTML = data.rows.length ? data.rows.slice(0, 8).map(function (r) { return messageRow(r, true); }).join('') : '<tr><td colspan="4" class="empty">No mail yet.</td></tr>';
  } catch (err) {
    body.innerHTML = loadFailedRow('recent decisions', err, 4);
  }
}

function legendHtml(items) {
  return items.map(function (s) { return '<li><span class="sw ' + esc(s.cls) + '"></span>' + esc(s.label) + ' <span class="num">' + esc(s.total) + '</span></li>'; }).join('');
}
async function loadTrends() {
  var data;
  try {
    data = await fetchJson('/dashboard/api/trends');
  } catch (err) {
    byId('volumeTrend').innerHTML = loadFailed('the volume chart', err);
    byId('categoryTrend').innerHTML = loadFailed('the category chart', err);
    return;
  }
  byId('volumeTrend').innerHTML = data.volumeSvg;
  byId('categoryTrend').innerHTML = data.categorySvg;
  byId('volumeLegend').innerHTML = legendHtml(data.volumeLegend || []);
  byId('categoryLegend').innerHTML = legendHtml(data.categoryLegend || []);
  // On a narrow screen the chart scrolls sideways; start at the newest day.
  ['volumeTrend', 'categoryTrend'].forEach(function (id) { var el = byId(id); el.scrollLeft = el.scrollWidth; });
}

async function loadHealth() {
  var pill = byId('health');
  var h;
  try {
    h = await fetchJson('/dashboard/api/health');
  } catch (err) {
    pill.className = 'health bad';
    pill.textContent = 'Status unknown';
    byId('system-facts').innerHTML = '<dt>Status</dt><dd class="load-error">Did not load: ' + esc(err.message || err) + '</dd>';
    return;
  }
  var lastMsg = h.latestMessageAt ? new Date(h.latestMessageAt) : null;
  var quietHours = lastMsg ? (Date.now() - lastMsg.getTime()) / 3600000 : Infinity;
  if (h.lastDeliveryFailedAt && (!h.lastDeliveredAt || h.lastDeliveryFailedAt > h.lastDeliveredAt)) {
    pill.className = 'health bad'; pill.textContent = 'Delivery failing';
  } else if (quietHours > 12) {
    pill.className = 'health warn'; pill.textContent = 'No mail for ' + Math.round(quietHours) + ' hours';
  } else {
    pill.className = 'health ok'; pill.textContent = 'Mail flowing';
  }
  pill.title = lastMsg ? 'Last message ' + fullFormat.format(lastMsg) : 'No messages logged';
  byId('system-facts').innerHTML =
    '<dt>Last message logged</dt><dd>' + timeTag(h.latestMessageAt, 'relative') + ' (' + timeTag(h.latestMessageAt) + ')</dd>' +
    '<dt>Last delivery to the mailbox</dt><dd>' + timeTag(h.lastDeliveredAt, 'relative') + '</dd>' +
    '<dt>Last failed delivery</dt><dd>' + (h.lastDeliveryFailedAt ? timeTag(h.lastDeliveryFailedAt, 'relative') + ': ' + esc(h.lastDeliveryFailure) : 'None on record') + '</dd>' +
    '<dt>Last event-log write</dt><dd>' + timeTag(h.latestEventAt, 'relative') + '</dd>' +
    '<dt>Last retention sweep</dt><dd>' + (h.lastRetentionSweepAt ? timeTag(h.lastRetentionSweepAt, 'relative') + ', ' + esc(h.lastRetentionSweep) : 'Never') + '</dd>' +
    (h.judgeComparisons ? '<dt>Judges disagree</dt><dd>' + esc(h.judgeDisagreements) + ' of ' + esc(h.judgeComparisons) + ' messages in 7 days (' + Math.round(h.judgeDisagreements / h.judgeComparisons * 100) + '%)</dd>' : '') +
    '<dt>Messages on record</dt><dd class="num">' + esc(h.messageCount) + '</dd>' +
    '<dt>Times shown in</dt><dd>Phoenix (MST, UTC-7, no daylight saving)</dd>';
}

// ---- activity ----
function pagerHtml(name, hasMore, shown) {
  var p = pagers[name];
  var from = shown ? p.offset + 1 : 0;
  return '<label for="limit-' + name + '">Rows</label>' +
    '<select id="limit-' + name + '" data-pager-limit="' + name + '">' + [20, 50, 100].map(function (n) { return '<option' + (n === p.limit ? ' selected' : '') + '>' + n + '</option>'; }).join('') + '</select>' +
    '<span class="spacer"></span><span class="num">' + (shown ? from + '-' + (p.offset + shown) : 'None') + '</span>' +
    '<button type="button" class="btn secondary small" data-pager-prev="' + name + '"' + (p.offset === 0 ? ' disabled' : '') + '>Newer</button>' +
    '<button type="button" class="btn secondary small" data-pager-next="' + name + '"' + (hasMore ? '' : ' disabled') + '>Older</button>';
}
function renderPager(name, hasMore, shown) {
  document.querySelector('[data-pager="' + name + '"]').innerHTML = pagerHtml(name, hasMore, shown);
}
document.addEventListener('change', function (e) {
  var sel = e.target.closest('[data-pager-limit]');
  if (!sel) return;
  var name = sel.dataset.pagerLimit;
  pagers[name].limit = Number(sel.value);
  pagers[name].offset = 0;
  reloadPager(name);
});
document.addEventListener('click', function (e) {
  var prev = e.target.closest('[data-pager-prev]');
  var next = e.target.closest('[data-pager-next]');
  var btn = prev || next;
  if (!btn) return;
  var name = btn.dataset.pagerPrev || btn.dataset.pagerNext;
  var p = pagers[name];
  p.offset = prev ? Math.max(0, p.offset - p.limit) : p.offset + p.limit;
  reloadPager(name);
});
function reloadPager(name) {
  if (name === 'messages') loadMessages();
  else if (name === 'rules') loadRules();
  else loadActions();
}

async function loadMessages() {
  var body = byId('messages');
  var p = pagers.messages;
  var params = new URLSearchParams({ limit: String(p.limit), offset: String(p.offset) });
  if (state.outcome) params.set('disposition', state.outcome);
  if (state.q) params.set('q', state.q);
  var data;
  try {
    data = await fetchJson('/dashboard/api/messages?' + params.toString());
  } catch (err) {
    body.innerHTML = loadFailedRow('activity', err, 8);
    renderPager('messages', false, 0);
    return;
  }
  var filtered = state.outcome || state.q;
  body.innerHTML = data.rows.length ? data.rows.map(function (r) { return messageRow(r, false); }).join('')
    : '<tr><td colspan="8" class="empty">' + (filtered ? 'Nothing in the message log matches this search or outcome filter.' : 'No mail yet.') + '</td></tr>';
  renderPager('messages', data.hasMore, data.rows.length);
}
byId('activity-search').addEventListener('submit', function (e) {
  e.preventDefault();
  state.q = byId('q').value.trim();
  pagers.messages.offset = 0;
  writeHash();
  loadMessages();
});
byId('outcome-chips').addEventListener('click', function (e) {
  var chip = e.target.closest('[data-outcome]');
  if (!chip) return;
  state.outcome = chip.dataset.outcome;
  pagers.messages.offset = 0;
  writeHash();
  showView();
  loadMessages();
});

// ---- message inspector ----
async function openMessage(id) {
  var dlg = byId('inspector');
  var body = byId('inspector-body');
  byId('inspector-title').textContent = 'Message #' + id;
  body.innerHTML = '<div class="empty">Loading...</div>';
  if (!dlg.open) dlg.showModal();
  var m;
  try {
    m = await fetchJson('/dashboard/api/messages/' + encodeURIComponent(id));
  } catch (err) {
    body.innerHTML = loadFailed('this message', err);
    return;
  }
  byId('inspector-title').textContent = m.subject || '(no subject)';
  var facts = [
    ['From', esc(m.from_display || m.from_domain || '-')],
    ['Received', timeTag(m.received_at) + ' <span class="muted">(' + timeTag(m.received_at, 'relative') + ')</span>'],
    ['Outcome', outcomeTag(m.enforced_disposition) + (m.disposition && m.disposition !== m.enforced_disposition ? ' <span class="muted">(recommended ' + esc(OUTCOME[m.disposition] || m.disposition) + ')</span>' : '')],
    ['Verdict', esc(m.verdict)],
    ['Category', esc(m.category)],
    ['Alert', esc(m.alert_level || 'NONE')],
    ['Recipient', m.recipient_class ? esc(RECIPIENT[m.recipient_class] || m.recipient_class) + (m.recipient_detail ? '<br><span class="muted">' + esc(m.recipient_detail) + '</span>' : '') : '<span class="muted">Not recorded</span>'],
    ['Injection check', esc(m.injection_label || '-') + (m.injection_score != null ? ' <span class="muted num">(' + Number(m.injection_score).toFixed(3) + ')</span>' : '')],
    ['Shadow mode', m.shadow_mode ? 'Yes' : 'No'],
  ];
  var html = '<dl class="facts">' + facts.map(function (f) { return '<dt>' + f[0] + '</dt><dd>' + f[1] + '</dd>'; }).join('') + '</dl>';
  html += '<div class="detail-block"><h3>Why</h3><pre class="detail-box">' + esc(m.reasoning || m.analysis || '(none saved)') + '</pre></div>';
  if (m.full_content) {
    html += '<div class="detail-block"><h3>Saved message</h3><pre class="detail-box">' + esc(m.full_content) + '</pre></div>';
  }
  if (m.triggered_rule) {
    html += '<div class="detail-block"><h3>Rule that decided this</h3><pre class="detail-box">' + esc(m.triggered_rule) + '</pre>' +
      '<p><button type="button" class="btn danger" data-reverse-rule="' + esc(m.triggered_rule) + '" data-message-id="' + esc(m.id) + '">Remove this rule</button></p></div>';
  }
  html += '<p class="muted" style="margin-top:16px"><button type="button" class="btn secondary small" data-copy="' + esc(m.from_domain || '') + '">Copy sender domain</button> ' +
    '<a href="#activity?q=' + encodeURIComponent(m.from_domain || '') + '" data-close-inspector>All mail from this domain</a></p>';
  body.innerHTML = html;
}
document.addEventListener('click', function (e) {
  var open = e.target.closest('[data-open-message]');
  if (open) { e.preventDefault(); openMessage(open.dataset.openMessage); return; }
  var row = e.target.closest('[data-row-message]');
  if (row && !e.target.closest('button, a, input, label')) { openMessage(row.dataset.rowMessage); return; }
  var copy = e.target.closest('[data-copy]');
  if (copy) {
    navigator.clipboard.writeText(copy.dataset.copy).then(function () { toast('Copied ' + copy.dataset.copy); }, function () { toast('Copy failed.'); });
    return;
  }
  if (e.target.closest('[data-close-inspector]')) byId('inspector').close();
  var rev = e.target.closest('[data-reverse-rule]');
  if (rev) reverseRule(rev);
});

function confirmDialog(title, bodyHtml, okLabel) {
  return new Promise(function (resolve) {
    var dlg = byId('confirm');
    byId('confirm-title').textContent = title;
    byId('confirm-body').innerHTML = bodyHtml;
    byId('confirm-ok').textContent = okLabel;
    dlg.returnValue = '';
    dlg.addEventListener('close', function done() { dlg.removeEventListener('close', done); resolve(dlg.returnValue === 'ok'); });
    dlg.showModal();
  });
}

async function reverseRule(btn) {
  var rule = btn.dataset.reverseRule;
  var ok = await confirmDialog('Remove this rule?', '<pre class="detail-box">' + esc(rule) + '</pre><p>New mail is decided without it. Messages it already decided stay as they are.</p>', 'Remove rule');
  if (!ok) return;
  btn.disabled = true;
  try {
    await fetchJson('/dashboard/api/rules/reverse', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rule: rule, message_id: Number(btn.dataset.messageId) || null }) });
    toast('Rule removed.');
    btn.textContent = 'Removed';
    loadFilteringPolicy();
  } catch (err) {
    btn.disabled = false;
    toast('The rule was not removed: ' + (err.message || err));
  }
}

// ---- policy ----
var SENDER_LISTS = [
  ['whitelist', 'Always accept', '250'],
  ['greylist', 'Always defer', '421'],
  ['blacklist', 'Always reject', '550'],
];
var SEMANTIC = [
  ['250', 'Accept when'],
  ['421', 'Defer when'],
  ['550', 'Reject when'],
];
var ruleStats = null;
function hitsLine(stat) {
  if (!ruleStats) return '';
  if (!stat) return 'No hits in ' + ruleStats.days + ' days';
  return stat.hits + (stat.hits === 1 ? ' hit' : ' hits') + ' in ' + ruleStats.days + ' days, last ' + relative(new Date(stat.last));
}
function policyEntry(primary, detail, kind, group, index, cardLabel) {
  var stat = null;
  if (ruleStats && kind === 'sender_list') stat = ruleStats.senders[primary];
  else if (ruleStats && (kind === 'semantic_rule' || kind === 'blacklist_pattern')) stat = ruleStats.rules[primary];
  var hits = kind === 'custom_action' ? '' : hitsLine(stat);
  return '<div class="policy-entry" data-search="' + esc((primary + ' ' + (detail || '')).toLowerCase()) + '">' +
    '<div><div>' + esc(primary) + '</div>' + (detail ? '<div class="policy-entry-detail">' + esc(detail) + '</div>' : '') +
    (hits ? '<div class="policy-entry-detail' + (stat ? '' : ' stale') + '">' + esc(hits) + '</div>' : '') + '</div>' +
    '<button type="button" class="btn secondary small" data-policy-remove="' + esc(kind) + '" data-policy-group="' + esc(group) + '" data-policy-index="' + index + '" data-policy-list="' + esc(cardLabel) + '" aria-label="Remove ' + esc(primary) + ' from ' + esc(cardLabel) + '">Remove</button>' +
    '</div>';
}
function policyCard(title, code, entries, form) {
  return '<div class="panel policy-card"><h4><span>' + esc(title) + (code ? ' <span class="muted">(' + code + ')</span>' : '') + '</span><span class="tag num">' + entries.length + '</span></h4>' +
    '<div class="policy-entries">' + (entries.length ? entries.join('') : '<div class="empty">No entries.</div>') + '</div>' + form + '</div>';
}
var formSeq = 0;
function field(label, control) {
  formSeq += 1;
  var id = 'pf-' + formSeq;
  return '<label for="' + id + '">' + label + '</label>' + control.split('ID').join(id);
}
function renderFilteringPolicy() {
  if (!filteringPolicy) return;
  var total = standingRuleCount(filteringPolicy);
  byId('policy-total').textContent = total + ' standing rules';
  var rc = byId('ruleCountValue');
  if (rc) rc.textContent = total;
  var warnings = filteringPolicy.migration_warnings || [];
  byId('filteringWarnings').innerHTML = warnings.length
    ? '<div class="policy-warning"><strong>Legacy rules need review.</strong><br>' + warnings.map(esc).join('<br>') + '</div>' : '';

  var senderCards = SENDER_LISTS.map(function (s) {
    var entries = (filteringPolicy.sender_lists[s[0]] || []).map(function (sel, i) { return policyEntry(sel, '', 'sender_list', s[0], i, s[1]); });
    var form = '<form class="policy-form" data-policy-form="sender_list" data-policy-group="' + s[0] + '">' +
      field('Add an address or domain', '<input id="ID" name="selector" required placeholder="person@example.com or example.com" autocomplete="off">') +
      '<div class="review-actions"><button class="btn" type="submit">Add</button><button class="btn secondary" type="button" data-preview>Preview</button></div><output class="preview" aria-live="polite"></output></form>';
    return policyCard(s[1], s[2], entries, form);
  }).join('');
  var patterns = (filteringPolicy.blacklist_patterns || []).map(function (p, i) { return policyEntry(p, '', 'blacklist_pattern', 'blacklist_patterns', i, 'Reject patterns'); });
  var patternCard = policyCard('Reject patterns', '550', patterns,
    '<form class="policy-form" data-policy-form="blacklist_pattern" data-policy-group="blacklist_patterns">' +
      field('Add a pattern, matched against the whole sender domain', '<input id="ID" name="pattern" required placeholder="(spam|promo)[0-9]+[.]example" autocomplete="off">') +
      '<div class="review-actions"><button class="btn" type="submit">Add</button><button class="btn secondary" type="button" data-preview>Preview</button></div><output class="preview" aria-live="polite"></output></form>');
  var semanticCards = SEMANTIC.map(function (s) {
    var entries = (filteringPolicy.semantic_rules[s[0]] || []).map(function (rule, i) { return policyEntry(rule, '', 'semantic_rule', s[0], i, s[1]); });
    var form = '<form class="policy-form" data-policy-form="semantic_rule" data-policy-group="' + s[0] + '">' +
      field('Add a condition about the content', '<textarea id="ID" name="rule" required placeholder="The message asks for a gift card payment"></textarea>') +
      '<button class="btn" type="submit">Add</button></form>';
    return policyCard(s[1], s[0], entries, form);
  }).join('');
  var custom = (filteringPolicy.custom_actions || []).map(function (a, i) {
    return policyEntry(a.selector, a.instruction + (a.native ? ' (folder: ' + a.native.folder + ')' : ' (agent)'), 'custom_action', 'custom_actions', i, 'After delivery');
  });
  var customCard = policyCard('After delivery', '', custom,
    '<form class="policy-form" data-policy-form="custom_action" data-policy-group="custom_actions">' +
      field('Sender address or domain', '<input id="ID" name="selector" required placeholder="person@example.com or example.com" autocomplete="off">') +
      field('What to do with accepted mail from them', '<textarea id="ID" name="instruction" required placeholder="File it in Receipts"></textarea>') +
      field('IMAP folder, or blank to let the agent act', '<input id="ID" name="native_folder" placeholder="Receipts" autocomplete="off">') +
      '<button class="btn" type="submit">Add or replace</button></form>');

  byId('filteringPolicy').innerHTML =
    '<div><h3 class="policy-group-title">Sender lists</h3><div class="policy-grid">' + senderCards + patternCard + '</div></div>' +
    '<div><h3 class="policy-group-title">Content rules</h3><p class="policy-group-note">Given to the judge for mail no sender list decided.</p><div class="policy-grid">' + semanticCards + '</div></div>' +
    '<div><h3 class="policy-group-title">Standing actions</h3><div class="policy-grid">' + customCard + '</div></div>';
  applyPolicyFilter();
}
function applyPolicyFilter() {
  var needle = byId('policy-filter').value.trim().toLowerCase();
  document.querySelectorAll('.policy-entry').forEach(function (el) { el.hidden = needle !== '' && el.dataset.search.indexOf(needle) === -1; });
}
byId('policy-filter').addEventListener('input', applyPolicyFilter);

async function loadFilteringPolicy() {
  try {
    var both = await Promise.all([
      fetchJson('/dashboard/api/filtering'),
      fetchJson('/dashboard/api/rule-stats').catch(function () { return null; }),
    ]);
    filteringPolicy = both[0];
    ruleStats = both[1];
    renderFilteringPolicy();
  } catch (err) {
    byId('filteringPolicy').innerHTML = loadFailed('the filtering policy', err);
  }
}
async function mutateFilteringPolicy(payload) {
  var data = await fetchJson('/dashboard/api/filtering', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  filteringPolicy = data.policy;
  renderFilteringPolicy();
  toast(data.changed === false ? 'No change was needed.' : 'Saved.');
}
byId('filteringPolicy').addEventListener('submit', async function (event) {
  var form = event.target.closest('[data-policy-form]');
  if (!form) return;
  event.preventDefault();
  var button = form.querySelector('button[type="submit"]');
  var values = new FormData(form);
  var kind = form.dataset.policyForm;
  var payload = { operation: 'put', kind: kind };
  if (kind === 'sender_list') { payload.list = form.dataset.policyGroup; payload.selector = values.get('selector'); }
  else if (kind === 'semantic_rule') { payload.disposition = form.dataset.policyGroup; payload.rule = values.get('rule'); }
  else if (kind === 'blacklist_pattern') { payload.pattern = values.get('pattern'); }
  else { payload.selector = values.get('selector'); payload.instruction = values.get('instruction'); payload.native_folder = values.get('native_folder'); }
  button.disabled = true;
  try {
    await mutateFilteringPolicy(payload);
  } catch (err) {
    toast('Save failed: ' + (err.message || err));
    button.disabled = false;
  }
});
byId('filteringPolicy').addEventListener('click', async function (event) {
  var preview = event.target.closest('[data-preview]');
  if (preview) { previewPolicyChange(preview); return; }
  var button = event.target.closest('[data-policy-remove]');
  if (!button || !filteringPolicy) return;
  var kind = button.dataset.policyRemove;
  var group = button.dataset.policyGroup;
  var index = Number(button.dataset.policyIndex);
  var payload = { operation: 'remove', kind: kind };
  var shown;
  if (kind === 'sender_list') { payload.list = group; payload.selector = shown = filteringPolicy.sender_lists[group][index]; }
  else if (kind === 'semantic_rule') { payload.disposition = group; payload.rule = shown = filteringPolicy.semantic_rules[group][index]; }
  else if (kind === 'blacklist_pattern') { payload.pattern = shown = filteringPolicy.blacklist_patterns[index]; }
  else { payload.selector = shown = filteringPolicy.custom_actions[index].selector; }
  var ok = await confirmDialog('Remove from ' + button.dataset.policyList + '?', '<pre class="detail-box">' + esc(shown) + '</pre><p>New mail is decided without it.</p>', 'Remove');
  if (!ok) return;
  button.disabled = true;
  try {
    await mutateFilteringPolicy(payload);
  } catch (err) {
    toast('Remove failed: ' + (err.message || err));
    button.disabled = false;
  }
});

var TARGET_WORD = { '250': 'accepted', '421': 'deferred', '550': 'rejected' };
async function previewPolicyChange(button) {
  var form = button.closest('[data-policy-form]');
  var out = form.querySelector('output.preview');
  var values = new FormData(form);
  var payload = { kind: form.dataset.policyForm };
  if (payload.kind === 'sender_list') { payload.list = form.dataset.policyGroup; payload.selector = values.get('selector'); }
  else { payload.pattern = values.get('pattern'); }
  if (!(payload.selector || payload.pattern)) { out.textContent = 'Type an entry first.'; return; }
  out.textContent = 'Checking recent mail...';
  try {
    var r = await fetchJson('/dashboard/api/simulate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!r.total) { out.textContent = 'No mail in the last ' + r.days + ' days came from a sender this covers.'; return; }
    var parts = ['250', '421', '550'].filter(function (k) { return r.byOutcome[k]; }).map(function (k) { return r.byOutcome[k] + ' ' + TARGET_WORD[k]; });
    out.innerHTML = esc(r.total + (r.total === 1 ? ' message' : ' messages') + ' in the last ' + r.days + ' days (' + parts.join(', ') + '). ' +
      (r.wouldChange ? r.wouldChange + ' would have been ' + TARGET_WORD[r.target] + ' instead.' : 'None would have changed.') +
      ' A more specific entry, and the DMARC check, can still decide otherwise.') +
      '<ul>' + r.samples.map(function (m) { return '<li><button type="button" class="linkish" data-open-message="' + esc(m.id) + '">' + esc(m.from_domain) + ': ' + esc(m.subject || '(no subject)') + '</button> ' + outcomeTag(m.enforced_disposition) + '</li>'; }).join('') + '</ul>';
  } catch (err) {
    out.textContent = 'Preview failed: ' + (err.message || err);
  }
}

// ---- review ----
var OUTCOME_LABEL = { '250': 'Accepted', '421': 'Deferred', '550': 'Rejected' };
async function loadReview() {
  var list = byId('review-list');
  var metrics = byId('review-metrics');
  var both;
  try {
    both = await Promise.all([fetchJson('/dashboard/api/review'), fetchJson('/dashboard/api/review/metrics')]);
  } catch (err) {
    list.innerHTML = loadFailed('the review sample', err);
    metrics.innerHTML = '';
    return;
  }
  var byOutcome = {};
  both[1].rows.forEach(function (r) {
    var o = byOutcome[r.outcome] || (byOutcome[r.outcome] = { sample: null, reversal: 0 });
    if (r.source === 'sample') o.sample = r; else o.reversal += r.labeled;
  });
  metrics.innerHTML = ['250', '421', '550'].map(function (k) {
    var o = byOutcome[k] || {};
    var line = o.sample ? o.sample.wrong + ' wrong of ' + o.sample.labeled + ' reviewed (' + Math.round(o.sample.wrong / o.sample.labeled * 100) + '%)' : 'None reviewed yet';
    if (o.reversal) line += '; ' + o.reversal + ' more marked wrong by removing their rule';
    return '<dt>' + OUTCOME_LABEL[k] + '</dt><dd>' + esc(line) + '</dd>';
  }).join('');
  var rows = both[0].rows;
  list.innerHTML = rows.length ? rows.map(function (m) {
    var others = ['250', '421', '550'].filter(function (k) { return k !== m.enforced_disposition; });
    return '<div class="panel review-card" data-review-id="' + esc(m.id) + '">' +
      '<div class="review-head">' + outcomeTag(m.enforced_disposition) + '<strong>' + esc(m.from_domain || m.from_display || '-') + '</strong>' + timeTag(m.received_at, 'relative') + '<span class="tag">' + esc(m.category) + '</span></div>' +
      '<div><button type="button" class="linkish" data-open-message="' + esc(m.id) + '">' + esc(m.subject || '(no subject)') + '</button></div>' +
      '<div class="review-why">' + esc((m.reasoning || '').slice(0, 280)) + '</div>' +
      '<div class="review-actions"><button type="button" class="btn secondary small" data-review="right">Right</button>' +
      others.map(function (k) { return '<button type="button" class="btn secondary small" data-review="wrong" data-correct="' + k + '">Should be ' + OUTCOME_LABEL[k].toLowerCase() + '</button>'; }).join('') +
      '</div></div>';
  }).join('') : '<div class="empty">Every recent message in the sample is reviewed.</div>';
}
byId('review-list').addEventListener('click', async function (e) {
  var btn = e.target.closest('[data-review]');
  if (!btn) return;
  var card = btn.closest('[data-review-id]');
  card.querySelectorAll('button[data-review]').forEach(function (b) { b.disabled = true; });
  try {
    await fetchJson('/dashboard/api/review', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: Number(card.dataset.reviewId), verdict: btn.dataset.review, correct_disposition: btn.dataset.correct || null }) });
    card.classList.add('done');
    btn.textContent = btn.dataset.review === 'right' ? 'Marked right' : 'Marked wrong';
  } catch (err) {
    card.querySelectorAll('button[data-review]').forEach(function (b) { b.disabled = false; });
    toast('The label was not saved: ' + (err.message || err));
  }
});

// ---- audit ----
async function loadRules() {
  var body = byId('rules');
  var p = pagers.rules;
  var data;
  try {
    data = await fetchJson('/dashboard/api/rules?limit=' + p.limit + '&offset=' + p.offset);
  } catch (err) {
    body.innerHTML = loadFailedRow('rule changes', err, 4);
    renderPager('rules', false, 0);
    return;
  }
  body.innerHTML = data.rows.length ? data.rows.map(function (r) {
    return '<tr><td>' + timeTag(r.changed_at) + '</td><td>' + esc(r.action) + '</td><td>' + esc(r.rule_text) + '</td><td class="muted">' + esc(r.source) + '</td></tr>';
  }).join('') : '<tr><td colspan="4" class="empty">No rule changes yet.</td></tr>';
  renderPager('rules', data.hasMore, data.rows.length);
}
async function loadActions() {
  var body = byId('actions');
  var p = pagers.actions;
  var data;
  try {
    data = await fetchJson('/dashboard/api/actions?limit=' + p.limit + '&offset=' + p.offset);
  } catch (err) {
    body.innerHTML = loadFailedRow('mailbox actions', err, 5);
    renderPager('actions', false, 0);
    return;
  }
  body.innerHTML = data.rows.length ? data.rows.map(function (r) {
    return '<tr><td>' + timeTag(r.executed_at) + '</td><td>' + esc(r.kind) + '</td><td>' + (esc(r.domain) || '<span class="muted">-</span>') + '</td><td>' + (esc(r.result) || '<span class="muted">-</span>') + '</td><td>' + esc(r.outcome_summary) + '</td></tr>';
  }).join('') : '<tr><td colspan="5" class="empty">No mailbox actions yet.</td></tr>';
  renderPager('actions', data.hasMore, data.rows.length);
}
byId('audit-chips').addEventListener('click', function (e) {
  var chip = e.target.closest('[data-audit]');
  if (!chip) return;
  state.audit = chip.dataset.audit;
  writeHash();
  showView();
  loadView();
});

// ---- loading and refresh ----
function loadView() {
  loadHealth();
  if (state.view === 'overview') { loadSummary(); loadAttention(); loadRecent(); loadTrends(); }
  else if (state.view === 'activity') loadMessages();
  else if (state.view === 'policy') loadFilteringPolicy();
  else if (state.view === 'audit') { if (state.audit === 'rules') loadRules(); else loadActions(); }
  else if (state.view === 'review') loadReview();
  state.loadedAt = Date.now();
  tick();
}
function tick() {
  if (!state.loadedAt) return;
  byId('updated').textContent = 'Updated ' + relative(new Date(state.loadedAt));
}
byId('refresh').addEventListener('click', loadView);
setInterval(tick, 15000);
setInterval(function () {
  if (document.visibilityState === 'visible' && (state.view === 'overview' || state.view === 'activity') && !byId('inspector').open) loadView();
}, 60000);
document.addEventListener('keydown', function (e) {
  if (e.key !== '/' || e.target.closest('input, textarea, select') || e.metaKey || e.ctrlKey) return;
  e.preventDefault();
  if (state.view !== 'activity') { location.hash = '#activity'; }
  setTimeout(function () { byId('q').focus(); }, 0);
});

readHash();
showView();
loadView();
loadFilteringPolicy();
</script>
</body>
</html>
`;

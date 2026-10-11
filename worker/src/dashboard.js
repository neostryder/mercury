// Server-side helpers for the dashboard's trend charts: the last N Phoenix
// days, the stacked-bar SVG, and the category color classes. The page itself
// is static (worker/ui, built into worker/public) and fetches the charts from
// /dashboard/api/trends.

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

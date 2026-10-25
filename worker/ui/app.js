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
// An expired Access session answers an API call with a redirect to the login
// page. With redirect: 'manual' that shows up as an opaque redirect rather
// than a cross-origin failure, so the page can say so and offer to sign in.
async function fetchJson(url, options) {
  var res;
  try {
    res = await fetch(url, Object.assign({ redirect: 'manual', credentials: 'same-origin' }, options || {}));
  } catch (err) {
    var offline = new Error('the network is unreachable');
    offline.code = 'offline';
    setBanner('offline');
    throw offline;
  }
  if (res.type === 'opaqueredirect' || res.status === 401 || res.status === 403) {
    var expired = new Error('your sign-in has expired');
    expired.code = 'session';
    setBanner('session');
    throw expired;
  }
  setBanner('');
  var data = null;
  try { data = await res.json(); } catch (err) { data = null; }
  if (!res.ok || data === null || data.ok === false) {
    throw new Error((data && (data.error || data.detail)) || ('HTTP ' + res.status));
  }
  return data;
}
function setBanner(kind) {
  var el = byId('banner');
  if (kind === 'session') {
    el.className = 'banner warn';
    el.innerHTML = 'Your sign-in has expired. <a href="/dashboard/login">Sign in again</a>';
    el.hidden = false;
    autoSignIn();
  } else if (kind === 'offline') {
    el.className = 'banner';
    el.innerHTML = 'You are offline, so what you see may be out of date. <button type="button" data-retry>Try again</button>';
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}
// Opening the app with no valid session goes straight to sign-in, once a
// minute at most, so a login that cannot succeed cannot loop.
function autoSignIn() {
  if (!navigator.onLine) return;
  try {
    var last = Number(sessionStorage.getItem('mercury-signin-at') || 0);
    if (Date.now() - last < 60000) return;
    sessionStorage.setItem('mercury-signin-at', String(Date.now()));
  } catch (err) { return; }
  location.href = '/dashboard/login';
}
function loadFailed(what, err) {
  var subject = what.charAt(0).toUpperCase() + what.slice(1);
  if (err && err.code === 'session') {
    return '<div class="empty">Your sign-in has expired, so ' + esc(what) + ' could not load. <a href="/dashboard/login">Sign in again</a></div>';
  }
  if (err && err.code === 'offline') {
    return '<div class="empty">' + esc(subject) + ' needs a connection. Pull down to try again.</div>';
  }
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

// ---- theme and motion ----
var THEME_COLORS = { light: '#f4efe3', dark: '#101624' };
function readSetting(key) {
  try { return localStorage.getItem(key) || ''; } catch (err) { return ''; }
}
function writeSetting(key, value) {
  try { if (value) localStorage.setItem(key, value); else localStorage.removeItem(key); } catch (err) { /* private window */ }
}
function applyTheme(value) {
  var root = document.documentElement;
  if (value) root.setAttribute('data-theme', value); else root.removeAttribute('data-theme');
  var light = byId('theme-color-light');
  var dark = byId('theme-color-dark');
  if (value) {
    // A forced theme overrides the device preference, so both tags say the same thing.
    light.content = dark.content = THEME_COLORS[value];
  } else {
    light.content = THEME_COLORS.light;
    dark.content = THEME_COLORS.dark;
  }
  var names = { '': 'follows the device', light: 'light', dark: 'dark' };
  var toggle = byId('theme-toggle');
  toggle.setAttribute('aria-label', 'Theme: ' + names[value] + '. Change theme');
  toggle.title = 'Theme: ' + names[value];
  byId('theme').value = value;
}
function applyMotion(value) {
  if (value) document.documentElement.setAttribute('data-motion', value); else document.documentElement.removeAttribute('data-motion');
  byId('motion').value = value;
}
(function () {
  applyTheme(readSetting('mercury-theme'));
  applyMotion(readSetting('mercury-motion'));
  byId('theme').addEventListener('change', function (e) {
    applyTheme(e.target.value);
    writeSetting('mercury-theme', e.target.value);
  });
  byId('motion').addEventListener('change', function (e) {
    applyMotion(e.target.value);
    writeSetting('mercury-motion', e.target.value);
  });
  // The toolbar button steps through: follow the device, light, dark.
  byId('theme-toggle').addEventListener('click', function () {
    var order = ['', 'light', 'dark'];
    var next = order[(order.indexOf(readSetting('mercury-theme')) + 1) % order.length];
    applyTheme(next);
    writeSetting('mercury-theme', next);
    toast('Theme: ' + (next || 'follows the device'));
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
  if (state.view === 'activity' || state.view === 'review') {
    if (state.outcome) params.set('outcome', state.outcome);
    if (state.q) params.set('q', state.q);
  }
  if (state.view === 'audit' && state.audit === 'actions') params.set('show', 'actions');
  var next = '#' + state.view + (params.toString() ? '?' + params.toString() : '');
  if (location.hash !== next) history.replaceState(null, '', next);
}
var shownView = null;
function showView() {
  document.querySelectorAll('[data-view]').forEach(function (el) { el.hidden = el.dataset.view !== state.view; });
  document.querySelectorAll('[data-nav]').forEach(function (a) {
    if (a.dataset.nav === state.view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  document.querySelectorAll('#outcome-chips .chip, #review-chips .chip').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.outcome === state.outcome)); });
  document.querySelectorAll('#audit-chips .chip').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.audit === state.audit)); });
  document.querySelectorAll('[data-audit-panel]').forEach(function (p) { p.hidden = p.dataset.auditPanel !== state.audit; });
  byId('q').value = state.q;
  byId('more-open').classList.toggle('on', ['policy', 'audit', 'system'].indexOf(state.view) !== -1);
  document.title = 'Mercury - ' + (state.view === 'overview' ? 'Briefing' : state.view.charAt(0).toUpperCase() + state.view.slice(1));
  if (shownView !== state.view) { shownView = state.view; window.scrollTo(0, 0); }
}
window.addEventListener('hashchange', function () {
  document.querySelectorAll('dialog[open]').forEach(function (d) { d.close(); });
  readHash(); showView(); loadView();
});

// ---- briefing ----
// Only the counts are kept for offline use: no sender, subject, or message text.
function saveLastSummary(d) {
  try {
    localStorage.setItem('mercury-last-summary', JSON.stringify({
      at: Date.now(), total: d.last24h.total, accepted: d.last24h.accepted, deferred: d.last24h.deferred,
      rejected: d.last24h.hardBounces, open: d.openActionItems || 0,
    }));
  } catch (err) { /* private window */ }
}
function readLastSummary() {
  try { return JSON.parse(localStorage.getItem('mercury-last-summary') || 'null'); } catch (err) { return null; }
}
function statLink(href, value, label, cls) {
  return '<a class="stat ' + cls + '" href="' + href + '"><b class="num">' + esc(value) + '</b><span>' + label + '</span></a>';
}
function renderStats(c, staleAt) {
  byId('stats').innerHTML =
    statLink('#activity', c.total, 'Received', '') +
    statLink('#activity?outcome=250', c.accepted, 'Accepted', 's-250') +
    statLink('#activity?outcome=421', c.deferred, 'Deferred', 's-421') +
    statLink('#activity?outcome=550', c.rejected, 'Rejected', 's-550') +
    (staleAt ? '<div class="stale-note">Last updated ' + timeTag(new Date(staleAt).toISOString(), 'relative') + '. Only these counts are available until you are back online.</div>' : '');
  setOpenCount(c.open);
}
function setOpenCount(n) {
  var count = byId('attention-count');
  count.textContent = n;
  count.dataset.zero = String(!n);
  var badge = byId('tab-badge');
  badge.textContent = n > 99 ? '99+' : n;
  badge.hidden = !n;
  // Where the browser supports it, the installed app's icon carries the same number.
  try {
    if (n && navigator.setAppBadge) navigator.setAppBadge(n);
    else if (navigator.clearAppBadge) navigator.clearAppBadge();
  } catch (err) { /* not supported */ }
}

async function loadSummary() {
  var data;
  try {
    data = await fetchJson('/dashboard/api/summary');
  } catch (err) {
    var cached = readLastSummary();
    if (cached && err.code === 'offline') renderStats(cached, cached.at);
    else byId('stats').innerHTML = loadFailed('the summary', err);
    byId('strip').innerHTML = '';
    byId('categoryBars').innerHTML = loadFailed('the category breakdown', err);
    return;
  }
  lastSummary = data;
  saveLastSummary(data);
  var d = data.last24h;
  var open = data.openActionItems || 0;
  renderStats({ total: d.total, accepted: d.accepted, deferred: d.deferred, rejected: d.hardBounces, open: open }, null);
  var rules = standingRuleCount(filteringPolicy);
  byId('strip').innerHTML =
    '<a href="#audit?show=actions"' + (d.deliveryFailed ? ' class="alarm"' : '') + '>' + (d.deliveryFailed ? esc(d.deliveryFailed) + ' failed, ' + esc(d.delivered) + ' delivered' : esc(d.delivered) + ' delivered, none failed') + '</a>' +
    '<span>' + (d.urgent ? esc(d.urgent) + ' urgent alert' + (d.urgent === 1 ? '' : 's') : 'No urgent alerts') + '</span>' +
    '<a href="#policy"><span id="ruleCountValue">' + esc(rules == null ? '-' : rules) + '</span> standing rules, ' + esc(data.last7d.ruleChanges) + ' changed this week</a>';
  var maxCount = Math.max.apply(null, [1].concat(data.categories.map(function (c) { return c.count; })));
  byId('categoryBars').innerHTML = data.categories.length ? data.categories.map(function (c) {
    return '<div class="bar-row"><div>' + esc(c.category || 'UNKNOWN') + '</div>' +
      '<div class="bar-track"><div class="bar-fill ' + esc(c.cls) + '" data-w="' + (c.count / maxCount * 100).toFixed(0) + '"></div></div>' +
      '<div class="bar-count num">' + esc(c.count) + '</div></div>';
  }).join('') : '<div class="empty">No mail in the last 7 days.</div>';
  // Widths go through the style object, which a strict content policy allows.
  requestAnimationFrame(function () {
    document.querySelectorAll('#categoryBars .bar-fill').forEach(function (el) { el.style.width = el.dataset.w + '%'; });
  });
}

// The home view leads with what needs a person: open action items first, then the
// spot-check sample when it has rows waiting.
async function loadAttention() {
  var box = byId('attention');
  var both = await Promise.all([
    fetchJson('/dashboard/api/action-items').then(function (r) { return { rows: r }; }, function (e) { return { error: e }; }),
    fetchJson('/dashboard/api/review').then(function (r) { return { rows: r.rows }; }, function () { return { rows: [] }; }),
  ]);
  if (both[0].error) {
    box.innerHTML = loadFailed('what needs you', both[0].error);
    return;
  }
  var items = both[0].rows;
  var sample = both[1].rows.length;
  setOpenCount(items.length);
  var html = items.map(function (r, i) {
    return '<article class="letter" data-id="' + esc(r.id) + '">' +
      '<span class="seal" aria-hidden="true">' + (i + 1) + '</span>' +
      '<small>' + esc(r.kind) + '</small>' +
      '<p class="body">' + esc(r.summary) + '</p>' +
      '<span class="meta">' + timeTag(r.created_at, 'relative') + '</span>' +
      '<div class="btns">' +
        '<button type="button" class="btn" data-complete-id="' + esc(r.id) + '">Mark done</button>' +
        (r.related_message_id ? '<button type="button" class="btn secondary" data-open-message="' + esc(r.related_message_id) + '">Open message</button>' : '') +
      '</div></article>';
  }).join('');
  if (sample) {
    html += '<article class="letter">' +
      '<small>Spot check</small>' +
      '<p class="body">' + esc(sample) + (sample === 1 ? ' recent decision needs' : ' recent decisions need') + ' a right or wrong mark.</p>' +
      '<div class="btns"><a class="btn secondary" href="#review">Open the review</a></div></article>';
  }
  box.innerHTML = html || '<article class="letter calm"><p class="body">Nothing needs you right now.</p></article>';
}

byId('attention').addEventListener('click', async function (e) {
  var btn = e.target.closest('[data-complete-id]');
  if (!btn) return;
  var card = btn.closest('.letter');
  btn.disabled = true;
  try {
    var data = await fetchJson('/dashboard/api/action-items/' + btn.dataset.completeId + '/complete', { method: 'POST' });
    if (!data.completed) throw new Error('it was already completed or no longer exists');
    card.classList.add('done');
    btn.textContent = 'Done';
    setOpenCount(document.querySelectorAll('#attention .letter[data-id]:not(.done)').length);
    toast('Marked done.');
  } catch (err) {
    btn.disabled = false;
    toast('This item was not marked done: ' + (err.message || err) + '. Tap it again to retry.');
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
  html += '<p class="muted detail-actions"><button type="button" class="btn secondary small" data-copy="' + esc(m.from_domain || '') + '">Copy sender domain</button> ' +
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
var reviewData = null;
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
  reviewData = both[0];
  renderReviewLists();
}
// The outcome chips narrow both lists to one outcome without another fetch.
function renderReviewLists() {
  if (!reviewData) return;
  var pick = function (m) { return !state.outcome || m.enforced_disposition === state.outcome; };
  var word = state.outcome ? OUTCOME_LABEL[state.outcome].toLowerCase() + ' ' : '';
  var rows = reviewData.rows.filter(pick);
  byId('review-list').innerHTML = rows.length ? rows.map(function (m) { return reviewCard(m, null); }).join('')
    : '<div class="empty">' + (state.outcome ? 'No ' + word + 'messages are waiting for a mark.' : 'Every recent message in the sample is reviewed.') + '</div>';
  var reviewed = (reviewData.reviewed || []).filter(pick);
  byId('review-done').innerHTML = reviewed.length ? reviewed.map(function (m) {
    return reviewCard(m, { verdict: m.label_verdict, correct: m.label_correct });
  }).join('') : '<div class="empty">' + (state.outcome ? 'Nothing ' + word + 'has been reviewed yet.' : 'Nothing reviewed yet. Answer a message in the sample to list it here.') + '</div>';
}
byId('review-chips').addEventListener('click', function (e) {
  var chip = e.target.closest('[data-outcome]');
  if (!chip) return;
  state.outcome = chip.dataset.outcome;
  writeHash();
  showView();
  renderReviewLists();
});
// One card per message. `label` is null for a message still waiting for a
// label, or the label it already carries, which adds a status line, marks the
// current answer, and offers an undo.
function reviewCard(m, label) {
  var others = ['250', '421', '550'].filter(function (k) { return k !== m.enforced_disposition; });
  function choice(text, verdict, correct) {
    var current = label && label.verdict === verdict && (label.correct || null) === (correct || null);
    return '<button type="button" class="btn secondary small" data-review="' + verdict + '"' +
      (correct ? ' data-correct="' + correct + '"' : '') +
      (current ? ' disabled aria-pressed="true"' : '') + '>' + text + '</button>';
  }
  var status = '';
  if (label) {
    status = '<div class="review-status">' + (label.verdict === 'right' ? 'Marked right.'
      : 'Marked wrong: should be ' + (OUTCOME_LABEL[label.correct] || 'different').toLowerCase() + '.') + '</div>';
  }
  return '<div class="panel review-card" data-review-id="' + esc(m.id) + '">' +
    '<div class="review-head">' + outcomeTag(m.enforced_disposition) + '<strong>' + esc(m.from_domain || m.from_display || '-') + '</strong>' + timeTag(m.received_at, 'relative') + '<span class="tag">' + esc(m.category) + '</span></div>' +
    '<div><button type="button" class="linkish" data-open-message="' + esc(m.id) + '">' + esc(m.subject || '(no subject)') + '</button></div>' +
    '<div class="review-why">' + esc((m.reasoning || '').slice(0, 280)) + '</div>' + status +
    '<div class="review-actions">' + choice('Right', 'right', null) +
    others.map(function (k) { return choice('Should be ' + OUTCOME_LABEL[k].toLowerCase(), 'wrong', k); }).join('') +
    (label ? '<button type="button" class="btn secondary small" data-review="undo">Undo</button>' : '') +
    '</div></div>';
}
async function reviewClick(e) {
  var btn = e.target.closest('[data-review]');
  if (!btn) return;
  var card = btn.closest('[data-review-id]');
  card.querySelectorAll('button[data-review]').forEach(function (b) { b.disabled = true; });
  try {
    var saved = await fetchJson('/dashboard/api/review', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: Number(card.dataset.reviewId), verdict: btn.dataset.review, correct_disposition: btn.dataset.correct || null }) });
    await loadReview();
    if (saved && saved.delivery) toast(saved.delivery);
  } catch (err) {
    card.querySelectorAll('button[data-review]').forEach(function (b) { b.disabled = false; });
    toast('The label was not saved: ' + (err.message || err));
  }
}
byId('review-list').addEventListener('click', reviewClick);
byId('review-done').addEventListener('click', reviewClick);

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

// ---- phone behavior: sheets, pull to refresh, the More menu ----
byId('more-open').addEventListener('click', function () { byId('more').showModal(); });
document.addEventListener('click', function (e) {
  if (e.target.closest('[data-close-more]')) byId('more').close();
  if (e.target.closest('[data-retry]')) loadView();
  // A tap on the dimmed area outside a sheet closes it.
  if (e.target.tagName === 'DIALOG' && (e.target.id === 'inspector' || e.target.id === 'more')) e.target.close();
});

// Dragging a sheet's top edge down closes it.
['inspector', 'more'].forEach(function (id) {
  var dlg = byId(id);
  var startY = null;
  var dy = 0;
  dlg.addEventListener('touchstart', function (e) {
    var onHandle = e.target.closest('.sheet-grip, .dialog-head');
    if (!onHandle || e.target.closest('button')) return;
    startY = e.touches[0].clientY;
    dy = 0;
    dlg.classList.add('dragging');
  }, { passive: true });
  dlg.addEventListener('touchmove', function (e) {
    if (startY === null) return;
    dy = Math.max(0, e.touches[0].clientY - startY);
    dlg.style.transform = 'translateY(' + dy + 'px)';
  }, { passive: true });
  function end() {
    if (startY === null) return;
    startY = null;
    dlg.classList.remove('dragging');
    dlg.style.transform = '';
    if (dy > 90) dlg.close();
  }
  dlg.addEventListener('touchend', end);
  dlg.addEventListener('touchcancel', end);
});

// Pull down from the top of the page to refresh, on the views that show live mail.
(function () {
  var pull = byId('pull');
  var startY = null;
  var dist = 0;
  var LIMIT = 80;
  function reset() {
    startY = null;
    dist = 0;
    pull.style.setProperty('--pull', '0px');
    pull.classList.remove('ready');
  }
  document.addEventListener('touchstart', function (e) {
    var blocked = window.scrollY > 0 || document.querySelector('dialog[open]') || e.touches.length !== 1 || e.target.closest('.table-wrap, .chart-wrap, textarea');
    startY = blocked ? null : e.touches[0].clientY;
  }, { passive: true });
  document.addEventListener('touchmove', function (e) {
    if (startY === null) return;
    var d = e.touches[0].clientY - startY;
    if (d <= 0) { reset(); return; }
    dist = Math.min(LIMIT + 20, d * 0.5);
    pull.style.setProperty('--pull', dist + 'px');
    var ready = dist >= LIMIT * 0.8;
    pull.classList.toggle('ready', ready);
    byId('pull-text').textContent = ready ? 'Release to refresh' : 'Pull to refresh';
  }, { passive: true });
  function end() {
    var go = startY !== null && dist >= LIMIT * 0.8;
    reset();
    if (go) { toast('Refreshing...'); loadView(); }
  }
  document.addEventListener('touchend', end);
  document.addEventListener('touchcancel', reset);
})();

// ---- this device: install and offline state ----
function showDeviceFacts() {
  var standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  var facts = '<dt>Opened as</dt><dd>' + (standalone ? 'An installed app' : 'A browser tab. Use Add to Home Screen to install it.') + '</dd>';
  facts += '<dt>Works offline</dt><dd id="sw-state">' + ('serviceWorker' in navigator ? 'Checking' : 'Not supported here') + '</dd>';
  facts += '<dt>Kept for offline</dt><dd>The page and the last 24-hour counts. Senders, subjects, and message text are never kept.</dd>';
  byId('device-facts').innerHTML = facts;
}
showDeviceFacts();
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').then(function (reg) {
    var el = byId('sw-state');
    if (el) el.textContent = reg.active || reg.waiting ? 'Ready' : 'Setting up';
    reg.addEventListener('updatefound', function () {
      var el2 = byId('sw-state');
      if (el2) el2.textContent = 'Updating';
    });
  }, function () {
    var el = byId('sw-state');
    if (el) el.textContent = 'Not available';
  });
}
window.addEventListener('online', function () { loadView(); });
window.addEventListener('offline', function () { setBanner('offline'); });

readHash();
showView();
loadView();
loadFilteringPolicy();

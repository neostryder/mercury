// The address ForwardEmail's webhook actually calls. Runs on Cloudflare's
// edge rather than any self-hosted machine so that a self-hosted outage
// cannot surface as a bounce of legitimate mail - see docs/ARCHITECTURE.md.
//
// Also relays any other POST path (e.g. /rules/propose, used by the
// Thunderbird extension) straight to the backend and returns its real
// response - that call is a direct, synchronous user action, not something
// arriving under SMTP bounce-risk, so there is nothing to protect it from.
import { DASHBOARD_HTML, lastNDays, renderStackedBarSVG, CHART_COLORS } from './dashboard.js';

const INGEST_TIMEOUT_MS = 20000;
const KNOWN_DISPOSITIONS = [250, 421, 550];

// Per-table column allowlists for the /log endpoint - hardcoded rather than
// accepting arbitrary column names from the request, since those get
// interpolated into the SQL text (D1's bind params cover values, not
// identifiers). Table names below are equally fixed, not looked up from
// input.
const LOG_TABLES = {
  messages: [
    'received_at', 'from_display', 'from_domain', 'subject', 'injection_label',
    'injection_score', 'verdict', 'disposition', 'enforced_disposition',
    'category', 'alert_level', 'reasoning', 'shadow_mode', 'full_content', 'analysis',
    'triggered_rule',
  ],
  rule_changes: ['changed_at', 'action', 'rule_text', 'source'],
  actions: ['executed_at', 'kind', 'details', 'outcome_summary', 'result', 'domain'],
  action_items: ['created_at', 'kind', 'summary', 'related_message_id', 'completed_at'],
  admin_log: ['at', 'event', 'detail'],
};

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.protocol === 'http:') {
      url.protocol = 'https:';
      return Response.redirect(url.toString(), 301);
    }
    const { pathname, search } = url;

    if (pathname === '/dashboard' || pathname.startsWith('/dashboard/')) {
      const unauthorized = checkDashboardAuth(request, env);
      if (unauthorized) return unauthorized;
      return handleDashboard(pathname, search, env, request);
    }

    if (request.method !== 'POST') {
      return new Response('OK', { status: 200 });
    }

    if (pathname === '/log') {
      return handleLog(request, env);
    }

    const bodyText = await request.text();

    // The ForwardEmail aliases were actually configured against /webhook and
    // /webhook-test (never /ingest) - a naming mismatch that meant every
    // real incoming message 404'd against this Worker/backend from the
    // start, discovered by checking the backend's own request log rather
    // than assuming the configured URL matched. Treated identically to
    // /ingest here (backendUrl is normalized to the real route) rather than
    // requiring a ForwardEmail-side change to fix.
    if (pathname === '/ingest' || pathname === '/webhook' || pathname === '/webhook-test') {
      return proxyIngest(`${env.BACKEND_BASE_URL}/ingest`, bodyText, env);
    }

    const backendUrl = `${env.BACKEND_BASE_URL}${pathname}`;

    return proxySynchronously(backendUrl, bodyText, env);
  },
};

// Low-friction single-user gate: standard HTTP Basic Auth, which the browser
// prompts for once and then remembers for the rest of the session - no
// login page, no cookies/sessions to manage. Restricted to whoever holds
// DASHBOARD_PASSWORD (a Worker secret), not tied to a specific email
// address the way Cloudflare Access would be; that's a stronger option to
// layer on later if wanted, but requires Zero Trust account configuration
// beyond what this Worker can set up on its own.
function checkDashboardAuth(request, env) {
  const auth = request.headers.get('Authorization') || '';
  if (auth.startsWith('Basic ')) {
    try {
      const [, password] = atob(auth.slice(6)).split(':');
      if (password === env.DASHBOARD_PASSWORD) return null;
    } catch (err) {
      // fall through to challenge
    }
  }
  return new Response('Authentication required', {
    status: 401,
    headers: { 'WWW-Authenticate': 'Basic realm="Mercury Dashboard"' },
  });
}

const HSTS = 'max-age=31536000; includeSubDomains';

async function handleDashboard(pathname, search, env, request) {
  if (pathname === '/dashboard' || pathname === '/dashboard/') {
    return new Response(DASHBOARD_HTML, {
      headers: { 'Content-Type': 'text/html; charset=utf-8', 'Strict-Transport-Security': HSTS },
    });
  }

  const params = new URLSearchParams(search);
  const json = (data) => new Response(JSON.stringify(data), {
    headers: { 'Content-Type': 'application/json', 'Strict-Transport-Security': HSTS },
  });

  const hardBounceDetailMatch = pathname.match(/^\/dashboard\/api\/hard-bounces\/(\d+)$/);
  const actionItemCompleteMatch = pathname.match(/^\/dashboard\/api\/action-items\/(\d+)\/complete$/);

  try {
    if (pathname === '/dashboard/api/trends') {
      const db = env.MERCURY_LOG;
      const [volumeRows, categoryRows] = await Promise.all([
        db.prepare(`
          SELECT date(received_at) AS day,
            SUM(CASE WHEN enforced_disposition = '250' THEN 1 ELSE 0 END) AS accepted,
            SUM(CASE WHEN enforced_disposition = '421' THEN 1 ELSE 0 END) AS deferred,
            SUM(CASE WHEN enforced_disposition = '550' THEN 1 ELSE 0 END) AS bounced
          FROM messages
          WHERE received_at >= datetime('now', '-30 day')
          GROUP BY day
        `).all(),
        db.prepare(`
          SELECT date(received_at) AS day, category, COUNT(*) AS count
          FROM messages
          WHERE received_at >= datetime('now', '-30 day')
          GROUP BY day, category
        `).all(),
      ]);

      const days = lastNDays(30);

      const byDayVolume = {};
      for (const r of volumeRows.results ?? []) {
        byDayVolume[r.day] = { accepted: r.accepted, deferred: r.deferred, bounced: r.bounced };
      }
      const volumeSvg = renderStackedBarSVG(days, [
        { key: 'accepted', label: 'Accepted', color: CHART_COLORS.good },
        { key: 'deferred', label: 'Soft-deferred', color: CHART_COLORS.warn },
        { key: 'bounced', label: 'Hard-bounced', color: CHART_COLORS.bad },
      ], byDayVolume);

      const totalsByCategory = {};
      for (const r of categoryRows.results ?? []) {
        const cat = r.category || 'UNKNOWN';
        totalsByCategory[cat] = (totalsByCategory[cat] || 0) + r.count;
      }
      const topCategories = Object.entries(totalsByCategory)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 6)
        .map(([c]) => c);
      const categorySeries = topCategories.map((c, i) => ({ key: c, label: c, color: CHART_COLORS.extra[i % CHART_COLORS.extra.length] }));
      categorySeries.push({ key: '__other', label: 'Other', color: CHART_COLORS.muted });

      const byDayCategory = {};
      for (const r of categoryRows.results ?? []) {
        const cat = r.category || 'UNKNOWN';
        const key = topCategories.includes(cat) ? cat : '__other';
        byDayCategory[r.day] = byDayCategory[r.day] || {};
        byDayCategory[r.day][key] = (byDayCategory[r.day][key] || 0) + r.count;
      }
      const categorySvg = renderStackedBarSVG(days, categorySeries, byDayCategory);

      return json({ volumeSvg, categorySvg });
    }

    if (pathname === '/dashboard/api/hard-bounces') {
      const result = await env.MERCURY_LOG.prepare(
        "SELECT id, received_at, from_display, from_domain, subject, category, verdict, triggered_rule FROM messages WHERE enforced_disposition = '550' ORDER BY id DESC LIMIT 200"
      ).all();
      return json(result.results ?? []);
    }

    if (hardBounceDetailMatch) {
      const row = await env.MERCURY_LOG.prepare(
        "SELECT * FROM messages WHERE id = ? AND enforced_disposition = '550'"
      ).bind(Number(hardBounceDetailMatch[1])).first();
      if (!row) return new Response('not found', { status: 404 });
      return json(row);
    }

    if (pathname === '/dashboard/api/rules/reverse') {
      if (request.method !== 'POST') return new Response('method not allowed', { status: 405 });
      let body;
      try {
        body = await request.json();
      } catch (err) {
        return new Response('bad json', { status: 400 });
      }
      const rule = body?.rule;
      if (!rule || typeof rule !== 'string') {
        return json({ ok: false, error: 'missing rule' });
      }
      return reverseRule(rule, env);
    }

    if (pathname === '/dashboard/api/action-items') {
      const result = await env.MERCURY_LOG.prepare(
        'SELECT * FROM action_items WHERE completed_at IS NULL ORDER BY id DESC LIMIT 100'
      ).all();
      return json(result.results ?? []);
    }

    if (actionItemCompleteMatch) {
      if (request.method !== 'POST') return new Response('method not allowed', { status: 405 });
      const id = Number(actionItemCompleteMatch[1]);
      const now = new Date().toISOString();
      const result = await env.MERCURY_LOG.prepare(
        'UPDATE action_items SET completed_at = ? WHERE id = ? AND completed_at IS NULL'
      ).bind(now, id).run();
      const completed = (result.meta?.changes ?? 0) > 0;
      if (completed) {
        await env.MERCURY_LOG.prepare(
          'INSERT INTO admin_log (at, event, detail) VALUES (?, ?, ?)'
        ).bind(now, 'action_item_completed', `id=${id}`).run();
      }
      return json({ ok: true, completed });
    }

    if (pathname === '/dashboard/api/summary') {
      const db = env.MERCURY_LOG;
      const [last24h, hardBounces24h, urgent24h, actions24h, ruleChanges7d, ruleCountRow, categories7d] = await Promise.all([
        db.prepare("SELECT COUNT(*) AS n FROM messages WHERE received_at >= datetime('now', '-1 day')").first(),
        db.prepare("SELECT COUNT(*) AS n FROM messages WHERE received_at >= datetime('now', '-1 day') AND enforced_disposition = '550'").first(),
        db.prepare("SELECT COUNT(*) AS n FROM messages WHERE received_at >= datetime('now', '-1 day') AND alert_level = 'URGENT'").first(),
        db.prepare("SELECT COUNT(*) AS n FROM actions WHERE executed_at >= datetime('now', '-1 day')").first(),
        db.prepare("SELECT COUNT(*) AS n FROM rule_changes WHERE changed_at >= datetime('now', '-7 day')").first(),
        db.prepare('SELECT COUNT(*) AS n FROM (SELECT rule_text FROM rule_changes GROUP BY rule_text HAVING SUM(CASE WHEN action = \'added\' THEN 1 ELSE -1 END) > 0)').first(),
        db.prepare("SELECT category, COUNT(*) AS count FROM messages WHERE received_at >= datetime('now', '-7 day') GROUP BY category ORDER BY count DESC").all(),
      ]);
      return json({
        last24h: {
          total: last24h?.n ?? 0,
          hardBounces: hardBounces24h?.n ?? 0,
          urgent: urgent24h?.n ?? 0,
          actions: actions24h?.n ?? 0,
        },
        last7d: { ruleChanges: ruleChanges7d?.n ?? 0 },
        ruleCount: ruleCountRow?.n ?? 0,
        categories: categories7d?.results ?? [],
      });
    }

    if (pathname === '/dashboard/api/messages') {
      const disposition = params.get('disposition');
      const stmt = disposition
        ? env.MERCURY_LOG.prepare('SELECT * FROM messages WHERE enforced_disposition = ? ORDER BY id DESC LIMIT 100').bind(disposition)
        : env.MERCURY_LOG.prepare('SELECT * FROM messages ORDER BY id DESC LIMIT 100');
      const result = await stmt.all();
      return json(result.results ?? []);
    }

    if (pathname === '/dashboard/api/rules') {
      const result = await env.MERCURY_LOG.prepare('SELECT * FROM rule_changes ORDER BY id DESC LIMIT 50').all();
      return json(result.results ?? []);
    }

    if (pathname === '/dashboard/api/actions') {
      const result = await env.MERCURY_LOG.prepare('SELECT * FROM actions ORDER BY id DESC LIMIT 50').all();
      return json(result.results ?? []);
    }
  } catch (err) {
    return json({ ok: false, error: String(err) });
  }

  return new Response('not found', { status: 404 });
}

// Logging endpoint for the backend's event log (see backend/event_log.py) -
// the backend has no Cloudflare credentials of its own, so it reaches D1
// through this authenticated route on the Worker, which already holds the
// binding. Best-effort from the caller's side; this endpoint itself still
// validates and reports real errors rather than silently swallowing them,
// since a logging gap should be visible in the Worker's own logs even if
// the backend doesn't wait around for the result.
async function handleLog(request, env) {
  if (request.headers.get('X-Mercury-Secret') !== env.MERCURY_SHARED_SECRET) {
    return new Response('forbidden', { status: 403 });
  }

  let payload;
  try {
    payload = await request.json();
  } catch (err) {
    return new Response('bad json', { status: 400 });
  }

  const { table, fields } = payload || {};
  const columns = LOG_TABLES[table];
  if (!columns || typeof fields !== 'object' || fields === null) {
    return new Response('unknown table or bad fields', { status: 400 });
  }

  const present = columns.filter((c) => Object.prototype.hasOwnProperty.call(fields, c));
  if (present.length === 0) {
    return new Response('no recognized fields', { status: 400 });
  }

  const placeholders = present.map(() => '?').join(', ');
  const sql = `INSERT INTO ${table} (${present.join(', ')}) VALUES (${placeholders})`;
  const values = present.map((c) => fields[c] ?? null);

  try {
    const result = await env.MERCURY_LOG.prepare(sql).bind(...values).run();
    return new Response(JSON.stringify({ ok: true, id: result.meta.last_row_id }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (err) {
    return new Response(JSON.stringify({ ok: false, error: String(err) }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

// Enforcement now depends on this call completing, but a self-hosted outage
// or a slow backend must still never itself cause a bounce of legitimate
// mail - see docs/ARCHITECTURE.md. Anything short of a clean, recognized
// disposition from the backend fails open (accept) rather than risking a
// false bounce.
async function proxyIngest(backendUrl, bodyText, env) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), INGEST_TIMEOUT_MS);
  try {
    const resp = await fetch(backendUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Mercury-Secret': env.MERCURY_SHARED_SECRET,
      },
      body: bodyText,
      signal: controller.signal,
    });
    const text = await resp.text();
    if (KNOWN_DISPOSITIONS.includes(resp.status)) {
      return new Response(text, { status: resp.status, headers: { 'Content-Type': 'application/json' } });
    }
    return new Response(text || 'OK', { status: 250 });
  } catch (err) {
    // Backend unreachable, slow, or errored - accept rather than guess.
    return new Response('OK', { status: 250 });
  } finally {
    clearTimeout(timeout);
  }
}

// Called from the dashboard's hard-bounce detail view. The rules ledger
// lives only on the backend's filesystem (see backend/app.py), not in D1, so
// reversing a rule means a real Worker-to-backend call - authenticated the
// same way the backend's own calls into this Worker's /log route are
// (X-Mercury-Secret against the shared secret both sides hold).
async function reverseRule(rule, env) {
  let resp;
  try {
    resp = await fetch(`${env.BACKEND_BASE_URL}/rules/reverse`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Mercury-Secret': env.MERCURY_SHARED_SECRET },
      body: JSON.stringify({ rule }),
    });
  } catch (err) {
    return new Response(JSON.stringify({ ok: false, error: 'backend unreachable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const text = await resp.text();
  if (resp.ok) {
    await env.MERCURY_LOG.prepare(
      'INSERT INTO admin_log (at, event, detail) VALUES (?, ?, ?)'
    ).bind(new Date().toISOString(), 'rule_reversed', rule).run();
  }
  return new Response(text, { status: resp.status, headers: { 'Content-Type': 'application/json' } });
}

async function proxySynchronously(backendUrl, bodyText, env) {
  try {
    const resp = await fetch(backendUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Mercury-Secret': env.MERCURY_SHARED_SECRET,
      },
      body: bodyText,
    });
    return new Response(await resp.text(), {
      status: resp.status,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (err) {
    return new Response(JSON.stringify({ ok: false, error: 'backend unreachable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json' },
    });
  }
}

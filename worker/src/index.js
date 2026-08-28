// The address ForwardEmail's webhook actually calls. Runs on Cloudflare's
// edge rather than any self-hosted machine so that a self-hosted outage
// cannot surface as a bounce of legitimate mail - see docs/ARCHITECTURE.md.
//
// Also relays any other POST path (e.g. /rules/propose, used by the
// Thunderbird extension) straight to the backend and returns its real
// response - that call is a direct, synchronous user action, not something
// arriving under SMTP bounce-risk, so there is nothing to protect it from.
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
  ],
  rule_changes: ['changed_at', 'action', 'rule_text', 'source'],
  actions: ['executed_at', 'kind', 'details', 'outcome_summary', 'result', 'domain'],
  action_items: ['created_at', 'kind', 'summary', 'related_message_id', 'completed_at'],
  admin_log: ['at', 'event', 'detail'],
};

export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('OK', { status: 200 });
    }

    const { pathname } = new URL(request.url);

    if (pathname === '/log') {
      return handleLog(request, env);
    }

    const bodyText = await request.text();
    const backendUrl = `${env.BACKEND_BASE_URL}${pathname}`;

    if (pathname === '/ingest') {
      return proxyIngest(backendUrl, bodyText, env);
    }

    return proxySynchronously(backendUrl, bodyText, env);
  },
};

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

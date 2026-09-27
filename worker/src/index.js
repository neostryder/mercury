// The address ForwardEmail's webhook actually calls. Runs on Cloudflare's
// edge rather than any self-hosted machine so that a self-hosted outage
// cannot surface as a bounce of legitimate mail - see docs/ARCHITECTURE.md.
//
// Also relays any other POST path (e.g. /rules/propose, used by the
// Thunderbird extension) straight to the backend and returns its real
// response - that call is a direct, synchronous user action, not something
// arriving under SMTP bounce-risk, so there is nothing to protect it from.
import { DASHBOARD_HTML, lastNDays, renderStackedBarSVG, categoryClass } from './dashboard.js';

const INGEST_TIMEOUT_MS = 20000;
const MAX_CREDENTIAL_BODY_BYTES = 4096;
const KNOWN_DISPOSITIONS = [250, 421, 550];
const PAGE_SIZES = [20, 50, 100];
const DEFAULT_PAGE_SIZE = 50;
const DEFAULT_RETENTION_DAYS = 365;
// Everything a message row carries except the saved body and analysis of a
// hard bounce, which only the bounce detail view reads.
const MESSAGE_LIST_COLUMNS = 'id, received_at, from_display, from_domain, subject, injection_label, injection_score, verdict, disposition, enforced_disposition, category, alert_level, reasoning, shadow_mode, triggered_rule, recipient_class, recipient_detail';

// Shared limit/offset parsing for the paginated dashboard table endpoints.
// limit is restricted to PAGE_SIZES rather than accepting any number, since
// it is interpolated into the SQL text below (D1's bind params cover values,
// not the LIMIT/OFFSET clause shape) - an allowlist keeps that safe without
// needing a bind param for it.
function pageParams(params) {
  const requestedLimit = Number(params.get('limit'));
  const limit = PAGE_SIZES.includes(requestedLimit) ? requestedLimit : DEFAULT_PAGE_SIZE;
  const requestedOffset = Number(params.get('offset'));
  const offset = Number.isInteger(requestedOffset) && requestedOffset >= 0 ? requestedOffset : 0;
  return { limit, offset };
}

// Fetches one extra row beyond `limit` to learn whether a next page exists,
// rather than a separate COUNT(*) query - half the D1 reads for the same
// answer, at the cost of discarding one row client-side.
function paginate(rows, limit) {
  const hasMore = rows.length > limit;
  return { rows: hasMore ? rows.slice(0, limit) : rows, hasMore };
}

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
    'triggered_rule', 'recipient_class', 'recipient_detail',
  ],
  // One row per message both judges answered. `fields` lists what they
  // disagreed on and is "[]" when they agreed.
  judge_comparisons: [
    'compared_at', 'authoritative', 'fields', 'detail', 'structured_confidence',
    'structured_severity', 'structured_why', 'latency_ms', 'model',
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
      // A Cloudflare Access application covers this hostname and path. The
      // Worker also checks the token Access attaches, so a route that
      // bypasses Access (a workers.dev URL, a new custom domain) still
      // cannot reach the dashboard.
      const denied = await checkAccessToken(request, env);
      if (denied) return denied;
      return handleDashboard(pathname, search, env, request);
    }

    const credentialMatch = pathname.match(/^\/credential\/([A-Za-z0-9_-]+)$/);
    if (credentialMatch) {
      if (request.method !== 'GET' && request.method !== 'POST') {
        return new Response('method not allowed', {
          status: 405,
          headers: { 'Strict-Transport-Security': HSTS },
        });
      }
      return handleCredentialPrompt(request, credentialMatch[1], env);
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
    const ingestMatch = pathname.match(/^\/(?:ingest|webhook|webhook-test)(?:\/([A-Za-z0-9_-]+))?$/);
    if (ingestMatch) {
      // The backend trusts whatever this route forwards, since the Worker
      // attaches the backend secret itself - including the payload's own
      // `dmarc` result, which gates deterministic sender-list matches. So the
      // caller has to prove it is the mail host: the webhook URL configured
      // at ForwardEmail ends in MERCURY_WEBHOOK_TOKEN, and nothing else gets
      // through. WEBHOOK_ALLOW_TOKENLESS keeps the bare paths open only while
      // the mail host's configured URL is being switched over. A refused call
      // gets 421 rather than a 4xx, because ForwardEmail turns a 4xx webhook
      // reply into an SMTP error: a mistyped token defers real mail instead of
      // bouncing it.
      const token = ingestMatch[1];
      const tokenOk = token ? await tokenMatches(token, env.MERCURY_WEBHOOK_TOKEN) : env.WEBHOOK_ALLOW_TOKENLESS === 'true';
      if (!tokenOk) {
        return new Response('unavailable', { status: 421 });
      }
      return proxyIngest(`${env.BACKEND_BASE_URL}/ingest`, bodyText, env);
    }

    // Unlike /rules/propose (a proposal still needs a human Approve tap
    // before anything commits), a relayed callback_query can directly commit
    // a filtering change with no further human step - the catch-all forward
    // below stamps its OWN backend-facing secret onto every request
    // regardless of what the real caller sent, so without this check any
    // anonymous POST to this path would reach the backend fully
    // "authenticated". Gated on a secret distinct from MERCURY_SHARED_SECRET
    // so only the process actually forwarding a real Telegram update (the
    // gateway holding this bot's getUpdates connection) can reach it.
    if (pathname === '/telegram/relay') {
      if (request.headers.get('X-Mercury-Relay-Secret') !== env.MERCURY_RELAY_SECRET) {
        return new Response('forbidden', { status: 403 });
      }
      return proxySynchronously(`${env.BACKEND_BASE_URL}${pathname}`, bodyText, env);
    }

    // The generic catch-all (currently just /rules/propose, from the
    // Thunderbird extension). The call below supplies the Worker's own backend
    // secret, so the caller's X-Mercury-Secret is checked here first. The
    // extension holds MERCURY_EXTENSION_SECRET, which is good for
    // /rules/propose and nothing else, so a copy lifted from a mail profile
    // cannot write to the event log or reach any other backend route.
    const callerSecret = request.headers.get('X-Mercury-Secret');
    const extensionCall = pathname === '/rules/propose'
      && env.MERCURY_EXTENSION_SECRET
      && await tokenMatches(callerSecret || '', env.MERCURY_EXTENSION_SECRET);
    if (!extensionCall && callerSecret !== env.MERCURY_SHARED_SECRET) {
      return new Response('forbidden', { status: 403 });
    }

    const backendUrl = `${env.BACKEND_BASE_URL}${pathname}`;

    return proxySynchronously(backendUrl, bodyText, env);
  },

  // Daily retention sweep (see [[triggers]].crons in wrangler.toml). Deletes
  // rows past LOG_RETENTION_DAYS from every time-stamped log table. An open
  // action_items row is never purged by age alone - it is still a pending
  // to-do regardless of how old it is, so only completed ones fall under
  // retention here.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(purgeExpiredLogs(env));
  },
};

// Every timestamp is stored as ISO 8601 text with a T separator, so time
// windows here and in the dashboard queries compare against strftime() output
// in that same format. datetime() writes a space instead, which sorts before
// T and pulls every row from the cutoff date into the window.
async function purgeExpiredLogs(env) {
  const days = Number(env.LOG_RETENTION_DAYS) || DEFAULT_RETENTION_DAYS;
  const cutoffModifier = `-${days} day`;
  const db = env.MERCURY_LOG;
  const results = await db.batch([
    db.prepare("DELETE FROM messages WHERE received_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)").bind(cutoffModifier),
    db.prepare("DELETE FROM judge_comparisons WHERE compared_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)").bind(cutoffModifier),
    db.prepare("DELETE FROM rule_changes WHERE changed_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)").bind(cutoffModifier),
    db.prepare("DELETE FROM actions WHERE executed_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)").bind(cutoffModifier),
    db.prepare("DELETE FROM admin_log WHERE at < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)").bind(cutoffModifier),
    db.prepare("DELETE FROM review_labels WHERE labeled_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)").bind(cutoffModifier),
    db.prepare("DELETE FROM action_items WHERE completed_at IS NOT NULL AND completed_at < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)").bind(cutoffModifier),
  ]);
  const totalDeleted = results.reduce((sum, r) => sum + (r.meta?.changes ?? 0), 0);
  await db.prepare(
    'INSERT INTO admin_log (at, event, detail) VALUES (?, ?, ?)'
  ).bind(new Date().toISOString(), 'log_retention_sweep', `days=${days} deleted=${totalDeleted}`).run();
}

// Cloudflare Access signs a JWT for every request it lets through, a service
// token's included, and sends it as Cf-Access-Jwt-Assertion. It is checked
// against the team's published keys and this application's audience tag.
let accessKeys = { fetchedAt: 0, byKid: new Map() };
const ACCESS_KEYS_MAX_AGE_MS = 60 * 60 * 1000;

function base64UrlBytes(text) {
  const b64 = text.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(text.length / 4) * 4, '=');
  return Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
}

function base64UrlJson(text) {
  return JSON.parse(new TextDecoder().decode(base64UrlBytes(text)));
}

async function loadAccessKeys(teamDomain) {
  const res = await fetch(`https://${teamDomain}/cdn-cgi/access/certs`);
  if (!res.ok) throw new Error(`certs fetch returned ${res.status}`);
  const { keys } = await res.json();
  const byKid = new Map();
  for (const jwk of keys ?? []) {
    if (jwk.kty !== 'RSA' || !jwk.kid) continue;
    byKid.set(jwk.kid, await crypto.subtle.importKey(
      'jwk', jwk, { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify'],
    ));
  }
  accessKeys = { fetchedAt: Date.now(), byKid };
}

async function accessKey(teamDomain, kid) {
  if (!accessKeys.byKid.has(kid) || Date.now() - accessKeys.fetchedAt > ACCESS_KEYS_MAX_AGE_MS) {
    await loadAccessKeys(teamDomain);
  }
  return accessKeys.byKid.get(kid);
}

// Returns null when the token is valid, or the response to send instead.
async function checkAccessToken(request, env) {
  const deny = (reason) => new Response(`forbidden: ${reason}`, {
    status: 403,
    headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' },
  });
  const teamDomain = env.ACCESS_TEAM_DOMAIN;
  const audience = env.ACCESS_AUD;
  if (!teamDomain || !audience) return deny('the Access check is not configured');

  const token = request.headers.get('Cf-Access-Jwt-Assertion');
  if (!token) return deny('no Access token');
  const parts = token.split('.');
  if (parts.length !== 3) return deny('malformed Access token');

  let header;
  let claims;
  try {
    header = base64UrlJson(parts[0]);
    claims = base64UrlJson(parts[1]);
  } catch (err) {
    return deny('malformed Access token');
  }
  if (header.alg !== 'RS256' || !header.kid) return deny('unexpected token algorithm');

  let key;
  try {
    key = await accessKey(teamDomain, header.kid);
  } catch (err) {
    return new Response('Access keys unavailable', { status: 503, headers: { 'Cache-Control': 'no-store' } });
  }
  if (!key) return deny('unknown signing key');

  const signed = new TextEncoder().encode(`${parts[0]}.${parts[1]}`);
  const valid = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, base64UrlBytes(parts[2]), signed);
  if (!valid) return deny('bad signature');

  const now = Math.floor(Date.now() / 1000);
  const auds = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!auds.includes(audience)) return deny('wrong audience');
  if (claims.iss !== `https://${teamDomain}`) return deny('wrong issuer');
  if (typeof claims.exp !== 'number' || claims.exp < now - 30) return deny('expired');
  if (typeof claims.nbf === 'number' && claims.nbf > now + 30) return deny('not yet valid');
  return null;
}

const HSTS = 'max-age=31536000; includeSubDomains';

const DASHBOARD_PAGE_HEADERS = {
  'Content-Type': 'text/html; charset=utf-8',
  'Cache-Control': 'no-store',
  'Content-Security-Policy': "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
  'Referrer-Policy': 'no-referrer',
  'Strict-Transport-Security': HSTS,
  'X-Content-Type-Options': 'nosniff',
};

const DASHBOARD_JSON_HEADERS = {
  'Content-Type': 'application/json',
  'Cache-Control': 'no-store',
  'Strict-Transport-Security': HSTS,
  'X-Content-Type-Options': 'nosniff',
};

const CREDENTIAL_PAGE_HEADERS = {
  'Content-Type': 'text/html; charset=utf-8',
  'Cache-Control': 'no-store',
  'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
  'Referrer-Policy': 'no-referrer',
  'Strict-Transport-Security': HSTS,
  'X-Content-Type-Options': 'nosniff',
};

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function credentialPage(title, content, status = 200) {
  return new Response(`<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(title)}</title>
  <style>
    body { font: 16px/1.5 system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem; color: #172033; }
    label { display: block; margin-top: 1rem; }
    input { box-sizing: border-box; display: block; width: 100%; padding: .7rem; margin-top: .3rem; }
    button { margin-top: 1.25rem; padding: .7rem 1rem; }
  </style>
</head>
<body>
  <main>
    <h1>${escapeHtml(title)}</h1>
    ${content}
  </main>
</body>
</html>`, { status, headers: CREDENTIAL_PAGE_HEADERS });
}

async function readTextWithLimit(message, maxBytes) {
  const declared = message.headers.get('Content-Length');
  if (declared !== null) {
    const declaredLength = Number(declared);
    if (!Number.isFinite(declaredLength) || declaredLength < 0 || declaredLength > maxBytes) {
      return null;
    }
  }

  if (!message.body) return '';
  const reader = message.body.getReader();
  const decoder = new TextDecoder();
  let size = 0;
  let text = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > maxBytes) {
      await reader.cancel();
      return null;
    }
    text += decoder.decode(value, { stream: true });
  }
  return text + decoder.decode();
}

async function readBackendJson(response) {
  const text = await readTextWithLimit(response, MAX_CREDENTIAL_BODY_BYTES);
  if (text === null) return null;
  try {
    return JSON.parse(text);
  } catch (err) {
    return null;
  }
}

async function handleCredentialPrompt(request, token, env) {
  const backendUrl = `${env.BACKEND_BASE_URL}/credential-prompt/${encodeURIComponent(token)}`;

  if (request.method === 'GET') {
    let response;
    let result;
    try {
      response = await fetch(backendUrl, { method: 'GET' });
      result = await readBackendJson(response);
    } catch (err) {
      return credentialPage(
        'Sign-in link unavailable',
        '<p>This link could not be checked right now. Reply in the Telegram conversation to try again.</p>',
        502,
      );
    }

    if (!response.ok || !result?.valid) {
      return credentialPage(
        'Sign-in link unavailable',
        '<p>This link has expired or was already used - reply in the Telegram conversation to ask for a new one.</p>',
      );
    }

    const domain = escapeHtml(result.domain);
    const seconds = Number(result.expires_in_seconds);
    const minutes = Math.max(1, Math.ceil((Number.isFinite(seconds) ? seconds : 0) / 60));
    const minuteLabel = minutes === 1 ? 'minute' : 'minutes';
    return credentialPage('Finish unsubscribe sign-in', `
      <p>Enter the account credential for <strong>${domain}</strong>. It will be used only for this unsubscribe attempt.</p>
      <p>This link remains valid for about ${minutes} ${minuteLabel}.</p>
      <form method="post" action="/credential/${escapeHtml(token)}" autocomplete="off">
        <label>Username
          <input name="username" type="text" required autocomplete="off">
        </label>
        <label>Password
          <input name="password" type="password" required autocomplete="off">
        </label>
        <button type="submit">Submit</button>
      </form>`);
  }

  let formText = await readTextWithLimit(request, MAX_CREDENTIAL_BODY_BYTES);
  if (formText === null) {
    return credentialPage('Submission failed', '<p>The submitted form was too large.</p>', 413);
  }
  let form = new URLSearchParams(formText);
  let username = form.get('username');
  let password = form.get('password');
  if (username === null || password === null) {
    return credentialPage('Submission failed', '<p>The username and password fields are required.</p>', 400);
  }

  let backendBody = JSON.stringify({ username, password });
  if (new TextEncoder().encode(backendBody).byteLength > MAX_CREDENTIAL_BODY_BYTES) {
    return credentialPage('Submission failed', '<p>The submitted form was too large.</p>', 413);
  }

  let response;
  let result;
  try {
    response = await fetch(backendUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: backendBody,
    });
    result = await readBackendJson(response);
  } catch (err) {
    return credentialPage(
      'Submission failed',
      '<p>The backend could not be reached. Reply in the Telegram conversation to try again.</p>',
      502,
    );
  } finally {
    formText = null;
    form = null;
    username = null;
    password = null;
    backendBody = null;
  }

  if (!response.ok || !result?.ok) {
    return credentialPage(
      'Submission failed',
      '<p>This link has expired or was already used - reply in the Telegram conversation to ask for a new one.</p>',
      400,
    );
  }
  return credentialPage(
    'Submitted',
    '<p>Submitted - check Telegram for what happened next.</p>',
  );
}

async function handleDashboard(pathname, search, env, request) {
  if (pathname === '/dashboard' || pathname === '/dashboard/') {
    return new Response(DASHBOARD_HTML, { headers: DASHBOARD_PAGE_HEADERS });
  }

  // A state-changing call must come from the dashboard page itself. The
  // browser always sends Origin on a POST, and a cross-site form or fetch
  // carries its own site there.
  if (request.method === 'POST' && request.headers.get('Origin') !== new URL(request.url).origin) {
    return new Response('forbidden', { status: 403, headers: DASHBOARD_JSON_HEADERS });
  }

  const params = new URLSearchParams(search);
  const json = (data, status = 200) => new Response(JSON.stringify(data), {
    status,
    headers: DASHBOARD_JSON_HEADERS,
  });

  const messageDetailMatch = pathname.match(/^\/dashboard\/api\/messages\/(\d+)$/);
  const actionItemCompleteMatch = pathname.match(/^\/dashboard\/api\/action-items\/(\d+)\/complete$/);
  const readBody = async () => {
    try {
      const text = await request.text();
      if (text.length > 16384) return null;
      return JSON.parse(text);
    } catch (err) {
      return null;
    }
  };

  try {
    if (pathname === '/dashboard/api/filtering') {
      if (request.method !== 'GET' && request.method !== 'POST') {
        return new Response('method not allowed', { status: 405 });
      }
      return proxyFilteringPolicy(request, env);
    }

    if (pathname === '/dashboard/api/trends') {
      const db = env.MERCURY_LOG;
      const [volumeRows, categoryRows] = await Promise.all([
        db.prepare(`
          SELECT date(received_at, '-7 hours') AS day,
            SUM(CASE WHEN enforced_disposition = '250' THEN 1 ELSE 0 END) AS accepted,
            SUM(CASE WHEN enforced_disposition = '421' THEN 1 ELSE 0 END) AS deferred,
            SUM(CASE WHEN enforced_disposition = '550' THEN 1 ELSE 0 END) AS bounced
          FROM messages
          WHERE received_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 day')
          GROUP BY day
        `).all(),
        db.prepare(`
          SELECT date(received_at, '-7 hours') AS day, category, COUNT(*) AS count
          FROM messages
          WHERE received_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-30 day')
          GROUP BY day, category
        `).all(),
      ]);

      const days = lastNDays(30);

      const byDayVolume = {};
      for (const r of volumeRows.results ?? []) {
        byDayVolume[r.day] = { accepted: r.accepted, deferred: r.deferred, bounced: r.bounced };
      }
      const volumeSeries = [
        { key: 'accepted', label: 'Accepted', cls: 's-250' },
        { key: 'deferred', label: 'Deferred', cls: 's-421' },
        { key: 'bounced', label: 'Rejected', cls: 's-550' },
      ];
      const volumeSvg = renderStackedBarSVG(days, volumeSeries, byDayVolume, { label: 'Messages per day by outcome, last 30 days' });
      const seriesTotal = (key, byDay) => days.reduce((sum, d) => sum + ((byDay[d] || {})[key] || 0), 0);
      const volumeLegend = volumeSeries.map((s) => ({ label: s.label, cls: s.cls, total: seriesTotal(s.key, byDayVolume) }));

      const totalsByCategory = {};
      for (const r of categoryRows.results ?? []) {
        const cat = r.category || 'UNKNOWN';
        totalsByCategory[cat] = (totalsByCategory[cat] || 0) + r.count;
      }
      const topCategories = Object.entries(totalsByCategory)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 6)
        .map(([c]) => c);
      const categorySeries = topCategories.map((c) => ({ key: c, label: c, cls: categoryClass(c) }));
      categorySeries.push({ key: '__other', label: 'Everything else', cls: 'cat-other' });

      const byDayCategory = {};
      for (const r of categoryRows.results ?? []) {
        const cat = r.category || 'UNKNOWN';
        const key = topCategories.includes(cat) ? cat : '__other';
        byDayCategory[r.day] = byDayCategory[r.day] || {};
        byDayCategory[r.day][key] = (byDayCategory[r.day][key] || 0) + r.count;
      }
      const categorySvg = renderStackedBarSVG(days, categorySeries, byDayCategory, { label: 'Messages per day by category, last 30 days' });
      const categoryLegend = categorySeries
        .map((s) => ({ label: s.label, cls: s.cls, total: seriesTotal(s.key, byDayCategory) }))
        .filter((s) => s.total > 0);

      return json({ volumeSvg, categorySvg, volumeLegend, categoryLegend });
    }

    if (messageDetailMatch) {
      const row = await env.MERCURY_LOG.prepare('SELECT * FROM messages WHERE id = ?')
        .bind(Number(messageDetailMatch[1])).first();
      if (!row) return json({ ok: false, error: 'no message with that id' }, 404);
      return json(row);
    }

    if (pathname === '/dashboard/api/rule-stats') {
      const result = await env.MERCURY_LOG.prepare(`
        SELECT triggered_rule, reasoning, category, received_at FROM messages
        WHERE received_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-${RULE_STATS_DAYS} day')
          AND (triggered_rule IS NOT NULL OR category = 'SENDER_LIST')
      `).all();
      const rules = {};
      const senders = {};
      const bump = (table, key, at) => {
        const entry = table[key] || (table[key] = { hits: 0, last: null });
        entry.hits += 1;
        if (!entry.last || at > entry.last) entry.last = at;
      };
      for (const row of result.results ?? []) {
        if (row.triggered_rule) {
          bump(rules, row.triggered_rule, row.received_at);
          continue;
        }
        // The backend names the list entry that matched in this sentence;
        // see _deterministic_verdict in backend/app.py.
        const m = /^Matched sender (\S+) on the deterministic (whitelist|greylist|blacklist)\b/.exec(row.reasoning || '');
        if (m) bump(senders, m[1], row.received_at);
      }
      return json({ days: RULE_STATS_DAYS, rules, senders });
    }

    if (pathname === '/dashboard/api/simulate') {
      if (request.method !== 'POST') return new Response('method not allowed', { status: 405 });
      const body = await readBody();
      if (!body) return json({ ok: false, error: 'bad request body' }, 400);
      let matches;
      let target;
      if (body.kind === 'sender_list') {
        target = SENDER_LIST_DISPOSITIONS[body.list];
        const selector = String(body.selector || '').trim().toLowerCase().replace(/^@/, '').replace(/\.$/, '');
        if (!target || !selector || selector.length > 320) return json({ ok: false, error: 'a list and an address or domain are needed' }, 400);
        matches = selector.includes('@')
          ? (row) => senderAddress(row.from_display) === selector
          : (row) => {
            const domain = (row.from_domain || '').toLowerCase();
            return domain === selector || domain.endsWith('.' + selector);
          };
      } else if (body.kind === 'blacklist_pattern') {
        target = '550';
        let compiled;
        try {
          compiled = new RegExp('^(?:' + String(body.pattern || '') + ')$', 'i');
        } catch (err) {
          return json({ ok: false, error: 'that pattern is not a valid regular expression' }, 400);
        }
        if (!body.pattern || String(body.pattern).length > 500) return json({ ok: false, error: 'a pattern is needed' }, 400);
        matches = (row) => compiled.test((row.from_domain || '').toLowerCase());
      } else {
        return json({ ok: false, error: 'only sender lists and patterns can be previewed' }, 400);
      }
      const result = await env.MERCURY_LOG.prepare(`
        SELECT id, received_at, from_display, from_domain, subject, enforced_disposition, category
        FROM messages
        WHERE received_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-${RULE_STATS_DAYS} day')
        ORDER BY id DESC
      `).all();
      const hit = (result.results ?? []).filter(matches);
      const byOutcome = { 250: 0, 421: 0, 550: 0 };
      for (const row of hit) byOutcome[row.enforced_disposition] = (byOutcome[row.enforced_disposition] || 0) + 1;
      return json({
        days: RULE_STATS_DAYS,
        target,
        total: hit.length,
        byOutcome,
        wouldChange: hit.filter((row) => row.enforced_disposition !== target).length,
        samples: hit.slice(0, 8).map(({ id, received_at, from_domain, subject, enforced_disposition }) => ({ id, received_at, from_domain, subject, enforced_disposition })),
      });
    }

    if (pathname === '/dashboard/api/review') {
      if (request.method === 'POST') {
        const body = await readBody();
        const messageId = Number(body?.message_id);
        const verdict = body?.verdict;
        const correct = body?.correct_disposition ?? null;
        if (!Number.isInteger(messageId) || messageId <= 0 || !['right', 'wrong'].includes(verdict)
          || (correct !== null && !['250', '421', '550'].includes(correct))) {
          return json({ ok: false, error: 'a message id and a verdict of right or wrong are needed' }, 400);
        }
        const stored = await recordReviewLabel(env, messageId, verdict, verdict === 'wrong' ? correct : null, 'sample');
        if (!stored) return json({ ok: false, error: 'no message with that id' }, 404);
        return json({ ok: true });
      }
      // A fixed pseudo-random order per outcome, so each outcome's sample is
      // an unbiased draw from that outcome and the queue is stable between
      // loads. Quotas lean toward rejections, the costliest mistakes.
      const draw = async (outcome, quota) => (await env.MERCURY_LOG.prepare(`
        SELECT m.id, m.received_at, m.from_display, m.from_domain, m.subject, m.enforced_disposition,
          m.category, m.verdict, m.reasoning, m.triggered_rule
        FROM messages m LEFT JOIN review_labels l ON l.message_id = m.id
        WHERE l.id IS NULL AND m.enforced_disposition = ?
          AND m.received_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-${REVIEW_WINDOW_DAYS} day')
        ORDER BY (m.id * 2654435761) % 4294967296
        LIMIT ?
      `).bind(outcome, quota).all()).results ?? [];
      const [accepted, deferred, rejected] = await Promise.all([draw('250', 8), draw('421', 4), draw('550', 8)]);
      return json({ days: REVIEW_WINDOW_DAYS, rows: [...rejected, ...accepted, ...deferred] });
    }

    if (pathname === '/dashboard/api/review/metrics') {
      const result = await env.MERCURY_LOG.prepare(`
        SELECT outcome, source,
          COUNT(*) AS labeled,
          SUM(CASE WHEN verdict = 'wrong' THEN 1 ELSE 0 END) AS wrong
        FROM review_labels
        WHERE labeled_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-${RULE_STATS_DAYS} day')
        GROUP BY outcome, source
      `).all();
      return json({ days: RULE_STATS_DAYS, rows: result.results ?? [] });
    }

    if (pathname === '/dashboard/api/health') {
      const db = env.MERCURY_LOG;
      const [latestMessage, latestEvent, sweep, delivered, failed, count, judges] = await Promise.all([
        db.prepare('SELECT MAX(received_at) AS at FROM messages').first(),
        db.prepare('SELECT MAX(at) AS at FROM admin_log').first(),
        db.prepare("SELECT at, detail FROM admin_log WHERE event = 'log_retention_sweep' ORDER BY id DESC LIMIT 1").first(),
        db.prepare("SELECT MAX(executed_at) AS at FROM actions WHERE kind = 'DELIVER' AND result LIKE 'delivered to %'").first(),
        db.prepare("SELECT executed_at AS at, result FROM actions WHERE kind = 'DELIVER' AND result LIKE 'failed%' ORDER BY id DESC LIMIT 1").first(),
        db.prepare('SELECT COUNT(*) AS n FROM messages').first(),
        db.prepare(`
          SELECT COUNT(*) AS total, SUM(CASE WHEN fields != '[]' THEN 1 ELSE 0 END) AS disagreed
          FROM judge_comparisons WHERE compared_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 day')
        `).first(),
      ]);
      return json({
        latestMessageAt: latestMessage?.at ?? null,
        latestEventAt: latestEvent?.at ?? null,
        lastRetentionSweepAt: sweep?.at ?? null,
        lastRetentionSweep: sweep?.detail ?? null,
        lastDeliveredAt: delivered?.at ?? null,
        lastDeliveryFailedAt: failed?.at ?? null,
        lastDeliveryFailure: failed?.result ?? null,
        messageCount: count?.n ?? 0,
        judgeComparisons: judges?.total ?? 0,
        judgeDisagreements: judges?.disagreed ?? 0,
      });
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
        return json({ ok: false, error: 'missing rule' }, 400);
      }
      const response = await reverseRule(rule, env);
      // Removing the rule that decided a message says that outcome was wrong.
      const messageId = Number(body?.message_id);
      if (response.ok && Number.isInteger(messageId) && messageId > 0) {
        await recordReviewLabel(env, messageId, 'wrong', null, 'reversal');
      }
      return response;
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
      const since24h = "strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 day')";
      const [last24h, actions24h, ruleChanges7d, categories7d, openItems] = await Promise.all([
        db.prepare(`
          SELECT COUNT(*) AS total,
            SUM(CASE WHEN enforced_disposition = '250' THEN 1 ELSE 0 END) AS accepted,
            SUM(CASE WHEN enforced_disposition = '421' THEN 1 ELSE 0 END) AS deferred,
            SUM(CASE WHEN enforced_disposition = '550' THEN 1 ELSE 0 END) AS rejected,
            SUM(CASE WHEN alert_level = 'URGENT' THEN 1 ELSE 0 END) AS urgent
          FROM messages WHERE received_at >= ${since24h}
        `).first(),
        db.prepare(`
          SELECT COUNT(*) AS total,
            SUM(CASE WHEN kind = 'DELIVER' AND result LIKE 'delivered to %' THEN 1 ELSE 0 END) AS delivered,
            SUM(CASE WHEN kind = 'DELIVER' AND result LIKE 'failed%' THEN 1 ELSE 0 END) AS failed
          FROM actions WHERE executed_at >= ${since24h}
        `).first(),
        db.prepare("SELECT COUNT(*) AS n FROM rule_changes WHERE changed_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 day')").first(),
        db.prepare("SELECT category, COUNT(*) AS count FROM messages WHERE received_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 day') GROUP BY category ORDER BY count DESC").all(),
        db.prepare('SELECT COUNT(*) AS n FROM action_items WHERE completed_at IS NULL').first(),
      ]);
      return json({
        last24h: {
          total: last24h?.total ?? 0,
          accepted: last24h?.accepted ?? 0,
          deferred: last24h?.deferred ?? 0,
          hardBounces: last24h?.rejected ?? 0,
          urgent: last24h?.urgent ?? 0,
          actions: actions24h?.total ?? 0,
          delivered: actions24h?.delivered ?? 0,
          deliveryFailed: actions24h?.failed ?? 0,
        },
        last7d: { ruleChanges: ruleChanges7d?.n ?? 0 },
        openActionItems: openItems?.n ?? 0,
        categories: (categories7d?.results ?? []).map((c) => ({ ...c, cls: categoryClass(c.category) })),
      });
    }

    if (pathname === '/dashboard/api/messages') {
      const disposition = params.get('disposition');
      const q = (params.get('q') || '').trim().slice(0, 200);
      const { limit, offset } = pageParams(params);
      const where = [];
      const binds = [];
      if (disposition) {
        where.push('enforced_disposition = ?');
        binds.push(disposition);
      }
      if (q) {
        // LIKE wildcards typed into the search box match literally.
        const pattern = '%' + q.replace(/[\\%_]/g, (c) => '\\' + c) + '%';
        where.push("(from_display LIKE ? ESCAPE '\\' OR from_domain LIKE ? ESCAPE '\\' OR subject LIKE ? ESCAPE '\\')");
        binds.push(pattern, pattern, pattern);
      }
      const whereSql = where.length ? `WHERE ${where.join(' AND ')}` : '';
      const result = await env.MERCURY_LOG.prepare(
        `SELECT ${MESSAGE_LIST_COLUMNS} FROM messages ${whereSql} ORDER BY id DESC LIMIT ? OFFSET ?`
      ).bind(...binds, limit + 1, offset).all();
      return json(paginate(result.results ?? [], limit));
    }

    if (pathname === '/dashboard/api/rules') {
      const { limit, offset } = pageParams(params);
      const result = await env.MERCURY_LOG.prepare('SELECT * FROM rule_changes ORDER BY id DESC LIMIT ? OFFSET ?').bind(limit + 1, offset).all();
      return json(paginate(result.results ?? [], limit));
    }

    if (pathname === '/dashboard/api/actions') {
      const { limit, offset } = pageParams(params);
      const result = await env.MERCURY_LOG.prepare('SELECT * FROM actions ORDER BY id DESC LIMIT ? OFFSET ?').bind(limit + 1, offset).all();
      return json(paginate(result.results ?? [], limit));
    }
  } catch (err) {
    return json({ ok: false, error: String(err) }, 500);
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

// ForwardEmail's own webhook-recipient code (helpers/retry-request.js)
// throws for any HTTP status other than exactly 200, and its error-code
// translation (helpers/get-error-code.js) only recognizes a thrown status
// in the 400-599 range as a real SMTP disposition - 421 and 550 both fall
// in that range and round-trip correctly, but 250 does not, so it falls
// through to that function's unconditional final `return 550`. The result:
// an accepted message was being reported to the ORIGINAL SENDER as a hard
// bounce, even though it was correctly delivered here - see
// neostryder/mercury#53. An accept must therefore be signaled as a literal
// HTTP 200, never the raw 250, or ForwardEmail miscodes it as a rejection.
const ACCEPT_DISPOSITION = 250;
const ACCEPT_HTTP_STATUS = 200;

async function tokenMatches(given, expected) {
  if (!expected) return false;
  const encoder = new TextEncoder();
  const a = encoder.encode(given);
  const b = encoder.encode(expected);
  if (a.byteLength !== b.byteLength) return false;
  return crypto.subtle.timingSafeEqual(a, b);
}

// Enforcement now depends on this call completing, but a self-hosted outage
// or a slow backend must still never itself cause a bounce of legitimate
// mail - see docs/ARCHITECTURE.md. Anything short of a clean, recognized
// disposition from the backend is never a 550. When the mailbox is a
// recipient of its own, it resolves to an accept, since the message still
// arrives there. When the backend is the only path into the mailbox
// (CUSTODY_REQUIRED, set alongside MERCURY_DELIVER_ACCEPTED_MAIL), an accept
// would lose the message, so it resolves to 421 and the mail host retries.
async function proxyIngest(backendUrl, bodyText, env) {
  const failureStatus = env.CUSTODY_REQUIRED === 'true' ? 421 : ACCEPT_HTTP_STATUS;
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
      const webhookStatus = resp.status === ACCEPT_DISPOSITION ? ACCEPT_HTTP_STATUS : resp.status;
      return new Response(text, { status: webhookStatus, headers: { 'Content-Type': 'application/json' } });
    }
    return new Response(text || 'OK', { status: failureStatus });
  } catch (err) {
    // Backend unreachable, slow, or errored - never guess a bounce.
    return new Response('OK', { status: failureStatus });
  } finally {
    clearTimeout(timeout);
  }
}

const RULE_STATS_DAYS = 90;
const REVIEW_WINDOW_DAYS = 14;
const SENDER_LIST_DISPOSITIONS = { whitelist: '250', greylist: '421', blacklist: '550' };

function senderAddress(fromDisplay) {
  const text = (fromDisplay || '').trim();
  const bracketed = /<([^<>\s]+@[^<>\s]+)>/.exec(text);
  const address = bracketed ? bracketed[1] : (/^[^\s<>]+@[^\s<>]+$/.test(text) ? text : '');
  return address.toLowerCase();
}

// Stores or replaces the label for one message, keeping its outcome at the
// time of labeling. Returns false when the message does not exist.
async function recordReviewLabel(env, messageId, verdict, correctDisposition, source) {
  const message = await env.MERCURY_LOG.prepare('SELECT enforced_disposition FROM messages WHERE id = ?')
    .bind(messageId).first();
  if (!message) return false;
  await env.MERCURY_LOG.prepare(`
    INSERT INTO review_labels (message_id, outcome, verdict, correct_disposition, source, labeled_at)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT (message_id) DO UPDATE SET outcome = excluded.outcome, verdict = excluded.verdict,
      correct_disposition = excluded.correct_disposition, source = excluded.source, labeled_at = excluded.labeled_at
  `).bind(messageId, message.enforced_disposition, verdict, correctDisposition, source, new Date().toISOString()).run();
  return true;
}

// Called from the dashboard message inspector. The rules ledger
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

// The filtering policy is stored on the backend filesystem, not in D1. The
// dashboard is already gated by the Cloudflare Access application on this
// path; this hop additionally uses the backend's shared secret and never
// exposes it to browser JavaScript.
async function proxyFilteringPolicy(request, env) {
  const headers = { 'X-Mercury-Secret': env.MERCURY_SHARED_SECRET };
  let body;
  if (request.method === 'POST') {
    const declaredLength = Number(request.headers.get('Content-Length') || 0);
    if (declaredLength > 32768) {
      return new Response('request too large', { status: 413 });
    }
    body = await request.text();
    if (body.length > 32768) {
      return new Response('request too large', { status: 413 });
    }
    headers['Content-Type'] = 'application/json';
  }

  try {
    const response = await fetch(`${env.BACKEND_BASE_URL}/filtering`, {
      method: request.method,
      headers,
      body,
    });
    return new Response(response.body, {
      status: response.status,
      headers: {
        'Content-Type': response.headers.get('Content-Type') || 'application/json',
        'Strict-Transport-Security': HSTS,
      },
    });
  } catch (err) {
    return new Response(JSON.stringify({ ok: false, error: 'backend unreachable' }), {
      status: 502,
      headers: { 'Content-Type': 'application/json', 'Strict-Transport-Security': HSTS },
    });
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

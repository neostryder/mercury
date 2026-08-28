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

export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('OK', { status: 200 });
    }

    const { pathname } = new URL(request.url);
    const bodyText = await request.text();
    const backendUrl = `${env.BACKEND_BASE_URL}${pathname}`;

    if (pathname === '/ingest') {
      return proxyIngest(backendUrl, bodyText, env);
    }

    return proxySynchronously(backendUrl, bodyText, env);
  },
};

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

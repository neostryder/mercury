// The address ForwardEmail's webhook actually calls. Runs on Cloudflare's
// edge rather than any self-hosted machine so that a self-hosted outage
// cannot surface as a bounce of legitimate mail - see docs/ARCHITECTURE.md.
//
// Also relays any other POST path (e.g. /rules/propose, used by the
// Thunderbird extension) straight to the backend and returns its real
// response - that call is a direct, synchronous user action, not something
// arriving under SMTP bounce-risk, so there is nothing to protect it from.
export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('OK', { status: 200 });
    }

    const { pathname } = new URL(request.url);
    const bodyText = await request.text();
    const backendUrl = `${env.BACKEND_BASE_URL}${pathname}`;

    if (pathname === '/ingest') {
      // Handed to the backend in the background: the response below does
      // not wait on it. Shadow mode always accepts, so there is nothing for
      // the backend's result to change on this request yet.
      ctx.waitUntil(forwardInBackground(backendUrl, bodyText, env));
      return new Response('OK', { status: 200 });
    }

    return proxySynchronously(backendUrl, bodyText, env);
  },
};

async function forwardInBackground(backendUrl, bodyText, env) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    await fetch(backendUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Mercury-Secret': env.MERCURY_SHARED_SECRET,
      },
      body: bodyText,
      signal: controller.signal,
    });
  } catch (err) {
    // Backend unreachable or slow. Nothing to do in shadow mode - the
    // message was already accepted above.
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

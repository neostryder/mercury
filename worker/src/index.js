// The address ForwardEmail's webhook actually calls. Runs on Cloudflare's
// edge rather than any self-hosted machine so that a self-hosted outage
// cannot surface as a bounce of legitimate mail - see docs/ARCHITECTURE.md.
export default {
  async fetch(request, env, ctx) {
    if (request.method !== 'POST') {
      return new Response('OK', { status: 200 });
    }

    const bodyText = await request.text();

    // Handed to the backend in the background: the response below does not
    // wait on it. Shadow mode always accepts, so there is nothing for the
    // backend's result to change on this request yet.
    ctx.waitUntil(forwardToBackend(bodyText, env));

    return new Response('OK', { status: 200 });
  },
};

async function forwardToBackend(bodyText, env) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    await fetch(env.BACKEND_URL, {
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

// The service worker keeps the page itself, never mail data. Every /dashboard/api
// call goes to the network untouched; what the page remembers of the last summary
// (counts only) lives in its own storage, not here.
const VERSION = 'f4d4f5217d';
const CACHE = 'mercury-shell-' + VERSION;
const SHELL = [
  "/",
  "/app.94992e090d.js",
  "/app.d97fa2809d.css",
  "/favicon.png",
  "/fonts/alegreya-sans-latin-400-normal.woff2",
  "/fonts/alegreya-sans-latin-500-normal.woff2",
  "/fonts/alegreya-sans-latin-700-normal.woff2",
  "/fonts/fraunces-latin-wght-normal.woff2",
  "/icons/icon-192.png",
  "/theme-boot.d93401047a.js"
];

function isPage(res) {
  return res.ok && !res.redirected && (res.headers.get('Content-Type') || '').includes('text/html');
}

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.all(SHELL.map(async (path) => {
      // The Worker answers a plain OK to / unless the request asks for HTML, and sends a
      // visitor without a sign-in to Access. Neither answer is the page, so neither is kept.
      const page = path === '/';
      const res = await fetch(path, page ? { cache: 'reload', redirect: 'manual', headers: { Accept: 'text/html' } } : { cache: 'reload' });
      if (page ? isPage(res) : res.ok) await cache.put(path, res);
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((n) => n.startsWith('mercury-shell-') && n !== CACHE).map((n) => caches.delete(n)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // The page: answer from the cache at once and refresh it in the background.
  if (req.mode === 'navigate' && url.pathname === '/') {
    event.respondWith((async () => {
      const cache = await caches.open(CACHE);
      const cached = await cache.match('/');
      const refresh = fetch(req).then((res) => {
        if (isPage(res)) cache.put('/', res.clone());
        return res;
      });
      if (cached) { event.waitUntil(refresh.catch(() => {})); return cached; }
      return refresh;
    })());
    return;
  }

  // Fingerprinted files, fonts, and icons: cache first.
  if (SHELL.includes(url.pathname)) {
    event.respondWith((async () => {
      const cached = await caches.match(url.pathname);
      return cached || fetch(req);
    })());
  }
});

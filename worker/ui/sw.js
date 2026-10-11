// The service worker keeps the page itself, never mail data. Every /dashboard/api
// call goes to the network untouched; what the page remembers of the last summary
// (counts only) lives in its own storage, not here.
const VERSION = '__VERSION__';
const CACHE = 'mercury-shell-' + VERSION;
const SHELL = __SHELL__;

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.all(SHELL.map(async (path) => {
      const res = await fetch(path, { cache: 'reload' });
      if (res.ok) await cache.put(path, res);
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
        if (res.ok && (res.headers.get('Content-Type') || '').includes('text/html')) cache.put('/', res.clone());
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

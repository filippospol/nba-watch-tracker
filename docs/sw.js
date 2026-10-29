/* NBA Watch Tracker — service worker.
   - Network-first for the page and the live data (so new builds & scores always land).
   - Cache-first for static assets (logos, icon, fonts) — instant, offline-friendly.
   - Supabase (accounts/sync) is never cached.
   Bump VERSION to force every client onto a fresh cache. */
const VERSION = 'v1';
const CACHE = 'nba-wt-' + VERSION;

// Minimal app shell precached on install (the rest fills in as it's used).
const SHELL = ['./', './index.html', './manifest.json', './icon.png', './icon-192.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k.startsWith('nba-wt-') && k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

function putInCache(req, res) {
  // only cache good, basic/cors responses (not opaque errors)
  if (res && res.ok) {
    const copy = res.clone();
    caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
  }
  return res;
}

function networkFirst(req) {
  return fetch(req)
    .then((res) => putInCache(req, res))
    .catch(() => caches.match(req).then((m) => m || caches.match('./index.html')));
}

function cacheFirst(req) {
  return caches.match(req).then((hit) =>
    hit || fetch(req).then((res) => putInCache(req, res)).catch(() => hit)
  );
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;

  let url;
  try { url = new URL(req.url); } catch (_) { return; }

  // Accounts / sync: always straight to the network, never cached.
  if (url.hostname.endsWith('.supabase.co')) return;

  const isDoc  = req.mode === 'navigate' || url.pathname.endsWith('/') || url.pathname.endsWith('.html');
  const isData = /\/(schedule|ratings|logos)\.json$/.test(url.pathname);

  e.respondWith((isDoc || isData) ? networkFirst(req) : cacheFirst(req));
});
const CACHE = 'lan-deck-v14';
const ASSETS = ['/', '/style.css', '/app.js', '/manifest.webmanifest', '/icon.svg', '/apple-touch-icon.png', '/icon-192.png', '/icon-512.png'];
self.addEventListener('install', e => { self.skipWaiting(); e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS))) });
self.addEventListener('activate', e => e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('lan-deck-') && k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())));
// Network first so UI updates show up immediately; fall back to cache when offline.
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/') || url.pathname.startsWith('/admin') || url.origin !== location.origin) return;
  e.respondWith(fetch(e.request).then(resp => { if (resp.ok) { const copy = resp.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)) } return resp }).catch(() => caches.match(e.request)));
});

// TYM Music — Service Worker (PWA shell caching)
const CACHE = 'tym-v1';
const SHELL = ['/', '/style.css', '/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  // API y YouTube: siempre red (no cachear)
  const url = e.request.url;
  if (url.includes('/api/') || url.includes('youtube') || url.includes('ytimg')) return;
  // Shell: red primero, caché como fallback
  e.respondWith(
    fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy));
      return r;
    }).catch(() => caches.match(e.request))
  );
});

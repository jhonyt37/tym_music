// TYM Music — Service Worker (PWA)
const CACHE = 'tym-v3';
const SHELL = ['/', '/style.css', '/icon.svg', '/icon-192.png', '/icon-512.png', '/offline.html'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('push', e => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch(_) {}
  const title = data.title || '🗳️ TYM Music';
  const opts = {
    body: data.body || '¡Nueva votación disponible! Abre la app para votar.',
    icon: data.icon || '/icon-192.png',
    badge: '/icon-192.png',
    tag: data.tag || 'tym-poll',
    renotify: true,
    data: { url: data.url || '/' }
  };
  e.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(wcs => {
      for (const wc of wcs) {
        if ('focus' in wc) return wc.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});

self.addEventListener('fetch', e => {
  const url = e.request.url;
  // API, YouTube y recursos externos: siempre red, sin caché
  if (url.includes('/api/') || url.includes('youtube') || url.includes('ytimg') ||
      url.includes('googleapis') || e.request.method !== 'GET') return;

  e.respondWith(
    fetch(e.request)
      .then(r => {
        // Guardar copia fresca en caché
        if (r.ok) {
          const copy = r.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return r;
      })
      .catch(async () => {
        // Sin red: intentar caché, y si no hay, mostrar offline.html
        const cached = await caches.match(e.request);
        if (cached) return cached;
        // Para navegación (páginas HTML), mostrar offline.html
        if (e.request.mode === 'navigate') {
          return caches.match('/offline.html');
        }
      })
  );
});

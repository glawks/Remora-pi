/* Pi Remote: cache UI files only. Never cache pairing/authentication responses. */
'use strict';
const CACHE = 'pi-remote-shell-v0.3.0';
const ASSETS = ['/', '/static/app.js', '/static/style.css', '/static/manifest.webmanifest',
                '/static/icon-192.png', '/static/icon-512.png', '/static/icon-maskable-512.png'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('pi-remote-shell-') && k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname === '/' && event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).catch(() => caches.match('/')));
    return;
  }
  if (!url.pathname.startsWith('/static/')) return;
  // Fetch updated code first so installed Android homescreen apps receive upgrades;
  // only use cached code as an offline fallback (never cache API/token responses).
  if (['/static/app.js','/static/style.css','/static/manifest.webmanifest'].includes(url.pathname)) {
    event.respondWith(fetch(event.request).catch(() => caches.match(url.pathname)));
    return;
  }
  event.respondWith(caches.match(url.pathname).then(cached => cached || fetch(event.request)));
});

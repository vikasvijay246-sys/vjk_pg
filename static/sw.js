/* sw.js — PG Manager Pro Service Worker v3
 * NEVER caches API responses or the HTML shell.
 * Only caches truly static assets (JS bundles, CSS, icons).
 */
const CACHE_NAME = "pg-pro-static-v3";

// Only these specific static files go into cache
const STATIC_ASSETS = [
  "/static/manifest.json",
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE_NAME)
      .then(c => c.addAll(STATIC_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);

  // NEVER intercept: API calls, HTML pages, POST/PUT/DELETE, cross-origin
  if (
    e.request.method !== "GET" ||
    url.pathname.startsWith("/api/") ||
    url.pathname === "/" ||
    url.pathname.endsWith(".html") ||
    url.origin !== self.location.origin
  ) {
    return; // Let the browser handle it directly — no cache
  }

  // For static assets only: cache-first strategy
  e.respondWith(
    caches.match(e.request).then(hit => {
      if (hit) return hit;
      return fetch(e.request).then(res => {
        if (res.ok && res.type === "basic") {
          caches.open(CACHE_NAME).then(c => c.put(e.request, res.clone()));
        }
        return res;
      });
    })
  );
});

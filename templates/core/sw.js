// Service worker: makes the app installable and shows a friendly page when the
// network is gone. No page is ever cached: every screen needs the server (and
// the AI) anyway, and cached HTML would outlive a login.
const CACHE = "tutor-en-{{ version }}";
const OFFLINE_URL = "{{ offline_url }}";
const PRECACHE = {{ precache|safe }};

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Pages always come from the server; the offline page only when it cannot be reached.
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)));
    return;
  }

  // Own static files: serve the cached copy and refresh it in the background.
  if (url.pathname.startsWith("{{ static_url }}")) {
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const cached = await cache.match(request);
        const fresh = fetch(request)
          .then((response) => {
            if (response.ok) cache.put(request, response.clone());
            return response;
          })
          .catch(() => cached);
        return cached || fresh;
      })
    );
  }
});

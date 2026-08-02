const CACHE = "kyotei-ai-v79";

self.addEventListener("install", e => {
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const fresh = e.request.mode === "navigate"
    ? fetch(e.request.url, { cache: "reload" })
    : fetch(e.request);
  e.respondWith(
    fresh
      .then(res => {
        if (res.ok) {
          const copy = res.clone();
          const u = new URL(e.request.url);
          u.search = "";
          caches.open(CACHE).then(c => c.put(new Request(u), copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});

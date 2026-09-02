const CACHE = "kyotei-ai-v149";

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll([
      "./",
      "index.html",
      "manifest.json",
      "icon-192.png",
      "icon-512.png"
    ]))
  );
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

// Web Push受信: ペイロードのJSON化に失敗した場合は既定文言にフォールバックする
self.addEventListener("push", e => {
  let title = "アリテイ", body = "新着情報があります";
  try {
    if (e.data) {
      const d = e.data.json();
      if (d && d.title) title = d.title;
      if (d && d.body) body = d.body;
    }
  } catch (_) {
    // JSONでない場合は既定文言のまま
  }
  e.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: "icon-192.png",
      badge: "icon-192.png"
    })
  );
});

// 通知タップ: 既存ウィンドウがあればフォーカス、無ければ新規に開く
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if ("focus" in c) return c.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow("./");
    })
  );
});

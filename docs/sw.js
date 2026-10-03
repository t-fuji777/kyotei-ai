const CACHE = "kyotei-ai-v163";

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

// Web Push受信: ペイロードは {"title","body","tag"(省略可)}(scripts/notify.py が送る形)。
// データが無い・JSONでない場合も、既定文言で必ず通知を出す(通知を出さない push が続くと、
// iPhone が購読を取り消すため)。
self.addEventListener("push", e => {
  let title = "アリテイ", body = "新着情報があります", tag = "";
  try {
    if (e.data) {
      const d = e.data.json();
      if (d && d.title) title = d.title;
      if (d && d.body) body = d.body;
      if (d && d.tag) tag = String(d.tag);
    }
  } catch (_) {
    // JSONでない場合は既定文言のまま
  }
  // badge(Android のステータスバーに出る小さな印)は指定しない。透明部分だけを使って単色で描かれるため、
  // 透明部分の無い icon-192.png を渡すと白い四角になる。
  const opt = { body, icon: "icon-192.png" };
  // 同じ出来事の送り直しは同じ tag で届く。重ねて出さず、前の通知を置き換える(鳴らし直しはしない)。
  if (tag) opt.tag = tag;
  e.waitUntil(self.registration.showNotification(title, opt));
});

// 通知タップ: 開いているアリテイがあれば、厳選一覧へ切り替える合図(push-click)を送って前面に出す。
// 無ければ "./?from=push" を新しく開く(index.html がこの印を見て厳選一覧を表示する)。
// 対象は自分の scope(/kyotei-ai/)のウィンドウだけに絞る。matchAll は同じオリジン
// (t-fuji777.github.io)の全ウィンドウを返すので、絞らないと別サイト(/kinlog/ など)が前面に出る。
self.addEventListener("notificationclick", e => {
  // 設定タブの「テスト通知を表示」で出した通知は、端末の表示を試すだけなので画面を切り替えない
  const test = e.notification.tag === "aritei-test";
  e.notification.close();
  const scope = self.registration.scope;
  e.waitUntil((async () => {
    let list = [];
    try {
      list = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    } catch (_) {
      // 一覧を取れない時は、新しく開く側に倒す
    }
    for (const c of list) {
      if (!c.url || c.url.indexOf(scope) !== 0 || !("focus" in c)) continue;
      try {
        if (!test) c.postMessage({ type: "push-click" });
        return await c.focus();
      } catch (_) {
        // 前面に出せなければ次の候補へ。どれも駄目なら新しく開く
      }
    }
    if (self.clients.openWindow) return self.clients.openWindow(test ? "./" : "./?from=push");
  })());
});

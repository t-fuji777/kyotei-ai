// Web Push購読を保管するCloudflare Worker(ES modules形式)。
// KVバインディング: SUBS
// 変数: VAPID_PUB (Web Push の公開鍵。docs/index.html の VAPID_PUB と同じ値。秘密ではない)
//
// エンドポイント:
//   OPTIONS *      CORSプリフライト応答(許可Originのみ)
//   POST /sub      購読を登録(endpointのSHA-256 hexをキーにKV保存)。アプリから呼ぶ
//   POST /unsub    購読を解除。アプリから呼ぶ
//   GET  /subs     全購読のJSON配列を返す(送信側専用・要署名)
//   DELETE /sub?id=..  購読を削除(送信側が 404/410 を受けた購読の掃除用・要署名)
//
// 送信側(GitHub Actions の scripts/notify.py)の認証:
//   Web Push 用の秘密鍵(VAPID)で署名した短命のトークン(JWT, ES256)を Authorization ヘッダで
//   受け取り、ここでは公開鍵で検証する。合言葉(共有の秘密)をこの Worker には置かない。
//   トークンは宛先(aud)がこの Worker 自身で、期限(exp)が15分以内のものだけを受け付ける。

const ALLOWED_ORIGIN = "https://t-fuji777.github.io";
const MAX_SUBS = 500;          // 登録できる購読の上限(誰でも登録できるため、際限なく増えないように)
const MAX_BODY_BYTES = 4096;   // 購読1件は1KB未満
const MAX_TOKEN_LIFE_SEC = 900;

// 実在するプッシュ配信サービスの宛先だけを受け付ける。任意のURLを登録できると、
// 送信側が見知らぬサーバーへ接続させられ、応答待ちで開催中の処理が遅れる。
const PUSH_HOST_SUFFIXES = [
  "fcm.googleapis.com",                 // Chrome / Android / Edge
  "updates.push.services.mozilla.com",  // Firefox
  "push.apple.com",                     // Safari / iOS (web.push.apple.com)
  "notify.windows.com",                 // Windows
];

function corsHeaders(request) {
  const origin = request.headers.get("Origin") || "";
  if (origin !== ALLOWED_ORIGIN) return {};
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

async function sha256Hex(text) {
  const data = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function isPushEndpoint(endpoint) {
  if (typeof endpoint !== "string" || endpoint.length > 2048) return false;
  let u;
  try {
    u = new URL(endpoint);
  } catch (e) {
    return false;
  }
  if (u.protocol !== "https:") return false;
  const host = u.hostname.toLowerCase();
  return PUSH_HOST_SUFFIXES.some((s) => host === s || host.endsWith("." + s));
}

function isValidSubscription(sub) {
  return Boolean(
    sub &&
      isPushEndpoint(sub.endpoint) &&
      sub.keys &&
      typeof sub.keys.p256dh === "string" &&
      sub.keys.p256dh.length <= 200 &&
      typeof sub.keys.auth === "string" &&
      sub.keys.auth.length <= 100
  );
}

async function readJson(request) {
  try {
    const text = await request.text();
    if (text.length > MAX_BODY_BYTES) return null;
    return JSON.parse(text);
  } catch (e) {
    return null;
  }
}

function jsonResponse(data, status, extraHeaders) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...(extraHeaders || {}) },
  });
}

function b64urlToBytes(s) {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0));
}

// Authorization: "vapid t=<JWT>,k=<公開鍵>" (py_vapid の形式) または "Bearer <JWT>"。
// k= は使わない(検証に使う公開鍵は、この Worker に設定した VAPID_PUB だけ)。
async function isSender(request, url, env) {
  try {
    if (!env.VAPID_PUB) return false;
    const h = request.headers.get("Authorization") || "";
    const m = h.match(/^vapid\s+t=([A-Za-z0-9_\-.]+)/) || h.match(/^Bearer\s+([A-Za-z0-9_\-.]+)$/);
    if (!m) return false;
    const parts = m[1].split(".");
    if (parts.length !== 3) return false;
    const header = JSON.parse(new TextDecoder().decode(b64urlToBytes(parts[0])));
    if (header.alg !== "ES256") return false;
    const key = await crypto.subtle.importKey(
      "raw",
      b64urlToBytes(env.VAPID_PUB),
      { name: "ECDSA", namedCurve: "P-256" },
      false,
      ["verify"]
    );
    const ok = await crypto.subtle.verify(
      { name: "ECDSA", hash: "SHA-256" },
      key,
      b64urlToBytes(parts[2]),
      new TextEncoder().encode(parts[0] + "." + parts[1])
    );
    if (!ok) return false;
    const claims = JSON.parse(new TextDecoder().decode(b64urlToBytes(parts[1])));
    const now = Math.floor(Date.now() / 1000);
    if (claims.aud !== url.origin) return false;
    if (typeof claims.exp !== "number" || claims.exp <= now || claims.exp > now + MAX_TOKEN_LIFE_SEC) return false;
    return true;
  } catch (e) {
    return false;
  }
}

async function handleSub(request, env, headers) {
  const body = await readJson(request);
  const sub = body && body.subscription;
  if (!isValidSubscription(sub)) {
    return jsonResponse({ error: "invalid subscription" }, 400, headers);
  }
  const id = await sha256Hex(sub.endpoint);
  if (!(await env.SUBS.get(id))) {
    const list = await env.SUBS.list({ limit: MAX_SUBS });
    if (list.keys.length >= MAX_SUBS) {
      return jsonResponse({ error: "too many subscriptions" }, 507, headers);
    }
  }
  const clean = { endpoint: sub.endpoint, keys: { p256dh: sub.keys.p256dh, auth: sub.keys.auth } };
  await env.SUBS.put(id, JSON.stringify({ subscription: clean }));
  return jsonResponse({ ok: true, id }, 201, headers);
}

async function handleUnsub(request, env, headers) {
  const body = await readJson(request);
  const endpoint = body && body.endpoint;
  if (!isPushEndpoint(endpoint)) {
    return jsonResponse({ error: "invalid endpoint" }, 400, headers);
  }
  const id = await sha256Hex(endpoint);
  await env.SUBS.delete(id);
  return jsonResponse({ ok: true }, 200, headers);
}

async function handleListSubs(env) {
  const out = [];
  let cursor;
  // KV listは1000件上限のため、list_completeがfalseの間cursorでループする。
  for (;;) {
    const list = await env.SUBS.list(cursor ? { cursor } : {});
    for (const entry of list.keys) {
      const raw = await env.SUBS.get(entry.name);
      if (!raw) continue;
      try {
        const parsed = JSON.parse(raw);
        out.push({ id: entry.name, subscription: parsed.subscription });
      } catch (e) {
        // 壊れたエントリはスキップ
      }
    }
    if (list.list_complete) break;
    cursor = list.cursor;
  }
  return jsonResponse(out, 200);
}

async function handleDeleteSub(url, env) {
  const id = url.searchParams.get("id") || "";
  if (!/^[0-9a-f]{64}$/.test(id)) {
    return jsonResponse({ error: "id required" }, 400);
  }
  await env.SUBS.delete(id);
  return jsonResponse({ ok: true }, 200);
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const headers = corsHeaders(request);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers });
    }
    if (request.method === "POST" && url.pathname === "/sub") {
      return handleSub(request, env, headers);
    }
    if (request.method === "POST" && url.pathname === "/unsub") {
      return handleUnsub(request, env, headers);
    }
    if (request.method === "GET" && url.pathname === "/subs") {
      if (!(await isSender(request, url, env))) return jsonResponse({ error: "unauthorized" }, 401);
      return handleListSubs(env);
    }
    if (request.method === "DELETE" && url.pathname === "/sub") {
      if (!(await isSender(request, url, env))) return jsonResponse({ error: "unauthorized" }, 401);
      return handleDeleteSub(url, env);
    }
    return jsonResponse({ error: "not found" }, 404, headers);
  },
};

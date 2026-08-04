// Web Push購読を保管するCloudflare Worker(ES modules形式)。
// KVバインディング: SUBS
// シークレット: AUTH_KEY (サーバ間エンドポイント /subs, /sub(DELETE) の認証用)
//
// エンドポイント:
//   OPTIONS *          CORSプリフライト応答(許可Originのみ)
//   POST /sub          購読を登録(endpointのSHA-256 hexをキーにKV保存)
//   POST /unsub        購読を解除
//   GET  /subs?key=..  全購読のJSON配列を返す(認証必須・サーバ間用・CORS不要)
//   DELETE /sub?key=..&id=..  認証付き削除(送信側の404/410掃除用)

const ALLOWED_ORIGIN = "https://t-fuji777.github.io";

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

function isValidSubscription(sub) {
  return Boolean(
    sub &&
      typeof sub.endpoint === "string" &&
      sub.endpoint.startsWith("https://") &&
      sub.keys &&
      typeof sub.keys.p256dh === "string" &&
      typeof sub.keys.auth === "string"
  );
}

async function readJson(request) {
  try {
    return await request.json();
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

async function handleSub(request, env, headers) {
  const body = await readJson(request);
  const sub = body && body.subscription;
  if (!isValidSubscription(sub)) {
    return jsonResponse({ error: "invalid subscription" }, 400, headers);
  }
  const id = await sha256Hex(sub.endpoint);
  await env.SUBS.put(id, JSON.stringify({ subscription: sub }));
  return jsonResponse({ ok: true, id }, 201, headers);
}

async function handleUnsub(request, env, headers) {
  const body = await readJson(request);
  const endpoint = body && body.endpoint;
  if (typeof endpoint !== "string" || !endpoint.startsWith("https://")) {
    return jsonResponse({ error: "invalid endpoint" }, 400, headers);
  }
  const id = await sha256Hex(endpoint);
  await env.SUBS.delete(id);
  return jsonResponse({ ok: true }, 200, headers);
}

function checkAuth(url, env) {
  const key = url.searchParams.get("key") || "";
  return Boolean(env.AUTH_KEY) && key === env.AUTH_KEY;
}

async function handleListSubs(url, env) {
  if (!checkAuth(url, env)) {
    return jsonResponse({ error: "unauthorized" }, 401);
  }
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
  if (!checkAuth(url, env)) {
    return jsonResponse({ error: "unauthorized" }, 401);
  }
  const id = url.searchParams.get("id") || "";
  if (!id) {
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
      return handleListSubs(url, env);
    }
    if (request.method === "DELETE" && url.pathname === "/sub") {
      return handleDeleteSub(url, env);
    }
    return jsonResponse({ error: "not found" }, 404, headers);
  },
};

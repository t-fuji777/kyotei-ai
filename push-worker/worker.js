// Web Push購読を保管するCloudflare Worker(ES modules形式)。
// KVバインディング: SUBS
// 変数: VAPID_PUB (Web Push の公開鍵。docs/index.html の VAPID_PUB と同じ値。秘密ではない)
//
// エンドポイント:
//   OPTIONS *      CORSプリフライト応答(許可Originのみ)
//   POST /sub      購読を登録。アプリから呼ぶ(アプリ以外の Origin からは 403)
//                    201 = 新しく登録した(同じ宛先で鍵が変わった時の更新も 201)
//                    200 = 同じ内容で登録済み(何も書き込まない)
//                    400 = 内容が不正 / 507 = 満杯 / 503 = 内部の一時的な失敗
//   POST /unsub    購読を解除。アプリから呼ぶ(アプリ以外の Origin からは 403)
//   GET  /subs     全購読のJSON配列 [{id, subscription:{endpoint, keys}}] を返す(送信側専用・要署名)
//   DELETE /sub?id=..  購読を削除(送信側が 404/410 を受けた購読の掃除用・要署名。id は複数並べてよい)
//
// 送信側(GitHub Actions の scripts/notify.py)の認証:
//   Web Push 用の秘密鍵(VAPID)で署名した短命のトークン(JWT, ES256)を Authorization ヘッダで
//   受け取り、ここでは公開鍵で検証する。合言葉(共有の秘密)をこの Worker には置かない。
//   トークンは宛先(aud)がこの Worker 自身で、期限(exp)が15分以内のものだけを受け付ける。
//
// 保管の形:
//   全購読を KV の1つの値(キー "table:v1"、中身は {id: {endpoint, keys}})にまとめて持つ。
//   id は宛先URL(正規形)の SHA-256 hex。
//   1件ずつ別のキーに分けると、件数の確認や一覧に KV の list が要る。list の無料枠は1日1000回で、
//   認証なしの登録要求だけで使い切られると、その日は送信側が一覧を取れず通知が全部止まる。
//   1つの値なら、どの要求も読み取り1回(変更がある時だけ書き込み1回)で済み、list は使わない。
//   弱点: ほぼ同時に来た登録は、後から書いた方が残り、先の登録が消えることがある(KV には
//   「読んでから書く」を1つにまとめる仕組みが無い)。アプリは起動のたびに登録を確かめ直すので、
//   消えた登録は次の起動で戻る。
//   復旧: 偽の登録で埋められた時は、Cloudflare の画面で KV の "table:v1" を消せば空に戻る
//   (旧形式のキーが残っていれば、次の要求でそこから取り込み直す)。
//
// テスト(Node だけで動く。Cloudflare にはつながない):
//   node push-worker/test/worker_test.mjs
// このファイルを変えても、Cloudflare へ設置し直す(push-worker/ で wrangler deploy)までは本番に反映されない。

const ALLOWED_ORIGIN = "https://t-fuji777.github.io";
// 登録できる購読の上限(誰でも登録できるため、際限なく増えないように)。
// 送信側(scripts/notify.py)が1回に相手にする件数と揃える。受け口の方が多く受け付けると、
// 一覧の後ろに回った宛先へは送られず、しかも外からは「登録できている」ように見えてしまう。
const MAX_SUBS = 100;
const MAX_BODY_BYTES = 4096;   // 購読1件は1KB未満
const MAX_ENDPOINT_LEN = 2048;
const MAX_TOKEN_LIFE_SEC = 900;
const TABLE_KEY = "table:v1";
const ID_RE = /^[0-9a-f]{64}$/;
// 旧形式(購読1件=キー1個)からの取り込みがまだ済んでいない、という印。表の中に入れる。
// 通常は付かない(取り込みに失敗した時だけ。importLegacy を参照)。
const LEGACY_PENDING = "legacy_pending";
const MAX_LEGACY_READS = 500;  // 旧形式で受け付けていた上限。取り込みで読む件数をこれ以下に抑える

// 実在するプッシュ配信サービスの宛先だけを受け付ける。任意のURLを登録できると、
// 送信側が見知らぬサーバーへ接続させられ、応答待ちで開催中の処理が遅れる。
// 送信側(scripts/notify.py)も同じ規則で宛先を確かめる。変える時は両方を揃えること。
const PUSH_HOSTS = new Set([
  "fcm.googleapis.com",                 // Chrome / Android / Edge
  "jmt17.google.com",                   // Chrome の一部の版(Dev / Canary など)が使う宛先
  "updates.push.services.mozilla.com",  // Firefox
  "web.push.apple.com",                 // Safari / iOS
]);
const WINDOWS_HOST_RE = /^[a-z0-9-]+\.notify\.windows\.com$/;  // Windows (例: wns2-xxx.notify.windows.com)

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

// 宛先URLを確かめ、正規形(URL として読み直して書き出した形)を返す。受け付けない時は null。
// 保存も id の計算も、受け取った文字列ではなくこの戻り値で行う(同じ宛先の書き方違いで
// 枠を複数使われないように。また、検査した形と送信側が使う形を同じにするため)。
// 規則: https のみ / ポート指定なし / ユーザー情報なし / 空白・バックスラッシュ・非ASCIIなし /
//       ホスト名は PUSH_HOSTS の完全一致か WINDOWS_HOST_RE。
function normalizeEndpoint(endpoint) {
  if (typeof endpoint !== "string" || endpoint.length > MAX_ENDPOINT_LEN) return null;
  // 印字できる ASCII だけを受ける(空白・改行・制御文字・全角を含まない)。バックスラッシュと # も受けない。
  // こうした文字は、URL を読む実装(この Worker と送信側の Python)の間で解釈が割れることがある。
  if (!/^https:\/\/[!-~]+$/.test(endpoint) || endpoint.includes("\\") || endpoint.includes("#")) return null;
  let u;
  try {
    u = new URL(endpoint);
  } catch (e) {
    return null;
  }
  if (u.protocol !== "https:" || u.port !== "" || u.username !== "" || u.password !== "") return null;
  if (!PUSH_HOSTS.has(u.hostname) && !WINDOWS_HOST_RE.test(u.hostname)) return null;
  // "https://" の直後が許可したホスト名そのもので、すぐ "/" が続く形だけを通す。
  // ":443" のようなポートの明示、"user@"、大文字や % 表記のホスト名は、読み直した後は
  // 同じホストに見えても断る(実際のブラウザが作る宛先には現れない)。
  if (!endpoint.startsWith("https://" + u.hostname + "/")) return null;
  if (u.href.length > MAX_ENDPOINT_LEN) return null;
  return u.href;
}

// 購読を確かめ、保存する形 {endpoint(正規形), keys:{p256dh, auth}} に整えて返す。不正なら null。
// 余計な項目はここで捨てる。登録の時だけでなく、保管済みの表を読む時にも同じ検査を通す。
function cleanSubscription(sub) {
  if (!sub || typeof sub !== "object") return null;
  const endpoint = normalizeEndpoint(sub.endpoint);
  const keys = sub.keys;
  if (!endpoint || !keys || typeof keys !== "object") return null;
  if (typeof keys.p256dh !== "string" || keys.p256dh.length > 200) return null;
  if (typeof keys.auth !== "string" || keys.auth.length > 100) return null;
  return { endpoint, keys: { p256dh: keys.p256dh, auth: keys.auth } };
}

async function readJson(request) {
  try {
    // 大きすぎると申告された本文は読まずに断る。申告(Content-Length)は無いことも偽ることも
    // できるので、読んだ後の長さでももう一度確かめる。
    if (Number(request.headers.get("Content-Length")) > MAX_BODY_BYTES) return null;
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
    // no-store: 途中の機器やブラウザに応答(特に購読一覧)を覚えさせない
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...(extraHeaders || {}) },
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

// 表を書き込む。pending(旧形式の取り込みが未完了)の間は、その印も一緒に残す。
async function saveTable(env, subs, pending) {
  const out = pending ? { [LEGACY_PENDING]: true, ...subs } : subs;
  await env.SUBS.put(TABLE_KEY, JSON.stringify(out));
}

// 旧形式(キー=64桁hex、値={"subscription":{...}})の購読を表へ取り込む。
// 表がまだ無い最初の1回だけ通る道で、KV の list を使うのはここだけ。
// 旧キーは消さない(消すのにも1日の枠を使う。表ができた後は読まれないので残っていても害が無い)。
async function importLegacy(env, subs) {
  let done = false;
  try {
    const page = await env.SUBS.list({ limit: 1000 });
    const names = page.keys
      .map((k) => k.name)
      .filter((name) => ID_RE.test(name))
      .slice(0, MAX_LEGACY_READS);
    const raws = await Promise.all(names.map((name) => env.SUBS.get(name)));
    let count = Object.keys(subs).length;
    for (const raw of raws) {
      if (count >= MAX_SUBS) break;
      let entry = null;
      try {
        entry = cleanSubscription(JSON.parse(raw).subscription);
      } catch (e) {
        // 壊れた1件は飛ばす(他の購読の取り込みは続ける)
      }
      if (!entry) continue;  // 今の検査を通らない宛先(ポートつき等)は取り込まない
      const id = await sha256Hex(entry.endpoint);
      if (!Object.hasOwn(subs, id)) {
        subs[id] = entry;
        count++;
      }
    }
    done = true;
  } catch (e) {
    // list や get の失敗(無料枠切れ・一時的な不調)。例外を外へ出さず、取り込めた分だけで続ける。
    // ここで「取り込み済み」にしてしまうと旧形式の購読(持ち主の端末)が二度と読まれないので、
    // 未完了の印を付けたままにし、次の要求でもう一度試す。
  }
  if (done) {
    try {
      // 取り込む物が無くても表を書く。表が無いままだと、要求のたびに list を使ってしまう。
      await saveTable(env, subs, false);
    } catch (e) {
      // 書けなくても、この要求には取り込んだ内容で応える。表が無いままなので次の要求でやり直す。
    }
  }
  return { subs, pending: !done };
}

// 表を読む。戻り値は { subs: {id: {endpoint, keys}}, pending: 旧形式の取り込みが未完了か }。
// KV の読み取りは1回だけ(表が無い最初の1回と、取り込みのやり直し中を除く)。
async function loadTable(env) {
  const raw = await env.SUBS.get(TABLE_KEY);
  if (raw === null) return importLegacy(env, {});
  // 表が壊れていたら例外にして 503 を返す。空の表として続けると、次の登録で全員の購読を
  // 上書きして消してしまう(直し方は冒頭の「復旧」と同じ)。
  const stored = JSON.parse(raw);
  if (!stored || typeof stored !== "object" || Array.isArray(stored)) throw new Error("broken table");
  const subs = {};
  for (const [id, value] of Object.entries(stored)) {
    // 保管済みの各件も、登録の時と同じ検査に通す。通らないものは無かったものとして扱う
    // (送信側へ返さない。次に表を書く時に消える)。
    const entry = ID_RE.test(id) ? cleanSubscription(value) : null;
    if (entry) subs[id] = entry;
  }
  if (stored[LEGACY_PENDING] === true) return importLegacy(env, subs);
  return { subs, pending: false };
}

async function handleSub(request, env, headers) {
  const body = await readJson(request);
  const entry = cleanSubscription(body && body.subscription);
  if (!entry) {
    return jsonResponse({ error: "invalid subscription" }, 400, headers);
  }
  const id = await sha256Hex(entry.endpoint);
  const { subs, pending } = await loadTable(env);
  const cur = Object.hasOwn(subs, id) ? subs[id] : null;
  if (cur && cur.keys.p256dh === entry.keys.p256dh && cur.keys.auth === entry.keys.auth) {
    // 同じ内容で登録済み。アプリは起動のたびに確かめに来るので、ここで書き込まないことで
    // 書き込みの枠(無料枠は1日1000回)を使わずに済ませる。
    return jsonResponse({ ok: true, id }, 200, headers);
  }
  if (!cur && Object.keys(subs).length >= MAX_SUBS) {
    return jsonResponse({ error: "too many subscriptions" }, 507, headers);
  }
  subs[id] = entry;
  await saveTable(env, subs, pending);
  return jsonResponse({ ok: true, id }, 201, headers);
}

// 表から id を消す。消す物が無ければ書き込まない(知らない宛先の解除で枠を使わせない)。
async function removeIds(env, ids) {
  const { subs, pending } = await loadTable(env);
  let removed = 0;
  for (const id of ids) {
    if (Object.hasOwn(subs, id)) {
      delete subs[id];
      removed++;
    }
  }
  if (removed) await saveTable(env, subs, pending);
  return removed;
}

async function handleUnsub(request, env, headers) {
  const body = await readJson(request);
  const endpoint = normalizeEndpoint(body && body.endpoint);
  if (!endpoint) {
    return jsonResponse({ error: "invalid endpoint" }, 400, headers);
  }
  await removeIds(env, [await sha256Hex(endpoint)]);
  return jsonResponse({ ok: true }, 200, headers);
}

async function handleListSubs(env) {
  const { subs } = await loadTable(env);
  const out = Object.entries(subs).map(([id, subscription]) => ({ id, subscription }));
  return jsonResponse(out, 200);
}

// DELETE /sub?id=..&id=..  id は1個でも複数でもよい(複数でも書き込みは1回)。
async function handleDeleteSub(url, env) {
  const ids = url.searchParams.getAll("id");
  if (!ids.length || ids.length > MAX_SUBS || !ids.every((id) => ID_RE.test(id))) {
    return jsonResponse({ error: "id required" }, 400);
  }
  const removed = await removeIds(env, ids);
  return jsonResponse({ ok: true, removed }, 200);
}

async function route(request, env, url, headers) {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: { "Cache-Control": "no-store", ...headers } });
  }
  if (request.method === "POST" && (url.pathname === "/sub" || url.pathname === "/unsub")) {
    // アプリ以外のページが、訪問者のブラウザに登録・解除の要求を出させるのを断る
    // (事前確認なしで飛ぶ形の POST でも、ブラウザは Origin を必ず付ける)。
    // ブラウザ以外(curl 等)は Origin を偽れるので、これは認証ではない。
    if (request.headers.get("Origin") !== ALLOWED_ORIGIN) {
      return jsonResponse({ error: "forbidden" }, 403, headers);
    }
    return url.pathname === "/sub" ? handleSub(request, env, headers) : handleUnsub(request, env, headers);
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
}

export default {
  async fetch(request, env, ctx) {
    let headers = {};
    try {
      headers = corsHeaders(request);
      return await route(request, env, new URL(request.url), headers);
    } catch (e) {
      // KV の一時的な失敗や無料枠切れ。そのまま例外にすると Cloudflare のエラー画面(HTML・CORS
      // ヘッダなし)になり、アプリからは通信失敗にしか見えない。内部の情報は返さず、アプリが
      // 読める形(JSON・CORS ヘッダつき)で「一時的に使えない」とだけ伝える。
      return jsonResponse({ error: "temporarily unavailable" }, 503, headers);
    }
  },
};

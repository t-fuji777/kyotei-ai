// 受け口(push-worker/worker.js)のテスト。
//
// 実行方法(リポジトリの直下で。Node 20 以上。外部パッケージは不要):
//   node push-worker/test/worker_test.mjs
// 最後の行が「… 項目を確認、失敗 0」なら合格。失敗があると「NG」の行が出て、終了コードが 1 になる。
//
// Cloudflare にはつながない(本番の受け口や購読には一切触れない)。
//   - KV は Map で代用し、list / get / put / delete を呼んだ回数を数える。
//   - 送信側の署名は、その場で作ったテスト用の鍵で作る(本番の鍵は使わない)。
//
// 確かめること:
//   1. アプリ・送信側との取り決めどおりの応答(201 / 200 / 400 / 403 / 507 / 503 / 401 / 404)
//   2. 登録できる宛先の規則(危ない書き方は 400、実在する配信サービスの形は通る)
//   3. Origin の確認(アプリ以外のページからの登録・解除は 403)
//   4. 同じ内容の再登録・知らない宛先の解除では書き込まない
//   5. 上限(100件)と、KV の list を使わないこと(旧形式の取り込みの1回を除く)
//   6. 旧形式からの取り込み(持ち主の購読が失われないこと。失敗しても止まらないこと)
//   7. 送信側の署名(通るもの・通らないもの)
//   8. 内部の失敗は 503(JSON・CORS ヘッダつき・内部情報なし)
//   9. 本文の大きさと形の確認
//  10. 保管済みの表を読む時の検査、同時登録の弱点と回復、wrangler.toml の設定

import { readFileSync } from "node:fs";
import { createHash, createHmac, webcrypto } from "node:crypto";

const subtle = webcrypto.subtle;

// worker.js は Cloudflare に置く都合で拡張子が .js のまま。Node に「ES modules として読む」ことを
// 確実に伝えるため、中身を読んで data: URL として読み込む(Node の版や設定に左右されない)。
const workerSrc = readFileSync(new URL("../worker.js", import.meta.url), "utf8");
const worker = (await import("data:text/javascript;base64," + Buffer.from(workerSrc, "utf8").toString("base64"))).default;

const WORKER = "https://aritei-push.t-fujino.workers.dev";
const APP = "https://t-fuji777.github.io";
const TABLE_KEY = "table:v1";
const MAX_SUBS = 100;

const b64u = (x) => Buffer.from(x).toString("base64url");
const sha = (text) => createHash("sha256").update(text, "utf8").digest("hex");
const nowSec = () => Math.floor(Date.now() / 1000);

// ---- テスト用の鍵と署名 ------------------------------------------------------------
async function newKey() {
  const kp = await subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  const raw = new Uint8Array(await subtle.exportKey("raw", kp.publicKey));
  return { kp, raw, pub: b64u(raw) };
}
const K = await newKey();     // 送信側の鍵(Worker の VAPID_PUB と対になる)
const EVIL = await newKey();  // 第三者の鍵

async function sign(key, header, claims) {
  const h = typeof header === "string" ? header : b64u(JSON.stringify(header));
  const p = typeof claims === "string" ? claims : b64u(JSON.stringify(claims));
  const sig = new Uint8Array(await subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key.kp.privateKey, Buffer.from(h + "." + p)));
  return { h, p, sig, tok: h + "." + p + "." + b64u(sig) };
}
const HDR = { typ: "JWT", alg: "ES256" };
const goodClaims = () => ({ aud: WORKER, exp: nowSec() + 300, sub: "mailto:test@example.com" });
// 送信側(py_vapid)と同じ形の Authorization ヘッダ
const senderAuth = async () => `vapid t=${(await sign(K, HDR, goodClaims())).tok},k=${K.pub}`;

// ---- KV の代用 ---------------------------------------------------------------------
function makeKV(entries) {
  const m = new Map(entries || []);
  const used = { list: 0, get: 0, put: 0, delete: 0 };
  // fail.list などを true にすると、その操作が例外になる。fail.getKey は特定のキーの get だけ失敗させる。
  const fail = { list: false, get: false, put: false, delete: false, getKey: null };
  const boom = (op) => new Error("KV " + op + " failed: INTERNAL-DETAIL-SHOULD-NOT-LEAK");
  return {
    m, used, fail,
    async get(k) {
      used.get++;
      if (fail.get || (fail.getKey && fail.getKey(k))) throw boom("get");
      return m.has(k) ? m.get(k) : null;
    },
    async put(k, v) {
      used.put++;
      if (fail.put) throw boom("put");
      m.set(k, String(v));
    },
    async delete(k) {
      used.delete++;
      if (fail.delete) throw boom("delete");
      m.delete(k);
    },
    async list(opts) {
      used.list++;
      if (fail.list) throw boom("list");
      const names = [...m.keys()].sort();
      const limit = (opts && opts.limit) || 1000;
      return { keys: names.slice(0, limit).map((name) => ({ name })), list_complete: names.length <= limit };
    },
  };
}
// table を渡すと、表(table:v1)が既にある状態から始める。legacy は旧形式のキーと値の組。
function newEnv({ table, legacy, pub = K.pub } = {}) {
  const kv = makeKV(legacy);
  if (table !== undefined) kv.m.set(TABLE_KEY, typeof table === "string" ? table : JSON.stringify(table));
  return { SUBS: kv, VAPID_PUB: pub };
}
// 保存されている表の中身(無い・読めない時は空として返し、確認の側で NG にする)
const storedTable = (env) => {
  try {
    return JSON.parse(env.SUBS.m.get(TABLE_KEY)) || {};
  } catch (e) {
    return {};
  }
};
const snap = (env) => ({ ...env.SUBS.used });
const sameUse = (a, b) => a.list === b.list && a.get === b.get && a.put === b.put && a.delete === b.delete;

// ---- 要求を出す ---------------------------------------------------------------------
async function call(env, method, path, { body, auth, origin = APP, ctype = "application/json", headers = {}, base = WORKER } = {}) {
  const h = { ...headers };
  if (auth) h.Authorization = auth;
  if (origin) h.Origin = origin;
  if (body !== undefined) h["Content-Type"] = ctype;
  const init = { method, headers: h };
  if (body !== undefined) init.body = typeof body === "string" ? body : JSON.stringify(body);
  let res;
  try {
    res = await worker.fetch(new Request(base + path, init), env, {});
  } catch (e) {
    return { status: "THROW", text: String(e), json: null, headers: new Headers() };
  }
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) { /* 本文なし(204)など */ }
  return { status: res.status, text, json, headers: res.headers };
}
const KEYS = { p256dh: "BPxx-test-p256dh", auth: "test-auth" };
const sub = (endpoint, keys = KEYS) => ({ subscription: { endpoint, keys } });
const postSub = (env, endpoint, opts = {}) => call(env, "POST", "/sub", { body: sub(endpoint, opts.keys), ...opts });
const postUnsub = (env, endpoint, opts = {}) => call(env, "POST", "/unsub", { body: { endpoint }, ...opts });
const getSubs = async (env) => call(env, "GET", "/subs", { auth: await senderAuth(), origin: null });
const delSubs = async (env, ids) => call(env, "DELETE", "/sub?" + ids.map((i) => "id=" + i).join("&"), { auth: await senderAuth(), origin: null });
const hasCors = (r) => r.headers.get("Access-Control-Allow-Origin") === APP;
const noStore = (r) => r.headers.get("Cache-Control") === "no-store";
const isJson = (r) => (r.headers.get("Content-Type") || "").startsWith("application/json") && r.json !== null;

// ---- 結果の記録 ---------------------------------------------------------------------
let total = 0;
let failed = 0;
function ok(cond, name) {
  total++;
  if (cond) {
    console.log("ok  " + name);
  } else {
    failed++;
    console.log("NG  " + name);
  }
}
// 節の途中で想定外の例外が出ても、そこを NG として記録し、残りの節は続ける
async function section(title, body) {
  console.log("\n== " + title);
  try {
    await body();
  } catch (e) {
    ok(false, "この節の途中で想定外の例外: " + String((e && e.message) || e).slice(0, 120));
  }
}
const show = (s) => JSON.stringify(s).slice(0, 72);

// 実在する配信サービスの宛先の形(トークン部分は作り物)
const REAL = {
  fcm: "https://fcm.googleapis.com/fcm/send/eXAmple-_tok:APA91bF-abc_DEF123",
  fcmWp: "https://fcm.googleapis.com/wp/eXAmple-_tok:APA91bF-abc_DEF123",
  jmt: "https://jmt17.google.com/fcm/send/eXAmple-_tok:APA91bF-abc_DEF123",
  jmtWp: "https://jmt17.google.com/wp/eXAmple-_tok",
  mozilla: "https://updates.push.services.mozilla.com/wpush/v2/gAAAAABexample-_x",
  apple: "https://web.push.apple.com/QOb5exampleTOKEN-_123",
  windows: "https://wns2-par02p.notify.windows.com/w/?token=BQYAAACm%2bFexample%2fabc%3d%3d",
  windows2: "https://db5p.notify.windows.com/w/?token=abc",
};
const OWNER = "https://fcm.googleapis.com/fcm/send/OWNER-device_tok:APA91bOwner-_123";
const OWNER_KEYS = { p256dh: "BOwner-p256dh", auth: "owner-auth" };

// =====================================================================================
await section("1. 取り決めどおりの応答", async () => {
  const env = newEnv();
  let r = await call(env, "OPTIONS", "/sub", { headers: { "Access-Control-Request-Method": "POST" } });
  ok(r.status === 204 && hasCors(r) && /POST/.test(r.headers.get("Access-Control-Allow-Methods") || "") &&
    /Content-Type/i.test(r.headers.get("Access-Control-Allow-Headers") || ""), "OPTIONS(アプリの Origin)→ 204 と許可ヘッダ");
  r = await call(env, "OPTIONS", "/sub", { origin: "https://evil.example" });
  ok(r.status === 204 && r.headers.get("Access-Control-Allow-Origin") === null, "OPTIONS(他の Origin)→ 204、許可ヘッダなし");
  ok(sameUse(snap(env), { list: 0, get: 0, put: 0, delete: 0 }), "OPTIONS は KV に触れない");

  r = await postSub(env, REAL.fcm);
  ok(r.status === 201 && r.json.ok === true && r.json.id === sha(REAL.fcm), "POST /sub 新規 → 201 {ok, id}(id は宛先の SHA-256)");
  ok(hasCors(r) && noStore(r) && isJson(r), "POST /sub の応答は JSON・CORS ヘッダつき・Cache-Control: no-store");
  const before = snap(env);
  r = await postSub(env, REAL.fcm);
  ok(r.status === 200 && r.json.ok === true && r.json.id === sha(REAL.fcm) && hasCors(r), "POST /sub 同じ内容で登録済み → 200(id は同じ)");
  ok(env.SUBS.used.put === before.put && env.SUBS.used.delete === before.delete, "…その時は書き込まない");
  r = await postSub(env, REAL.fcm, { keys: { p256dh: "NEW-p256dh", auth: "NEW-auth" } });
  ok(r.status === 201 && env.SUBS.used.put === before.put + 1, "POST /sub 同じ宛先で鍵が変わった → 201(書き込み1回)");
  ok(Object.keys(storedTable(env)).length === 1, "…件数は増えない(同じ id を上書き)");

  r = await postSub(env, "https://evil.example/x");
  ok(r.status === 400 && hasCors(r) && isJson(r), "POST /sub 不正な宛先 → 400(CORS ヘッダつき)");
  r = await postSub(env, REAL.apple, { origin: "https://evil.example" });
  ok(r.status === 403, "POST /sub 他の Origin → 403");

  await postSub(env, REAL.apple);
  r = await getSubs(env);
  ok(r.status === 200 && Array.isArray(r.json) && r.json.length === 2, "GET /subs(署名つき)→ 200 と配列");
  ok(r.json.every((e) => JSON.stringify(Object.keys(e)) === '["id","subscription"]' &&
    JSON.stringify(Object.keys(e.subscription)) === '["endpoint","keys"]' &&
    JSON.stringify(Object.keys(e.subscription.keys)) === '["p256dh","auth"]' &&
    e.id === sha(e.subscription.endpoint)), "GET /subs の各件は {id, subscription:{endpoint, keys:{p256dh, auth}}} だけ");
  ok(r.json[0].subscription.endpoint === REAL.fcm && r.json[0].subscription.keys.p256dh === "NEW-p256dh" &&
    r.json[1].subscription.endpoint === REAL.apple, "GET /subs は登録した順に、最新の鍵で返す");
  ok(r.headers.get("Access-Control-Allow-Origin") === null && noStore(r), "GET /subs の応答に CORS ヘッダは付かず、no-store");
  r = await call(env, "GET", "/subs", { auth: await senderAuth() });
  ok(r.status === 200 && r.headers.get("Access-Control-Allow-Origin") === null, "GET /subs はアプリの Origin から呼んでも CORS ヘッダを付けない(ブラウザから読めない)");
  r = await call(env, "GET", "/subs", { origin: null });
  ok(r.status === 401 && isJson(r), "GET /subs 署名なし → 401");

  r = await postUnsub(env, REAL.apple);
  ok(r.status === 200 && r.json.ok === true && hasCors(r) && (await getSubs(env)).json.length === 1, "POST /unsub → 200、一覧から消える");
  r = await postUnsub(env, REAL.apple);
  ok(r.status === 200, "POST /unsub 登録の無い宛先 → 200");
  r = await postUnsub(env, "not a url");
  ok(r.status === 400 && hasCors(r), "POST /unsub 不正な宛先 → 400");
  r = await postUnsub(env, REAL.fcm, { origin: null });
  ok(r.status === 403 && (await getSubs(env)).json.length === 1, "POST /unsub Origin なし → 403(消えない)");

  await postSub(env, REAL.mozilla);
  await postSub(env, REAL.windows);
  r = await delSubs(env, [sha(REAL.fcm)]);
  ok(r.status === 200 && r.json.ok === true && (await getSubs(env)).json.length === 2, "DELETE /sub?id=…(署名つき・1個)→ 200、消える");
  const p0 = env.SUBS.used.put;
  r = await delSubs(env, [sha(REAL.mozilla), sha(REAL.windows)]);
  ok(r.status === 200 && (await getSubs(env)).json.length === 0 && env.SUBS.used.put === p0 + 1, "DELETE /sub?id=…&id=…(複数)→ 200、書き込みは1回");
  const p1 = env.SUBS.used.put;
  r = await delSubs(env, ["0".repeat(64)]);
  ok(r.status === 200 && env.SUBS.used.put === p1, "DELETE 登録の無い id → 200(書き込まない)");
  r = await call(env, "DELETE", "/sub?id=" + sha(REAL.fcm), { origin: null });
  ok(r.status === 401, "DELETE 署名なし → 401");
  ok((await delSubs(env, ["xyz"])).status === 400, "DELETE id の形が違う → 400");
  ok((await delSubs(env, [sha("a"), "XYZ"])).status === 400, "DELETE 複数のうち1個でも形が違う → 400");
  ok((await call(env, "DELETE", "/sub", { auth: await senderAuth(), origin: null })).status === 400, "DELETE id なし → 400");
  const many = Array.from({ length: MAX_SUBS + 1 }, (_, i) => sha("many-" + i));
  ok((await delSubs(env, many)).status === 400, "DELETE id が上限(100個)より多い → 400");

  r = await call(env, "GET", "/nope");
  ok(r.status === 404 && isJson(r), "知らないパス → 404");
  ok((await call(env, "GET", "/sub")).status === 404 && (await call(env, "POST", "/subs", { body: {} })).status === 404, "メソッド違い(GET /sub、POST /subs)→ 404");
  ok((await call(env, "HEAD", "/subs", { auth: await senderAuth(), origin: null })).status === 404, "HEAD /subs → 404");
  ok(env.SUBS.used.list === 1, "ここまでで KV の list は、表が無かった最初の1回だけ");
});

// =====================================================================================
await section("2. 登録できる宛先の規則", async () => {
  const env = newEnv({ table: {} });
  const accepted = Object.values(REAL);
  for (const ep of accepted) {
    const r = await postSub(env, ep);
    ok(r.status === 201 && r.json.id === sha(ep), "通す: " + ep.slice(0, 60));
  }
  let r = await getSubs(env);
  ok(r.json.length === accepted.length && r.json.every((e, i) => e.subscription.endpoint === accepted[i]),
    "実在の形は、受け取った文字列のまま保存される(正規形と同じなので id も変わらない)");

  // 危ない書き方。レビューで見つかった28種(1100文字の長さだけは 2049 文字に置き換え)と、追加分。
  const DANGEROUS = [
    ["ポート 81", "https://fcm.googleapis.com:81/fcm/send/abc"],
    ["ポート 8443 (apple)", "https://web.push.apple.com:8443/abc"],
    ["ユーザー情報 user:pass@", "https://user:pass@fcm.googleapis.com/x"],
    ["ユーザー情報に別のホスト名", "https://evil.example@fcm.googleapis.com/x"],
    ["fcm の任意のサブドメイン", "https://anything.fcm.googleapis.com/x"],
    ["notify.windows.com の2段のサブドメイン", "https://a.b.notify.windows.com/x"],
    ["apple の APNs 提供者向け API", "https://api.push.apple.com/3/device/abc"],
    ["apple の courier", "https://1-courier.push.apple.com/x"],
    ["push.apple.com そのもの", "https://push.apple.com/x"],
    ["notify.windows.com そのもの", "https://notify.windows.com/x"],
    ["全角の句点「。」", "https://fcm.googleapis。com/x"],
    ["全角の英字", "https://ｆｃｍ.googleapis.com/x"],
    ["バックスラッシュ + @evil", "https://fcm.googleapis.com\\@evil.example/x"],
    ["タブで区切ったユーザー情報", "https://evil.example\t@fcm.googleapis.com/x"],
    ["ホスト名の途中に改行", "https://fcm.goog\nleapis.com/x"],
    ["先頭に空白", "   https://fcm.googleapis.com/x"],
    ["末尾に改行", "https://fcm.googleapis.com/x\n"],
    ["スラッシュなし", "https:fcm.googleapis.com/x"],
    ["スラッシュの代わりにバックスラッシュ", "https:\\\\fcm.googleapis.com\\x"],
    ["CRLF でヘッダを差し込む試み", "https://fcm.googleapis.com/x\r\nX-Evil: 1"],
    ["ヌル文字", "https://fcm.googleapis.com/x\u0000y"],
    ["# つき", "https://fcm.googleapis.com/x#frag"],
    ["http", "http://fcm.googleapis.com/x"],
    ["無関係のホスト", "https://evil.example/x"],
    ["許可ホストを前に付けた別ドメイン", "https://fcm.googleapis.com.evil.example/x"],
    ["長さ 2049 文字", "https://fcm.googleapis.com/" + "a".repeat(2049 - 27)],
    ["この Worker 自身", WORKER + "/subs"],
    ["大文字", "HTTPS://FCM.GOOGLEAPIS.COM/X"],
    // ここから追加分
    ["ポート 443 の明示", "https://fcm.googleapis.com:443/fcm/send/abc"],
    ["ホスト名だけ大文字", "https://FCM.googleapis.com/x"],
    ["許可ホストをユーザー情報にした別ホスト", "https://fcm.googleapis.com@evil.example/x"],
    ["許可ホストをパスに入れた別ホスト", "https://evil.example/fcm.googleapis.com"],
    ["許可ホストを # の後に入れた別ホスト", "https://evil.example/#.fcm.googleapis.com"],
    ["許可ホストを ? の後に入れた別ホスト", "https://evil.example/?.fcm.googleapis.com"],
    ["前に文字を足したホスト", "https://evilfcm.googleapis.com/x"],
    ["apple の深いサブドメイン", "https://a.b.c.push.apple.com/x"],
    ["apple の旧 APNs", "https://gateway.push.apple.com/x"],
    ["末尾ドットのホスト", "https://fcm.googleapis.com./x"],
    ["% 表記のドット", "https://fcm.googleapis.com%2eevil.example/x"],
    ["% 表記のホスト文字", "https://fcm.googleapis.c%6fm/x"],
    ["別ホスト + バックスラッシュ + @許可ホスト", "https://evil.example\\@fcm.googleapis.com/x"],
    ["別ホスト + バックスラッシュ + .許可ホスト", "https://evil.example\\.fcm.googleapis.com/x"],
    ["ホスト名の途中にタブ", "https://fcm.googleapis.com\t.evil.example/x"],
    ["スラッシュ3本", "https:///evil.example/x"],
    ["wss", "wss://fcm.googleapis.com/x"],
    ["IPv4", "https://127.0.0.1/x"],
    ["IPv6", "https://[::1]/x"],
    ["10進のIP", "https://2130706433/x"],
    ["punycode の似た名前", "https://xn--fcm-googleapis-com.evil.example/x"],
    ["mozilla のサブドメイン", "https://x.updates.push.services.mozilla.com/"],
    ["パスなし", "https://fcm.googleapis.com"],
    ["パスなしで ? が続く", "https://fcm.googleapis.com?x=1"],
    ["途中に空白", "https://fcm.googleapis.com/x y"],
    ["パスに全角", "https://fcm.googleapis.com/あ"],
    ["jmt17 にポート", "https://jmt17.google.com:8443/fcm/send/abc"],
    ["jmt17 を前に付けた別ドメイン", "https://jmt17.google.com.evil.example/fcm/send/abc"],
    ["jmt17 の前に文字", "https://xjmt17.google.com/fcm/send/abc"],
    ["jmt18(取り決めは jmt17 の完全一致)", "https://jmt18.google.com/fcm/send/abc"],
    ["www.google.com", "https://www.google.com/fcm/send/abc"],
    ["古い GCM のホスト", "https://android.googleapis.com/gcm/send/abc"],
    ["windows の名前に下線", "https://wns2_x.notify.windows.com/w/?token=abc"],
    ["windows の名前が空", "https://.notify.windows.com/w/?token=abc"],
    ["windows を前に付けた別ドメイン", "https://db5p.notify.windows.com.evil.example/w/"],
    ["空文字", ""],
    ["文字列でない(数値)", 12345],
    ["文字列でない(配列)", ["https://fcm.googleapis.com/x"]],
    ["文字列でない(オブジェクト)", { href: "https://fcm.googleapis.com/x" }],
    ["null", null],
  ];
  const beforeUse = snap(env);
  let rejected = 0;
  for (const [label, ep] of DANGEROUS) {
    r = await postSub(env, ep);
    if (r.status === 400) rejected++;
    ok(r.status === 400, "断る: " + label + "  " + show(ep));
  }
  ok(rejected === DANGEROUS.length && DANGEROUS.length >= 28, `危ない書き方 ${DANGEROUS.length} 種がすべて 400`);
  ok(sameUse(snap(env), beforeUse), "不正な宛先は KV に触れる前に断る(読み取りも書き込みも 0 回)");
  ok((await getSubs(env)).json.length === accepted.length, "断った宛先は1件も保存されていない");
  let unsubRejected = 0;
  for (const [, ep] of DANGEROUS) if ((await postUnsub(env, ep)).status === 400) unsubRejected++;
  ok(unsubRejected === DANGEROUS.length, "POST /unsub も同じ規則で断る(すべて 400)");

  // 正規化: 書き方が違っても同じ宛先なら同じ1件になる
  const env2 = newEnv({ table: {} });
  const dotted = "https://fcm.googleapis.com/a/../fcm/send/tok-1";
  const canon = "https://fcm.googleapis.com/fcm/send/tok-1";
  r = await postSub(env2, dotted);
  ok(r.status === 201 && r.json.id === sha(canon), "書き方違い(/a/../)は正規形に直して保存し、id も正規形から計算する");
  ok((await getSubs(env2)).json[0].subscription.endpoint === canon, "…送信側へ返すのも正規形");
  ok((await postSub(env2, canon)).status === 200 && Object.keys(storedTable(env2)).length === 1, "…正規形で送り直すと 200(枠を余分に使わない)");
  const odd = 'https://fcm.googleapis.com/fcm/send/a"b<c>d`e{f}g|h';
  r = await postSub(env2, odd);
  const savedOdd = (await getSubs(env2)).json[1].subscription.endpoint;
  ok(r.status === 201 && /^[!-~]+$/.test(savedOdd) && !savedOdd.includes('"') && (await postSub(env2, savedOdd)).status === 200,
    "記号入りの宛先は % 表記に直して保存し、保存した形を送り直すと同じ1件に当たる");
  ok((await postUnsub(env2, dotted)).status === 200 && (await getSubs(env2)).json.length === 1, "解除も正規形で照合する(書き方違いでも消せる)");

  // 長さの境界
  const env3 = newEnv({ table: {} });
  ok((await postSub(env3, "https://fcm.googleapis.com/" + "a".repeat(2048 - 27))).status === 201, "長さ 2048 文字ちょうどは通す");
  ok((await postSub(env3, "https://fcm.googleapis.com/" + '"'.repeat(1000))).status === 400, "正規形にすると 2048 文字を超える宛先は断る");
});

// =====================================================================================
await section("3. Origin の確認", async () => {
  const env = newEnv({ table: {} });
  await postSub(env, REAL.fcm);
  const before = snap(env);
  let r = await call(env, "POST", "/sub", { body: JSON.stringify(sub(REAL.apple)), origin: "https://evil.example", ctype: "text/plain" });
  ok(r.status === 403 && r.headers.get("Access-Control-Allow-Origin") === null && isJson(r), "他サイトからの単純な POST(text/plain)→ 403、CORS ヘッダなし");
  for (const [label, origin] of [
    ["Origin なし(curl 等)", null],
    ["Origin: null", "null"],
    ["末尾スラッシュつき", APP + "/"],
    ["似た名前", APP + ".evil.example"],
    ["http", "http://t-fuji777.github.io"],
    ["大文字", "https://T-FUJI777.github.io"],
  ]) {
    r = await postSub(env, REAL.apple, { origin });
    const u = await postUnsub(env, REAL.fcm, { origin });
    ok(r.status === 403 && u.status === 403, "登録・解除とも 403: " + label);
  }
  ok(sameUse(snap(env), before), "403 の要求は KV に触れない");
  r = await getSubs(env);
  ok(r.json.length === 1 && r.json[0].subscription.endpoint === REAL.fcm, "403 の要求では登録も解除も起きていない");
  ok((await getSubs(env)).status === 200 && (await delSubs(env, [sha("x")])).status === 200, "送信側(Origin なし・署名つき)の GET /subs と DELETE は Origin の確認の対象外");
});

// =====================================================================================
await section("4. 書き込みを増やさない", async () => {
  const env = newEnv({ table: {} });
  await postSub(env, REAL.fcm);
  const before = snap(env);
  let all200 = true;
  for (let i = 0; i < 50; i++) if ((await postSub(env, REAL.fcm)).status !== 200) all200 = false;
  ok(all200 && env.SUBS.used.put === before.put && env.SUBS.used.delete === before.delete, "同じ内容の再登録 50 回 → すべて 200、書き込み 0 回");
  ok(env.SUBS.used.get === before.get + 50, "…読み取りは1回の要求につき1回だけ");
  for (let i = 0; i < 50; i++) await postUnsub(env, "https://fcm.googleapis.com/fcm/send/unknown-" + i);
  ok(env.SUBS.used.put === before.put && env.SUBS.used.delete === before.delete, "登録の無い宛先の解除 50 回 → 書き込み 0 回");
  ok(env.SUBS.used.list === 0, "list は使わない");
});

// =====================================================================================
await section("5. 上限と list を使わないこと", async () => {
  const env = newEnv({ table: {} });
  await postSub(env, OWNER, { keys: OWNER_KEYS });
  const st = {};
  for (let i = 0; i < 3000; i++) {
    const s = (await postSub(env, "https://web.push.apple.com/junk-" + i)).status;
    st[s] = (st[s] || 0) + 1;
  }
  console.log("    別々の宛先 3000 件を登録 → 応答の内訳 " + JSON.stringify(st) + " / KV の使用 " + JSON.stringify(env.SUBS.used));
  ok(st[201] === MAX_SUBS - 1 && st[507] === 3000 - (MAX_SUBS - 1), "上限 100 件: 99 件が 201、残りは 507");
  ok(env.SUBS.used.list === 0, "3000 件の登録で list は 0 回");
  ok(env.SUBS.used.put === MAX_SUBS, "書き込みは受け付けた 100 件ぶんだけ(507 では書き込まない)");
  let r = await postSub(env, "https://web.push.apple.com/one-more");
  ok(r.status === 507 && hasCors(r) && isJson(r), "満杯の時の新規登録 → 507(JSON・CORS ヘッダつき)");
  r = await getSubs(env);
  ok(r.status === 200 && r.json.length === MAX_SUBS, "満杯でも GET /subs は 200 で、ちょうど 100 件");
  ok(r.json[0].subscription.endpoint === OWNER && r.json[0].subscription.keys.auth === OWNER_KEYS.auth, "先に登録していた端末は、押し出されずに一覧の先頭に残る");
  ok((await postSub(env, OWNER, { keys: OWNER_KEYS })).status === 200, "満杯でも、登録済みの端末の確かめ直しは 200");
  ok((await postSub(env, OWNER, { keys: { p256dh: "BOwner-new", auth: "owner-new" } })).status === 201, "満杯でも、登録済みの端末の鍵の更新は 201");
  await postUnsub(env, "https://web.push.apple.com/junk-0");
  ok((await postSub(env, "https://web.push.apple.com/after-unsub")).status === 201, "1件解除すれば、新規登録がまた 201 になる");
  const junk = (await getSubs(env)).json.filter((e) => e.subscription.endpoint !== OWNER).map((e) => e.id);
  const p0 = env.SUBS.used.put;
  r = await delSubs(env, junk);
  ok(r.status === 200 && r.json.removed === junk.length && env.SUBS.used.put === p0 + 1, `送信側が ${junk.length} 件をまとめて掃除 → 書き込み1回`);
  r = await getSubs(env);
  ok(r.json.length === 1 && r.json[0].subscription.endpoint === OWNER, "掃除の後も持ち主の端末は残る");
  ok(env.SUBS.used.list === 0 && env.SUBS.used.delete === 0, "最後まで list も delete も 0 回(表の読み書きだけ)");
  ok(env.SUBS.m.size === 1 && env.SUBS.m.has(TABLE_KEY), "KV に作られるキーは table:v1 の1つだけ");
});

// =====================================================================================
await section("6. 旧形式(購読1件=キー1個)からの取り込み", async () => {
  // 今の本番と同じ形: キー=宛先の SHA-256、値={"subscription":{endpoint, keys}}
  const legacyOwner = () => [sha(OWNER), JSON.stringify({ subscription: { endpoint: OWNER, keys: OWNER_KEYS } })];
  const badPort = "https://fcm.googleapis.com:81/fcm/send/abc";
  const legacyJunk = () => [
    [sha(badPort), JSON.stringify({ subscription: { endpoint: badPort, keys: KEYS } })],  // 今の検査では通らない宛先
    [sha("broken"), "{not json"],
    [sha("no-sub"), JSON.stringify({ something: 1 })],
    [sha("no-keys"), JSON.stringify({ subscription: { endpoint: REAL.apple } })],
    ["memo", "64桁hexでないキーは購読ではない"],
  ];
  const ownerOnly = (r) => r.status === 200 && r.json.length === 1 && r.json[0].id === sha(OWNER) &&
    r.json[0].subscription.endpoint === OWNER && r.json[0].subscription.keys.p256dh === OWNER_KEYS.p256dh &&
    r.json[0].subscription.keys.auth === OWNER_KEYS.auth;

  // (a) 最初の要求が送信側の GET /subs
  let env = newEnv({ legacy: [legacyOwner(), ...legacyJunk()] });
  let r = await getSubs(env);
  ok(ownerOnly(r), "(a) 最初の GET /subs で、旧形式の持ち主の購読がそのまま返る(id も同じ)");
  ok(env.SUBS.used.list === 1, "(a) list は1回だけ使う");
  ok(JSON.stringify(storedTable(env)) === JSON.stringify({ [sha(OWNER)]: { endpoint: OWNER, keys: OWNER_KEYS } }),
    "(a) 表(table:v1)には持ち主の1件だけが入る(通らない宛先・壊れた値・購読でないキーは取り込まない)");
  ok(env.SUBS.m.has(sha(OWNER)) && env.SUBS.used.delete === 0, "(a) 旧キーは消さずに残す");
  const g0 = env.SUBS.used.get;
  r = await getSubs(env);
  ok(ownerOnly(r) && env.SUBS.used.list === 1 && env.SUBS.used.get === g0 + 1, "(a) 2回目以降は list を使わず、読み取り1回で返す");

  // (b) 最初の要求が、持ち主のアプリの確かめ直し
  env = newEnv({ legacy: [legacyOwner()] });
  r = await postSub(env, OWNER, { keys: OWNER_KEYS });
  ok(r.status === 200 && r.json.id === sha(OWNER), "(b) 持ち主のアプリが同じ購読を送ると 200(取り込み済みとして扱う)");
  ok(ownerOnly(await getSubs(env)) && env.SUBS.used.list === 1, "(b) 表に持ち主の購読がある");

  // (c) 最初の要求が、別の端末の新規登録
  env = newEnv({ legacy: [legacyOwner()] });
  r = await postSub(env, REAL.apple);
  ok(r.status === 201, "(c) 別の端末の新規登録は 201");
  ok(env.SUBS.used.put === 1, "(c) 取り込みと登録を合わせても、表への書き込みは1回(KV は同じキーへの書き込みが1秒に1回まで)");
  r = await getSubs(env);
  ok(r.json.length === 2 && r.json[0].subscription.endpoint === OWNER && r.json[1].subscription.endpoint === REAL.apple &&
    env.SUBS.used.list === 1, "(c) 持ち主の購読は上書きされず、2件とも一覧にある");

  // (d) 最初の要求が、持ち主の解除
  env = newEnv({ legacy: [legacyOwner()] });
  r = await postUnsub(env, OWNER);
  ok(r.status === 200 && (await getSubs(env)).json.length === 0, "(d) 持ち主が解除すると一覧から消える");
  ok((await getSubs(env)).json.length === 0 && env.SUBS.used.list === 1, "(d) 旧キーが残っていても、取り込み直して復活することはない");

  // (e) 旧形式が何も無い(まっさらな KV)
  env = newEnv();
  r = await getSubs(env);
  ok(r.status === 200 && r.json.length === 0 && env.SUBS.m.get(TABLE_KEY) === "{}", "(e) 取り込む物が無くても空の表を作る");
  await getSubs(env);
  await postSub(env, REAL.fcm);
  ok(env.SUBS.used.list === 1, "(e) …以後は list を使わない");

  // (f) list が失敗する(無料枠切れなど)
  env = newEnv({ legacy: [legacyOwner()] });
  env.SUBS.fail.list = true;
  r = await getSubs(env);
  ok(r.status === 503, "(f) 取り込みが済まず返せる購読が無い間、GET /subs は 503(空の一覧を 200 で返すと送信側が「購読ゼロ=送信済み」と記録してしまう)");
  ok(!env.SUBS.m.has(TABLE_KEY), "(f) この時点では表を作らない(取り込み済みにしない)");
  r = await postSub(env, REAL.apple);
  ok(r.status === 201, "(f) list が失敗している間も、新規登録は 201 で受け付ける");
  ok(storedTable(env).legacy_pending === true, "(f) 表に「取り込み未完了」の印が付く");
  r = await getSubs(env);
  ok(r.status === 200 && r.json.length === 1 && r.json[0].subscription.endpoint === REAL.apple, "(f) 印は購読として返さない");
  env.SUBS.fail.list = false;
  r = await getSubs(env);
  ok(r.status === 200 && r.json.length === 2 && r.json.some((e) => e.id === sha(OWNER) && e.subscription.keys.auth === OWNER_KEYS.auth) &&
    r.json.some((e) => e.subscription.endpoint === REAL.apple), "(f) list が回復した次の要求で、持ち主の購読が取り込まれる(失われない)");
  ok(storedTable(env).legacy_pending === undefined && Object.keys(storedTable(env)).length === 2, "(f) 取り込みが済むと印が消える");
  const l0 = env.SUBS.used.list;
  await getSubs(env);
  await postSub(env, REAL.mozilla);
  ok(env.SUBS.used.list === l0, "(f) 取り込みが済んだ後は list を使わない");

  // (g) 旧キーの読み取りが失敗する
  env = newEnv({ legacy: [legacyOwner()] });
  env.SUBS.fail.getKey = (k) => k === sha(OWNER);
  r = await getSubs(env);
  ok(r.status === 503 && !env.SUBS.m.has(TABLE_KEY), "(g) 旧キーの読み取りが失敗した時は 503 で応え、取り込み済みにもしない");
  env.SUBS.fail.getKey = null;
  ok(ownerOnly(await getSubs(env)), "(g) 次の要求で持ち主の購読が取り込まれる");

  // (h) 取り込んだ表の書き込みが失敗する(書き込みの枠切れ)
  env = newEnv({ legacy: [legacyOwner()] });
  env.SUBS.fail.put = true;
  r = await getSubs(env);
  ok(ownerOnly(r), "(h) 表を書けなくても、その要求には取り込んだ内容(持ち主の購読)で応える");
  env.SUBS.fail.put = false;
  ok(ownerOnly(await getSubs(env)) && env.SUBS.m.has(TABLE_KEY), "(h) 次の要求でやり直して表ができる");

  // (i) 旧形式が上限より多い
  const lots = Array.from({ length: 150 }, (_, i) => {
    const ep = "https://web.push.apple.com/legacy-" + i;
    return [sha(ep), JSON.stringify({ subscription: { endpoint: ep, keys: KEYS } })];
  });
  env = newEnv({ legacy: lots });
  r = await getSubs(env);
  ok(r.status === 200 && r.json.length === MAX_SUBS && env.SUBS.used.list === 1, "(i) 旧形式が 150 件あっても、取り込むのは上限の 100 件まで");
});

// =====================================================================================
await section("7. 送信側の署名", async () => {
  const env = newEnv({ table: {} });
  await postSub(env, REAL.fcm);
  const good = await sign(K, HDR, goodClaims());
  const authStatus = async (auth, opts = {}) => (await call(env, opts.method || "GET", opts.path || "/subs", { auth, origin: null, base: opts.base })).status;

  ok((await authStatus(`vapid t=${good.tok},k=${K.pub}`)) === 200, "通す: vapid t=…,k=…(送信側 py_vapid の形)");
  ok((await authStatus(`Bearer ${good.tok}`)) === 200, "通す: Bearer …");
  ok((await authStatus(`vapid t=${good.tok},k=${EVIL.pub}`)) === 200, "通す: k= が別の鍵でも、検証に使うのは Worker の VAPID_PUB だけ");
  ok((await authStatus(`vapid t=${good.tok},k=${K.pub}`, { method: "DELETE", path: "/sub?id=" + sha("none") })) === 200, "通す: 同じ署名で DELETE");
  ok((await authStatus(`Bearer ${(await sign(K, HDR, { aud: WORKER, exp: nowSec() + 900 })).tok}`)) === 200, "通す: 期限がちょうど15分後");

  const before = snap(env);
  const evilTok = await sign(EVIL, HDR, goodClaims());
  const enc = (o) => b64u(JSON.stringify(o));
  const hs = (() => {
    const h = enc({ alg: "HS256", typ: "JWT" });
    const p = enc(goodClaims());
    return `${h}.${p}.${b64u(createHmac("sha256", Buffer.from(K.raw)).update(h + "." + p).digest())}`;
  })();
  const REJECT = [
    ["Authorization なし", undefined],
    ["空のトークン", "Bearer "],
    ["alg none・署名なし", `Bearer ${enc({ alg: "none" })}.${enc(goodClaims())}.`],
    ["alg HS256(公開鍵を共有鍵として使う取り違え)", `Bearer ${hs}`],
    ["alg が小文字 es256", `Bearer ${(await sign(K, { alg: "es256" }, goodClaims())).tok}`],
    ["alg ES384", `Bearer ${(await sign(K, { alg: "ES384" }, goodClaims())).tok}`],
    ["第三者の鍵で署名(k= に自分の公開鍵)", `vapid t=${evilTok.tok},k=${EVIL.pub}`],
    ["第三者の鍵で署名(Bearer)", `Bearer ${evilTok.tok}`],
    ["本文を差し替え(署名は元のまま)", `Bearer ${good.h}.${enc({ aud: WORKER, exp: nowSec() + 600 })}.${b64u(good.sig)}`],
    ["署名が 63 バイト", `Bearer ${good.h}.${good.p}.${b64u(good.sig.slice(0, 63))}`],
    ["署名が全部 0", `Bearer ${good.h}.${good.p}.${b64u(Buffer.alloc(64))}`],
    ["署名が空", `Bearer ${good.h}.${good.p}.`],
    ["aud が別のサイト", `Bearer ${(await sign(K, HDR, { aud: "https://other.example", exp: nowSec() + 300 })).tok}`],
    ["aud が配信サービス(pywebpush が配信サービスへ送る形)", `Bearer ${(await sign(K, HDR, { aud: "https://fcm.googleapis.com", exp: nowSec() + 300 })).tok}`],
    ["aud の末尾にスラッシュ", `Bearer ${(await sign(K, HDR, { aud: WORKER + "/", exp: nowSec() + 300 })).tok}`],
    ["aud が配列", `Bearer ${(await sign(K, HDR, { aud: [WORKER], exp: nowSec() + 300 })).tok}`],
    ["aud なし", `Bearer ${(await sign(K, HDR, { exp: nowSec() + 300 })).tok}`],
    ["期限切れ", `Bearer ${(await sign(K, HDR, { aud: WORKER, exp: nowSec() - 1 })).tok}`],
    ["期限が15分より先", `Bearer ${(await sign(K, HDR, { aud: WORKER, exp: nowSec() + 960 })).tok}`],
    ["期限が12時間後(pywebpush の既定)", `Bearer ${(await sign(K, HDR, { aud: WORKER, exp: nowSec() + 43200 })).tok}`],
    ["期限が文字列", `Bearer ${(await sign(K, HDR, { aud: WORKER, exp: String(nowSec() + 300) })).tok}`],
    ["期限なし", `Bearer ${(await sign(K, HDR, { aud: WORKER })).tok}`],
    ["本文が null", `Bearer ${(await sign(K, HDR, "bnVsbA")).tok}`],
    ["bearer が小文字", `bearer ${good.tok}`],
    ["Vapid の V が大文字", `Vapid t=${good.tok},k=${K.pub}`],
    ["k= が先", `vapid k=${K.pub},t=${good.tok}`],
    ["4つに区切られたトークン", `Bearer ${good.tok}.abc`],
    ["Bearer の後ろに余計な文字", `Bearer ${good.tok} x`],
  ];
  for (const [label, auth] of REJECT) ok((await authStatus(auth)) === 401, "通さない: " + label);
  ok((await authStatus(`Bearer ${evilTok.tok}`, { method: "DELETE", path: "/sub?id=" + sha(REAL.fcm) })) === 401, "通さない: 第三者の鍵での DELETE");
  ok((await authStatus(`Bearer ${good.tok}`, { base: "https://abcd1234-aritei-push.t-fujino.workers.dev" })) === 401, "通さない: 正しい署名を別のアドレス(版ごとのプレビューURL)で使う");
  ok((await authStatus(`Bearer ${good.tok}`, { base: WORKER.replace("https:", "http:") })) === 401, "通さない: 正しい署名を http:// で使う");
  ok(sameUse(snap(env), before), "署名が通らない要求は KV に触れない");
  ok((await getSubs(env)).json.length === 1, "通らなかった DELETE では消えていない");
  for (const [label, pub] of [["空", ""], ["壊れている", "!!!"], ["別の鍵", EVIL.pub]]) {
    const e2 = newEnv({ table: {}, pub });
    ok((await call(e2, "GET", "/subs", { auth: `Bearer ${good.tok}`, origin: null })).status === 401, "通さない: Worker の VAPID_PUB が" + label);
  }
});

// =====================================================================================
await section("8. 内部の失敗は 503", async () => {
  const clean503 = (r) => r.status === 503 && isJson(r) && noStore(r) && typeof r.json.error === "string" &&
    !r.text.includes("INTERNAL-DETAIL") && !r.text.includes("KV");
  let env = newEnv({ table: { [sha(REAL.fcm)]: { endpoint: REAL.fcm, keys: KEYS } } });
  env.SUBS.fail.get = true;
  let r = await postSub(env, REAL.apple);
  ok(clean503(r) && hasCors(r), "KV の読み取りが失敗 → POST /sub は 503(JSON・CORS ヘッダつき・内部情報なし)");
  r = await postUnsub(env, REAL.fcm);
  ok(clean503(r) && hasCors(r), "…POST /unsub も 503(アプリが応答を読める)");
  ok(clean503(await getSubs(env)), "…GET /subs も 503(空の一覧は返さない=送信側は「取れなかった」と分かる)");
  ok(clean503(await delSubs(env, [sha(REAL.fcm)])), "…DELETE も 503");
  env.SUBS.fail.get = false;
  env.SUBS.fail.put = true;
  ok(clean503(await postSub(env, REAL.apple)), "KV の書き込みが失敗 → 新規登録は 503");
  ok((await postSub(env, REAL.fcm)).status === 200, "…登録済みの確かめ直しは書き込まないので 200 のまま");
  r = await getSubs(env);
  ok(r.status === 200 && r.json.length === 1, "…書き込み・list・delete が使えなくても GET /subs は既存の購読を返す");

  for (const [label, raw] of [["JSON でない", "{{{"], ["配列", "[]"], ["null", "null"], ["文字列", '"x"']]) {
    env = newEnv({ table: raw });
    const a = await postSub(env, REAL.apple);
    const b = await getSubs(env);
    ok(clean503(a) && clean503(b) && env.SUBS.m.get(TABLE_KEY) === raw && env.SUBS.used.put === 0,
      "表が壊れている(" + label + ")→ 503。空の表で上書きしない");
  }
  env = newEnv({ table: {} });
  let threw = false;
  try {
    r = await worker.fetch({ method: "POST", url: "not a url", headers: new Headers({ Origin: APP }), text: async () => "{}" }, env, {});
  } catch (e) {
    threw = true;
  }
  ok(!threw && r.status === 503, "想定外の例外(URL が読めない等)も外へ出さず 503");
});

// =====================================================================================
await section("9. 本文の大きさと形", async () => {
  const env = newEnv({ table: {} });
  // Content-Length の申告だけを変えた要求を作る(本文そのものは正しい購読)
  const fakeReq = (len, state) => ({
    method: "POST",
    url: WORKER + "/sub",
    headers: new Headers({ Origin: APP, "Content-Type": "application/json", "Content-Length": String(len) }),
    text: async () => { state.read = true; return JSON.stringify(sub(REAL.fcm)); },
  });
  let state = { read: false };
  let res = await worker.fetch(fakeReq(5000000, state), env, {});
  ok(res.status === 400 && state.read === false, "Content-Length が上限(4096)超 → 本文を読まずに 400");
  state = { read: false };
  res = await worker.fetch(fakeReq(4097, state), env, {});
  ok(res.status === 400 && state.read === false, "Content-Length 4097 → 読まずに 400");
  state = { read: false };
  res = await worker.fetch(fakeReq(300, state), env, {});
  ok(res.status === 201 && state.read === true, "同じ本文で Content-Length が上限内 → 201(違いは申告の大きさだけ)");

  // 正しい購読に詰め物を足して、本文をちょうど n 文字にする
  const bodyOfLen = (n) => {
    const base = JSON.stringify({ subscription: { endpoint: REAL.apple, keys: KEYS }, pad: "" });
    return JSON.stringify({ subscription: { endpoint: REAL.apple, keys: KEYS }, pad: "x".repeat(n - base.length) });
  };
  ok(bodyOfLen(4096).length === 4096 && (await call(env, "POST", "/sub", { body: bodyOfLen(4096) })).status === 201, "本文 4096 文字ちょうどは通す");
  await postUnsub(env, REAL.apple);
  ok(bodyOfLen(4097).length === 4097 && (await call(env, "POST", "/sub", { body: bodyOfLen(4097) })).status === 400, "本文 4097 文字 → 400(読んだ後の長さでも確かめる)");
  ok((await call(env, "POST", "/sub", { body: "x".repeat(200000) })).status === 400, "大きな本文(20万文字)→ 400");

  const before = snap(env);
  const BAD_BODIES = [
    ["JSON でない", "{oops"],
    ["空", ""],
    ["配列", [1, 2]],
    ["文字列", '"x"'],
    ["null", "null"],
    ["subscription なし", { endpoint: REAL.apple, keys: KEYS }],
    ["subscription が文字列", { subscription: REAL.apple }],
    ["keys なし", { subscription: { endpoint: REAL.apple } }],
    ["keys が配列", { subscription: { endpoint: REAL.apple, keys: ["a", "b"] } }],
    ["keys が文字列", { subscription: { endpoint: REAL.apple, keys: "ab" } }],
    ["p256dh が数値", { subscription: { endpoint: REAL.apple, keys: { p256dh: 1, auth: "a" } } }],
    ["auth なし", { subscription: { endpoint: REAL.apple, keys: { p256dh: "a" } } }],
    ["p256dh が 201 文字", { subscription: { endpoint: REAL.apple, keys: { p256dh: "a".repeat(201), auth: "a" } } }],
    ["auth が 101 文字", { subscription: { endpoint: REAL.apple, keys: { p256dh: "a", auth: "a".repeat(101) } } }],
  ];
  for (const [label, body] of BAD_BODIES) ok((await call(env, "POST", "/sub", { body })).status === 400, "POST /sub 400: " + label);
  for (const [label, body] of [["JSON でない", "{oops"], ["endpoint なし", {}], ["endpoint が配列", { endpoint: [REAL.fcm] }], ["null", "null"]]) {
    ok((await call(env, "POST", "/unsub", { body })).status === 400, "POST /unsub 400: " + label);
  }
  ok(sameUse(snap(env), before), "形が不正な本文は KV に触れる前に断る");

  // 余計な項目は保存しない
  const r = await call(env, "POST", "/sub", {
    body: '{"subscription":{"endpoint":"' + REAL.mozilla + '","expirationTime":null,"evil":{"a":1},"__proto__":{"polluted":1},' +
      '"keys":{"p256dh":"p","auth":"a","extra":"x","__proto__":{"polluted":1}}},"other":1}',
  });
  ok(r.status === 201 && JSON.stringify(storedTable(env)[sha(REAL.mozilla)]) === JSON.stringify({ endpoint: REAL.mozilla, keys: { p256dh: "p", auth: "a" } }),
    "余計な項目(expirationTime など)は捨て、endpoint と keys だけを保存する");
  ok(({}).polluted === undefined, "__proto__ を使った汚染は起きない");
});

// =====================================================================================
await section("10. 保管済みの表の検査・同時登録・設定ファイル", async () => {
  // 表の中に、今の検査を通らない物が混ざっている場合(直す前に登録された宛先や、手で書き換えた値)
  const good = { endpoint: REAL.fcm, keys: KEYS };
  const env = newEnv({
    table: {
      [sha(REAL.fcm)]: good,
      [sha("port")]: { endpoint: "https://fcm.googleapis.com:81/fcm/send/abc", keys: KEYS },
      [sha("user")]: { endpoint: "https://evil.example@fcm.googleapis.com/x", keys: KEYS },
      [sha("sub")]: { endpoint: "https://anything.fcm.googleapis.com/x", keys: KEYS },
      [sha("nokeys")]: { endpoint: REAL.apple },
      [sha("str")]: "https://fcm.googleapis.com/x",
      [sha("null")]: null,
      "not-an-id": { endpoint: REAL.mozilla, keys: KEYS },
    },
  });
  let r = await getSubs(env);
  ok(r.status === 200 && r.json.length === 1 && r.json[0].subscription.endpoint === REAL.fcm,
    "GET /subs は、保管済みの各件を同じ検査に通し、通らない物を返さない");
  ok(env.SUBS.used.put === 0 && env.SUBS.used.list === 0, "…読むだけなら書き込まない");
  await postSub(env, REAL.apple);
  ok(JSON.stringify(Object.keys(storedTable(env))) === JSON.stringify([sha(REAL.fcm), sha(REAL.apple)]), "…次に表を書く時、通らない物は表から消える");

  // 既知の弱点: ほぼ同時の登録は後から書いた方が残る。アプリが送り直せば戻る。
  const m = new Map([[TABLE_KEY, "{}"]]);
  const tick = () => new Promise((res) => setImmediate(res));
  const slow = { VAPID_PUB: K.pub, SUBS: { async get(k) { await tick(); return m.has(k) ? m.get(k) : null; }, async put(k, v) { await tick(); m.set(k, v); } } };
  const eps = [1, 2, 3, 4, 5].map((i) => "https://web.push.apple.com/device-" + i);
  const sts = await Promise.all(eps.map((ep) => postSub(slow, ep)));
  const left = Object.keys(JSON.parse(m.get(TABLE_KEY))).length;
  console.log("    5件を同時に登録 → 応答 " + sts.map((x) => x.status).join(",") + " / 残った件数 " + left + "(同時だと後から書いた方だけが残ることがある)");
  for (const ep of eps) await postSub(slow, ep);
  ok(Object.keys(JSON.parse(m.get(TABLE_KEY))).length === 5, "同時登録で消えた分は、各端末が送り直す(アプリ起動時の確かめ直し)と5件とも戻る");

  // wrangler.toml: 既存の設定を変えていないことと、プレビューURLの無効化
  const toml = readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8").replace(/\r\n/g, "\n");
  const top = toml.split(/^\[/m)[0];  // 最初の [表] より前(トップレベルの設定)
  ok(/^preview_urls = false$/m.test(top), "wrangler.toml: preview_urls = false がトップレベルにある");
  ok(/^name = "aritei-push"$/m.test(top) && /^main = "worker\.js"$/m.test(top) && /^workers_dev = true$/m.test(top) &&
    top.includes('{ binding = "SUBS", id = "baa7450161984da8b931e8a449bfec4d" }'), "wrangler.toml: name / main / workers_dev / KV の設定は元のまま");
  ok(/^\[vars\]\nVAPID_PUB = "BBwe0VKS6ZoMa4CDjXDpKHvp5xPR9okvoPQCQGQfZdmX8JcIBAls-FuE3qOGFd7b2dPbxI9T0XWYHsJDqvby3hQ"$/m.test(toml),
    "wrangler.toml: [vars] VAPID_PUB は元のまま");
});

console.log(`\n${total} 項目を確認、失敗 ${failed}`);
process.exit(failed ? 1 : 0);

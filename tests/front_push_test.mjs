// アプリ側(docs/index.html と docs/sw.js)のプッシュ通知まわりを、Node だけで確かめる。
// ブラウザも通信も使わない。index.html のインラインスクリプトと sw.js から該当の部分を取り出し、
// 偽のブラウザ環境(偽の画面要素・購読・通信・時計)の上で場面を再現する。
//
// 実行: node tests/front_push_test.mjs
//   リポジトリのどこから実行してもよい。全部通れば終了コード0、1つでも落ちれば1。
//   取り出しの目印(関数名や宣言)が index.html から消えると「目印が見つからない」で止まる。
//
// 場面の一覧:
//   S  構文と定数(インラインスクリプト全体が構文エラーにならないこと、公開鍵、CACHE)
//   F  設定タブの基本の流れ(登録・失敗・拒否・鍵の入れ替え・解除・iPhone)
//   A〜I  登録の同期(起動時に登録し直す、失効した購読の取り直し、ready が返らない時 など)
//   T  弱点を突く場面(圏外での誤警報、通信が返らない、未設定の端末、解除の失敗 など)
//   N  新旧の Worker の応答、行き違い、案内文、時計のずれ、localStorage が使えない端末
//   P  通知タップで開いた時(?from=push と push-click)と、起動時の同期の呼び出し
//   W  sw.js(push の表示、通知タップで前面に出す相手、オフライン時の ?from=push)
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const html = fs.readFileSync(path.join(root, "docs", "index.html"), "utf8");
const swSrc = fs.readFileSync(path.join(root, "docs", "sw.js"), "utf8");
const script = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));

let passed = 0, failed = 0;
function check(name, cond, detail) {
  if (cond) { passed++; console.log("ok   " + name); }
  else { failed++; console.log("NG   " + name + (detail !== undefined ? "\n       " + detail : "")); }
}
function eq(name, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  check(name, g === w, "実際: " + g + " / 期待: " + w);
}
process.on("unhandledRejection", (e) => { failed++; console.log("NG   捕まえていない失敗: " + (e && e.stack || e)); });

function cut(a, b) {
  const s = script.indexOf(a), e = script.indexOf(b, s);
  if (s < 0 || e < 0) throw new Error("index.html に目印が見つからない: " + (s < 0 ? a : b));
  return script.slice(s, e);
}
const consts = cut("const PUSH_WORKER=", "const esc=");
const escDef = cut("const esc=", "const laneChip=");
const pushFns = cut("function _isIOSDevice()", "async function show(tab)");
const startup = cut("let _pushClickTm=[];", "let refreshBusy=false;");
const VAPID_PUB = consts.match(/const VAPID_PUB="([^"]+)"/)[1];
const WORKER = consts.match(/const PUSH_WORKER="([^"]*)"/)[1];
const KEY = new Uint8Array(Buffer.from(VAPID_PUB, "base64url"));
const OLD_KEY = new Uint8Array(65).fill(7); OLD_KEY[0] = 4;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ANDROID = { ua: "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/130 Mobile Safari/537.36", platform: "Linux armv8l" };
const IPHONE = { ua: "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1", platform: "iPhone" };

// ---- 偽のブラウザ環境(設定タブのプッシュ通知セクション用) ----
// opts: ua / platform / standalone / hasPush / permission / requestResult / existing:{endpoint,key} /
//       fetchImpl / neverReady / fastTimeout / unsubFails / subscribeFails / lsBroken
// shared: 別の環境と端末の状態(購読・localStorage)を共有する時に渡す(アプリを開き直した場面)
function makeEnv(opts = {}, shared) {
  const els = {};
  const drop = (id) => { for (const k of Object.keys(els)) if (els[k] && els[k]._parent === id) { drop(k); delete els[k]; } };
  const mkEl = (id) => {
    const el = { id, _html: "", textContent: "", onclick: null, disabled: false, style: {} };
    Object.defineProperty(el, "innerHTML", {
      get() { return this._html; },
      set(v) {
        this._html = v;
        drop(id); // 中身を書き換えたら、中にあった要素は消える
        const re = /id="([^"]+)"/g; let m;
        while ((m = re.exec(v))) { const c = mkEl(m[1]); c._parent = id; els[m[1]] = c; }
      },
    });
    return el;
  };
  els.pushSec = mkEl("pushSec");
  const document = { getElementById: (id) => els[id] || null };
  const state = shared || { sub: null, fetchCalls: [], bodies: [], subscribeCalls: 0, requestCalls: 0, store: {}, shown: [], epSeq: 0, now: null };
  const mkSub = (key, endpoint) => ({
    endpoint: endpoint || "https://fcm.googleapis.com/fcm/send/new" + (++state.epSeq),
    options: key ? { applicationServerKey: new Uint8Array(key).buffer } : undefined,
    toJSON() { return { endpoint: this.endpoint, expirationTime: null, keys: { p256dh: "P", auth: "A" } }; },
    unsubscribe: async function () { if (opts.unsubFails) throw new Error("unsub failed"); if (state.sub === this) state.sub = null; return true; },
  });
  if (opts.existing) state.sub = mkSub(opts.existing.key, opts.existing.endpoint);
  const reg = {
    showNotification: async (t, o) => { if (opts.showFails) throw new TypeError("no permission"); state.shown.push({ title: t, opt: o }); },
    pushManager: {
      getSubscription: async () => state.sub,
      subscribe: async (o) => {
        state.subscribeCalls++;
        if (opts.subscribeFails) throw new Error("NotAllowedError");
        state.sub = mkSub(o.applicationServerKey);
        return state.sub;
      },
    },
  };
  const navigator = {
    userAgent: opts.ua || ANDROID.ua, platform: opts.platform || ANDROID.platform, maxTouchPoints: 5, standalone: !!opts.standalone,
    serviceWorker: { ready: opts.neverReady ? new Promise(() => {}) : Promise.resolve(reg) },
  };
  const Notification = {
    permission: opts.permission || "default",
    requestPermission: async () => { state.requestCalls++; Notification.permission = env.requestResult || "granted"; return Notification.permission; },
  };
  const window = { navigator, matchMedia: () => ({ matches: !!opts.standalone }), Notification };
  if (opts.hasPush !== false) window.PushManager = function () {};
  const localStorage = {
    getItem: (k) => { if (opts.lsBroken) throw new Error("ls"); return k in state.store ? state.store[k] : null; },
    setItem: (k, v) => { if (opts.lsBroken) throw new Error("ls"); state.store[k] = String(v); },
    removeItem: (k) => { if (opts.lsBroken) throw new Error("ls"); delete state.store[k]; },
  };
  const env = { els, state, Notification, fetchImpl: opts.fetchImpl || null, requestResult: opts.requestResult || "granted" };
  const fetch = async (url, init) => {
    state.fetchCalls.push(init.method + " " + url.replace(WORKER, ""));
    state.bodies.push(init.body);
    return env.fetchImpl ? env.fetchImpl(url, init) : { ok: true, status: 201 };
  };
  const ctx = vm.createContext({
    document, navigator, window, Notification, fetch, localStorage, atob, Uint8Array, URL, AbortController, console,
    Date: { now: () => (state.now != null ? state.now : Date.now()) },
    setTimeout: (f, ms) => setTimeout(f, opts.fastTimeout ? Math.min(ms, 30) : ms), clearTimeout,
  });
  vm.runInContext(consts + "\n" + escDef + "\n" + pushFns + "\nthis.__api={renderPushSection,_pushSync,_pushSubscribe,_pushUnsubscribe,urlBase64ToUint8Array};", ctx);
  env.api = ctx.__api;
  env.ls = () => JSON.parse(state.store.aritei_push || "null");
  // 設定タブを開き直す
  env.reopen = async () => { els.pushSec.innerHTML = ""; await env.api.renderPushSection(); };
  // 画面に出ている文字(タグを除く)。途中で書き足した結果の行(pushMsg)も含める
  env.text = () => ((els.pushBody && els.pushBody.innerHTML ? els.pushBody.innerHTML : els.pushSec.innerHTML).replace(/<[^>]+>/g, " ")
    + " " + (els.pushMsg ? els.pushMsg.textContent : "")).replace(/\s+/g, " ").trim();
  env.posts = (p) => state.fetchCalls.filter((c) => c === "POST " + p).length;
  return env;
}
const ON = "通知は有効です", DEAD = "このままでは通知が届きません", OFFMSG = "通知がオフ";
const withLimit = (p, ms) => Promise.race([p.then(() => "done"), new Promise((r) => setTimeout(() => r("まだ終わらない"), ms))]);
const offline = async () => { throw new TypeError("Failed to fetch"); };
const status = (s) => async () => ({ ok: s >= 200 && s < 300, status: s });

// ================= S: 構文と定数 =================
{
  let err = "";
  try { new vm.Script(script, { filename: "docs/index.html(inline script)" }); } catch (e) { err = String(e && e.stack || e); }
  check("S1 index.html のインラインスクリプト全体が構文エラーにならない", !err, err);
  check("S1 インラインスクリプトは1つだけ(取り出し範囲が全体であること)", (html.match(/<script[\s>]/g) || []).length === 1);
  err = "";
  try { new vm.Script(swSrc, { filename: "docs/sw.js" }); } catch (e) { err = String(e && e.stack || e); }
  check("S2 sw.js が構文エラーにならない", !err, err);
  check("S2 sw.js の CACHE が kyotei-ai-v<番号> の形", /^const CACHE = "kyotei-ai-v\d+";/.test(swSrc));
  eq("S3 公開鍵は65バイト・先頭0x04", [KEY.length, KEY[0]], [65, 4]);
  const toml = path.join(root, "push-worker", "wrangler.toml");
  if (fs.existsSync(toml)) {
    const m = fs.readFileSync(toml, "utf8").match(/VAPID_PUB\s*=\s*"([^"]+)"/);
    check("S3 公開鍵が push-worker/wrangler.toml と同じ", !!m && m[1] === VAPID_PUB);
  }
  check("S4 PUSH_WORKER は https のURL", /^https:\/\/[a-z0-9.-]+$/.test(WORKER), WORKER);
  check("S5 起動タブを決める箇所は refreshBusy の宣言より前にある(だから setTimeout 経由が必要)",
    script.indexOf("let _pushClickTm=[];") < script.indexOf("let refreshBusy=false;"));
}

// ================= F: 設定タブの基本の流れ =================
// F1) 正常系(Android Chrome): 押す → 許可 → 購読 → Worker へ登録 →「通知は有効です」
{
  const env = makeEnv();
  await env.api.renderPushSection();
  check("F1 初期表示に「通知を受け取る」ボタン", !!env.els.pushOn && !env.text().includes(ON), env.text());
  check("F1 何がいつ届くかの説明(1レースにつき確定と結果の2回・異常のお知らせ)",
    env.text().includes("1レースにつき確定と結果の2回") && env.text().includes("データ更新の異常"), env.text());
  await env.els.pushOn.onclick();
  check("F1 押した後は「通知は有効です」", env.text().includes(ON), env.text());
  eq("F1 Worker への通信は POST /sub の1回", env.state.fetchCalls, ["POST /sub"]);
  const sent = JSON.parse(env.state.bodies[0]);
  eq("F1 送る本文は {subscription:{endpoint,expirationTime,keys:{p256dh,auth}}}", sent,
    { subscription: { endpoint: env.state.sub.endpoint, expirationTime: null, keys: { p256dh: "P", auth: "A" } } });
  check("F1 登録できた宛先と時刻を端末に控える", env.ls().on === 1 && env.ls().ep === env.state.sub.endpoint && env.ls().t > 0, JSON.stringify(env.ls()));
  eq("F1 許可の要求は1回", env.state.requestCalls, 1);
  check("F1 有効時は「テスト通知を表示」「通知を止める」が出る", !!env.els.pushTest && !!env.els.pushOff);
  check("F1 テスト通知は端末の設定までの確認だと書いてある", env.text().includes("この端末の通知設定までを確かめるもの") && env.text().includes("実際の送信"), env.text());
}
// F2) 登録の POST が失敗(507=満杯 / 回線断)→ 有効と出さない → 開き直すと登録し直す → 復旧したら有効
for (const [label, impl, diag] of [["507", status(507), "応答507"], ["回線断", offline, "通信できず"]]) {
  const env = makeEnv({ fetchImpl: impl });
  await env.api.renderPushSection();
  await env.els.pushOn.onclick();
  check(`F2(${label}) 失敗直後は「有効」と出さず、届かない状態だと出す`, !env.text().includes(ON) && env.text().includes(DEAD), env.text());
  check(`F2(${label}) やり直しのボタンが出る`, !!env.els.pushRetry);
  check(`F2(${label}) 診断用の表示に理由と宛先のホスト`, env.text().includes(diag) && env.text().includes("fcm.googleapis.com"), env.text());
  check(`F2(${label}) 端末側の購読は残っている`, !!env.state.sub);
  const before = env.posts("/sub");
  await env.reopen();
  check(`F2(${label}) 開き直すと Worker へ登録し直す`, env.posts("/sub") === before + 1);
  check(`F2(${label}) まだ失敗なら、開き直しても「有効」と出さない`, !env.text().includes(ON) && env.text().includes(DEAD), env.text());
  env.fetchImpl = null; // Worker 復旧
  await env.els.pushRetry.onclick();
  check(`F2(${label}) 復旧後に「もう一度確認する」で有効になる`, env.text().includes(ON), env.text());
}
// F3) 端末に購読があるのに Worker 側だけ消えている(掃除・KV消失)。設定タブを開くと登録し直す
{
  const env = makeEnv({ existing: { endpoint: "https://web.push.apple.com/xyz", key: KEY }, permission: "granted" });
  await env.api.renderPushSection();
  check("F3 表示は「通知は有効です」", env.text().includes(ON), env.text());
  eq("F3 Worker へ登録し直している", env.state.fetchCalls, ["POST /sub"]);
  eq("F3 購読は取り直さない", env.state.subscribeCalls, 0);
}
// F4) 許可が拒否済み: ボタンではなく直し方の案内を出す
{
  const env = makeEnv({ permission: "denied", requestResult: "denied" });
  await env.api.renderPushSection();
  check("F4 拒否済みなら「通知を受け取る」を出さず、オフだと知らせる", !env.els.pushOn && env.text().includes(OFFMSG), env.text());
  check("F4 「もう一度確認する」が出る", !!env.els.pushRetry);
  eq("F4 許可の要求も通信もしない", [env.state.requestCalls, env.state.fetchCalls.length], [0, 0]);
  env.Notification.permission = "default"; // 端末の設定を直した
  await env.els.pushRetry.onclick();
  check("F4 設定を直して「もう一度確認する」→「通知を受け取る」が出る", !!env.els.pushOn, env.text());
}
// F5) 公開鍵が変わった(古い鍵で作った購読が端末に残っている)→ 取り直して登録し直す
{
  const env = makeEnv({ existing: { endpoint: "https://fcm.googleapis.com/fcm/send/old", key: OLD_KEY }, permission: "granted" });
  await env.api.renderPushSection();
  check("F5 表示は「通知は有効です」", env.text().includes(ON), env.text());
  eq("F5 購読の取り直しは1回", env.state.subscribeCalls, 1);
  eq("F5 新しい宛先の登録は1回", env.posts("/sub"), 1);
  const i = env.state.fetchCalls.indexOf("POST /sub");
  check("F5 登録したのは新しい宛先", JSON.parse(env.state.bodies[i]).subscription.endpoint === env.state.sub.endpoint && !/old$/.test(env.state.sub.endpoint));
  const j = env.state.fetchCalls.indexOf("POST /unsub");
  check("F5 古い宛先は Worker から消すよう知らせる", j >= 0 && JSON.parse(env.state.bodies[j]).endpoint === "https://fcm.googleapis.com/fcm/send/old");
}
// F6) 権限は拒否だが購読オブジェクトは残っている → 有効とは出さない
{
  const env = makeEnv({ existing: { endpoint: "https://web.push.apple.com/xyz", key: KEY }, permission: "denied" });
  await env.api.renderPushSection();
  check("F6 権限が拒否なら、購読が残っていても「有効」と出さない", !env.text().includes(ON) && env.text().includes(OFFMSG), env.text());
}
// F7) 解除: Worker が落ちていても端末側は止まる
{
  const env = makeEnv({ existing: { endpoint: "https://fcm.googleapis.com/fcm/send/a", key: KEY }, permission: "granted", fetchImpl: offline });
  await env.api.renderPushSection(); // 登録を確認できない状態(届かない表示)でも「通知を止める」は出る
  check("F7 届かない状態の表示にも「通知を止める」がある", !!env.els.pushOff && env.text().includes(DEAD), env.text());
  await env.els.pushOff.onclick();
  await sleep(5);
  check("F7 Worker 不通でも端末側の購読は解除される", env.state.sub === null);
  check("F7 表示は「通知を受け取る」に戻る", !!env.els.pushOn && !env.text().includes("止められません"), env.text());
}
// F8) iPhone: Safari のタブ(ホーム画面に未追加)/ 全画面だが非対応 / 全画面で対応
{
  let env = makeEnv({ ...IPHONE, standalone: false, hasPush: false });
  await env.api.renderPushSection();
  check("F8a iPhone の Safari タブ: ホーム画面に追加する案内", env.text().includes("ホーム画面に追加") && !env.els.pushBody, env.text());
  env = makeEnv({ ...IPHONE, standalone: true, hasPush: false });
  await env.api.renderPushSection();
  check("F8b iPhone 全画面・非対応(16.3以前): 受け取れない理由と対処", env.text().includes("このブラウザでは通知を受け取れません") && env.text().includes("iOS 16.4以降"), env.text());
  eq("F8b 非対応の端末は起動時の同期でも何もしない", [await env.api._pushSync(false), env.state.fetchCalls.length], ["off", 0]);
  env = makeEnv({ ...IPHONE, standalone: true });
  await env.api.renderPushSection();
  check("F8c iPhone 全画面・対応: 「通知を受け取る」が出る", !!env.els.pushOn, env.text());
}

// ================= A〜I: 登録の同期 =================
// A) 正常系とテスト通知
{
  const env = makeEnv();
  await env.api.renderPushSection();
  await env.els.pushOn.onclick();
  check("A2 押した後は有効", env.text().includes(ON));
  eq("A3 通信", env.state.fetchCalls, ["POST /sub"]);
  await env.els.pushTest.onclick();
  eq("A4 テスト通知が1件出る(tag は aritei-test)", env.state.shown.map((s) => s.title + "/" + s.opt.tag), ["厳選プラン確定 from アリテイ/aritei-test"]);
  check("A4 テスト通知の本文と、押した後の結果の表示", env.state.shown[0].opt.body.includes("下関5R 締切17:19") && env.state.shown[0].opt.body.includes("的中率") && env.text().includes("テスト通知を表示しました"), env.text());
  eq("A4 テスト通知では Worker へ通信しない", env.state.fetchCalls, ["POST /sub"]);
}
// B) 登録の POST が失敗 → 正直な表示 → 次の起動時に自動で登録し直す
{
  const env = makeEnv({ fetchImpl: status(507) });
  await env.api.renderPushSection();
  await env.els.pushOn.onclick();
  check("B1 失敗直後は届かない状態の表示", env.text().includes(DEAD), env.text());
  env.fetchImpl = null; // Worker 復旧
  eq("B2 次の起動時の同期で登録できる", await env.api._pushSync(false), "on");
  eq("B2 通信は失敗1回+やり直し1回", env.state.fetchCalls, ["POST /sub", "POST /sub"]);
  await env.reopen();
  check("B3 設定を開き直すと有効", env.text().includes(ON), env.text());
}
// C) 今のコードで購読済みの端末(控え無し): 起動時に登録し直し、直後の2回目は通信しない
{
  const env = makeEnv({ existing: { endpoint: "https://web.push.apple.com/xyz", key: KEY }, permission: "granted" });
  eq("C1 起動時の同期(初回)", [await env.api._pushSync(false), env.state.fetchCalls], ["on", ["POST /sub"]]);
  eq("C2 直後の2回目は通信しない(10分に1回まで)", [await env.api._pushSync(false), env.state.fetchCalls.length], ["on", 1]);
  eq("C3 購読は取り直さない", env.state.subscribeCalls, 0);
  eq("C4 設定タブを開いた時は毎回確かめる", [await env.api._pushSync(true), env.state.fetchCalls.length], ["on", 2]);
}
// D) 公開鍵が変わった: 起動時の同期で取り直す
{
  const env = makeEnv({ existing: { endpoint: "https://fcm.googleapis.com/fcm/send/old", key: OLD_KEY }, permission: "granted" });
  eq("D 鍵が違う購読は起動時に取り直して登録", [await env.api._pushSync(false), env.state.subscribeCalls, env.posts("/sub")], ["on", 1, 1]);
  check("D 新しい宛先になっている", /new1$/.test(env.state.sub.endpoint), env.state.sub.endpoint);
  const env2 = makeEnv({ existing: { endpoint: "https://web.push.apple.com/k", key: null }, permission: "granted" });
  eq("D 鍵を照合できない端末(options 無し)は取り直さない", [await env2.api._pushSync(false), env2.state.subscribeCalls], ["on", 0]);
  for (const [label, odd] of [["文字列", VAPID_PUB], ["長さ0", new ArrayBuffer(0)], ["null", null]]) {
    const env3 = makeEnv({ existing: { endpoint: "https://web.push.apple.com/k", key: KEY }, permission: "granted" });
    env3.state.sub.options = { applicationServerKey: odd };
    eq(`D 鍵が想定外の形(${label})で返る端末は取り直さない`, [await env3.api._pushSync(false), env3.state.subscribeCalls, env3.posts("/unsub")], ["on", 0, 0]);
  }
  const env4 = makeEnv({ existing: { endpoint: "https://web.push.apple.com/k", key: KEY }, permission: "granted" });
  env4.state.sub.options = { applicationServerKey: new Uint8Array(KEY) }; // ArrayBuffer ではなく Uint8Array で返す端末
  eq("D 同じ鍵が Uint8Array で返っても取り直さない", [await env4.api._pushSync(false), env4.state.subscribeCalls], ["on", 0]);
}
// E) 権限が拒否(購読は残っている)
{
  const env = makeEnv({ existing: { endpoint: "https://web.push.apple.com/xyz", key: KEY }, permission: "denied" });
  eq("E 起動時の同期は denied を返し、通信しない", [await env.api._pushSync(false), env.state.fetchCalls.length], ["denied", 0]);
}
// F) 許可の確認で「許可しない」/ 閉じた
{
  let env = makeEnv({ requestResult: "denied" });
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  check("FF1 「許可しない」を選んだ: 直し方の案内と「もう一度確認する」", env.text().includes(OFFMSG) && !!env.els.pushRetry && !env.els.pushOn, env.text());
  eq("FF1 購読も通信もしない", [env.state.subscribeCalls, env.state.fetchCalls.length], [0, 0]);
  env = makeEnv({ requestResult: "default" });
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  check("FF2 閉じた: もう一度押せる(ボタンが残る)", env.text().includes("許可の確認が閉じられました") && !!env.els.pushOn, env.text());
  env.requestResult = "granted";
  await env.els.pushOn.onclick();
  check("FF2 もう一度押して許可すると有効", env.text().includes(ON), env.text());
}
// G) 解除: Worker 不通でも端末側は止まり、その後の起動時に勝手に取り直さない
{
  const env = makeEnv();
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  const ep = env.state.sub.endpoint;
  env.fetchImpl = offline;
  await env.els.pushOff.onclick();
  await sleep(5);
  check("G 解除(Worker不通): 表示は「通知を受け取る」", !!env.els.pushOn && !env.text().includes(ON), env.text());
  eq("G 端末側の購読と控えが消える", [env.state.sub, env.state.store], [null, {}]);
  const k = env.state.fetchCalls.lastIndexOf("POST /unsub");
  check("G Worker へは解除を知らせようとしている(失敗しても構わない)", k >= 0 && JSON.parse(env.state.bodies[k]).endpoint === ep);
  eq("G2 解除後の起動時の同期で勝手に購読しない", [await env.api._pushSync(false), env.state.sub, env.state.subscribeCalls], ["off", null, 1]);
}
// G3) 解除の順番: 端末側の解除が先、Worker への連絡は後
{
  const order = [];
  const env = makeEnv();
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  const realUnsub = env.state.sub.unsubscribe;
  env.state.sub.unsubscribe = async function () { order.push("device"); return realUnsub.call(this); };
  env.fetchImpl = async (url) => { order.push(url.replace(WORKER, "")); return { ok: true, status: 200 }; };
  await env.els.pushOff.onclick();
  eq("G3 端末側の解除 → Worker への連絡 の順", order, ["device", "/unsub"]);
}
// H) 「受け取る」にしていたのに購読が消えた(OS側の失効)→ 起動時に黙って取り直す
{
  const env = makeEnv();
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  env.state.sub = null; // 失効
  const n = env.state.fetchCalls.length, rq = env.state.requestCalls;
  eq("H 失効後の起動時の同期", await env.api._pushSync(false), "on");
  check("H 購読を取り直して登録している", !!env.state.sub && env.state.fetchCalls.slice(n).join() === "POST /sub");
  eq("H 許可の確認は出さない", env.state.requestCalls, rq);
}
// I) serviceWorker.ready が返らない
{
  const env = makeEnv({ neverReady: true, fastTimeout: true });
  eq("I ready が返らなくても設定タブの表示が終わる", await withLimit(env.api.renderPushSection(), 1000), "done");
  check("I 「確認中…」のまま止まらず、やり直しのボタンが出る", !env.text().includes("確認中") && !!env.els.pushRetry && env.text().includes("確認できませんでした"), env.text());
  check("I 起動時の同期は失敗として返る(画面には出さない)", (await env.api._pushSync(false).then(() => "ok", () => "rejected")) === "rejected");
}

// ================= T: 弱点を突く場面 =================
// T1) 登録済みの端末で、圏外や Worker の一時的な不調の時に設定タブを開く(誤警報を出さない)
{
  const env = makeEnv();
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  check("T1a 登録直後は有効", env.text().includes(ON));
  env.fetchImpl = offline;
  await env.reopen();
  check("T1b 圏外で開き直しても有効のまま(誤警報を出さない)", env.text().includes(ON) && !env.text().includes(DEAD), env.text());
  env.fetchImpl = status(503);
  await env.reopen();
  check("T1c Worker が 503 でも有効のまま", env.text().includes(ON) && !env.text().includes(DEAD), env.text());
  env.fetchImpl = status(400);
  await env.reopen();
  check("T1d Worker が 400 で断った時は、届かない状態とやり直しのボタン", !env.text().includes(ON) && env.text().includes(DEAD) && !!env.els.pushRetry, env.text());
  check("T1d 診断用の表示に応答コード", env.text().includes("応答400"), env.text());
}
// T2) Worker への通信が返ってこない。「確認中…」のまま止まらない(8秒で打ち切る。ここでは早回し)
{
  const hang = (url, init) => new Promise((res, rej) => { init.signal.addEventListener("abort", () => rej(new Error("aborted"))); });
  const env = makeEnv({ fastTimeout: true });
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  env.fetchImpl = hang;
  eq("T2 登録済みの端末: 通信が返らなくても表示が終わる", await withLimit(env.reopen(), 1000), "done");
  check("T2 登録済みの端末は有効のまま", env.text().includes(ON) && !env.text().includes("確認中"), env.text());
  const env2 = makeEnv({ fastTimeout: true, fetchImpl: hang });
  await env2.api.renderPushSection();
  eq("T2 未登録の端末: 押した後、通信が返らなくても表示が終わる", await withLimit(env2.els.pushOn.onclick(), 1000), "done");
  check("T2 未登録の端末は届かない状態の表示", env2.text().includes(DEAD) && !!env2.els.pushRetry, env2.text());
  check("T2 通信の上限は8秒", /const PUSH_NET_MS=8000;/.test(pushFns));
}
// T3) 何も設定していない端末の起動時の同期: 許可の要求・購読・通信をしない
{
  const env = makeEnv();
  eq("T3 未設定の端末は何もしない", [await env.api._pushSync(false), env.state.requestCalls, env.state.subscribeCalls, env.state.fetchCalls.length], ["off", 0, 0, 0]);
  const env2 = makeEnv({ permission: "granted" });
  eq("T3 許可済みでも「受け取る」にしていなければ購読しない", [await env2.api._pushSync(false), env2.state.subscribeCalls], ["off", 0]);
}
// T4) 今のコード(控えを使わない)で購読済みの端末: 起動時の同期で Worker に登録し直し、控えを作る
{
  const env = makeEnv({ existing: { endpoint: "https://web.push.apple.com/xyz", key: KEY }, permission: "granted" });
  eq("T4 起動時の同期で登録し直す", [await env.api._pushSync(false), env.state.fetchCalls], ["on", ["POST /sub"]]);
  eq("T4 控えに on / ep / t が入る", Object.keys(env.ls()).sort(), ["ep", "on", "t"]);
}
// T5) 「受け取る」済みで購読が消え、取り直しも失敗
{
  const env = makeEnv();
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  env.state.sub = null;
  const env2 = makeEnv({ permission: "granted", subscribeFails: true }, env.state);
  eq("T5 取り直しに失敗した時の起動時の同期", await env2.api._pushSync(false), "unsynced");
  await env2.api.renderPushSection();
  check("T5 設定タブは届かない状態とやり直しのボタン", env2.text().includes(DEAD) && !!env2.els.pushRetry, env2.text());
}
// T6) 「通知を止める」で端末側の解除が失敗
{
  const env = makeEnv({ unsubFails: true });
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  await env.els.pushOff.onclick();
  check("T6 止められなかったと表示し、ボタンは押せる状態に戻る", env.text().includes("通知を止められませんでした") && !!env.els.pushOff && env.els.pushOff.disabled === false, env.text());
  check("T6 端末側の購読は残っている", !!env.state.sub);
  eq("T6 Worker へ解除は知らせていない", env.posts("/unsub"), 0);
  eq("T6 次の同期で有効な状態に戻る(止まっていないので)", await env.api._pushSync(false), "on");
}

// ================= N: 新旧の Worker、行き違い、案内文など =================
// N1) Worker の応答ごとの扱い。古い Worker は常に 201、新しい Worker は 登録済み=200 / Origin 違い=403 / 満杯=507 / 内部の失敗=503
{
  for (const [code, firstTime, known] of [[201, "on", "on"], [200, "on", "on"], [400, "unsynced", "unsynced"], [403, "unsynced", "unsynced"], [507, "unsynced", "unsynced"], [503, "unsynced", "on"], [500, "unsynced", "on"]]) {
    const env = makeEnv({ existing: { endpoint: "https://fcm.googleapis.com/fcm/send/x", key: KEY }, permission: "granted", fetchImpl: status(code) });
    eq(`N1 応答${code}: 一度も確認できていない端末`, await env.api._pushSync(true), firstTime);
    const env2 = makeEnv({ existing: { endpoint: "https://fcm.googleapis.com/fcm/send/x", key: KEY }, permission: "granted" });
    await env2.api._pushSync(true); // 以前に確認済み
    env2.fetchImpl = status(code);
    eq(`N1 応答${code}: 以前に確認済みの端末`, await env2.api._pushSync(true), known);
  }
  // はっきり断られた後は「確認済み」に戻らない: 圏外でも有効と出さず、次の起動時は間引かずに登録し直す
  const env = makeEnv({ existing: { endpoint: "https://fcm.googleapis.com/fcm/send/x", key: KEY }, permission: "granted" });
  await env.api._pushSync(true);
  env.fetchImpl = status(507);
  await env.api._pushSync(true);
  env.fetchImpl = offline;
  eq("N1 断られた後に圏外になっても「有効」に戻らない", await env.api._pushSync(true), "unsynced");
  const n = env.posts("/sub");
  eq("N1 断られた後の起動時は、10分以内でも登録を試す", [await env.api._pushSync(false), env.posts("/sub")], ["unsynced", n + 1]);
  env.fetchImpl = null;
  eq("N1 Worker が受け付けるようになれば、起動時の同期だけで有効に戻る", [await env.api._pushSync(false), env.ls().ep], ["on", "https://fcm.googleapis.com/fcm/send/x"]);
  check("N1 断られた時の文面は「受け付けられませんでした」", await (async () => { env.fetchImpl = status(400); await env.reopen(); return env.text().includes("受け付けられませんでした") && env.text().includes(DEAD); })(), env.text());
}
// N2) 行き違い: 登録の通信が返る前に「通知を止める」を押した → 後から返った登録が「受け取る」の控えを書き戻さない
{
  const env = makeEnv();
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  let release;
  env.fetchImpl = (url) => (url.endsWith("/sub") ? new Promise((r) => { release = () => r({ ok: true, status: 200 }); }) : Promise.resolve({ ok: true, status: 200 }));
  const p = env.api._pushSync(true); // 前面復帰時などの同期が通信中
  await sleep(5);
  await env.els.pushOff.onclick();
  release();
  await p;
  eq("N2 止めた後に控えが書き戻されない", env.state.store, {});
  env.fetchImpl = null;
  eq("N2 次の起動時に勝手に購読し直さない", [await env.api._pushSync(false), env.state.sub, env.state.subscribeCalls], ["off", null, 1]);
}
// N3) 拒否時の案内文: iPhone のホーム画面アプリ / Android のホーム画面アプリ / Chrome のタブ
{
  let env = makeEnv({ ...IPHONE, standalone: true, permission: "denied" });
  await env.api.renderPushSection();
  check("N3 iPhone: 設定アプリからの直し方", env.text().includes("設定アプリ→通知→アリテイ") && !env.text().includes("Chromeで開いている場合"), env.text());
  env = makeEnv({ permission: "denied" });
  await env.api.renderPushSection();
  check("N3 Android: ホーム画面のアプリの直し方", env.text().includes("アイコンを長押し→アプリ情報→通知"), env.text());
  check("N3 Android: Chrome のタブで開いている場合の直し方", env.text().includes("Chromeで開いている場合: アドレス欄の左のマーク→権限→通知"), env.text());
  check("N3 Android には iPhone の手順を出さない", !env.text().includes("設定アプリ→通知→アリテイ"), env.text());
}
// N4) 登録し直しの間引き: 10分たてば起動時にも確かめ直す。端末の時計が戻っていても確かめ直す。宛先が変われば登録し直す
{
  const env = makeEnv({ existing: { endpoint: "https://fcm.googleapis.com/fcm/send/x", key: KEY }, permission: "granted" });
  env.state.now = 1_800_000_000_000;
  await env.api._pushSync(false);
  env.state.now += 9 * 60 * 1000;
  eq("N4 9分後の起動時は通信しない", [await env.api._pushSync(false), env.posts("/sub")], ["on", 1]);
  env.state.now += 2 * 60 * 1000;
  eq("N4 10分を過ぎた起動時は確かめ直す", [await env.api._pushSync(false), env.posts("/sub")], ["on", 2]);
  env.state.now -= 5 * 24 * 3600 * 1000;
  eq("N4 時計が戻っている時も確かめ直す", [await env.api._pushSync(false), env.posts("/sub")], ["on", 3]);
  env.state.sub.endpoint = "https://fcm.googleapis.com/fcm/send/moved"; // ブラウザが宛先を付け替えた
  eq("N4 宛先が変わっていれば登録し直す", [await env.api._pushSync(false), env.posts("/sub"), env.ls().ep], ["on", 4, "https://fcm.googleapis.com/fcm/send/moved"]);
}
// N5) localStorage が使えない端末(プライベートモード等)でも、押せば登録できる
{
  const env = makeEnv({ lsBroken: true });
  await env.api.renderPushSection();
  await env.els.pushOn.onclick();
  check("N5 控えを保存できなくても登録でき、有効と出る", env.text().includes(ON) && env.posts("/sub") === 1, env.text());
  eq("N5 次の起動時は(控えが無いので)毎回登録し直す", [await env.api._pushSync(false), env.posts("/sub")], ["on", 2]);
}
// N6) テスト通知を出せない時
{
  const env = makeEnv({ showFails: true });
  await env.api.renderPushSection(); await env.els.pushOn.onclick();
  await env.els.pushTest.onclick();
  check("N6 出せなかった時はその旨を表示", env.text().includes("テスト通知を表示できませんでした"), env.text());
}
// N7) 控えが壊れている(JSON でない・オブジェクトでない)時も動く
{
  for (const bad of ["{", "5", '"x"', "null"]) {
    const env = makeEnv({ existing: { endpoint: "https://fcm.googleapis.com/fcm/send/x", key: KEY }, permission: "granted" });
    env.state.store.aritei_push = bad;
    eq(`N7 壊れた控え(${bad})でも登録し直して有効`, [await env.api._pushSync(false), env.ls().on], ["on", 1]);
  }
}

// ================= P: 通知タップで開いた時と、起動時の同期の呼び出し =================
// index.html の「起動タブを決める箇所」から refreshBusy の宣言の直前までを、そのままの順で動かす。
function bootEnv(o = {}) {
  const log = [], timers = [], swL = {}, docL = {};
  let phase = "startup", tid = 0;
  const store = o.session || {};
  const location = { search: o.search || "", pathname: "/kyotei-ai/", hash: o.hash || "", reload() { log.push("reload"); } };
  const history = { state: null, scrollRestoration: "auto", replaceState(st, t, url) { log.push("replaceState " + url); const q = url.indexOf("?"), h = url.indexOf("#"); location.search = q < 0 ? "" : url.slice(q, h < 0 ? undefined : h); } };
  const document = { visibilityState: "visible", addEventListener(t, f) { (docL[t] = docL[t] || []).push(f); } };
  const sessionStorage = {
    getItem: (k) => { if (o.ssBroken) throw new Error("ss"); return k in store ? store[k] : null; },
    setItem: (k, v) => { if (o.ssBroken) throw new Error("ss"); store[k] = String(v); },
  };
  const clock = { t: o.now || 1_800_000_000_000 };
  const ctx = vm.createContext({
    console, URLSearchParams, location, history, document, sessionStorage,
    localStorage: { getItem: (k) => (k === "aritei_start_tab" ? o.startTab || null : null) },
    navigator: { serviceWorker: { controller: o.controller || null, register() { log.push("register"); }, addEventListener(t, f) { swL[t] = f; } } },
    Date: { now: () => clock.t },
    setTimeout: (f, ms) => { timers.push({ f, ms, id: ++tid, cleared: false }); return tid; },
    clearTimeout: (id) => { const t = timers.find((x) => x.id === id); if (t) t.cleared = true; },
    show: (tab) => { log.push("show " + tab); return Promise.resolve(); },
    fetchJSON: () => new Promise(() => {}),
    _renderAppVer: () => {},
    PUSH_WORKER: o.noWorker ? "" : "https://worker.example",
    _pushSync: (force) => { log.push("sync " + force); return o.syncFails ? Promise.reject(new Error("sync failed")) : Promise.resolve("off"); },
    __refresh: () => { log.push("refresh@" + phase); },
  });
  // 本物と同じ並び: この後ろに refreshBusy の宣言と refreshLatest が来る
  vm.runInContext(startup + "\nlet refreshBusy=false;\nasync function refreshLatest(){if(refreshBusy)return;__refresh();}", ctx);
  phase = "later";
  const live = () => timers.filter((t) => !t.cleared);
  return { log, timers, live, swL, docL, location, document, store, clock,
    refreshDelays: () => live().filter((t) => t.f.name !== "_pushSyncBg").map((t) => t.ms),
    runRefreshTimers: () => live().filter((t) => t.f.name !== "_pushSyncBg").forEach((t) => t.f()),
  };
}
// P1) 通知タップで開かれた("./?from=push"): 起動タブの設定に関わらず厳選一覧。取り直しを予約。URL の印は消す
{
  const b = bootEnv({ search: "?from=push" });
  check("P1 厳選一覧を表示する(起動タブの設定は当日予測のまま)", b.log.includes("show sen") && !b.log.includes("show today"), b.log.join(" ; "));
  eq("P1 取り直しの予約は 5・30・60・90・120秒後", b.refreshDelays(), [5000, 30000, 60000, 90000, 120000]);
  check("P1 起動の途中で取り直しを直接呼ばない(refreshBusy の宣言より前なので)", !b.log.includes("refresh@startup"), b.log.join(" ; "));
  check("P1 URL の ?from=push を表示の後で消す", b.log.indexOf("replaceState /kyotei-ai/") > b.log.indexOf("show sen") && b.location.search === "", b.log.join(" ; "));
  b.runRefreshTimers();
  await sleep(5);
  eq("P1 予約した時刻に取り直しが5回動く", b.log.filter((x) => x === "refresh@later").length, 5);
  const b2 = bootEnv({ search: "?x=1&from=push", hash: "#h" });
  check("P1 他のパラメータと # は残す", b2.log.includes("replaceState /kyotei-ai/?x=1#h"), b2.log.join(" ; "));
  const b3 = bootEnv({ search: "?from=push" });
  b3.document.visibilityState = "hidden";
  b3.runRefreshTimers();
  await sleep(5);
  eq("P1 画面が裏にある間は取り直さない", b3.log.filter((x) => x.startsWith("refresh")).length, 0);
}
// P2) 普通に開いた時は今までどおり(起動タブの設定に従う。予約も URL の書き換えもしない)
{
  const b = bootEnv({});
  check("P2 既定は当日予測", b.log.includes("show today") && !b.log.includes("show sen"), b.log.join(" ; "));
  eq("P2 取り直しの予約なし・URL の書き換えなし", [b.refreshDelays(), b.log.filter((x) => x.startsWith("replaceState")).length], [[], 0]);
  const b2 = bootEnv({ startTab: "sen" });
  check("P2 起動タブの設定が厳選一覧ならそのとおり(予約はしない)", b2.log.includes("show sen") && b2.refreshDelays().length === 0, b2.log.join(" ; "));
  const b3 = bootEnv({ search: "?from=other" });
  check("P2 from=push 以外の印では切り替えない", b3.log.includes("show today") && b3.refreshDelays().length === 0, b3.log.join(" ; "));
  const b4 = bootEnv({ ssBroken: true });
  check("P2 sessionStorage が使えない端末でも普通に起動する", b4.log.includes("show today"), b4.log.join(" ; "));
  const b5 = bootEnv({ ssBroken: true, search: "?from=push" });
  check("P2 sessionStorage が使えない端末でも ?from=push は効く", b5.log.includes("show sen") && b5.refreshDelays().length === 5, b5.log.join(" ; "));
}
// P3) 通知タップで開いた直後に、アプリの更新で自動の再読み込みが入った(URL の印はもう無い)→ 60秒以内なら厳選一覧のまま
{
  const b = bootEnv({ search: "?from=push" });
  const again = bootEnv({ session: b.store, now: b.clock.t + 3000 });
  check("P3 3秒後の再読み込みでも厳選一覧と取り直しの予約", again.log.includes("show sen") && again.refreshDelays().length === 5, again.log.join(" ; "));
  const late = bootEnv({ session: b.store, now: b.clock.t + 61000 });
  check("P3 60秒を過ぎた再読み込みは、いつもの起動タブ", late.log.includes("show today") && late.refreshDelays().length === 0, late.log.join(" ; "));
}
// P4) 開いている画面へ sw.js から {type:"push-click"} が届いた: 厳選一覧を表示し、取り直しを予約
{
  const b = bootEnv({});
  check("P4 メッセージの受け口がある", typeof b.swL.message === "function");
  b.swL.message({ data: { type: "push-click" } });
  check("P4 厳選一覧を表示する", b.log.includes("show sen"), b.log.join(" ; "));
  eq("P4 取り直しの予約は 5・30・60・90・120秒後", b.refreshDelays(), [5000, 30000, 60000, 90000, 120000]);
  b.swL.message({ data: { type: "push-click" } }); // 続けてもう1通タップ
  eq("P4 続けてタップしても予約は積み上がらない(前の予約を取り消す)", b.refreshDelays(), [5000, 30000, 60000, 90000, 120000]);
  const n = b.log.length;
  b.swL.message({ data: { type: "other" } }); b.swL.message({ data: null }); b.swL.message({});
  eq("P4 関係ないメッセージでは何もしない", b.log.length, n);
}
// P5) 起動時の同期: 起動を遅らせない(その場では呼ばず、少し後に呼ぶ)。前面に戻った時にも呼ぶ。失敗しても外へ漏らさない
{
  const b = bootEnv({ syncFails: true });
  check("P5 起動の途中では同期を呼ばない", !b.log.some((x) => x.startsWith("sync")), b.log.join(" ; "));
  const t = b.timers.find((x) => x.f.name === "_pushSyncBg");
  check("P5 少し後(2秒後)に予約している", !!t && t.ms === 2000);
  t.f();
  await sleep(5);
  eq("P5 予約の時刻に、間引きあり(force=false)で同期を呼ぶ", b.log.filter((x) => x.startsWith("sync")), ["sync false"]);
  b.docL.visibilitychange.forEach((f) => f());
  eq("P5 前面に戻った時にも同期を呼ぶ", b.log.filter((x) => x.startsWith("sync")).length, 2);
  b.document.visibilityState = "hidden";
  b.docL.visibilitychange.forEach((f) => f());
  eq("P5 裏に回った時は呼ばない", b.log.filter((x) => x.startsWith("sync")).length, 2);
  await sleep(5); // 同期の失敗が「捕まえていない失敗」にならないこと(なれば全体の最後で NG になる)
  const off = bootEnv({ noWorker: true });
  off.timers.find((x) => x.f.name === "_pushSyncBg").f();
  check("P5 PUSH_WORKER が空なら同期を呼ばない", !off.log.some((x) => x.startsWith("sync")));
}
// P6) 既存の動き: アプリの更新時の自動の再読み込みは今までどおり
{
  const b = bootEnv({ controller: {} });
  b.swL.controllerchange(); b.swL.controllerchange();
  eq("P6 更新時の再読み込みは1回だけ", b.log.filter((x) => x === "reload").length, 1);
  const first = bootEnv({});
  first.swL.controllerchange();
  eq("P6 初回訪問では再読み込みしない", first.log.filter((x) => x === "reload").length, 0);
}

// ================= W: sw.js =================
const SCOPE = "https://t-fuji777.github.io/kyotei-ai/";
function loadSw(clientList, o = {}) {
  const handlers = {}, shown = [], actions = [], cache = new Map(), matchOpts = [];
  const self = {
    addEventListener: (t, f) => { handlers[t] = f; },
    skipWaiting() {},
    registration: { scope: SCOPE, showNotification: async (title, opt) => { shown.push({ title, opt }); } },
    clients: {
      claim: async () => {},
      matchAll: async (q) => { actions.push("matchAll " + JSON.stringify(q)); if (o.matchAllFails) throw new Error("matchAll failed"); return clientList; },
    },
  };
  if (o.openWindow !== false) self.clients.openWindow = async (u) => { actions.push("openWindow " + u); return {}; };
  const caches = {
    open: async () => ({ put: async (req, res) => { cache.set(req.url, res); }, addAll: async () => {} }),
    keys: async () => [],
    match: async (req, opt) => { matchOpts.push(opt); const u = new URL(req.url); if (opt && opt.ignoreSearch) u.search = ""; return cache.get(u.href); },
  };
  const Request = function (u) { this.url = String(u); };
  vm.runInContext(swSrc, vm.createContext({ self, caches, fetch: (...a) => o.fetch(...a), URL, Request, console }));
  return { handlers, shown, actions, cache, matchOpts };
}
async function fire(h, ev) { let p; ev.waitUntil = (x) => { p = x; }; h(ev); await p; }
const mkClient = (url, log, focusFails) => ({ url, focus: async () => { if (focusFails) throw new Error("focus failed"); log.push("focus " + url); }, postMessage: (m) => log.push("message " + JSON.stringify(m) + " -> " + url) });
const tap = (tag) => ({ notification: { tag: tag || "", close() {} } });
// W1) push: notify.py が送る形。tag があれば通知の tag に使い、badge は付けない
{
  const { handlers, shown } = loadSw([]);
  const body = "厳選プラン確定 14:47 / 住之江9R 締切15:02\n買い目 1-2-3 / 1-3-2 / 1-2-4";
  await fire(handlers.push, { data: { json: () => ({ title: "アリテイ", body, tag: "conf-20261003-12-9" }) } });
  await fire(handlers.push, { data: { json: () => ({ title: "アリテイ", body }) } });
  eq("W1 tag つき: 題名・本文・アイコン・tag", shown[0], { title: "アリテイ", opt: { body, icon: "icon-192.png", tag: "conf-20261003-12-9" } });
  eq("W1 tag なしのペイロードもそのまま表示(tag を付けない)", shown[1], { title: "アリテイ", opt: { body, icon: "icon-192.png" } });
  check("W1 badge を指定しない", shown.every((s) => !("badge" in s.opt)));
}
// W2) push: データ無し / JSON でない / 中身が空 でも、既定の文面で必ず通知を出す
{
  const { handlers, shown } = loadSw([]);
  await fire(handlers.push, { data: null });
  await fire(handlers.push, { data: { json: () => { throw new Error("not json"); } } });
  await fire(handlers.push, { data: { json: () => ({}) } });
  await fire(handlers.push, { data: { json: () => null } });
  eq("W2 4通とも既定の文面で表示", shown.map((s) => s.title + "/" + s.opt.body), Array(4).fill("アリテイ/新着情報があります"));
}
// W3) 通知タップ: アリテイが開いていない → "./?from=push" を開く
{
  const { handlers, actions } = loadSw([]);
  await fire(handlers.notificationclick, tap());
  eq("W3 閉じている時は ./?from=push を開く", actions.filter((a) => a.startsWith("openWindow")), ["openWindow ./?from=push"]);
  check("W3 開いているウィンドウは、制御下に無いものも含めて探す", actions[0] === 'matchAll {"type":"window","includeUncontrolled":true}', actions[0]);
  eq("W3 開く先は scope の中", new URL("./?from=push", SCOPE + "sw.js").href, SCOPE + "?from=push");
}
// W4) 通知タップ: アリテイが開いている → push-click を送って前面に出す(新しくは開かない)
{
  const log = [];
  const { handlers, actions } = loadSw([mkClient(SCOPE, log)]);
  await fire(handlers.notificationclick, tap("conf-20261003-12-9"));
  eq("W4 合図を送ってから前面に出す", log, ['message {"type":"push-click"} -> ' + SCOPE, "focus " + SCOPE]);
  check("W4 新しくは開かない", !actions.some((a) => a.startsWith("openWindow")));
}
// W5) 同じオリジンの別サイト(kinlog)だけが開いている → それには触らず、新しく開く
{
  const log = [];
  const { handlers, actions } = loadSw([mkClient("https://t-fuji777.github.io/kinlog/", log)]);
  await fire(handlers.notificationclick, tap());
  eq("W5 別サイトには合図も送らず前面にも出さない", log, []);
  check("W5 アリテイを新しく開く", actions.includes("openWindow ./?from=push"));
}
// W6) 別サイトが先・アリテイが後(別サイトを後に見ていた)→ アリテイだけを前面に出す
{
  const log = [];
  const A = SCOPE + "index.html?t=1";
  const { handlers, actions } = loadSw([mkClient("https://t-fuji777.github.io/kinlog/", log), mkClient(A, log)]);
  await fire(handlers.notificationclick, tap());
  eq("W6 アリテイだけを前面に出す", log, ['message {"type":"push-click"} -> ' + A, "focus " + A]);
  check("W6 新しくは開かない", !actions.some((a) => a.startsWith("openWindow")));
  const log2 = [];
  const r = loadSw([mkClient("https://t-fuji777.github.io/kyotei-ai-old/", log2)]);
  await fire(r.handlers.notificationclick, tap());
  check("W6 名前が似ているだけの別サイト(/kyotei-ai-old/)も対象にしない", log2.length === 0 && r.actions.includes("openWindow ./?from=push"));
}
// W7) 前面に出すのに失敗した → 新しく開く。openWindow が無い環境・一覧を取れない時も落ちない
{
  const log = [];
  let r = loadSw([mkClient(SCOPE, log, true)]);
  await fire(r.handlers.notificationclick, tap());
  check("W7 前面に出せなければ新しく開く", r.actions.includes("openWindow ./?from=push"));
  r = loadSw([], { openWindow: false });
  let err = "";
  try { await fire(r.handlers.notificationclick, tap()); } catch (e) { err = String(e); }
  check("W7 openWindow が無くても落ちない", !err, err);
  r = loadSw([], { matchAllFails: true });
  await fire(r.handlers.notificationclick, tap());
  check("W7 一覧を取れない時は新しく開く", r.actions.includes("openWindow ./?from=push"));
}
// W8) 設定タブの「テスト通知を表示」で出した通知のタップ: 前面に出すだけで、画面は切り替えない
{
  const log = [];
  let r = loadSw([mkClient(SCOPE, log)]);
  await fire(r.handlers.notificationclick, tap("aritei-test"));
  eq("W8 合図は送らず前面に出すだけ", log, ["focus " + SCOPE]);
  r = loadSw([]);
  await fire(r.handlers.notificationclick, tap("aritei-test"));
  eq("W8 閉じていた時は印なしで開く", r.actions.filter((a) => a.startsWith("openWindow")), ["openWindow ./"]);
}
// W9) "./?from=push" の読み込み: 取れた時は検索文字列を除いた名前で保存し、オフラインの時はその保存分を返す
{
  let online = true;
  const page = { ok: true, clone() { return "保存した index.html"; } };
  const r = loadSw([], { fetch: async () => { if (!online) throw new TypeError("offline"); return page; } });
  const ev = () => { const e = { request: { method: "GET", mode: "navigate", url: SCOPE + "?from=push" }, respondWith(p) { e.res = p; } }; return e; };
  let e = ev(); r.handlers.fetch(e);
  check("W9 オンライン: ネットワークの応答をそのまま返す", (await e.res) === page);
  await sleep(5);
  eq("W9 保存する名前は検索文字列なし", [...r.cache.keys()], [SCOPE]);
  online = false;
  e = ev(); r.handlers.fetch(e);
  eq("W9 オフライン: 保存分を返す(?from=push が付いていても)", await e.res, "保存した index.html");
  const post = { request: { method: "POST", url: WORKER + "/sub" }, respondWith() { post.touched = true; } };
  r.handlers.fetch(post);
  check("W9 Worker への POST には手を出さない", !post.touched);
}

await sleep(20);
console.log("");
console.log(failed ? `失敗 ${failed}件 / 成功 ${passed}件` : `全${passed}件 成功`);
process.exit(failed ? 1 : 0);

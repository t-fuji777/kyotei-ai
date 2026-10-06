// アプリ側(docs/index.html)の「モデルの世代」まわりを、Node だけで確かめる。
// ブラウザも通信も使わない。index.html のインラインスクリプトから該当の部分を取り出し、
// 偽の画面要素の上で動かす(tests/front_push_test.mjs と同じやり方)。
//
// 実行: node tests/front_gen_test.mjs
//   リポジトリのどこから実行してもよい。全部通れば終了コード0、1つでも落ちれば1。
//   取り出しの目印(関数名や宣言)が index.html から消えると「目印が見つからない」で止まる。
//
// 場面の一覧:
//   S  構文と定数(しきい値の表が scripts/common.py の SENGEN_CFG_BY_GEN と同じ、sw.js の版)
//   G  世代の読み取り(g 無し / g=1 / g=2 / 読めない値)
//   T  厳選の候補判定 sengenOk(g 無し・g=1・g=2 で、その世代のしきい値を使う。打刻は最優先)
//   C  較正表の選び方 _tb(gens[世代] → トップレベル → 内蔵。gens の無い古い形の calib.json でも動く)
//      と、それを使う calT5 / actLbl / calT3 / cumCls
//   M  設定タブのモデル情報(model_report.json の項目が欠ける・形が違っても落ちない。世代2の説明文)
//   A  的中実績・監視の「モデル更新日」の印(世代2の日が無ければ何も出ない)
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
function noThrow(name, fn) {
  let err = "";
  try { fn(); } catch (e) { err = String(e && e.stack || e); }
  check(name, !err, err);
}

function cut(a, b) {
  const s = script.indexOf(a), e = script.indexOf(b, s);
  if (s < 0 || e < 0) throw new Error("index.html に目印が見つからない: " + (s < 0 ? a : b));
  return script.slice(s, e);
}
// 取り出す範囲: 定数と判定の関数(sengenOk まで)/ pickOdds / 当日予測(raceCard〜renderToday)/ 的中実績(todayStats, renderAcc)/ 設定タブ(renderModel)
const head = cut("const $=", "function fitRtype()");
const oddsFn = cut("function pickOdds(", "function renderSengen()");
const todayFns = cut("function _stampEta(deadline)", "function todayStats()");
const accFns = cut("function todayStats()", "function positionAccSticky()");
const modelFn = cut("function renderModel(rep)", "// 画面診断");

// ---- 偽の画面環境 ----
function makeEnv() {
  const mkEl = () => ({ innerHTML: "", textContent: "", scrollTop: 0, style: {}, hidden: false, querySelectorAll: () => [], querySelector: () => null, classList: { toggle() {}, add() {}, remove() {} }, getBoundingClientRect: () => ({ top: 0, height: 0 }), offsetHeight: 0 });
  const main = mkEl(), byId = {}, bySel = { "#main": main };
  const document = {
    querySelector: (s) => (bySel[s] = bySel[s] || mkEl()),
    querySelectorAll: () => [],
    getElementById: (id) => (byId[id] = byId[id] || mkEl()),
    addEventListener() {},
    createElement: mkEl,
    body: { appendChild() {} },
  };
  const ctx = vm.createContext({
    document, console, localStorage: { getItem: () => null, setItem() {} },
    window: { addEventListener() {}, innerHeight: 800 }, requestAnimationFrame() {}, setTimeout: () => 1, clearTimeout() {},
    // 取り出していない関数の代わり(呼ばれても何もしない)
    positionAccSticky() {}, renderPushSection() {}, _renderAppVer() {}, _appReload() {}, _screenInfo() { return "-"; }, _lossNote() { return ""; }, fitRtype() {},
    fetch() { return new Promise(() => {}); }, atob, Uint8Array, URL,
  });
  vm.runInContext(head + "\n" + oddsFn + "\n" + todayFns + "\n" + accFns + "\n" + modelFn
    + "\nthis.__api={SENGEN_CFG_BY_GEN,raceGen,sengenCfgFor,sengenOk,_tb,calT5,calT3,actLbl,cumCls,modelGenStarts,renderModel,renderAcc,renderToday,VIEW,"
    + "CAL_T5L,CAL_T3L,setCalib:c=>{CALIB=c;VIEW.calib=c;},setPred:p=>{pred=p;},setLatest:d=>{latestDate=d;},setMode:m=>{todayMode=m;},setVenue:v=>{selVenue=v;}};", ctx);
  const api = ctx.__api;
  const strip = (h) => String(h || "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
  return { api, main, byId, text: () => strip(main.innerHTML), idText: (id) => strip(byId[id] && byId[id].innerHTML) };
}
const env = makeEnv();
const A = env.api;

// 試験用のレース: 上位3点の合計(top3p)と上位5点の合計(top5p)を指定して作る。buy目は5点、確率は均等でなく上位に寄せる
function race(o = {}) {
  const t3 = o.top3p != null ? o.top3p : 0.40, t5 = o.top5p != null ? o.top5p : t3 + 0.08;
  const r = { no: o.no != null ? o.no : 7, picks: [
    { c: "1-2-3", p: +(t3 * 0.5).toFixed(4) }, { c: "1-3-2", p: +(t3 * 0.3).toFixed(4) }, { c: "1-2-4", p: +(t3 * 0.2).toFixed(4) },
    { c: "1-4-2", p: +((t5 - t3) * 0.6).toFixed(4) }, { c: "2-1-3", p: +((t5 - t3) * 0.4).toFixed(4) }] };
  for (const k of ["g", "tk", "att", "odds", "result"]) if (k in o) r[k] = o[k];
  return r;
}

// ================= S: 構文と定数 =================
{
  let err = "";
  try { new vm.Script(script, { filename: "docs/index.html(inline script)" }); } catch (e) { err = String(e && e.stack || e); }
  check("S1 index.html のインラインスクリプト全体が構文エラーにならない", !err, err);
  const m = swSrc.match(/^const CACHE = "kyotei-ai-v(\d+)";/);
  check("S2 sw.js の CACHE が kyotei-ai-v<番号> の形で、165 以上(世代2の土台を入れた版)", !!m && +m[1] >= 165, swSrc.split("\n")[0]);
  // 表示版は sw.js の通し番号から自動で決まる(GEN_STARTS)。165 なら v6.21
  const gs = script.match(/const GEN_STARTS=(\[[^\]]*\][^;]*);/);
  check("S2 GEN_STARTS の表がある", !!gs);
  if (gs && m) {
    const tbl = JSON.parse(gs[1]);
    const n = +m[1], g = tbl.find((x) => n >= x[1]) || [1, 1];
    check("S2 表示版は v6.<通し番号-144>(165 なら v6.21)", g[0] === 6 && (n !== 165 || n - g[1] === 21), `v${g[0]}.${n - g[1]}`);
  }
  // しきい値の表は scripts/common.py の SENGEN_CFG_BY_GEN の写し
  const py = path.join(root, "scripts", "common.py");
  if (fs.existsSync(py)) {
    const src = fs.readFileSync(py, "utf8");
    const num = (re) => { const mm = src.match(re); return mm ? +mm[1] : null; };
    const g1 = A.SENGEN_CFG_BY_GEN[1], g2 = A.SENGEN_CFG_BY_GEN[2];
    eq("S3 世代1の top3p_min は common.py の SENGEN_TOP3P_MIN", g1.top3p_min, num(/^SENGEN_TOP3P_MIN\s*=\s*([\d.]+)/m));
    eq("S3 世代1の min_odds は common.py の SENGEN_MIN_ODDS", g1.min_odds, num(/^SENGEN_MIN_ODDS\s*=\s*([\d.]+)/m));
    eq("S3 世代1の min_rno は common.py の SENGEN_MIN_RNO", g1.min_rno, num(/^SENGEN_MIN_RNO\s*=\s*(\d+)/m));
    const ex = src.match(/^SENGEN_EXCLUDE_VENUES\s*=\s*frozenset\(\{([\d,\s]+)\}\)/m);
    eq("S3 除外会場は common.py の SENGEN_EXCLUDE_VENUES", g1.exclude_venues, ex ? ex[1].split(",").map((x) => +x.trim()).sort((a, b) => a - b) : null);
    // 世代2の項は複数行にまたがる: "2: {" から次の "}" までを取り出して中を見る
    const i2 = src.indexOf("\n    2: {", src.indexOf("SENGEN_CFG_BY_GEN = {"));
    const blk2 = i2 >= 0 ? src.slice(i2, src.indexOf("}", i2)) : "";
    const g2py = blk2.match(/"top3p_min":\s*([\d.]+)/);
    eq("S3 世代2の top3p_min は common.py の SENGEN_CFG_BY_GEN[2]", g2.top3p_min, g2py ? +g2py[1] : null);
    const c2py = blk2.match(/"cand_top4p_min":\s*([\d.]+)/);
    eq("S3 世代2の cand_top4p_min は common.py と同じ", g2.cand_top4p_min, c2py ? +c2py[1] : null);
  }
  eq("S4 世代1のしきい値は今の定数(0.36 / 3.1 / 5R / 3,4,14)", A.SENGEN_CFG_BY_GEN[1], { top3p_min: 0.36, min_odds: 3.1, min_rno: 5, exclude_venues: [3, 4, 14], cand_top4p_min: 0.36 });
  check("S4 世代2のしきい値は世代1より高い(目盛りが変わるため)", A.SENGEN_CFG_BY_GEN[2].top3p_min > 0.36);
  check("S5 古い定数名(SENGEN_TOP3P_MIN など)の直接参照が index.html に残っていない",
    !/SENGEN_TOP3P_MIN|SENGEN_MIN_ODDS|SENGEN_EXCLUDE\b/.test(script));
  // 当日予測の「会場別」: 会場の見出し(最終日/初日/n日目)を作る所で isFinalDay(v) を呼ぶ。
  // 2026-10-04 の版で v.isFinalDay(v) と書かれて例外になっていた(会場別に切り替えると描けない)。
  const boats = [1, 2, 3, 4, 5, 6].map((l) => ({ lane: l, name: "選手" + l, cls: "A1", wp: 0.6 - l * 0.08 }));
  const P = { date: "20261006", generated_at: "x", model_trained_at: "y", venues: [
    { code: 12, name: "住之江", day_n: 1, races: [Object.assign(race({ top3p: 0.4 }), { boats, deadline: "15:00", type: "予選" })] },
    { code: 24, name: "大村", day_n: 6, races: [Object.assign(race({ top3p: 0.4 }), { boats, deadline: "16:00", type: "優勝戦" })] }] };
  A.setPred(P); A.setLatest("20261006");
  A.setMode("venue");
  noThrow("S6 当日予測の会場別が例外にならない", () => A.renderToday());
  check("S6 会場別の会場一覧(ヘッダー)に「最終日」(優勝戦のある会場)と「初日」が出て、本文に買い目が出る",
    env.idText("hdrChips").includes("大 村：最終日") && /住之江：初\s日/.test(env.idText("hdrChips")) && env.text().includes("推奨買い目"), env.idText("hdrChips").slice(0, 300));
  A.setMode("time");
  noThrow("S6 当日予測の時間別が例外にならない", () => A.renderToday());
  check("S6 時間別に注目/様子見のバッジが出る", /注目|様子見/.test(env.text()), env.text().slice(0, 300));
  A.setPred(null);
  // S7 実在の当日ファイル(docs/predictions/2026*.json)で会場別を全会場について描く。main(d75435d46)の
  // renderToday は会場チップの _lb が v.isFinalDay(v) を呼び、実在の当日ファイルでは全会場で TypeError になって
  // 当日予測タブが「データの取得に失敗しました」になっていた(2026-10-04 の版から)。この枝の
  // isFinalDay(v) への修正が、世代1の画面の動きを変えた唯一の箇所。再発を防ぐために本物のファイルで確かめる
  const predDir = path.join(root, "docs", "predictions");
  const dayFiles = fs.existsSync(predDir) ? fs.readdirSync(predDir).filter((f) => /^2026\d{4}\.json$/.test(f)).sort().slice(-8) : [];
  if (!dayFiles.length) {
    check("S7 実在の当日ファイルが無いので会場別の描画は省いた(docs/predictions/2026*.json)", true);
  } else {
    let nV = 0, nThrow = 0, nLabel = 0, nBody = 0, detail = "";
    for (const f of dayFiles) {
      const P = JSON.parse(fs.readFileSync(path.join(predDir, f), "utf8"));
      if (!P.venues || !P.venues.length) continue;
      A.setPred(P); A.setLatest(P.date); A.setMode("venue");
      for (const v of P.venues) {
        nV++;
        A.setVenue(v.code);
        env.main.innerHTML = ""; env.byId.hdrChips && (env.byId.hdrChips.innerHTML = "");
        try { A.renderToday(); } catch (e) { nThrow++; if (!detail) detail = f + " 会場 " + v.code + ": " + (e && e.message); continue; }
        const chips = env.idText("hdrChips");
        // 自分の会場の名前(1〜2文字は全角空白で埋める)と、日程のラベル(最終日 / 初 日 / n日目 / —)が出ている
        if (chips.includes(String(v.name).slice(0, 1)) && /最終日|初\s*日|日目|—/.test(chips)) nLabel++;
        else if (!detail) detail = f + " 会場 " + v.code + " chips: " + chips.slice(0, 200);
        if (/推奨買い目|予測対象外|レースがありません/.test(env.text())) nBody++;
        else if (!detail) detail = f + " 会場 " + v.code + " body: " + env.text().slice(0, 200);
      }
    }
    check(`S7 実在の当日ファイル ${dayFiles.length} 日・${nV} 会場で会場別の renderToday が例外にならない(isFinalDay)`, nV > 0 && nThrow === 0, detail || `例外 ${nThrow}/${nV}`);
    check(`S7 全 ${nV} 会場で会場チップに名前と日程のラベルが出て、本文に買い目が出る`, nLabel === nV && nBody === nV, detail || `ラベル ${nLabel}/${nV} 本文 ${nBody}/${nV}`);
    A.setPred(null); A.setVenue(null);
  }
}

// ================= G: 世代の読み取り =================
{
  eq("G1 レースが無い/印が無い/null は世代1", [A.raceGen(undefined), A.raceGen({}), A.raceGen({ g: null })], [1, 1, 1]);
  eq("G2 g=1 / g=2 / 文字列の \"2\" / 2.0", [A.raceGen({ g: 1 }), A.raceGen({ g: 2 }), A.raceGen({ g: "2" }), A.raceGen({ g: 2.0 })], [1, 2, 2, 2]);
  eq("G3 読めない値(0, -1, \"x\", NaN)は世代1", [A.raceGen({ g: 0 }), A.raceGen({ g: -1 }), A.raceGen({ g: "x" }), A.raceGen({ g: NaN })], [1, 1, 1, 1]);
  eq("G4 しきい値: 世代1・印なし・読めない値は世代1の表", [A.sengenCfgFor({ g: 1 }).top3p_min, A.sengenCfgFor({}).top3p_min, A.sengenCfgFor(1).top3p_min, A.sengenCfgFor("x").top3p_min], [0.36, 0.36, 0.36, 0.36]);
  eq("G4 しきい値: 世代2(レース・番号・文字列)", [A.sengenCfgFor({ g: 2 }).top3p_min, A.sengenCfgFor(2).top3p_min, A.sengenCfgFor("2").top3p_min], [0.46, 0.46, 0.46]);
  eq("G5 表に無い世代(3)は世代1の値に落ちる(現行の動きを変えない側)", A.sengenCfgFor({ g: 3 }), A.SENGEN_CFG_BY_GEN[1]);
}

// ================= T: 厳選の候補判定 =================
{
  // 打刻の無いレース(候補): 確率条件は世代のしきい値で
  eq("T1 top3p 0.40・7R・12場: 印なし=候補 / g=1=候補 / g=2=候補でない(0.46 未満)",
    [A.sengenOk(race(), 12), A.sengenOk(race({ g: 1 }), 12), A.sengenOk(race({ g: 2 }), 12)], [true, true, false]);
  eq("T1 top3p 0.47: どの世代でも候補", [A.sengenOk(race({ top3p: 0.47 }), 12), A.sengenOk(race({ top3p: 0.47, g: 2 }), 12)], [true, true]);
  eq("T1 top3p 0.35: どの世代でも候補でない", [A.sengenOk(race({ top3p: 0.35 }), 12), A.sengenOk(race({ top3p: 0.35, g: 2 }), 12)], [false, false]);
  // 境目(浮動小数の足し算で「ちょうど」は作れないので、しきい値の上下 0.0001 で見る)
  eq("T2 境目: 世代1は 0.3601 で候補・0.3599 で候補でない",
    [A.sengenOk(race({ top3p: 0.3601 }), 12), A.sengenOk(race({ top3p: 0.3599 }), 12)], [true, false]);
  eq("T2 境目: 世代2は 0.4601 で候補・0.4599 で候補でない(0.36 と 0.46 の間は世代1だけ候補)",
    [A.sengenOk(race({ top3p: 0.4601, g: 2 }), 12), A.sengenOk(race({ top3p: 0.4599, g: 2 }), 12), A.sengenOk(race({ top3p: 0.4599, g: 1 }), 12)], [true, false, true]);
  eq("T3 4R以前は候補でない(世代に関わらず)", [A.sengenOk(race({ no: 4, top3p: 0.6 }), 12), A.sengenOk(race({ no: 4, top3p: 0.6, g: 2 }), 12), A.sengenOk(race({ no: 5, top3p: 0.6, g: 2 }), 12)], [false, false, true]);
  eq("T4 除外会場(3,4,14)は候補でない(世代に関わらず)", [3, 4, 14].map((v) => A.sengenOk(race({ top3p: 0.6 }), v)).concat([3, 4, 14].map((v) => A.sengenOk(race({ top3p: 0.6, g: 2 }), v))), [false, false, false, false, false, false]);
  // オッズ条件(min_odds)も世代の表から
  const oddsOf = (a, b, c) => ({ t3: { "1-2-3": a, "1-3-2": b, "1-2-4": c } });
  eq("T5 上位3点に 3.1 倍未満があれば候補でない / 全部 3.1 以上なら候補(両世代)",
    [A.sengenOk(race({ top3p: 0.5, odds: oddsOf(3.0, 5, 8) }), 12), A.sengenOk(race({ top3p: 0.5, odds: oddsOf(3.1, 5, 8) }), 12),
     A.sengenOk(race({ top3p: 0.5, g: 2, odds: oddsOf(3.0, 5, 8) }), 12), A.sengenOk(race({ top3p: 0.5, g: 2, odds: oddsOf(3.1, 5, 8) }), 12)], [false, true, false, true]);
  eq("T5 オッズが無い買い目は条件を妨げない(取得前は候補)", A.sengenOk(race({ top3p: 0.5, g: 2, odds: oddsOf(4.0, null, undefined) }), 12), true);
  // 打刻は世代より優先(打刻済みのレースはしきい値を変えても変わらない)
  eq("T6 tk=1 なら確率が低くても厳選(g=2 でも)", [A.sengenOk(race({ top3p: 0.2, tk: 1 }), 12), A.sengenOk(race({ top3p: 0.2, tk: 1, g: 2 }), 12)], [true, true]);
  eq("T6 tk=0 なら確率が高くても厳選でない", [A.sengenOk(race({ top3p: 0.6, tk: 0 }), 12), A.sengenOk(race({ top3p: 0.6, tk: 0, g: 2 }), 12)], [false, false]);
  eq("T6 旧・結果側の tk も同じ", A.sengenOk(race({ top3p: 0.2, g: 2, result: { order: "1-2-3", tk: 1 } }), 12), true);
  eq("T7 表に無い世代(g=3)は世代1のしきい値で判定", A.sengenOk(race({ top3p: 0.40, g: 3 }), 12), true);
}

// ================= C: 較正表の選び方 =================
{
  // 表: 値を見れば どの表が選ばれたか分かるように、同じ x で違う値を返す表を使う
  const GEN1 = { t5e: [[30, 31], [40, 46], [100, 61]], t5l: [[30, 30], [40, 45], [100, 60]], t3e: [[30, 41], [40, 51], [100, 61]], t3l: [[30, 40], [40, 50], [100, 60]] };
  const GEN2 = { t5e: [[30, 21], [40, 36], [100, 56]], t5l: [[30, 20], [40, 35], [100, 55]], t3e: [[30, 31], [40, 39], [100, 51]], t3l: [[30, 30], [40, 39], [100, 50]] };
  const TOP = { t5e: [[30, 11], [40, 26], [100, 31]], t5l: [[30, 10], [40, 25], [100, 30]], t3e: [[30, 11], [40, 21], [100, 31]], t3l: [[30, 10], [40, 20], [100, 30]] };
  const r = race({ top3p: 0.40, top5p: 0.40 }), r1 = race({ top3p: 0.40, top5p: 0.40, g: 1 }), r2 = race({ top3p: 0.40, top5p: 0.40, g: 2 });
  // C1) calib.json が取れていない(CALIB=null): 内蔵の表(世代に関わらず)
  A.setCalib(null);
  check("C1 CALIB が無い時は内蔵の表(世代1・世代2・印なし とも)", A._tb("t5l", 1) === A.CAL_T5L && A._tb("t5l", 2) === A.CAL_T5L && A._tb("t3l", undefined) === A.CAL_T3L);
  eq("C1 内蔵の表での calT5(top5p 0.40, 5R以降)= 48 で、注目", [A.calT5(r), A.actLbl(r), A.actLbl(r2)], [48, "注目", "注目"]);
  // C2) 古い形の calib.json(トップレベルだけ、gens 無し): どの世代もトップレベル
  A.setCalib(Object.assign({ updated_at: "x", races: 10 }, TOP));
  eq("C2 gens の無い古い形: 世代1も世代2も印なしもトップレベルの表", [A._tb("t5l", 1), A._tb("t5l", 2), A._tb("t5l", undefined)].map((t) => t === A.setCalib && 0 || t[1][1]), [25, 25, 25]);
  eq("C2 古い形での表示(top5p 0.40 → 25%)は世代に関わらず様子見", [A.calT5(r), A.calT5(r1), A.calT5(r2), A.actLbl(r), A.actLbl(r2)], [25, 25, 25, "様子見", "様子見"]);
  // C3) 新しい形(gens あり。トップレベル = 現在の世代の表 = gens["1"])
  const NEW = Object.assign({ updated_at: "x", races: 10, gen: 1, gens: { "1": GEN1, "2": GEN2 } }, GEN1);
  A.setCalib(NEW);
  eq("C3 世代1のレースは gens[\"1\"]、世代2は gens[\"2\"]、印なしは世代1", [A._tb("t5l", 1), A._tb("t5l", 2), A._tb("t5l", undefined)].map((t) => t[1][1]), [45, 35, 45]);
  eq("C3 1〜4R は e 表、5R以降は l 表(世代ごと)", [A.calT5(race({ no: 3, top5p: 0.40, g: 1 })), A.calT5(race({ no: 3, top5p: 0.40, g: 2 })), A.calT5(r1), A.calT5(r2)], [46, 36, 45, 35]);
  eq("C3 同じ生の確率(top5p 0.40)で、世代1=注目 / 世代2=様子見 / 印なし=注目", [A.actLbl(r1), A.actLbl(r2), A.actLbl(r)], ["注目", "様子見", "注目"]);
  eq("C3 calT3 も世代の表(top3p 0.40 → 世代1 50% / 世代2 39%)", [A.calT3(r1), A.calT3(r2), A.calT3(r)], [50, 39, 50]);
  eq("C3 %バッジの色は世代の表で出した値から(世代1=金 50% / 世代2=灰 39%)", [A.cumCls(A.calT3(r1) / 100), A.cumCls(A.calT3(r2) / 100)], ["confA", "confC"]);
  // 打刻済みの att は表より優先
  eq("C4 att があればそれを使う(世代・表に関わらず)", [A.actLbl(race({ top5p: 0.40, g: 1, att: 0 })), A.actLbl(race({ top5p: 0.40, g: 2, att: 1 })), A.actLbl(race({ top5p: 0.10, att: 1 }))], ["様子見", "注目", "注目"]);
  // C5) 世代の表が欠けている時はトップレベルへ
  A.setCalib(Object.assign({ gens: { "1": GEN1, "2": { t5e: GEN2.t5e, t5l: [] } } }, TOP));
  eq("C5 gens[\"2\"] に無い表(t3l)・空の表(t5l)はトップレベルに落ちる。ある表(t5e)は使う", [A._tb("t3l", 2)[1][1], A._tb("t5l", 2)[1][1], A._tb("t5e", 2)[1][1]], [20, 25, 36]);
  A.setCalib({ gens: { "1": GEN1 } });
  eq("C5 gens に世代2が無く、トップレベルも無ければ内蔵の表", [A._tb("t5l", 2) === A.CAL_T5L, A._tb("t5l", 1)[1][1]], [true, 45]);
  // C6) 壊れた形でも落ちない
  for (const bad of [{ gens: "x" }, { gens: [1, 2] }, { gens: { "2": "x" } }, { gens: { "2": [1] } }, { t5l: "x" }, { t5l: {} }, { gens: null }]) {
    A.setCalib(bad);
    noThrow("C6 壊れた calib.json(" + JSON.stringify(bad) + ")でも _tb/actLbl/calT3 が例外にならない", () => { A._tb("t5l", 2); A.actLbl(r2); A.calT3(r2); A.actLbl(r); });
  }
  A.setCalib(null);
}

// ================= M: 設定タブのモデル情報 =================
{
  const base = { trained_at: "2026-10-06 05:12", rows_train: 1234567, rows_valid: 1, rows_test: 2, period: ["20210611", "20261005"],
    best_iterations: { win: 1, top2: 2, top3: 3 }, sengen: { top5_min: 0.4 },
    test_metrics: { races: 29219, win_hit_rate: 0.5691, top1_hit_rate: 0.0986, top5_hit_rate: 0.349, top10_hit_rate: 0.515, top5_roi: 0.7857, sengen: { races: 4574, races_per_day: 23.5, top5_min: 0.4, top5_hit_rate: 0.523 } },
    baseline_metrics: { win_hit_rate: 0.5, top1_hit_rate: 0.05, top5_hit_rate: 0.2, top10_hit_rate: 0.3 },
    feature_importance: { a: 300, b: 200, c: 100 } };
  A.setPred(null);
  A.VIEW.acc = null; A.VIEW.calib = null;
  noThrow("M1 今の形の報告で描ける", () => A.renderModel(base));
  check("M1 学習期間・学習行数・高確率帯・特徴量が出る", env.text().includes("20210611 - 20261005") && env.text().includes("1,234,567") && env.text().includes("52.3% (23.5R/日)") && env.text().includes("a 300"), env.text());
  check("M1 世代の無い報告(世代1)には「モデル」(世代2の説明)の行を出さない(今の文言のまま)", !env.text().includes("世代2:") && !env.text().includes("特徴量153個"), env.text());
  check("M1 プラン条件の文言は今のまま(36%・3.1倍。段階Bで差し替え)", env.text().includes("TOP3合計確率36%以上"));
  // 項目が欠ける・形が違う
  const variants = [
    ["period が文字列", Object.assign({}, base, { period: "2021-2026" }), "2021-2026"],
    ["period が無い", (() => { const o = Object.assign({}, base); delete o.period; return o; })(), "学習期間 -"],
    ["period が数値", Object.assign({}, base, { period: 20261005 }), "20261005"],
    ["test_metrics が無い", (() => { const o = Object.assign({}, base); delete o.test_metrics; return o; })(), "0.0%"],
    ["baseline_metrics が無い", (() => { const o = Object.assign({}, base); delete o.baseline_metrics; return o; })(), "基準 0.0%"],
    ["sengen に races_per_day が無い", Object.assign({}, base, { test_metrics: Object.assign({}, base.test_metrics, { sengen: { top5_min: 0.47, top5_hit_rate: 0.5 } }) }), "合計47%以上"],
    ["sengen が無い", Object.assign({}, base, { test_metrics: Object.assign({}, base.test_metrics, { sengen: undefined }) }), "高確率帯 TOP5内 -"],
    ["test_metrics が文字列", Object.assign({}, base, { test_metrics: "x" }), "0.0%"],
    ["feature_importance が配列", Object.assign({}, base, { feature_importance: [["a", 1], ["b", 2]] }), "特徴量重要度 TOP10"],
    ["feature_importance が無い", (() => { const o = Object.assign({}, base); delete o.feature_importance; return o; })(), "特徴量重要度 TOP10"],
    ["feature_importance の値が文字列", Object.assign({}, base, { feature_importance: { a: "x", b: 5 } }), "b 5"],
    ["rows_train が文字列", Object.assign({}, base, { rows_train: "12" }), "12"],
    ["trained_at が無い", (() => { const o = Object.assign({}, base); delete o.trained_at; return o; })(), "学習日時 -"],
    ["best_iterations が {pl: n}", Object.assign({}, base, { best_iterations: { pl: 1200 } }), "20210611 - 20261005"],
    ["空の報告 {}", {}, "モデル情報"],
  ];
  for (const [label, rep, want] of variants) {
    noThrow("M2 " + label + " でも落ちない", () => A.renderModel(rep));
    check("M2 " + label + ": 表示に「" + want + "」", env.text().includes(want), env.text().slice(0, 400));
    check("M2 " + label + ": 「undefined」「NaN」が表示に出ない", !/undefined|NaN/.test(env.text()), env.text().slice(0, 400));
  }
  noThrow("M2 報告が null でも落ちない", () => A.renderModel(null));
  check("M2 報告が null: 「モデル情報がありません」", env.text().includes("モデル情報がありません"));
  // 世代2の報告(特徴量153個・1本のモデル)
  const fi153 = {}; for (let i = 0; i < 153; i++) fi153[(i < 45 ? "f" : "h_") + i] = 1000 - i * 3;
  const rep2 = Object.assign({}, base, { format: 2, gen: 2, best_iterations: { pl: 1800 }, feature_importance: fi153, features: Object.keys(fi153), stage_cols: ["stage"], cat_idx: [0] });
  noThrow("M3 世代2の報告(feature_importance 153個)でも落ちない", () => A.renderModel(rep2));
  check("M3 世代2の説明文(特徴量153個・1本で120通りを直接出す)が出る", env.text().includes("世代2:") && env.text().includes("特徴量153個") && env.text().includes("120通り"), env.text());
  eq("M3 特徴量重要度は上位10個だけ", (env.main.innerHTML.match(/<div class="kv"><span>(f|h_)\d+<\/span>/g) || []).length, 10);
  check("M3 上位10個は値の大きい順(f0〜f9)", env.main.innerHTML.indexOf("<span>f0</span>") >= 0 && env.main.innerHTML.indexOf("<span>f9</span>") >= 0 && env.main.innerHTML.indexOf("<span>f10</span>") < 0);
  // 並びに頼らず値で降順にするので、辞書の並びが昇順でも同じ上位10個
  const fiAsc = {}; Object.keys(fi153).reverse().forEach((k) => { fiAsc[k] = fi153[k]; });
  A.renderModel(Object.assign({}, rep2, { feature_importance: fiAsc }));
  check("M3 辞書の並びが昇順でも上位10個は同じ", env.main.innerHTML.indexOf("<span>f0</span>") >= 0 && env.main.innerHTML.indexOf("<span>f10</span>") < 0);
  A.renderModel(Object.assign({}, base, { gen: "2" }));
  check("M3 gen が文字列の \"2\" でも世代2の説明文", env.text().includes("世代2:"));
  A.renderModel(Object.assign({}, base, { gen: 1 }));
  check("M3 gen=1 なら説明文を出さない", !env.text().includes("世代2:"));
  // 本物の docs/model_report.json(あれば)
  const mr = path.join(root, "docs", "model_report.json");
  if (fs.existsSync(mr)) {
    const rep = JSON.parse(fs.readFileSync(mr, "utf8"));
    noThrow("M4 本物の docs/model_report.json で描ける", () => A.renderModel(rep));
    const vals = Object.values(rep.feature_importance || {});
    check("M4 本物の feature_importance は降順(=値で並べ直しても表示は変わらない)", vals.every((v, i) => i === 0 || vals[i - 1] >= v));
  }
  // calib.json の形(verify が欠ける・形が違う)
  for (const cal of [{ updated_at: "x", races: 1 }, { verify: "x" }, { verify: { full: "x", recent30: null } }, { verify: { full: [{ band: "40-49", n: 10, act: 44 }] }, n_races: "12" }, { gens: { "1": {}, "2": {} }, gen: 1, verify: {} }]) {
    A.VIEW.calib = cal;
    noThrow("M5 calib.json が " + JSON.stringify(cal) + " でも設定タブが描ける", () => A.renderModel(base));
    check("M5 「表示的中率の検証」が出て、undefined/NaN が無い", env.text().includes("表示的中率の検証") && !/undefined|NaN/.test(env.text()), env.text().slice(0, 300));
  }
  A.VIEW.calib = null;
}

// ================= A: 的中実績・監視の「モデル更新日」 =================
{
  const day = (date, o = {}) => Object.assign({ date, races: 100, win_hit: 50, top1_hit: 10, top5_hit: 30, stake5: 50000, return5: 40000, sen_n: 1, sen_hit: 1, sen_stake: 300, sen_ret: 900,
    att_n: 40, att_hit: 20, att_stake: 20000, att_ret: 18000, yos_n: 60, yos_hit: 10, yos_stake: 30000, yos_ret: 20000 }, o);
  const total = (days) => days.reduce((t, d) => { for (const k of Object.keys(d)) if (k !== "date" && k !== "gen") t[k] = (t[k] || 0) + d[k]; return t; }, {});
  // modelGenStarts(純粋な関数)
  eq("A1 gen の無い日だけなら空", A.modelGenStarts({ days: [day("20261001"), day("20261002")] }, null), []);
  eq("A1 gen=1 の日だけでも空", A.modelGenStarts({ days: [day("20261001", { gen: 1 }), day("20261002", { gen: 1 })] }, null), []);
  eq("A1 世代が 1→2 に変わった日が更新日", A.modelGenStarts({ days: [day("20261001"), day("20261002", { gen: 1 }), day("20261003", { gen: 2 }), day("20261004", { gen: 2 })] }, null), [{ gen: 2, date: "20261003" }]);
  eq("A1 1→2→1(予備のモデルに落ちた日)は両方の切り替わりを出す", A.modelGenStarts({ days: [day("20261003", { gen: 2 }), day("20261001"), day("20261004", { gen: 1 })] }, null), [{ gen: 2, date: "20261003" }, { gen: 1, date: "20261004" }]);
  eq("A1 実績の行がまだ無い当日は当日ファイルの model_gen で判断", A.modelGenStarts({ days: [day("20261001")] }, { date: "20261002", model_gen: 2 }), [{ gen: 2, date: "20261002" }]);
  eq("A1 当日ファイルに model_gen が無ければ世代1(=今の本番。何も出ない)", A.modelGenStarts({ days: [day("20261001")] }, { date: "20261002" }), []);
  eq("A1 当日の行が実績にもある時は実績の gen を使う(二重に数えない)", A.modelGenStarts({ days: [day("20261001"), day("20261002", { gen: 1 })] }, { date: "20261002", model_gen: 2 }), []);
  eq("A1 実績も当日ファイルも無ければ空", [A.modelGenStarts(null, null), A.modelGenStarts({}, {}), A.modelGenStarts({ days: "x" }, null)], [[], [], []]);
  // renderAcc: 世代2の日が無い → 「モデル更新」は出ない(今の表示のまま)
  A.setPred(null); A.setLatest("20261002");
  const days1 = [day("20261001"), day("20261002", { gen: 1 })];
  noThrow("A2 的中実績(世代の印なし)が描ける", () => A.renderAcc({ days: days1, total: total(days1) }));
  check("A2 世代2の日が無ければ「モデル更新」の印は出ない", !env.text().includes("モデル更新") && env.text().includes("累計") && env.text().includes("2日(金)"), env.text());
  // 世代2の日がある → その日の行と累計の見出しに印
  const days2 = [day("20261001"), day("20261002", { gen: 1 }), day("20261003", { gen: 2 }), day("20261004", { gen: 2 })];
  A.setLatest("20261004");
  noThrow("A3 的中実績(世代2の日あり)が描ける", () => A.renderAcc({ days: days2, total: total(days2) }));
  const h = env.main.innerHTML;
  const marks = (h.match(/モデル更新/g) || []).length;
  eq("A3 印は2か所(累計の見出しと 10/3 の行)", marks, 2);
  check("A3 累計の見出しに「モデル更新 10/3」", h.includes("累計</span><span class=\"gmark\">モデル更新 10/3</span>"), h.slice(0, 600));
  check("A3 3日(土)の行に印、4日(日)・2日(金)の行には無い", h.includes("3日(土)<span class=\"gmark\">モデル更新</span>") && !h.includes("4日(日)<span class=\"gmark\">") && !h.includes("2日(金)<span class=\"gmark\">"), h);
  // 当日(速報)の行: 実績の行はまだ無く、当日ファイルの model_gen が 2
  const P = { date: "20261005", model_gen: 2, venues: [{ code: 12, name: "住之江", races: [Object.assign(race({ top3p: 0.4, g: 2 }), { result: { order: "1-2-3", pay3t: 1200, hit_win: 1 } })] }] };
  A.setPred(P); A.setLatest("20261005");
  noThrow("A4 本日速報つきの的中実績が描ける", () => A.renderAcc({ days: days1, total: total(days1) }));
  const h2 = env.main.innerHTML;
  check("A4 速報の行に印、累計の見出しに「モデル更新 10/5」", h2.includes("速報</span><span class=\"gmark\">モデル更新</span>") && h2.includes("モデル更新 10/5"), h2.slice(0, 800));
  eq("A4 印は2か所", (h2.match(/モデル更新/g) || []).length, 2);
  A.setPred(null);
  // 設定タブの「バッジ実測の監視」にモデル更新日
  A.VIEW.acc = { days: days1, total: total(days1) };
  A.renderModel({});
  check("A5 世代2の日が無ければ監視に「モデル更新日」を出さない", env.text().includes("バッジ実測の監視") && !env.text().includes("モデル更新日"), env.text());
  A.VIEW.acc = { days: days2, total: total(days2) };
  A.renderModel({});
  check("A5 世代2の日があれば監視に「モデル更新日 2026-10-03(世代2)」", env.text().includes("モデル更新日 2026-10-03(世代2)"), env.text());
  A.VIEW.acc = { days: days1, total: total(days1) };
  A.setPred({ date: "20261005", model_gen: 2, venues: [] });
  A.renderModel({});
  check("A5 当日ファイルの model_gen が 2 なら、実績の行が無くても監視に更新日", env.text().includes("モデル更新日 2026-10-05(世代2)"), env.text());
  A.setPred(null); A.VIEW.acc = null;
}

console.log("");
console.log(failed ? `失敗 ${failed}件 / 成功 ${passed}件` : `全${passed}件 成功`);
if (!failed) console.log("ALL OK");
process.exit(failed ? 1 : 0);

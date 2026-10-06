# -*- coding: utf-8 -*-
"""較正テーブル自動生成: docs/predictions/YYYYMMDD.json の決着レースを集計し、
フロント(docs/index.html)が表示する的中率(TOP5/TOP3/TOP4 x 序盤/中盤以降)の
較正テーブルを実測データから再計算して docs/calib.json に書き出す。

目的:
    フロントの CAL_T5E〜CAL_T4L は「予測確率(picks上位合計)→表示%」の較正テーブル
    だが、これまでは手作業で作った固定値だった。本スクリプトを毎日の結果反映(daily.yml)
    後に走らせることで、日々増えていく決着レースの実測的中率に表示%を自動追随させ、
    「表示%=実測的中率%」の状態を保守なしで維持し続けることを目的とする。

仕様(docs/index.html の CAL_T5E〜CAL_T4L / calPct / calT5〜calT4 と同一のロジック):
    対象   : result.order があり result.status が無く picks が5点以上ある決着レース。
    6表    : (序盤E = 1〜4R / 中盤以降L = 5R以降) x (T5 = top5p / T3 = top3p / T4 = top4p)。
    ビン   : 下で定義する固定境界(表示%=生値(0-1)x100 で分類)。世代ごとに別の境界を持つ。
    採用   : 各ビンはサンプル数 n >= 25 のときのみ採用する。
    値     : 採用ビンごとに (ビン中央, 実測的中率%) の点を作り、
             重み付きPAV(pool adjacent violators)で単調非減少化した値を最終値とする
             (フロント初期テーブル作成時と同一手法)。

モデルの世代(2026-10 の作り直しから):
    モデルを作り直すと確率の目盛りが変わる(同じ top5p でも実際の的中率が違う)ので、
    世代の違うレースを1つの表に混ぜない。レースの印 g(無ければ世代1。common.race_gen と
    同じ規則)で分けて、世代ごとに6表を作る。
    世代1の表は世代1のレースだけから、今までと同じビンで作る(切り替え後は増えないので
    自然に固定され、今と同じ数字になる)。
    世代2の表は「種」+ 世代2の本番レースから作る。種は train.py が学習に使っていない
    試験期間の予測から数えたビン別の [件数, 的中数](data/calib_seed_gen2.json)。
    切り替えの最初の朝は本番の世代2のレースが0件なので、種が無いと表が空になる。
    種は本番60日分相当(SEED_TARGET_RACES)に重みで縮めて足し、本番の世代2のレースが
    SEED_DROP_RACES を超えたら外す(本番の目盛りに追随させる)。

出力 docs/calib.json:
    {
      "updated_at": "YYYY-MM-DDTHH:MM+09:00", "races": 現在の世代の決着レース数,
      "t5e": [[x, y], ...], "t3e": [...], "t4e": [...],      ← 現在の世代の表(今までと同じ置き場)
      "t5l": [...], "t3l": [...], "t4l": [...],
      "verify": {"full": [...], "recent30": [...]},          ← 現在の世代の自己検証
      "gen": 現在の世代,
      "gens": {"1": {"t5e"..."t4l", "races", "verify"},
               "2": {"t5e"..."t4l", "races", "verify", "seed": {...}}}
    }
verify は較正結果を使って各レースの表示%(フロント calPct と同一実装)を求め、
表示帯別に「表示%の平均」と「実測的中率%」を突き合わせた自己検証(T5のみ)。
世代2の verify は本番の世代2のレースだけで作る(種は入れない。最初の数週間は空になる)。
使う側(common._calib_tbl / 画面の _tb)はレースの世代の表 gens[g] を引き、無ければ
トップレベルに落ちる。

現在の世代: 引数 --gen。省略時は、最後に学習したモデルの報告 docs/model_report.json の gen
(train.py が meta.json と一緒に書く。無い・読めない・gen が無ければ 1)。
種のファイルの有無では決めない: 種は世代2の学習が1回でも成功すれば残り続けるので、
MODEL_GEN を 1 に戻した日や、世代2の学習が時間切れで止まって前日の世代1の資産で予測する日に、
使うモデルと違う世代の表がトップレベルに出てしまう。model_report.json は学習が最後まで通った
時だけ書き換わり、daily.yml はその日の学習 → 配布 → 較正表 の順なので、朝の予測に使うモデルと
同じ世代になる(配布が失敗した日もその実行機では data/model_build の同じモデルで予測する)。
段階Aでは model_report.json に gen が無いので 1 のまま。daily.yml から明示的に渡してもよい。

種の作り方(train.py から import する):
    seed_from_predictions(rows)   rows = [{"no", "picks", "order"}, ...](picks は上位5点でよい)
                                  → {"t5e": [[n, hit], ...], ..., "t4l": [...]}(世代2のビン順)
    make_seed(rows, built_from)   → {"gen": 2, "built_from": "...", "bins": {...}}(保存する形)
"""
import argparse
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    # レースの世代の読み方は判定側(common.py)と同じ規則を使う(印 g。無ければ世代1)。
    from common import race_gen
except ImportError:  # common に race_gen がまだ無い・単体で動かす時の予備(同じ規則)
    def race_gen(race) -> int:
        try:
            g = (race or {}).get("g")
        except Exception:
            return 1
        if g is None:
            return 1
        try:
            return int(g)
        except Exception:
            return 1

ROOT = Path(__file__).parent.parent
PRED_DIR = ROOT / "docs" / "predictions"
OUT_PATH = ROOT / "docs" / "calib.json"
SEED_PATH = ROOT / "data" / "calib_seed_gen2.json"
REPORT_PATH = ROOT / "docs" / "model_report.json"   # 最後に学習したモデルの報告(gen を読む)
JST = timezone(timedelta(hours=9))

# ビン定義: (下限%, 上限%) の半開区間。表示%=生値(0-1)x100 をここで分類する。
# 中央値はdocs/index.htmlのCAL_T5E等のx座標と同じ(区間の算術平均)。
# 世代1(現行モデル)のビン。変えない(世代1の表が今と同じ数字になること)。
T5_BINS = [(0, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 40), (40, 45), (45, 50), (50, 100)]
T3_BINS = [(0, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 36), (36, 100)]
T4_BINS = [(0, 12), (12, 18), (18, 24), (24, 30), (30, 36), (36, 42), (42, 100)]

# 世代2のビン: 上側を細かくする。世代2のモデルは top5p 0.50 以上が全レースの約10%(世代1は約1%)、
# top3p 0.36 以上が約11%(同約2%)あり、世代1のビンだと最後のビンに広い範囲が入って
# 厳選帯(top3p 0.45前後)の%バッジが実際より10ポイント以上低く出るため。
T5_BINS_V2 = [(0, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 40), (40, 45), (45, 50),
              (50, 55), (55, 60), (60, 100)]
T3_BINS_V2 = [(0, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 40), (40, 45),
              (45, 50), (50, 100)]
T4_BINS_V2 = [(0, 12), (12, 18), (18, 24), (24, 30), (30, 36), (36, 42), (42, 48), (48, 100)]

# 世代 → (上位k点 → ビン)。知らない世代のレースは表を作らない(警告を出して飛ばす)。
BINS_BY_GEN = {
    1: {5: T5_BINS, 3: T3_BINS, 4: T4_BINS},
    2: {5: T5_BINS_V2, 3: T3_BINS_V2, 4: T4_BINS_V2},
}
TABLE_KEYS = ["t5e", "t3e", "t4e", "t5l", "t3l", "t4l"]
# 表の名前 → (上位k点, 序盤か)。"e" = 1〜4R、"l" = 5R以降。
_TABLE_SPEC = {"t5e": (5, True), "t3e": (3, True), "t4e": (4, True),
               "t5l": (5, False), "t3l": (3, False), "t4l": (4, False)}

SEED_GEN = 2                # 種を持つ世代(世代1は本番の予測だけで足りる)
SEED_TARGET_RACES = 10000   # 種をこの件数相当に縮めて足す(本番60日分相当)
SEED_DROP_RACES = 20000     # 世代2の本番の決着レースがこれを超えたら種を外す

MIN_BIN_N = 25          # 較正テーブル採用の最低サンプル数
VERIFY_BAND_MIN_N = 20  # 検証帯の最低サンプル数
VERIFY_BANDS = [(0, 20), (20, 30), (30, 40), (40, 50), (50, 100)]
VERIFY_BAND_LABELS = ["0-19", "20-29", "30-39", "40-49", "50-100"]
RECENT_DAYS = 30


def _atomic_write_text(path: Path, text: str):
    """同一ディレクトリのtmpファイルに書いてからos.replaceで差し替える(破損防止)。
    Windowsローカル実行でも文字化けしないようencoding=utf-8を明示する。"""
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def js_round(x: float) -> int:
    """JSのMath.round相当(0.5は常に+方向に丸める)。x>=0前提。"""
    return math.floor(x + 0.5)


def top_k_p(picks, k):
    return sum((p.get("p") or 0) for p in (picks or [])[:k])


def hit_top_k(picks, order, k):
    return any(p.get("c") == order for p in (picks or [])[:k])


def bin_index(x, bins):
    """xが属するビンのindexを返す(下限含む上限含まずの半開区間、最終ビンのみ上限側も含む)。"""
    n = len(bins)
    for i, (lo, hi) in enumerate(bins):
        if i == n - 1:
            if x >= lo:
                return i
        elif lo <= x < hi:
            return i
    return None


def weighted_pav(points):
    """(weight, value) の列を重み付きPAV(pool adjacent violators)で
    単調非減少列に変換する(フロント初期テーブル作成時と同一手法)。"""
    blocks = []  # 各要素 [sum(w*v), sum(w), count]
    for w, v in points:
        blocks.append([w * v, w, 1])
        while len(blocks) >= 2 and (blocks[-2][0] / blocks[-2][1]) > (blocks[-1][0] / blocks[-1][1]):
            b2 = blocks.pop()
            b1 = blocks.pop()
            blocks.append([b1[0] + b2[0], b1[1] + b2[1], b1[2] + b2[2]])
    out = []
    for sw, w, cnt in blocks:
        out.extend([sw / w] * cnt)
    return out


def bin_counts(races, k, bins):
    """races の上位k点をビンに振り分け、ビンごとの [n, hit] を返す(種と同じ形)。"""
    stats = [[0, 0] for _ in bins]
    for r in races:
        x = top_k_p(r["picks"], k) * 100
        idx = bin_index(x, bins)
        if idx is None:
            continue
        stats[idx][0] += 1
        if hit_top_k(r["picks"], r["order"], k):
            stats[idx][1] += 1
    return stats


def table_from_counts(stats, bins):
    """ビンごとの [n, hit] から、n>=MIN_BIN_N のビンのみ採用し、重み付きPAVで
    単調非減少化した [center, rate%(小数1桁)] の配列を返す。"""
    adopted = [(i, n, h) for i, (n, h) in enumerate(stats) if n >= MIN_BIN_N]
    if not adopted:
        return []
    points = [(n, (h / n) * 100.0) for (_, n, h) in adopted]
    fitted = weighted_pav(points)
    centers = [(bins[i][0] + bins[i][1]) / 2.0 for (i, _, _) in adopted]
    return [[c, round(v, 1)] for c, v in zip(centers, fitted)]


def build_table(races, k, bins, seed=None, seed_weight=1.0):
    """races: [{"no", "picks", "order"}, ...] の1グループ(序盤 or 中盤以降)。
    n>=MIN_BIN_N のビンのみ採用し、重み付きPAVで単調非減少化した
    [center, rate%(小数1桁)] の配列を返す。
    seed: 同じビン順の [[n, hit], ...](世代2の種)。seed_weight 倍して本番の件数に足す
    (種が無い世代1では何も足さないので、今までと同じ計算になる)。"""
    stats = bin_counts(races, k, bins)
    if seed and seed_weight > 0:
        for i, (n, h) in enumerate(seed[:len(bins)]):
            stats[i][0] += n * seed_weight
            stats[i][1] += h * seed_weight
    return table_from_counts(stats, bins)


def cal_pct(p, tbl):
    """フロントcalPct(docs/index.html)と同一の区分線形補間のローカル実装。
    pは0-1の生値(top5p等)、tblは[[x_center, y_pct], ...]の較正テーブル。"""
    if not tbl:
        return js_round(p * 100)
    x = p * 100
    if x <= tbl[0][0]:
        return js_round(tbl[0][1] * x / tbl[0][0]) if tbl[0][0] else js_round(tbl[0][1])
    for i in range(1, len(tbl)):
        if x <= tbl[i][0]:
            a, b = tbl[i - 1], tbl[i]
            return js_round(a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]))
    return js_round(tbl[-1][1])


def band_index(x):
    n = len(VERIFY_BANDS)
    for i, (lo, hi) in enumerate(VERIFY_BANDS):
        if i == n - 1:
            if x >= lo:
                return i
        elif lo <= x < hi:
            return i
    return None


def summarize_bands(subset, t5e, t5l):
    """T5の表示%(E/L判定込みでcalPct相当を適用)を表示帯別に集計し、
    n>=VERIFY_BAND_MIN_N の帯のみ {band, n, disp, act} を返す。"""
    stats = [[0, 0.0, 0] for _ in VERIFY_BANDS]  # [n, sum(disp), hit]
    for r in subset:
        no = r["no"]
        if no is None:
            continue
        tbl = t5e if no <= 4 else t5l
        disp = cal_pct(top_k_p(r["picks"], 5), tbl)
        idx = band_index(disp)
        if idx is None:
            continue
        stats[idx][0] += 1
        stats[idx][1] += disp
        if hit_top_k(r["picks"], r["order"], 5):
            stats[idx][2] += 1
    out = []
    for i, (n, sum_disp, hit) in enumerate(stats):
        if n < VERIFY_BAND_MIN_N:
            continue
        out.append({
            "band": VERIFY_BAND_LABELS[i],
            "n": n,
            "disp": round(sum_disp / n, 1),
            "act": round(hit / n * 100, 1),
        })
    return out


def build_verify(races, t5e, t5l):
    dates = sorted({r["date"] for r in races})
    recent_dates = set(dates[-RECENT_DAYS:])
    recent = [r for r in races if r["date"] in recent_dates]
    return {
        "full": summarize_bands(races, t5e, t5l),
        "recent30": summarize_bands(recent, t5e, t5l),
    }


# ---------------------------------------------------------------- 種(世代2)

def _split_el(rows):
    """no で 序盤(1〜4R) / 中盤以降(5R〜) に分ける。no の無い行はどちらにも入れない(今までと同じ)。"""
    e = [r for r in rows if r.get("no") is not None and r["no"] <= 4]
    l = [r for r in rows if r.get("no") is not None and r["no"] >= 5]
    return e, l


def seed_from_predictions(rows, gen=SEED_GEN):
    """予測の表 → 種のビン別件数。train.py が、学習に使っていない試験期間の予測から呼ぶ。
    rows: [{"no": レース番号, "picks": [{"c": "1-2-3", "p": 0.12}, ...](上位5点でよい), "order": "1-2-3"}, ...]
    戻り値: {"t5e": [[n, hit], ...], "t3e": ..., "t4e": ..., "t5l": ..., "t3l": ..., "t4l": ...}
    (その世代のビン順。件数は整数)。"""
    bins = BINS_BY_GEN[gen]
    e, l = _split_el(rows)
    return {key: bin_counts(e if early else l, k, bins[k]) for key, (k, early) in _TABLE_SPEC.items()}


def make_seed(rows, built_from, gen=SEED_GEN):
    """保存する種の形(data/calib_seed_gen2.json)。built_from は出どころの説明
    (例: "train.py 2026-10-07 test period 20260324-20261005")。"""
    return {"gen": gen, "built_from": str(built_from), "bins": seed_from_predictions(rows, gen)}


def seed_total_races(bins):
    """種のレース数。全レースは t5e か t5l のどちらか1つのビンに入る(最初のビンの下限が0、
    最後のビンは上限なし)ので、その2表の n の合計がレース数になる。"""
    return sum(n for n, _ in bins.get("t5e", [])) + sum(n for n, _ in bins.get("t5l", []))


def validate_seed(seed, gen=SEED_GEN):
    """種の形を確かめる。問題があれば理由の文字列、無ければ None。"""
    if not isinstance(seed, dict):
        return "not a dict"
    if seed.get("gen") != gen:
        return f"gen is {seed.get('gen')!r}, expected {gen}"
    bins = seed.get("bins")
    if not isinstance(bins, dict):
        return "bins missing"
    spec = BINS_BY_GEN[gen]
    for key, (k, _) in _TABLE_SPEC.items():
        tbl = bins.get(key)
        if not isinstance(tbl, list) or len(tbl) != len(spec[k]):
            return f"bins[{key}] must have {len(spec[k])} entries"
        for ent in tbl:
            if (not isinstance(ent, (list, tuple)) or len(ent) != 2
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in ent)
                    or ent[0] < 0 or ent[1] < 0 or ent[1] > ent[0]):
                return f"bins[{key}] entries must be [n, hit] with 0 <= hit <= n"
    return None


def load_seed(path=None, gen=SEED_GEN):
    """種を読む。無ければ None(静かに)。読めない・形が違う時は WARN を出して None
    (世代2の表は本番のレースだけから作られ、足りなければ空になる。使う側がログに出す)。"""
    path = Path(path) if path is not None else SEED_PATH
    if not path.exists():
        return None
    try:
        seed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN calib seed unreadable {path.name}: {e}")
        return None
    problem = validate_seed(seed, gen)
    if problem:
        print(f"WARN calib seed ignored {path.name}: {problem}")
        return None
    return seed


def seed_weight(seed_bins, n_prod):
    """種に掛ける重み。本番の決着レースが SEED_DROP_RACES を超えたら 0(外す)。
    それまでは種の総数が SEED_TARGET_RACES になるよう縮める(それより少ない種はそのまま)。"""
    if n_prod > SEED_DROP_RACES:
        return 0.0
    total = seed_total_races(seed_bins)
    if total <= 0:
        return 0.0
    return min(1.0, SEED_TARGET_RACES / total)


# ---------------------------------------------------------------- 表の組み立て

def build_tables(races, gen, seed_bins=None, weight=1.0):
    """1世代の6表。races はその世代の決着レースだけ。seed_bins があれば weight 倍して足す。"""
    bins = BINS_BY_GEN[gen]
    e, l = _split_el(races)
    out = {}
    for key, (k, early) in _TABLE_SPEC.items():
        seed = seed_bins.get(key) if seed_bins else None
        out[key] = build_table(e if early else l, k, bins[k], seed, weight)
    return out


def load_races():
    """docs/predictions/YYYYMMDD.json から決着レース(result.orderあり,statusなし,
    picks>=5点)を読み込む。latest.json(直近日の複製)は日付形式が違うため自然に除外される。
    各レースに世代 gen(レースの印 g。無ければ 1)を付ける。"""
    races = []
    if not PRED_DIR.exists():
        return races
    for path in sorted(PRED_DIR.glob("*.json")):
        if not re.match(r"^\d{8}\.json$", path.name):
            continue
        ymd = path.stem
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"WARN failed to load {path.name}: {e}")
            continue
        for v in data.get("venues", []):
            for r in v.get("races", []):
                res = r.get("result")
                picks = r.get("picks") or []
                if not res or res.get("status") or len(picks) < 5:
                    continue
                order = res.get("order")
                if not order:
                    continue
                races.append({"date": ymd, "no": r.get("no"), "picks": picks, "order": order,
                              "gen": race_gen(r)})
    return races


def default_gen(report_path=None):
    """現在の世代の既定: 最後に学習したモデルの報告(docs/model_report.json)の gen。
    無い・読めない・gen が無い・知らない世代なら 1(現行の動きを変えない側。段階Aの報告には gen が無い)。
    種のファイルの有無では決めない(モジュールの説明を参照)。"""
    path = Path(report_path) if report_path is not None else REPORT_PATH
    if not path.exists():
        return 1
    try:
        rep = json.loads(path.read_text(encoding="utf-8"))
        g = rep.get("gen") if isinstance(rep, dict) else None
    except Exception as e:
        print(f"WARN model report unreadable {path.name}: {e}; current gen = 1")
        return 1
    if g is None:
        return 1
    try:
        g = int(g)
    except Exception:
        print(f"WARN model report gen {g!r} is not a number; current gen = 1")
        return 1
    if g not in BINS_BY_GEN:
        print(f"WARN model report gen {g} is unknown (known: {sorted(BINS_BY_GEN)}); current gen = 1")
        return 1
    return g


def build_all(races, cur_gen, seed=None):
    """全世代の表と、出力(docs/calib.json)の中身を作る。I/O はしない(テストから呼ぶ)。"""
    if cur_gen not in BINS_BY_GEN:
        raise SystemExit(f"unknown model gen {cur_gen!r} (known: {sorted(BINS_BY_GEN)})")
    by_gen = {}
    for r in races:
        by_gen.setdefault(r["gen"], []).append(r)
    for g in sorted(by_gen):
        if g not in BINS_BY_GEN:
            print(f"WARN {len(by_gen[g])} races with unknown model gen {g!r}: no table built")

    # 世代1は常に出す(過去の日は全部世代1)。現在の世代と、種のある世代も出す。
    want = {1, cur_gen} | {g for g in by_gen if g in BINS_BY_GEN}
    if seed is not None:
        want.add(SEED_GEN)

    gens = {}
    for g in sorted(want):
        rs = by_gen.get(g, [])
        entry = {}
        if g == SEED_GEN and seed is not None:
            w = seed_weight(seed["bins"], len(rs))
            tables = build_tables(rs, g, seed["bins"], w)
            entry_seed = {"races": seed_total_races(seed["bins"]), "weight": round(w, 4),
                          "applied": w > 0, "built_from": seed.get("built_from")}
        else:
            tables = build_tables(rs, g)
            entry_seed = None
        entry.update(tables)
        entry["races"] = len(rs)
        entry["verify"] = build_verify(rs, tables["t5e"], tables["t5l"])
        if entry_seed is not None:
            entry["seed"] = entry_seed
        gens[str(g)] = entry

    cur = gens[str(cur_gen)]
    out = {
        "updated_at": datetime.now(JST).strftime("%Y-%m-%dT%H:%M+09:00"),
        "races": cur["races"],
    }
    for key in TABLE_KEYS:
        out[key] = cur[key]
    out["verify"] = cur["verify"]
    out["gen"] = cur_gen
    out["gens"] = gens
    return out


def main(gen=None):
    races = load_races()
    seed = load_seed()
    cur_gen = default_gen() if gen is None else gen
    out = build_all(races, cur_gen, seed)
    _atomic_write_text(OUT_PATH, json.dumps(out, ensure_ascii=False))

    by_gen = {}
    for r in races:
        by_gen[r["gen"]] = by_gen.get(r["gen"], 0) + 1
    cur = out["gens"][str(cur_gen)]
    e_n = sum(1 for r in races if r["gen"] == cur_gen and r["no"] is not None and r["no"] <= 4)
    l_n = sum(1 for r in races if r["gen"] == cur_gen and r["no"] is not None and r["no"] >= 5)
    print(f"gen={cur_gen} ({'--gen' if gen is not None else REPORT_PATH.name + ' or default 1'}) "
          f"races(total decided)={len(races)} by_gen={by_gen} "
          f"current gen: {cur['races']} (E={e_n} L={l_n})")
    if "seed" in cur:
        print(f"seed: {cur['seed']}")
    elif cur_gen == SEED_GEN:
        print(f"WARN no calib seed for gen {SEED_GEN} ({SEED_PATH.name}); tables from production races only")
    for name in TABLE_KEYS:
        print(f"{name}: {out[name]}")
    print("verify.full   :", out["verify"]["full"])
    print("verify.recent30:", out["verify"]["recent30"])
    for g, entry in out["gens"].items():
        if int(g) != cur_gen:
            print(f"gens[{g}]: races={entry['races']} " +
                  " ".join(f"{k}={len(entry[k])}pts" for k in TABLE_KEYS))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="較正表(docs/calib.json)を作る")
    ap.add_argument("--gen", type=int, default=None,
                    help="現在のモデルの世代(トップレベルの表に使う)。省略時は docs/model_report.json の gen(無ければ1)")
    args = ap.parse_args()
    main(gen=args.gen)

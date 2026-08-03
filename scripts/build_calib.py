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
    ビン   : 下で定義する固定境界(表示%=生値(0-1)x100 で分類)。
    採用   : 各ビンはサンプル数 n >= 25 のときのみ採用する。
    値     : 採用ビンごとに (ビン中央, 実測的中率%) の点を作り、
             重み付きPAV(pool adjacent violators)で単調非減少化した値を最終値とする
             (フロント初期テーブル作成時と同一手法)。

出力 docs/calib.json:
    {
      "updated_at": "YYYY-MM-DDTHH:MM+09:00", "races": 総決着レース数,
      "t5e": [[x, y], ...], "t3e": [...], "t4e": [...],
      "t5l": [...], "t3l": [...], "t4l": [...],
      "verify": {"full": [...], "recent30": [...]}
    }
verify は較正結果を使って各レースの表示%(フロント calPct と同一実装)を求め、
表示帯別に「表示%の平均」と「実測的中率%」を突き合わせた自己検証(T5のみ)。
"""
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
PRED_DIR = ROOT / "docs" / "predictions"
OUT_PATH = ROOT / "docs" / "calib.json"
JST = timezone(timedelta(hours=9))

# ビン定義: (下限%, 上限%) の半開区間。表示%=生値(0-1)x100 をここで分類する。
# 中央値はdocs/index.htmlのCAL_T5E等のx座標と同じ(区間の算術平均)。
T5_BINS = [(0, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 40), (40, 45), (45, 50), (50, 100)]
T3_BINS = [(0, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 36), (36, 100)]
T4_BINS = [(0, 12), (12, 18), (18, 24), (24, 30), (30, 36), (36, 42), (42, 100)]

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


def build_table(races, k, bins):
    """races: [{"no", "picks", "order"}, ...] の1グループ(序盤 or 中盤以降)。
    n>=MIN_BIN_N のビンのみ採用し、重み付きPAVで単調非減少化した
    [center, rate%(小数1桁)] の配列を返す。"""
    stats = [[0, 0] for _ in bins]  # [n, hit]
    for r in races:
        x = top_k_p(r["picks"], k) * 100
        idx = bin_index(x, bins)
        if idx is None:
            continue
        stats[idx][0] += 1
        if hit_top_k(r["picks"], r["order"], k):
            stats[idx][1] += 1

    adopted = [(i, n, h) for i, (n, h) in enumerate(stats) if n >= MIN_BIN_N]
    if not adopted:
        return []
    points = [(n, (h / n) * 100.0) for (_, n, h) in adopted]
    fitted = weighted_pav(points)
    centers = [(bins[i][0] + bins[i][1]) / 2.0 for (i, _, _) in adopted]
    return [[c, round(v, 1)] for c, v in zip(centers, fitted)]


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


def load_races():
    """docs/predictions/YYYYMMDD.json から決着レース(result.orderあり,statusなし,
    picks>=5点)を読み込む。latest.json(直近日の複製)は日付形式が違うため自然に除外される。"""
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
                races.append({"date": ymd, "no": r.get("no"), "picks": picks, "order": order})
    return races


def main():
    races = load_races()
    total = len(races)

    e_races = [r for r in races if r["no"] is not None and r["no"] <= 4]
    l_races = [r for r in races if r["no"] is not None and r["no"] >= 5]

    t5e = build_table(e_races, 5, T5_BINS)
    t3e = build_table(e_races, 3, T3_BINS)
    t4e = build_table(e_races, 4, T4_BINS)
    t5l = build_table(l_races, 5, T5_BINS)
    t3l = build_table(l_races, 3, T3_BINS)
    t4l = build_table(l_races, 4, T4_BINS)

    verify = build_verify(races, t5e, t5l)

    out = {
        "updated_at": datetime.now(JST).strftime("%Y-%m-%dT%H:%M+09:00"),
        "races": total,
        "t5e": t5e, "t3e": t3e, "t4e": t4e,
        "t5l": t5l, "t3l": t3l, "t4l": t4l,
        "verify": verify,
    }
    _atomic_write_text(OUT_PATH, json.dumps(out, ensure_ascii=False))

    print(f"races(total decided)={total} (E={len(e_races)} L={len(l_races)})")
    for name, tbl in [("t5e", t5e), ("t3e", t3e), ("t4e", t4e),
                       ("t5l", t5l), ("t3l", t3l), ("t4l", t4l)]:
        print(f"{name}: {tbl}")
    print("verify.full   :", verify["full"])
    print("verify.recent30:", verify["recent30"])


if __name__ == "__main__":
    main()

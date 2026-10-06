# -*- coding: utf-8 -*-
"""scripts/build_calib.py(較正表)を合成の予測ファイルで確かめる。通信なし。

確かめること:
  - 世代1の表は、世代1(印 g 無し・g=1)のレースだけから今までのビンで作られ、
    今のコード(main 枝の build_calib.py d75435d46 の算法を下に写した Ref)と同じ数字になる。
  - 世代2の表は 種(data/calib_seed_gen2.json)+ 世代2のレースから作られる。
  - 種は本番1万レース相当に縮め、世代2の本番が2万レースを超えたら外す。
  - トップレベルの6表・races・verify は「現在の世代」(--gen。既定は docs/model_report.json の gen、
    無ければ1。種の有無では決めない)。
  - gens の形。seed_from_predictions / make_seed / load_seed の形。

実行: python tests/test_build_calib.py   (Windows では PYTHONUTF8=1 を付ける)"""
import itertools
import json
import math
import random
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_calib as BC

KEYS = ["t5e", "t3e", "t4e", "t5l", "t3l", "t4l"]
COMBOS = ["%d-%d-%d" % t for t in itertools.permutations(range(1, 7), 3)]
SHARE5 = [0.35, 0.25, 0.18, 0.12, 0.10]   # 上位5点の確率の配分(top3p = 0.78 x top5p, top4p = 0.90 x top5p)


# ---------------------------------------------------------------- 参照実装(今のコードの算法の写し)
# main 枝 scripts/build_calib.py(d75435d46)の bin_index / weighted_pav / build_table / cal_pct /
# summarize_bands / build_verify / load_races を、世代を知らないまま写したもの。
# 新しいコードの世代1の数字が、この算法と1つも違わないことを見張る。

REF_T5 = [(0, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 40), (40, 45), (45, 50), (50, 100)]
REF_T3 = [(0, 10), (10, 15), (15, 20), (20, 25), (25, 30), (30, 36), (36, 100)]
REF_T4 = [(0, 12), (12, 18), (18, 24), (24, 30), (30, 36), (36, 42), (42, 100)]
REF_BANDS = [(0, 20), (20, 30), (30, 40), (40, 50), (50, 100)]
REF_LABELS = ["0-19", "20-29", "30-39", "40-49", "50-100"]


def ref_bin_index(x, bins):
    n = len(bins)
    for i, (lo, hi) in enumerate(bins):
        if i == n - 1:
            if x >= lo:
                return i
        elif lo <= x < hi:
            return i
    return None


def ref_pav(points):
    blocks = []
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


def ref_topk(picks, k):
    return sum((p.get("p") or 0) for p in picks[:k])


def ref_hit(picks, order, k):
    return any(p.get("c") == order for p in picks[:k])


def ref_counts(races, k, bins):
    stats = [[0, 0] for _ in bins]
    for r in races:
        i = ref_bin_index(ref_topk(r["picks"], k) * 100, bins)
        if i is None:
            continue
        stats[i][0] += 1
        if ref_hit(r["picks"], r["order"], k):
            stats[i][1] += 1
    return stats


def ref_table_from_counts(stats, bins):
    adopted = [(i, n, h) for i, (n, h) in enumerate(stats) if n >= 25]
    if not adopted:
        return []
    fitted = ref_pav([(n, (h / n) * 100.0) for (_, n, h) in adopted])
    centers = [(bins[i][0] + bins[i][1]) / 2.0 for (i, _, _) in adopted]
    return [[c, round(v, 1)] for c, v in zip(centers, fitted)]


def ref_table(races, k, bins):
    return ref_table_from_counts(ref_counts(races, k, bins), bins)


def ref_cal_pct(p, tbl):
    if not tbl:
        return math.floor(p * 100 + 0.5)
    x = p * 100
    if x <= tbl[0][0]:
        return math.floor(tbl[0][1] * x / tbl[0][0] + 0.5) if tbl[0][0] else math.floor(tbl[0][1] + 0.5)
    for i in range(1, len(tbl)):
        if x <= tbl[i][0]:
            a, b = tbl[i - 1], tbl[i]
            return math.floor(a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]) + 0.5)
    return math.floor(tbl[-1][1] + 0.5)


def ref_bands(subset, t5e, t5l):
    stats = [[0, 0.0, 0] for _ in REF_BANDS]
    for r in subset:
        if r["no"] is None:
            continue
        disp = ref_cal_pct(ref_topk(r["picks"], 5), t5e if r["no"] <= 4 else t5l)
        i = ref_bin_index(disp, REF_BANDS)
        if i is None:
            continue
        stats[i][0] += 1
        stats[i][1] += disp
        if ref_hit(r["picks"], r["order"], 5):
            stats[i][2] += 1
    return [{"band": REF_LABELS[i], "n": n, "disp": round(s / n, 1), "act": round(h / n * 100, 1)}
            for i, (n, s, h) in enumerate(stats) if n >= 20]


def ref_verify(races, t5e, t5l):
    dates = sorted({r["date"] for r in races})
    recent = set(dates[-30:])
    return {"full": ref_bands(races, t5e, t5l),
            "recent30": ref_bands([r for r in races if r["date"] in recent], t5e, t5l)}


def ref_load_races(pred_dir, keep=lambda raw: True):
    """今の load_races と同じ選び方。keep(生のレース) で世代の絞り込みをテスト側で行う。"""
    races = []
    for path in sorted(Path(pred_dir).glob("*.json")):
        if not re.match(r"^\d{8}\.json$", path.name):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for v in data.get("venues", []):
            for r in v.get("races", []):
                res = r.get("result")
                picks = r.get("picks") or []
                if not res or res.get("status") or len(picks) < 5:
                    continue
                if not res.get("order"):
                    continue
                if keep(r):
                    races.append({"date": path.stem, "no": r.get("no"), "picks": picks, "order": res["order"]})
    return races


def ref_six(races, bins5=REF_T5, bins3=REF_T3, bins4=REF_T4):
    """今の main と同じ組み立て(6表 + verify + races)。"""
    e = [r for r in races if r["no"] is not None and r["no"] <= 4]
    l = [r for r in races if r["no"] is not None and r["no"] >= 5]
    out = {"races": len(races),
           "t5e": ref_table(e, 5, bins5), "t3e": ref_table(e, 3, bins3), "t4e": ref_table(e, 4, bins4),
           "t5l": ref_table(l, 5, bins5), "t3l": ref_table(l, 3, bins3), "t4l": ref_table(l, 4, bins4)}
    out["verify"] = ref_verify(races, out["t5e"], out["t5l"])
    return out


# ---------------------------------------------------------------- 合成データ

def make_race(no, rng, hit_scale, g=None):
    """上位5点の買い目と結果。top5p は 0.05〜0.95 に広く散らし(全ビンに件数が入るように)、
    結果は hit_scale x top5p の確率で上位5点のどれか(確率に比例)、外れなら別の目。"""
    top5p = rng.uniform(0.05, 0.95)
    cs = rng.sample(COMBOS, 5)
    picks = [{"c": c, "p": round(top5p * s, 4)} for c, s in zip(cs, SHARE5)]
    if rng.random() < min(0.98, hit_scale * top5p):
        order = rng.choices(cs, weights=SHARE5)[0]
    else:
        order = rng.choice([c for c in COMBOS if c not in cs])
    r = {"no": no, "deadline": "12:00", "picks": picks, "boats": [], "fuku": {"lane": 1},
         "result": {"order": order, "pay3t": 1000}}
    if g is not None:
        r["g"] = g
    return r


def make_day(ymd, rng, g=None, model_gen=None, venues=20, mixed=False):
    """1日分(venues 会場 x 12R)。mixed なら g=1 と g=2 のレースを交互に混ぜる(切り替えの日の形)。
    除外されるべきレース(結果なし・status あり・picks 4点・order なし)も1会場に1つずつ混ぜる。"""
    out = {"date": ymd, "generated_at": ymd, "venues": []}
    if model_gen is not None:
        out["model_gen"] = model_gen
    for vc in range(1, venues + 1):
        races = []
        for no in range(1, 13):
            rg = ((no + vc) % 2 + 1) if mixed else g
            races.append(make_race(no, rng, 0.9 if rg in (None, 1) else 1.1, rg))
        # 除外されるべきレース
        skip = make_race(1, rng, 1.0, g)
        skip.pop("result")
        races.append(skip)
        bad = make_race(2, rng, 1.0, g)
        bad["result"]["status"] = "中止"
        races.append(bad)
        short = make_race(3, rng, 1.0, g)
        short["picks"] = short["picks"][:4]
        races.append(short)
        noorder = make_race(4, rng, 1.0, g)
        noorder["result"] = {"pay3t": None}
        races.append(noorder)
        out["venues"].append({"code": vc, "name": "v%d" % vc, "races": races})
    return out


def days_unmarked(rng):
    return [make_day("202607%02d" % d, rng) for d in (1, 2, 3)]


def days_g1(rng):
    return [make_day("202607%02d" % d, rng, g=1, model_gen=1) for d in (4, 5, 6)]


def days_g2(rng):
    return [make_day("202607%02d" % d, rng, g=2, model_gen=2) for d in (7, 8, 9)]


def days_mixed(rng):
    return [make_day("202607%02d" % d, rng, model_gen=2, mixed=True) for d in (10, 11, 12)]


def all_days():
    rng = random.Random(42)
    return days_unmarked(rng) + days_g1(rng) + days_g2(rng) + days_mixed(rng)


def is_gen1(raw):
    return raw.get("g") in (None, 1)


def is_gen2(raw):
    return raw.get("g") == 2


class Root:
    """一時フォルダに予測ファイル・種・モデルの報告を置き、build_calib の置き場をそこへ向ける。
    report: docs/model_report.json の中身(dict。None なら置かない = 段階Aより前と同じ)。report_text: 生の文字列。"""

    def __init__(self, days, seed=None, seed_text=None, report=None, report_text=None):
        self.root = Path(tempfile.mkdtemp(prefix="calib_test_"))
        self.pred = self.root / "docs" / "predictions"
        self.pred.mkdir(parents=True)
        (self.root / "data").mkdir()
        for d in days:
            (self.pred / (d["date"] + ".json")).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        # 対象外のファイル(日付形式でない)は読まれない
        (self.pred / "latest.json").write_text(json.dumps(days[-1]), encoding="utf-8")
        (self.pred / "notify_state.json").write_text("{}", encoding="utf-8")
        self.seed_path = self.root / "data" / "calib_seed_gen2.json"
        if seed is not None:
            self.seed_path.write_text(json.dumps(seed), encoding="utf-8")
        elif seed_text is not None:
            self.seed_path.write_text(seed_text, encoding="utf-8")
        self.report_path = self.root / "docs" / "model_report.json"
        if report is not None:
            self.report_path.write_text(json.dumps(report), encoding="utf-8")
        elif report_text is not None:
            self.report_path.write_text(report_text, encoding="utf-8")
        self.out = self.root / "docs" / "calib.json"

    def __enter__(self):
        self.saved = (BC.PRED_DIR, BC.OUT_PATH, BC.SEED_PATH, BC.REPORT_PATH)
        BC.PRED_DIR, BC.OUT_PATH, BC.SEED_PATH, BC.REPORT_PATH = self.pred, self.out, self.seed_path, self.report_path
        return self

    def __exit__(self, *a):
        BC.PRED_DIR, BC.OUT_PATH, BC.SEED_PATH, BC.REPORT_PATH = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    def run(self, gen=None):
        out = BC.main(gen=gen)
        written = json.loads(self.out.read_text(encoding="utf-8"))
        assert written == json.loads(json.dumps(out)), "書いた JSON と戻り値が違う"
        return out


def seed_doc(bins_overrides, built_from="test"):
    """世代2のビン順で、指定したビンだけ件数を入れた種(ほかは 0)。"""
    bins = {k: [[0, 0] for _ in BC.BINS_BY_GEN[2][kk]] for k, (kk, _) in BC._TABLE_SPEC.items()}
    for key, idx, n, hit in bins_overrides:
        bins[key][idx] = [n, hit]
    return {"gen": 2, "built_from": built_from, "bins": bins}


def six(entry):
    return {k: entry[k] for k in KEYS}


# ---------------------------------------------------------------- テスト

def test_gen1_bins_are_unchanged():
    """世代1のビンを変えると、世代1の表が今と違う数字になる。"""
    assert BC.T5_BINS == REF_T5 and BC.T3_BINS == REF_T3 and BC.T4_BINS == REF_T4
    assert BC.MIN_BIN_N == 25 and BC.VERIFY_BAND_MIN_N == 20 and BC.RECENT_DAYS == 30
    assert BC.BINS_BY_GEN[1] == {5: REF_T5, 3: REF_T3, 4: REF_T4}
    # 世代2のビン(設計書 4.6 / 担当の指示どおり)
    assert BC.T5_BINS_V2[-4:] == [(45, 50), (50, 55), (55, 60), (60, 100)] and len(BC.T5_BINS_V2) == 11
    assert BC.T3_BINS_V2[-4:] == [(35, 40), (40, 45), (45, 50), (50, 100)] and len(BC.T3_BINS_V2) == 10
    assert BC.T4_BINS_V2 == [(0, 12), (12, 18), (18, 24), (24, 30), (30, 36), (36, 42), (42, 48), (48, 100)]


def test_all_gen1_output_equals_current_code():
    """印の無い日と g=1 の日だけ(今の本番と同じ状態)。種が無ければ現在の世代は1で、
    トップレベルの6表・races・verify が今のコードの出力と完全に一致する。"""
    rng = random.Random(42)
    days = days_unmarked(rng) + days_g1(rng)
    with Root(days) as t:
        out = t.run()
        ref = ref_six(ref_load_races(t.pred))
    assert out["gen"] == 1
    for k in ["races"] + KEYS + ["verify"]:
        assert out[k] == ref[k], (k, out[k], ref[k])
    assert set(out["gens"]) == {"1"}
    for k in ["races"] + KEYS + ["verify"]:
        assert out["gens"]["1"][k] == ref[k], k
    assert out["races"] == 6 * 20 * 12 and all(len(out[k]) >= 5 for k in KEYS), "大半のビンに件数が入っているはず"
    assert list(out.keys())[:9] == ["updated_at", "races"] + KEYS + ["verify"], "今までのキーの順を保つ"


def test_mixed_days_are_split_by_race_mark_not_by_file():
    """g 無し・g=1・g=2・1日の中で混在、が混ざった予測ファイル。
    世代1の表 = 世代1のレースだけを今のコードに通した数字。世代2の表(種なし)= 世代2のレースだけ。
    1日の中で混ざった日は、ファイルの model_gen ではなくレースの g で分かれる。"""
    with Root(all_days()) as t:
        out = t.run(gen=1)
        g1 = ref_six(ref_load_races(t.pred, is_gen1))
        g2 = ref_six(ref_load_races(t.pred, is_gen2), BC.T5_BINS_V2, BC.T3_BINS_V2, BC.T4_BINS_V2)
        everything = ref_six(ref_load_races(t.pred))
    assert out["gen"] == 1 and set(out["gens"]) == {"1", "2"}
    for k in ["races"] + KEYS + ["verify"]:
        assert out["gens"]["1"][k] == g1[k], ("gen1", k)
        assert out["gens"]["2"][k] == g2[k], ("gen2", k)
        assert out[k] == g1[k], ("top", k)
    # 混ぜて作ったものとは違う(世代で分けている証拠)
    assert out["t5l"] != everything["t5l"]
    # 件数: 印なし3日 + g=1 3日 + 混在3日の半分 / g=2 3日 + 混在3日の半分
    assert out["gens"]["1"]["races"] == 240 * (3 + 3) + 240 * 3 // 2
    assert out["gens"]["2"]["races"] == 240 * 3 + 240 * 3 // 2
    assert "seed" not in out["gens"]["2"]


def test_gen2_table_is_seed_plus_production():
    """種(重み1になる件数)+ 世代2の本番レース。各ビンの [n, hit] を足してから採用・PAV する。
    本番に無く種にだけあるビンも採用される。verify と races は本番のレースだけ。"""
    rng = random.Random(7)
    # train.py が渡す形 {no, picks, order}(当日ファイルのレースの形ではない)
    seed_rows = [{"no": r["no"], "picks": r["picks"], "order": r["result"]["order"]}
                 for r in (make_race(no, rng, 1.1) for _ in range(500) for no in range(1, 13))]   # 6000 < 10000 → 重み1
    seed = BC.make_seed(seed_rows, "unit test rows")
    # 種の数え方そのものを参照実装で確かめる
    e = [r for r in seed_rows if r["no"] <= 4]
    l = [r for r in seed_rows if r["no"] >= 5]
    for key, (k, early) in BC._TABLE_SPEC.items():
        assert seed["bins"][key] == ref_counts(e if early else l, k, BC.BINS_BY_GEN[2][k]), key
    assert BC.seed_total_races(seed["bins"]) == 6000

    rng = random.Random(42)
    prod_days = days_g2(rng)[:1]   # 240 レース
    with Root(prod_days, seed=seed, report={"gen": 2, "format": 2}) as t:
        out = t.run()
        prod = ref_load_races(t.pred, is_gen2)
    assert out["gen"] == 2, "最後に学習したモデルの報告が世代2なら、既定の現在の世代は2"
    pe = [r for r in prod if r["no"] <= 4]
    pl = [r for r in prod if r["no"] >= 5]
    for key, (k, early) in BC._TABLE_SPEC.items():
        bins = BC.BINS_BY_GEN[2][k]
        pc = ref_counts(pe if early else pl, k, bins)
        stats = [[sn + pn, sh + ph] for (sn, sh), (pn, ph) in zip(seed["bins"][key], pc)]
        expect = ref_table_from_counts(stats, bins)
        assert out["gens"]["2"][key] == expect, (key, out["gens"]["2"][key], expect)
        assert out[key] == expect, key
        assert out["gens"]["2"][key] != ref_table_from_counts(pc, bins), "本番だけの表とは違う(種が効いている)"
    g2 = out["gens"]["2"]
    assert g2["races"] == 240 and out["races"] == 240, "races は本番の世代2のレース数(種は入れない)"
    assert g2["verify"] == ref_verify(prod, g2["t5e"], g2["t5l"]), "verify は本番のレースだけ"
    assert g2["seed"] == {"races": 6000, "weight": 1.0, "applied": True, "built_from": "unit test rows"}, g2["seed"]
    # 本番が0件でも種だけで表ができる(切り替えの最初の朝)
    with Root(days_g1(random.Random(1)), seed=seed) as t:
        out0 = t.run(gen=2)
    for key, (k, _) in BC._TABLE_SPEC.items():
        assert out0["gens"]["2"][key] == ref_table_from_counts(seed["bins"][key], BC.BINS_BY_GEN[2][k]), key
    assert out0["gens"]["2"]["races"] == 0 and out0["gens"]["2"]["verify"] == {"full": [], "recent30": []}
    assert out0["gens"]["1"]["races"] == 720, "世代1の表はそのまま作られている"


def test_seed_is_scaled_to_ten_thousand_races():
    """種が1万レースより多ければ 10000/総数 の重みで縮める(少なければそのまま)。
    縮めた後の n で採用(n>=25)を判定し、率は変わらない。"""
    seed = seed_doc([("t5e", 0, 29850, 2985),     # 0-15: 29850 x 1/3 = 9950 → 採用、率 10.0
                     ("t5l", 8, 60, 30),           # 50-55: 60 x 1/3 = 20 < 25 → 不採用
                     ("t5l", 9, 90, 60),           # 55-60: 90 x 1/3 = 30 → 採用、率 66.7
                     ("t3l", 9, 75, 50)])          # 50-100(T3): 75 x 1/3 = 25 → 採用(ちょうど)、率 66.7
    assert BC.seed_total_races(seed["bins"]) == 30000
    with Root(days_g1(random.Random(3)), seed=seed) as t:
        out = t.run()
    assert out["gen"] == 1, "種があっても報告が無ければ現在の世代は1(種の有無では決めない)"
    g2 = out["gens"]["2"]
    assert g2["seed"]["weight"] == round(10000 / 30000, 4) and g2["seed"]["applied"] is True
    assert g2["t5e"] == [[7.5, 10.0]], g2["t5e"]
    assert g2["t5l"] == [[57.5, 66.7]], g2["t5l"]
    assert g2["t3l"] == [[75.0, 66.7]], g2["t3l"]
    assert g2["t3e"] == [] and g2["t4e"] == [] and g2["t4l"] == []
    # 少ない種はそのまま(膨らませない)
    small = seed_doc([("t5l", 9, 90, 60)])
    assert BC.seed_weight(small["bins"], 0) == 1.0
    assert BC.seed_weight(seed["bins"], 0) == 10000 / 30000
    assert BC.seed_weight({"t5e": [], "t5l": []}, 0) == 0.0


def test_seed_is_dropped_once_production_exceeds_twenty_thousand():
    """世代2の本番の決着レースが SEED_DROP_RACES を超えたら種を外す(超えるまでは足す)。
    2万レースの合成は重いので上限を下げて確かめ、定数の値は別に見張る。"""
    assert BC.SEED_DROP_RACES == 20000 and BC.SEED_TARGET_RACES == 10000
    seed = seed_doc([("t5l", 9, 9000, 6000), ("t5e", 10, 1000, 700)])
    days = days_g2(random.Random(5))[:1]   # 240 レース
    saved = BC.SEED_DROP_RACES
    try:
        BC.SEED_DROP_RACES = 240          # ちょうど = 超えていない → まだ足す
        with Root(days, seed=seed, report={"gen": 2}) as t:
            kept = t.run()
        BC.SEED_DROP_RACES = 239          # 超えた → 外す
        with Root(days, seed=seed, report={"gen": 2}) as t:
            dropped = t.run()
            prod_only = ref_six(ref_load_races(t.pred, is_gen2), BC.T5_BINS_V2, BC.T3_BINS_V2, BC.T4_BINS_V2)
    finally:
        BC.SEED_DROP_RACES = saved
    assert kept["gens"]["2"]["seed"]["applied"] is True and kept["gens"]["2"]["seed"]["weight"] == 1.0
    assert 57.5 in [pt[0] for pt in kept["gens"]["2"]["t5l"]], "種のビン(55-60)が表に出ている"
    d2 = dropped["gens"]["2"]
    assert kept["gens"]["2"]["t5l"] != prod_only["t5l"], "外す前は種が効いている"
    assert d2["seed"] == {"races": 10000, "weight": 0.0, "applied": False, "built_from": "test"}, d2["seed"]
    for k in KEYS + ["races", "verify"]:
        assert d2[k] == prod_only[k], k
    assert dropped["gen"] == 2, "種を外しても現在の世代は2のまま"


def test_top_level_is_the_current_gen():
    """トップレベル(今までの置き場)は --gen の世代の表。既定は最後に学習したモデルの報告
    (docs/model_report.json)の gen。報告が無い・gen が無い(段階A)・読めない・知らない世代なら1。
    種の有無では決めない(MODEL_GEN を1に戻した日や、世代2の学習が時間切れで前日の世代1の資産で予測する日に、
    使うモデルと違う世代の表がトップレベルに出ないように)。"""
    seed = seed_doc([("t5l", 9, 9000, 6000), ("t5e", 10, 1000, 700)])
    with Root(all_days(), seed=seed, report={"gen": 2, "format": 2, "trained_at": "x"}) as t:
        auto = t.run()
        as1 = t.run(gen=1)
        as2 = t.run(gen=2)
    assert auto["gen"] == 2 and as1["gen"] == 1 and as2["gen"] == 2
    for k in KEYS + ["races", "verify"]:
        assert auto[k] == auto["gens"]["2"][k] == as2[k] == as2["gens"]["2"][k], k
        assert as1[k] == as1["gens"]["1"][k], k
        assert as1["gens"]["2"][k] == as2["gens"]["2"][k], "gens の中身は --gen で変わらない"
        assert as1["gens"]["1"][k] == as2["gens"]["1"][k]
    assert as1["t5l"] != as2["t5l"]
    # 種はあるが報告が世代1(MODEL_GEN を戻した日 / 世代2の学習が通らず前日の世代1の資産で予測する日)→ 1
    with Root(all_days(), seed=seed, report={"gen": 1, "format": 1}) as t:
        back = t.run()
    assert back["gen"] == 1 and "seed" in back["gens"]["2"], "種はあるが現在の世代は報告どおり1(世代2の表は gens に残る)"
    for k in KEYS + ["races", "verify"]:
        assert back[k] == back["gens"]["1"][k] == as1[k], k
    # 報告の形いろいろ: 無い / gen 無し(段階A) / 読めない / 文字列の "2" / 知らない世代 / 数でない
    for label, kw, want in (("報告なし", {}, 1), ("gen なし(段階A)", {"report": {"trained_at": "x"}}, 1),
                            ("読めない", {"report_text": "{not json"}, 1), ("文字列の \"2\"", {"report": {"gen": "2"}}, 2),
                            ("知らない世代 3", {"report": {"gen": 3}}, 1), ("数でない", {"report": {"gen": "x"}}, 1),
                            ("配列", {"report_text": "[1,2]"}, 1)):
        with Root(all_days(), seed=seed, **kw) as t:
            got = t.run()["gen"]
            assert BC.default_gen() == want, (label, BC.default_gen())
        assert got == want, (label, got)
    with Root(all_days()) as t:         # 種なし・報告なし
        auto = t.run()
        forced = t.run(gen=2)
    assert auto["gen"] == 1
    assert forced["gen"] == 2 and "seed" not in forced["gens"]["2"], "種が無くても --gen 2 は動く(表は本番だけ)"
    # 知らない世代は止める(黙って世代1の表を世代3と呼ばない)
    try:
        with Root(all_days()) as t:
            t.run(gen=3)
        raise AssertionError("gen=3 で止まらなかった")
    except SystemExit as e:
        assert "unknown model gen 3" in str(e)


def test_gens_shape():
    seed = seed_doc([("t5l", 9, 9000, 6000), ("t5e", 10, 1000, 700), ("t3l", 9, 500, 300), ("t4e", 7, 400, 200)])
    with Root(all_days(), seed=seed, report={"gen": 2}) as t:
        out = t.run()
    assert isinstance(out["gen"], int) and set(out["gens"]) == {"1", "2"}
    centers = {g: {k: [(lo + hi) / 2.0 for lo, hi in BC.BINS_BY_GEN[g][kk]] for k, (kk, _) in BC._TABLE_SPEC.items()}
               for g in (1, 2)}
    for g, entry in out["gens"].items():
        assert set(entry) >= set(KEYS) | {"races", "verify"}, (g, set(entry))
        assert isinstance(entry["races"], int) and set(entry["verify"]) == {"full", "recent30"}
        for k in KEYS:
            tbl = entry[k]
            assert isinstance(tbl, list) and all(isinstance(pt, list) and len(pt) == 2 for pt in tbl), (g, k)
            xs = [pt[0] for pt in tbl]
            assert xs == sorted(xs) and all(x in centers[int(g)][k] for x in xs), (g, k, xs)
            ys = [pt[1] for pt in tbl]
            assert ys == sorted(ys), "PAV で単調非減少"
    assert "seed" not in out["gens"]["1"] and set(out["gens"]["2"]["seed"]) == {"races", "weight", "applied", "built_from"}
    # 世代2の表には世代2にしかない中央値(52.5 / 57.5 / 80.0 など)が出る
    assert 57.5 in [pt[0] for pt in out["gens"]["2"]["t5l"]]
    # 世代1の表に世代2の中央値は出ない
    assert all(pt[0] in centers[1]["t5l"] for pt in out["gens"]["1"]["t5l"])
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}\+09:00$", out["updated_at"])


def test_seed_from_predictions_and_seed_file_shape():
    """種を作る側(train.py が呼ぶ)と読む側の形。"""
    def row(no, top5p, hit_rank=None):
        cs = COMBOS[:5]
        picks = [{"c": c, "p": round(top5p * s, 4)} for c, s in zip(cs, SHARE5)]
        order = cs[hit_rank] if hit_rank is not None else "6-5-4"
        return {"no": no, "picks": picks, "order": order}
    rows = [row(1, 0.52, 0),      # E: top5p 52 → (50,55) 的中(1位) / top3p 40.56 → (40,45) 的中 / top4p 46.8 → (42,48) 的中
            row(2, 0.52, 4),      # E: top5p 的中(5位) / top3p 不的中 / top4p 不的中
            row(5, 0.10, None),   # L: top5p 10 → (0,15) 不的中
            row(12, 0.70, 2),     # L: top5p 70 → (60,100) 的中 / top3p 54.6 → (50,100) 的中 / top4p 63 → (48,100) 的中
            {"no": None, "picks": [], "order": "1-2-3"}]   # no 無しはどちらにも入れない(今までと同じ)
    bins = BC.seed_from_predictions(rows)
    assert set(bins) == set(KEYS)
    for key, (k, _) in BC._TABLE_SPEC.items():
        assert len(bins[key]) == len(BC.BINS_BY_GEN[2][k]), key
    assert bins["t5e"][8] == [2, 2] and sum(n for n, _ in bins["t5e"]) == 2
    assert bins["t3e"][7] == [2, 1] and bins["t4e"][6] == [2, 1]
    assert bins["t5l"][0] == [1, 0] and bins["t5l"][10] == [1, 1] and sum(n for n, _ in bins["t5l"]) == 2
    assert bins["t3l"][9] == [1, 1] and bins["t4l"][7] == [1, 1]
    assert BC.seed_total_races(bins) == 4
    # 上位5点だけの picks でも、10点の picks でも同じ(上位5点しか見ない)
    rows10 = [dict(r, picks=r["picks"] + [{"c": c, "p": 0.001} for c in COMBOS[50:55]]) for r in rows[:4]]
    assert BC.seed_from_predictions(rows10) == BC.seed_from_predictions(rows[:4])

    seed = BC.make_seed(rows, "train.py test period 20260324-20261005")
    assert seed == {"gen": 2, "built_from": "train.py test period 20260324-20261005", "bins": bins}
    assert BC.validate_seed(seed) is None
    tmp = Path(tempfile.mkdtemp(prefix="calib_seed_"))
    try:
        p = tmp / "calib_seed_gen2.json"
        p.write_text(json.dumps(seed), encoding="utf-8")
        assert BC.load_seed(p) == seed, "JSON を通しても同じ(整数のまま)"
        assert BC.load_seed(tmp / "missing.json") is None
        # 壊れた種は使わない(None)。例外にしない
        bad = [dict(seed, gen=1),
               {"gen": 2, "built_from": "x"},
               dict(seed, bins=dict(bins, t5e=bins["t5e"][:-1])),
               dict(seed, bins=dict(bins, t3l=[[1, 2]] + bins["t3l"][1:])),     # hit > n
               dict(seed, bins=dict(bins, t4l=[[1, None]] + bins["t4l"][1:])),
               [seed]]
        for i, b in enumerate(bad):
            p.write_text(json.dumps(b), encoding="utf-8")
            assert BC.load_seed(p) is None, i
        p.write_text("{not json", encoding="utf-8")
        assert BC.load_seed(p) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert BC.SEED_PATH.name == "calib_seed_gen2.json" and BC.SEED_PATH.parent.name == "data"


def test_race_gen_follows_the_race_mark():
    """load_races が付ける世代はレースの印 g(無ければ1)。ファイルの model_gen は見ない
    (common.race_gen と同じ規則。切り替えの日に世代の違う買い目が混ざっても正しく分かれる)。"""
    rng = random.Random(9)
    day = {"date": "20260801", "model_gen": 2, "venues": [{"code": 1, "name": "v", "races": [
        make_race(1, rng, 1.0),                 # g 無し → 1(ファイルは model_gen 2 でも)
        make_race(2, rng, 1.0, g=1),
        make_race(3, rng, 1.0, g=2),
        make_race(4, rng, 1.0, g="2"),          # 文字列でも int に読む
        make_race(5, rng, 1.0, g="x"),          # 読めない → 1
    ]}]}
    with Root([day]) as t:
        races = BC.load_races()
    assert [r["gen"] for r in races] == [1, 1, 2, 2, 1], [r["gen"] for r in races]
    assert all(set(r) == {"date", "no", "picks", "order", "gen"} for r in races)
    assert BC.race_gen({"g": 2}) == 2 and BC.race_gen({}) == 1 and BC.race_gen(None) == 1


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok   " + name)
    print("%d tests passed" % n)
    print("ALL OK")

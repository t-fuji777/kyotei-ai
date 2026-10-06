# -*- coding: utf-8 -*-
"""モデルの世代(gen)ごとのしきい値・較正表の引き方を確かめる。通信なし。
対象: scripts/common.py の SENGEN_CFG_BY_GEN / race_gen / sengen_cfg_for / is_sengen / stamp_plans /
badge_attention / _calib_tbl、scripts/update_all.py の候補判定(qc)・判定直前の取り直し・締切10分前の
取り直し、scripts/update_results.py / recompute_sengen.py の集計(打刻の無い日の判定と日の行の gen)。
守るべき性質: 印(race["g"])が無いレースは世代1 = 今までの定数(0.36 / 3.1 / 5R以降 / 除外会場)で、
動きが1つも変わらない。同じ日に世代1と世代2の買い目が混ざっても、各レースは自分の世代の数字で判定される。

実行: python tests/test_gen_rules.py   (Windows では PYTHONUTF8=1 を付ける)"""
import contextlib
import io
import json
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.modules.setdefault("notify", types.SimpleNamespace(notify_events=lambda *a, **k: None))
import common as C
import update_all as U
import update_results as UR
import recompute_sengen as RS

JST = timezone(timedelta(hours=9))
YMD = "20261001"
COMBOS = ["1-2-3", "1-3-2", "2-1-3", "1-2-4", "1-4-2", "2-1-4", "3-1-2", "3-2-1", "1-3-4", "1-4-3"]
HIGH = {c: 9.9 for c in COMBOS}
LOW = {c: 2.0 for c in COMBOS}
G1 = C.sengen_cfg_for(1)
G2 = C.sengen_cfg_for(2)


def race(no, top3p=0.40, g=None, odds=HIGH, deadline="12:14", **kw):
    """上位3点に top3p を等分、4点目以降は 0.02(top4p = top3p + 0.02、top5p = top3p + 0.04)。
    top3p 0.40 は世代1(0.36)では候補、世代2(仮 0.46)では候補でない、という境目の値。"""
    p = top3p / 3
    r = {"no": no, "deadline": deadline, "type": "x", "rn_full": True,
         "picks": [{"c": c, "p": (p if i < 3 else 0.02)} for i, c in enumerate(COMBOS)],
         "boats": [], "fuku": {"lane": 1}}
    if g is not None:
        r["g"] = g
    if odds is not None:
        r["odds"] = {"t3": dict(odds)}
    r.update(kw)
    return r


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def day_rows(results):
    """results: {race_no: (着順 "1-2-3", 払戻)} → evaluate が読む形の1日分の表(tests/test_record_rules.py と同じ)。"""
    rows = []
    for no, (order, pay) in results.items():
        top = [int(x) for x in order.split("-")]
        rest = [x for x in range(1, 7) if x not in top]
        for pos, lane in enumerate(top + rest, 1):
            rows.append({"venue": 1, "race_no": no, "lane": lane, "pos": float(pos), "abnormal": None,
                         "pay3t_combo": order, "pay3t_amount": float(pay)})
    return pd.DataFrame(rows)


def with_results(races, results, ymd=YMD, model_gen=None):
    out = []
    for r in races:
        r = dict(r)
        if r["no"] in results:
            order, pay = results[r["no"]]
            r["result"] = {"order": order, "pay3t": pay}
        out.append(r)
    pred = {"date": ymd, "venues": [{"code": 1, "name": "t", "races": out}]}
    if model_gen is not None:
        pred["model_gen"] = model_gen
    return pred


def evaluate(races, results, ymd=YMD, model_gen=None):
    tmp = Path(tempfile.mkdtemp())
    d = tmp / "docs" / "predictions"
    d.mkdir(parents=True)
    pred = {"date": ymd, "venues": [{"code": 1, "name": "t", "races": races}]}
    if model_gen is not None:
        pred["model_gen"] = model_gen
    (d / f"{ymd}.json").write_text(json.dumps(pred, ensure_ascii=False), encoding="utf-8")
    saved = UR.ROOT
    UR.ROOT = tmp
    try:
        with quiet():
            return UR.evaluate(ymd, day_rows(results))
    finally:
        UR.ROOT = saved


# ---------------------------------------------------------------- common.py

def test_table_gen1_is_the_existing_constants():
    c1 = C.SENGEN_CFG_BY_GEN[1]
    assert c1["top3p_min"] == C.SENGEN_TOP3P_MIN == 0.36
    assert c1["min_odds"] == C.SENGEN_MIN_ODDS == 3.1
    assert c1["min_rno"] == C.SENGEN_MIN_RNO == 5
    assert sorted(c1["exclude_venues"]) == sorted(C.SENGEN_EXCLUDE_VENUES) == [3, 4, 14]
    assert c1["cand_top4p_min"] == 0.36
    c2 = C.SENGEN_CFG_BY_GEN[2]
    assert c2["top3p_min"] == 0.46 and c2["cand_top4p_min"] == 0.46, "世代2の仮の値(段階Bで確定)"
    for k in ("min_odds", "min_rno", "exclude_venues"):
        assert c2[k] == c1[k], f"{k} は世代で変えない"
    assert set(c1) == set(c2) == {"top3p_min", "min_odds", "min_rno", "exclude_venues", "cand_top4p_min"}
    json.dumps(C.SENGEN_CFG_BY_GEN)   # 当日ファイル(JSON)に書ける形であること


def test_race_gen_defaults_to_1():
    assert C.race_gen({}) == 1 and C.race_gen({"g": None}) == 1 and C.race_gen(None) == 1
    assert C.race_gen({"g": 1}) == 1 and C.race_gen({"g": 2}) == 2 and C.race_gen({"g": "2"}) == 2
    assert C.race_gen({"g": "x"}) == 1, "読めない印は世代1"


def test_sengen_cfg_for_accepts_gen_or_race():
    assert C.sengen_cfg_for(1) == C.SENGEN_CFG_BY_GEN[1] and C.sengen_cfg_for(2) == C.SENGEN_CFG_BY_GEN[2]
    assert C.sengen_cfg_for({"g": 2}) == C.SENGEN_CFG_BY_GEN[2]
    assert C.sengen_cfg_for({}) == C.SENGEN_CFG_BY_GEN[1] and C.sengen_cfg_for("1") == C.SENGEN_CFG_BY_GEN[1]
    # 知らない世代は世代1(現行)に落ち、警告は1プロセスに1回
    C._WARNED.clear()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert C.sengen_cfg_for(9) == C.SENGEN_CFG_BY_GEN[1]
        assert C.sengen_cfg_for({"g": 9}) == C.SENGEN_CFG_BY_GEN[1]
    assert buf.getvalue().count("unknown model gen") == 1, buf.getvalue()
    # 戻り値は写し: 呼び出し側が書き足しても表は汚れない
    cfg = C.sengen_cfg_for(2)
    cfg["gen"] = 2
    cfg["top3p_min"] = 0
    assert "gen" not in C.SENGEN_CFG_BY_GEN[2] and C.SENGEN_CFG_BY_GEN[2]["top3p_min"] == 0.46


def test_is_sengen_default_is_gen1_and_cfg_overrides():
    # cfg 無し = 今までの定数
    assert C.is_sengen(0.36, 1, 5) and not C.is_sengen(0.3599, 1, 5)
    assert not C.is_sengen(0.9, 3, 5) and not C.is_sengen(0.9, 4, 5) and not C.is_sengen(0.9, 14, 5)
    assert not C.is_sengen(0.9, 1, 4) and C.is_sengen(0.9, 1, 5)
    for args in ((0.36, 1, 5), (0.3599, 1, 5), (0.9, 3, 5), (0.9, 1, 4), (0.40, 24, 12)):
        assert C.is_sengen(*args) == C.is_sengen(*args, G1), args
    # 世代2のしきい値
    assert C.is_sengen(0.40, 1, 5) and not C.is_sengen(0.40, 1, 5, G2)
    assert C.is_sengen(0.46, 1, 5, G2) and not C.is_sengen(0.4599, 1, 5, G2)
    assert not C.is_sengen(0.9, 14, 5, G2) and not C.is_sengen(0.9, 1, 4, G2)
    # 古い形の cfg(項目が欠ける)は世代1の値で補う。明示の空の除外会場は「除外なし」
    assert C.is_sengen(0.40, 1, 5, {"top3p_min": 0.40}) and not C.is_sengen(0.40, 3, 5, {"top3p_min": 0.40})
    assert not C.is_sengen(0.40, 1, 4, {"top3p_min": 0.40})
    assert C.is_sengen(0.9, 3, 5, {"top3p_min": 0.4, "exclude_venues": []})
    assert C.is_sengen(0.9, 1, 1, {"top3p_min": 0.4, "min_rno": 1})
    # 例外は False(今までどおり)
    assert not C.is_sengen("x", 1, 5) and not C.is_sengen(0.9, None, 5, G2)


def test_stamp_plans_uses_the_race_gen():
    saved = C._CALIB_CACHE
    C._CALIB_CACHE = {}   # 内蔵の表(世代の違いはしきい値だけを見る)
    try:
        with quiet():
            # 同じ買い目(top3p 0.40)・同じ高いオッズ。印が無い = 世代1 → 厳選
            r1 = race(5)
            C.stamp_plans(r1, 1, None, "12:00")
            assert r1["tk"] == 1 and "rs" not in r1 and r1["pt"] == "12:00" and r1["mt"] == 0
            assert r1["os"] == {c: 9.9 for c in COMBOS[:4]} and r1.get("pr")
            # 打刻で増える項目は今までと同じ(世代の印は打刻では書かない)
            assert set(r1) == {"no", "deadline", "type", "rn_full", "picks", "boats", "fuku", "odds",
                               "tk", "os", "mt", "pr", "att", "pt"}, sorted(r1)
            # g=1 を明示しても印なしと同じ
            r1b = race(5, g=1)
            C.stamp_plans(r1b, 1, None, "12:00")
            assert {k: v for k, v in r1b.items() if k != "g"} == r1
            # g=2 → 0.46 に届かず候補ですらない(見送り理由も書かない)。os は今までどおり残す
            r2 = race(5, g=2)
            C.stamp_plans(r2, 1, None, "12:00")
            assert r2["tk"] == 0 and "rs" not in r2 and "pr" not in r2 and r2["g"] == 2
            assert r2["os"] == {c: 9.9 for c in COMBOS[:4]} and r2["att"] in (0, 1)
            # g=2 でも 0.47 なら厳選
            r3 = race(5, top3p=0.47, g=2)
            C.stamp_plans(r3, 1, None, "12:00")
            assert r3["tk"] == 1 and "rs" not in r3
            # g=2 の候補がオッズで見送られた時の理由は今までと同じ文言(画面が先頭一致で拾う)
            r4 = race(5, top3p=0.47, g=2, odds=LOW)
            C.stamp_plans(r4, 1, None, "12:00")
            assert r4["tk"] == 0 and r4["rs"] == "3.1倍未満 2.0倍", r4.get("rs")
            part = dict(HIGH)
            del part["1-3-2"]
            r4b = race(5, top3p=0.47, g=2, odds=part)
            C.stamp_plans(r4b, 1, None, "12:00")
            assert r4b["tk"] == 0 and r4b["rs"] == "オッズ未取得"
            # 確率低下(朝の候補 qc が打刻時に候補でなくなった)の理由も世代2で書かれる
            r6 = race(5, g=2, qc=1, qp=0.48)
            C.stamp_plans(r6, 1, None, "12:00")
            assert r6["tk"] == 0 and r6["rs"] == "確率低下 48%→40%", r6.get("rs")
            # 結果確定後のフォールバック(res あり)も世代で判定し、ph=1 が付く
            r7 = race(5, g=2)
            C.stamp_plans(r7, 1, {"order": "1-2-3", "pay3t": 990}, "13:00")
            assert r7["tk"] == 0 and r7["ph"] == 1
            r8 = race(5)
            C.stamp_plans(r8, 1, {"order": "1-2-3", "pay3t": 990}, "13:00")
            assert r8["tk"] == 1 and r8["ph"] == 1
            # 除外会場・1〜4R は世代2でも対象外
            r9 = race(5, top3p=0.9, g=2)
            C.stamp_plans(r9, 3, None, "12:00")
            r10 = race(4, top3p=0.9, g=2)
            C.stamp_plans(r10, 1, None, "12:00")
            assert r9["tk"] == 0 and r10["tk"] == 0 and "rs" not in r9 and "rs" not in r10
            # first-wins: 打刻済みは世代が何でも触らない
            r11 = race(5, g=2, tk=1)
            C.stamp_plans(r11, 1, None, "12:00")
            assert r11 == race(5, g=2, tk=1)
    finally:
        C._CALIB_CACHE = saved


def test_badge_attention_uses_the_gen_table():
    saved = C._CALIB_CACHE
    try:
        hi = [[10, 10], [90, 90]]   # 恒等: 生の50% → 50
        lo = [[10, 6], [90, 54]]    # 0.6倍: 生の50% → 30
        # top3p 0.46 → top5p 0.50。世代1の表(hi)なら注目、世代2の表(lo)なら様子見
        C._CALIB_CACHE = {"t5l": hi, "t5e": hi, "gens": {"1": {"t5l": hi, "t5e": hi}, "2": {"t5l": lo, "t5e": lo}}}
        with quiet():
            assert C.badge_attention(race(5, top3p=0.46)) == 1
            assert C.badge_attention(race(5, top3p=0.46, g=1)) == 1
            assert C.badge_attention(race(5, top3p=0.46, g=2)) == 0
            # 1〜4R は t5e 表(世代ごと)
            C._CALIB_CACHE = {"t5l": hi, "t5e": lo, "gens": {"2": {"t5l": lo, "t5e": hi}}}
            assert C.badge_attention(race(3, top3p=0.46)) == 0 and C.badge_attention(race(3, top3p=0.46, g=2)) == 1
            assert C.badge_attention(race(5, top3p=0.46)) == 1 and C.badge_attention(race(5, top3p=0.46, g=2)) == 0
            # 打刻(stamp_plans)の att も世代の表で決まる
            C._CALIB_CACHE = {"t5l": hi, "gens": {"2": {"t5l": lo}}}
            r1, r2 = race(5, top3p=0.46), race(5, top3p=0.46, g=2)
            C.stamp_plans(r1, 1, None, "12:00")
            C.stamp_plans(r2, 1, None, "12:00")
            assert (r1["att"], r2["att"]) == (1, 0) and r1["tk"] == 1 and r2["tk"] == 1
            # 自己修復(update_all.do_stamps: tk はあるが att が無い)も世代の表
            pred = {"date": YMD, "venues": [{"code": 1, "name": "t", "races": [
                race(5, top3p=0.46, tk=1), race(6, top3p=0.46, g=2, tk=1)]}]}
            U.do_stamps(pred, datetime(2026, 10, 1, 12, 0, tzinfo=JST))
            atts = [r["att"] for r in pred["venues"][0]["races"]]
            assert atts == [1, 0], atts
    finally:
        C._CALIB_CACHE = saved


def test_calib_tbl_fallback_order():
    saved = C._CALIB_CACHE
    C._WARNED.clear()
    try:
        A = [[10, 1], [90, 1]]
        B = [[10, 2], [90, 2]]
        D = [[10, 3], [90, 3]]
        # (1) 世代の表があればそれ。既定の世代は1。何も出さない
        C._CALIB_CACHE = {"t5l": A, "gens": {"1": {"t5l": B}, "2": {"t5l": D}}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert C._calib_tbl("t5l", 1) is B and C._calib_tbl("t5l", 2) is D and C._calib_tbl("t5l") is B
            assert C._calib_tbl("t5l", "2") is D
        assert buf.getvalue() == "", buf.getvalue()
        # (2) 世代2の表が無い → 黙って落とさずログに1行(同じ警告は1回)、値はトップレベル
        C._CALIB_CACHE = {"t5l": A}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert C._calib_tbl("t5l", 2) is A and C._calib_tbl("t5l", 2) is A
        assert buf.getvalue().count("\n") == 1 and "gen 2" in buf.getvalue(), buf.getvalue()
        # (3) 世代1で gens が無い(今の calib.json の形)のは正常: 黙ってトップレベル
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert C._calib_tbl("t5l", 1) is A and C._calib_tbl("t5l") is A
        assert buf.getvalue() == "", buf.getvalue()
        # (4) gens はあるのに世代1の表が無い(トップレベルが世代2の表かもしれない) → ログに出してトップレベル
        C._CALIB_CACHE = {"t5l": A, "gens": {"2": {"t5l": D}}}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert C._calib_tbl("t5l", 1) is A
        assert "gen 1" in buf.getvalue(), buf.getvalue()
        # (5) 何も無ければ内蔵の表(今までどおり)
        for empty in ({}, {"gens": {}}, {"t5l": [], "gens": {"2": {"t5l": []}}}):
            C._CALIB_CACHE = empty
            with quiet():
                assert C._calib_tbl("t5l", 1) is C._CAL_T5L and C._calib_tbl("t5e", 2) is C._CAL_T5E
        # (6) 世代の表に無い key だけトップレベルへ
        C._CALIB_CACHE = {"t5e": A, "t5l": A, "gens": {"2": {"t5l": D}}}
        with quiet():
            assert C._calib_tbl("t5l", 2) is D and C._calib_tbl("t5e", 2) is A
        # (7) 壊れた形(gens が dict でない)でも落ちない
        C._CALIB_CACHE = {"t5l": A, "gens": [1, 2]}
        with quiet():
            assert C._calib_tbl("t5l", 2) is A
    finally:
        C._CALIB_CACHE = saved
        C._WARNED.clear()


def test_real_calib_json_gen1_equals_the_top_level():
    """本物の docs/calib.json: 現在の世代が1の間、世代1の表はトップレベル(今の数字)と同じ。"""
    C._CALIB_CACHE = None
    cal = C._load_calib()
    assert cal.get("t5e") and cal.get("t5l"), "docs/calib.json が読めること"
    with quiet():
        for k in ("t5e", "t5l"):
            want = ((cal.get("gens") or {}).get("1") or {}).get(k) or cal[k]
            assert C._calib_tbl(k, 1) == want and C._calib_tbl(k) == want
            assert C.cal_pct(0.40, C._calib_tbl(k, 1)) == C.cal_pct(0.40, want)
    C._CALIB_CACHE = None


# ---------------------------------------------------------------- update_all.py

def test_do_stamps_judges_each_race_by_its_own_gen():
    """同じ日に世代1と世代2の買い目が混ざっても、各レースは自分の世代で: 打刻(tk)、候補の証跡(qc/qp)、
    判定直前のオッズ取り直し(refresh)のすべて。"""
    now = datetime(2026, 10, 1, 12, 0, tzinfo=JST)
    races = [race(5, g=1), race(6, g=2), race(7), race(8, top3p=0.47, g=2), race(9, top3p=0.47),
             race(10, top3p=0.30), race(11, top3p=0.30, g=2)]
    pred = {"date": YMD, "venues": [{"code": 1, "name": "t", "races": races}]}
    refreshed = []
    saved = C._CALIB_CACHE
    C._CALIB_CACHE = {}
    try:
        with quiet():
            n = U.do_stamps(pred, now, refresh=lambda v, r: refreshed.append(r["no"]))
    finally:
        C._CALIB_CACHE = saved
    R = {r["no"]: r for r in races}
    assert n == 7
    assert [R[k]["tk"] for k in (5, 6, 7, 8, 9, 10, 11)] == [1, 0, 1, 1, 1, 0, 0]
    assert all(R[k].get("qc") == 1 and R[k].get("qp") for k in (5, 7, 8, 9))
    assert all("qc" not in R[k] and "qp" not in R[k] for k in (6, 10, 11))
    assert refreshed == [5, 7, 8, 9], refreshed
    assert R[6]["g"] == 2 and R[8]["g"] == 2 and "g" not in R[7], "印は打刻で触らない"


def test_do_results_fallback_stamp_uses_the_race_gen():
    """結果確定時の代わりの打刻(do_results → stamp_plans)。過去3日の取りこぼし回収(_carryover)も同じ関数を
    通るので、過去日のファイルのレースが自分の世代で判定される。"""
    now = datetime(2026, 10, 1, 13, 0, tzinfo=JST)
    races = [race(5, deadline="12:30"), race(6, g=2, deadline="12:30"), race(7, top3p=0.47, g=2, deadline="12:30")]
    pred = {"date": YMD, "venues": [{"code": 1, "name": "t", "races": races}]}
    saved = (U.fetch_result, U.time, C._CALIB_CACHE)
    U.fetch_result = lambda ymd, jcd, rno: {"order": "1-2-3", "pay3t": 990}
    U.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=lambda: 0.0)
    C._CALIB_CACHE = {}
    try:
        with quiet():
            n = U.do_results(pred, now, YMD)
    finally:
        U.fetch_result, U.time, C._CALIB_CACHE = saved
    R = {r["no"]: r for r in races}
    assert n == 3 and all(R[k]["ph"] == 1 and R[k]["result"]["order"] == "1-2-3" for k in (5, 6, 7))
    assert (R[5]["tk"], R[6]["tk"], R[7]["tk"]) == (1, 0, 1)


def test_do_odds_refetch_threshold_by_gen():
    """締切10分前の本オッズ取り直し(top4p のしきい値 cand_top4p_min)。本オッズ取得済み・締切8分前・
    top4p 0.42: 印なし(0.36)は取り直す、g=2(仮 0.46)は取り直さない、g=2 でも 0.47 なら取り直す。4R は対象外。"""
    now = datetime(2026, 10, 1, 12, 0, tzinfo=JST)
    races = [race(5, deadline="12:08"), race(6, g=2, deadline="12:08"), race(7, g=2, top3p=0.45, deadline="12:08"),
             race(4, deadline="12:08"), race(8, g=1, deadline="12:08")]
    pred = {"date": YMD, "venues": [{"code": 1, "name": "t", "races": races}]}
    fetched = []

    def f_odds(ymd, jcd, rno):
        fetched.append(rno)
        return {"fuku": {}, "t3": dict(HIGH), "f3": {}, "t2": {}, "f2": {}, "k": {}}

    saved = (U.fetch_odds, U.time)
    U.fetch_odds = f_odds
    U.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=lambda: 0.0)
    try:
        with quiet():
            n = U.do_odds(pred, now, YMD)
        assert fetched == [5, 7, 8] and n == 3, fetched
        # 取得の優先順(1回の上限 8 件)も世代で: 候補(自分の世代で top4p がしきい値以上)が先。
        # 世代2で 0.42 のレースが9件と、世代1で 0.42 のレース1件(最後尾)。世代1の1件は候補として枠に入り、
        # 世代2の末尾2件が次の周回へ回る。
        fetched.clear()
        many = [race(n_, g=2, odds=None, deadline="12:30") for n_ in range(5, 14)] + [race(14, odds=None, deadline="12:30")]
        pred = {"date": YMD, "venues": [{"code": 1, "name": "t", "races": many}]}
        with quiet():
            U.do_odds(pred, now, YMD)
        assert len(fetched) == U.ODDS_MAX_PER_RUN and 14 in fetched and 12 not in fetched and 13 not in fetched, fetched
        # 同じ並びで最後尾も g=2 なら、候補ではないので枠に入らない(10件とも同順位 → 先頭8件)。
        # 上の呼び出しで本オッズが付いたレースは取り直されないので、レースは作り直す。
        fetched.clear()
        many = [race(n_, g=2, odds=None, deadline="12:30") for n_ in range(5, 15)]
        pred = {"date": YMD, "venues": [{"code": 1, "name": "t", "races": many}]}
        with quiet():
            U.do_odds(pred, now, YMD)
        assert 14 not in fetched and fetched == list(range(5, 13)), fetched
    finally:
        U.fetch_odds, U.time = saved
    assert U.CAND_TOP4P_MIN == 0.36 and U._cand_top4p_min({}) == 0.36 and U._cand_top4p_min({"g": 2}) == 0.46


# ---------------------------------------------------------------- update_results.py / recompute_sengen.py

def test_unstamped_days_before_the_stamping_era_use_the_race_gen():
    """打刻の無い日(8/3 以前)の判定: レースの g(無ければ世代1 = 0.36)。同じ日に混ざっても各レースは自分の世代。"""
    races = [race(5, top3p=0.45), race(6, top3p=0.45, g=2), race(7, top3p=0.47, g=2), race(8, top3p=0.45, g=1)]
    results = {k: ("1-2-3", 1230) for k in (5, 6, 7, 8)}
    day = evaluate(races, results, ymd="20260803")
    assert day["races"] == 4 and day["top5_hit"] == 4
    assert day["sen_n"] == 3 and day["sen_hit"] == 3 and day["sen_ret"] == 3 * 1230, day   # 5R・7R・8R(6R は 0.46 に届かない)
    agg = RS.recompute_day(with_results(races, results, ymd="20260803"), "20260803")
    for k in ("sen_n", "sen_hit", "sen_stake", "sen_ret", "sen_hitloss"):
        assert agg[k] == day[k], (k, agg[k], day[k])
    # 価値フィルタ(3.1倍未満)も世代の min_odds(どちらも 3.1)
    low = [race(5, top3p=0.45, odds=LOW), race(6, top3p=0.47, g=2, odds=LOW)]
    assert evaluate(low, {5: ("6-5-4", 30000), 6: ("6-5-4", 30000)}, ymd="20260803")["sen_n"] == 0
    assert RS.recompute_day(with_results(low, {5: ("6-5-4", 30000), 6: ("6-5-4", 30000)}, ymd="20260803"), "20260803")["sen_n"] == 0
    # 8/4 以降は打刻が無ければ世代に関係なく数えない(今までどおり)
    assert evaluate(races, results, ymd="20260804")["sen_n"] == 0
    assert RS.recompute_day(with_results(races, results, ymd="20260804"), "20260804")["sen_n"] == 0
    # 打刻があれば打刻が勝つ(世代に関係なく)
    stamped = [race(5, g=2, tk=1, os={c: 5.0 for c in COMBOS[:4]}), race(6, tk=0, rs="x")]
    assert evaluate(stamped, {5: ("1-2-3", 1230), 6: ("1-2-3", 1230)})["sen_n"] == 1


def test_day_row_carries_the_model_gen():
    races = [race(5, tk=1, os={c: 5.0 for c in COMBOS[:4]})]
    results = {5: ("1-2-3", 1230)}
    assert evaluate(races, results)["gen"] == 1, "model_gen の無い日は 1"
    assert evaluate(races, results, model_gen=2)["gen"] == 2
    assert evaluate(races, results, model_gen="2")["gen"] == 2
    assert RS.recompute_day(with_results(races, results), YMD)["gen"] == 1
    assert RS.recompute_day(with_results(races, results, model_gen=2), YMD)["gen"] == 2
    assert UR._model_gen({}) == 1 and UR._model_gen({"model_gen": None}) == 1 and UR._model_gen({"model_gen": "x"}) == 1
    assert RS._model_gen({"model_gen": 2}) == 2


def test_totals_do_not_include_gen():
    """update_results.main / recompute_sengen.main: 日の行に gen を書き、total(決まった項目の合計)には足さない。
    前日(世代1・gen 無し)の行はそのまま。"""
    tmp = Path(tempfile.mkdtemp())
    d = tmp / "docs" / "predictions"
    d.mkdir(parents=True)
    races = [race(5, tk=1, os={c: 5.0 for c in COMBOS[:4]}), race(6, tk=0, rs="3.1倍未満 2.0倍")]
    results = {5: ("1-2-3", 1230), 6: ("1-2-3", 500)}
    pred = with_results(races, results, model_gen=2)
    (d / f"{YMD}.json").write_text(json.dumps(pred, ensure_ascii=False), encoding="utf-8")
    prev = {"date": "20260930", "races": 10, "top5_hit": 4, "stake5": 5000, "return5": 4000,
            "sen_n": 1, "sen_hit": 1, "sen_stake": 300, "sen_ret": 500}
    (tmp / "docs" / "accuracy.json").write_text(json.dumps({"days": [prev], "total": {}}), encoding="utf-8")
    saved = (UR.ROOT, UR.build_day, UR.save_year, sys.argv)
    UR.ROOT = tmp
    UR.build_day = lambda ymd: day_rows(results)
    UR.save_year = lambda df, y: None
    sys.argv = ["update_results.py", "--date", YMD]
    try:
        with quiet():
            UR.main()
    finally:
        UR.ROOT, UR.build_day, UR.save_year, sys.argv = saved
    out = json.loads((tmp / "docs" / "accuracy.json").read_text(encoding="utf-8"))
    rows = {r["date"]: r for r in out["days"]}
    assert rows[YMD]["gen"] == 2 and rows[YMD]["races"] == 2 and rows[YMD]["sen_n"] == 1
    assert rows["20260930"] == prev, "前日の行は触らない(gen も足さない)"
    assert "gen" not in out["total"] and "date" not in out["total"]
    assert out["total"]["races"] == 12 and out["total"]["sen_n"] == 2 and out["total"]["sen_hit"] == 2
    assert out["total"]["sen_ret"] == 500 + 1230 and out["total"]["top5_hit"] == 6
    # recompute_sengen.main: 同じく日の行に gen、total は同じ
    saved = RS.ROOT
    RS.ROOT = tmp
    try:
        with quiet():
            RS.main()
    finally:
        RS.ROOT = saved
    out2 = json.loads((tmp / "docs" / "accuracy.json").read_text(encoding="utf-8"))
    rows2 = {r["date"]: r for r in out2["days"]}
    assert rows2[YMD]["gen"] == 2 and rows2["20260930"] == prev
    assert "gen" not in out2["total"] and out2["total"] == out["total"], (out2["total"], out["total"])


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok   " + name)
    print("%d tests passed" % n)
    print("ALL OK")

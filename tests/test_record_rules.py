# -*- coding: utf-8 -*-
"""厳選の実績の数え方(scripts/common.py の sengen_counts / sengen_picks と、
scripts/update_results.py の evaluate)を確かめる。通信なし。

実行: python tests/test_record_rules.py   (Windows では PYTHONUTF8=1 を付ける)"""
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import common as C
import recompute_sengen as RS
import update_results as UR

YMD = "20261001"


def race(no, picks, **kw):
    r = {"no": no, "deadline": "12:00", "type": "x",
         "picks": [{"c": c, "p": 0.15 if i < 3 else 0.02} for i, c in enumerate(picks)],
         "boats": [], "fuku": {"lane": 1}}
    r.update(kw)
    return r


def day_rows(results):
    """results: {race_no: (着順 "1-2-3", 払戻)} → evaluate が読む形の1日分の表。"""
    rows = []
    for no, (order, pay) in results.items():
        top = [int(x) for x in order.split("-")]
        rest = [x for x in range(1, 7) if x not in top]
        for pos, lane in enumerate(top + rest, 1):
            rows.append({"venue": 1, "race_no": no, "lane": lane, "pos": float(pos), "abnormal": None,
                         "pay3t_combo": order, "pay3t_amount": float(pay)})
    return pd.DataFrame(rows)


def evaluate(races, results, ymd=YMD):
    tmp = Path(tempfile.mkdtemp())
    d = tmp / "docs" / "predictions"
    d.mkdir(parents=True)
    pred = {"date": ymd, "venues": [{"code": 1, "name": "t", "races": races}]}
    (d / f"{ymd}.json").write_text(json.dumps(pred, ensure_ascii=False), encoding="utf-8")
    saved = UR.ROOT
    UR.ROOT = tmp
    try:
        return UR.evaluate(ymd, day_rows(results))
    finally:
        UR.ROOT = saved


def with_results(races, results):
    """recompute_sengen は当日ファイルの result を読むので、結果を載せた写しを作る。"""
    out = []
    for r in races:
        r = dict(r)
        if r["no"] in results:
            order, pay = results[r["no"]]
            r["result"] = {"order": order, "pay3t": pay}
        out.append(r)
    return {"date": YMD, "venues": [{"code": 1, "name": "t", "races": out}]}


P = ["1-2-3", "1-3-2", "2-1-3", "1-2-4", "1-4-2", "2-1-4", "3-1-2", "3-2-1", "1-3-4", "1-4-3"]


def test_counts_only_judgments_made_before_the_deadline():
    assert C.sengen_counts({"tk": 1}) is True
    assert C.sengen_counts({"tk": 1, "ph": 1}) is False, "締切後に判定したレースは数えない"
    assert C.sengen_counts({"tk": 0}) is False and C.sengen_counts({"tk": 0, "ph": 1}) is False
    assert C.sengen_counts({}) is False


def test_hits_are_counted_with_the_picks_at_confirmation():
    # os は確定時の買い目の上位4点を、その順で持っている
    r = race(5, P, tk=1, os={"1-3-2": 4.0, "1-2-3": 5.0, "3-1-2": 9.0, "1-2-4": 12.0})
    assert C.sengen_picks(r) == ["1-3-2", "1-2-3", "3-1-2"], "保存されている買い目ではなく、確定時の目"
    assert C.sengen_picks(race(5, P, tk=1)) == P[:3], "os が無い古い記録は保存されている上位3点"
    assert C.sengen_picks(race(5, P, tk=1, os={"1-2-3": None})) == P[:3], "os が3点に満たなければ使わない"
    assert C.sengen_picks(race(5, P, tk=1, os={c: None for c in P[:4]})) == P[:3], "値が無くても順は同じ"


def test_evaluate_applies_both_rules():
    races = [
        race(5, P, tk=1, os={c: 5.0 for c in P[:4]}),                       # ふつうの厳選・的中
        race(6, P, tk=1, ph=1, os={c: 5.0 for c in P[:4]}),                 # 締切後の判定 → 数えない
        # 確定の後に買い目が差し替わった: 確定時は [3-1-2, 3-2-1, 1-3-4]、今の買い目は P(1-2-3 が先頭)
        race(7, P, tk=1, os={"3-1-2": 5.0, "3-2-1": 6.0, "1-3-4": 7.0, "1-4-3": 8.0}),
        race(8, P, tk=1, os={c: 5.0 for c in P[:4]}),                       # 厳選・不的中
        race(9, P, tk=0, rs="3.1倍未満 2.0倍"),                              # 見送り
        race(10, P, tk=1, os={c: 2.5 for c in P[:4]}),                      # 的中したが払戻が購入額未満
    ]
    results = {5: ("1-2-3", 1230), 6: ("1-2-3", 900), 7: ("1-2-3", 2000), 8: ("6-5-4", 30000),
               9: ("1-2-3", 500), 10: ("1-3-2", 250)}
    day = evaluate(races, results)
    # 逆向き: 確定時の買い目 [3-1-2, 3-2-1, 1-3-4] では的中、今の保存の上位3点では不的中
    races.append(race(11, P, tk=1, os={"3-1-2": 5.0, "3-2-1": 6.0, "1-3-4": 7.0, "1-4-3": 8.0}))
    results[11] = ("3-2-1", 4000)
    day = evaluate(races, results)
    assert day["races"] == 7
    assert day["sen_n"] == 5, day["sen_n"]                  # 5R・7R・8R・10R・11R(6R と 9R は数えない)
    assert day["sen_hit"] == 3, day["sen_hit"]              # 5R・10R・11R(7R は差し替え後の目でしか当たっていない)
    assert day["sen_stake"] == 1500 and day["sen_ret"] == 1230 + 250 + 4000, (day["sen_stake"], day["sen_ret"])
    assert day["sen_hitloss"] == 1
    # 全R(上位5点)の集計は今までどおり、保存されている買い目で数える
    assert day["top5_hit"] == 5 and day["return5"] == 1230 + 900 + 2000 + 500 + 250
    # 手動の再集計(recompute_sengen.py)も同じ規則で、同じ数字になる
    agg = RS.recompute_day(with_results(races, results), YMD)
    for k in ("sen_n", "sen_hit", "sen_stake", "sen_ret", "sen_hitloss"):
        assert agg[k] == day[k], (k, agg[k], day[k])


def test_unstamped_races_count_only_before_the_stamping_era():
    """打刻の無いレースを後から条件に当てはめて数えるのは、打刻の方式を始める前の日(〜8/3)だけ。
    8/4 以降は、打刻が無い=誰にも確定を知らせていないレースなので、厳選の実績に数えない
    (周回が丸1日止まった時に、翌朝の集計で勝手に厳選が増えないように)。"""
    r = race(5, P, odds={"t3": {c: 5.0 for c in P}})
    old = evaluate([r], {5: ("1-2-3", 1230)}, ymd="20260803")
    assert old["sen_n"] == 1 and old["sen_hit"] == 1 and old["sen_ret"] == 1230
    for ymd in ("20260804", "20261001"):
        new = evaluate([r], {5: ("1-2-3", 1230)}, ymd=ymd)
        assert new["sen_n"] == 0 and new["sen_hit"] == 0 and new["races"] == 1, (ymd, new["sen_n"])
    pred = with_results([r], {5: ("1-2-3", 1230)})
    assert RS.recompute_day(pred, "20260803")["sen_n"] == 1 and RS.recompute_day(pred, "20261001")["sen_n"] == 0


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok   " + name)
    print("%d tests passed" % n)
    print("ALL OK")

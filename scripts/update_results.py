# -*- coding: utf-8 -*-
"""結果更新: 指定日(既定=前日JST)のB+Kを取得してデータセットへ追記し、
その日の予測JSONと突き合わせて的中実績をdocs/accuracy.jsonに記録する。"""
import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import build_day, save_year

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))


def evaluate(ymd: str, day_df: pd.DataFrame):
    pred_path = ROOT / "docs" / "predictions" / f"{ymd}.json"
    if not pred_path.exists():
        print(f"no prediction file for {ymd}, skip evaluation")
        return None
    pred = json.loads(pred_path.read_text())
    actual = {}
    pays = {}
    top2_lanes = {}
    for (venue, rno), g in day_df.groupby(["venue", "race_no"]):
        g = g.dropna(subset=["pos"])
        try:
            a = int(g.loc[g["pos"] == 1, "lane"].iloc[0])
            b = int(g.loc[g["pos"] == 2, "lane"].iloc[0])
            c = int(g.loc[g["pos"] == 3, "lane"].iloc[0])
        except IndexError:
            continue
        actual[(int(venue), int(rno))] = f"{a}-{b}-{c}"
        top2_lanes[(int(venue), int(rno))] = (a, b)
        pay = g["pay3t_amount"].dropna()
        pays[(int(venue), int(rno))] = float(pay.iloc[0]) if len(pay) else 0.0

    day = {"date": ymd, "races": 0, "win_hit": 0,
           "top1_hit": 0, "top6_hit": 0, "top10_hit": 0,
           "stake6": 0, "return6": 0,
           "fuku_hit": 0, "sen_n": 0, "sen_hit": 0}
    for v in pred.get("venues", []):
        for r in v["races"]:
            key = (v["code"], r["no"])
            act = actual.get(key)
            if not act:
                continue
            day["races"] += 1
            picks = [p["c"] for p in r["picks"]]
            win_lane = int(act.split("-")[0])
            top_boat = max(r["boats"], key=lambda b: b["wp"])["lane"]
            fuku_lane = r.get("fuku", {}).get("lane", top_boat)
            if top_boat == win_lane:
                day["win_hit"] += 1
            if fuku_lane in top2_lanes.get(key, ()):
                day["fuku_hit"] += 1
                if r.get("sengen"):
                    day["sen_hit"] += 1
            if r.get("sengen"):
                day["sen_n"] += 1
            if picks and picks[0] == act:
                day["top1_hit"] += 1
            day["stake6"] += 600
            if act in picks[:6]:
                day["top6_hit"] += 1
                day["return6"] += pays.get(key, 0)
            if act in picks[:10]:
                day["top10_hit"] += 1
    return day


def main():
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y%m%d")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=yesterday)
    a = ap.parse_args()
    ymd = a.date

    df = build_day(ymd)
    if df is None:
        print(f"{ymd}: no data (no races held)")
        return
    save_year(df, ymd[:4])

    day = evaluate(ymd, df)
    if day:
        acc_path = ROOT / "docs" / "accuracy.json"
        acc = json.loads(acc_path.read_text()) if acc_path.exists() else {"days": []}
        acc["days"] = [d for d in acc["days"] if d["date"] != ymd] + [day]
        acc["days"].sort(key=lambda d: d["date"])
        acc["days"] = acc["days"][-365:]
        t = {"races": 0, "win_hit": 0, "top1_hit": 0, "top6_hit": 0,
             "top10_hit": 0, "stake6": 0, "return6": 0,
             "fuku_hit": 0, "sen_n": 0, "sen_hit": 0}
        for d in acc["days"]:
            for k in t:
                t[k] += d.get(k, 0)
        acc["total"] = t
        acc_path.write_text(json.dumps(acc, ensure_ascii=False))
        print("accuracy:", json.dumps(day, ensure_ascii=False))


if __name__ == "__main__":
    main()

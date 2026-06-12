# -*- coding: utf-8 -*-
"""直前更新: 締切が近いレースのbeforeinfo(展示タイム・風波)を取得し、
該当レースのみ再予測して latest.json / YYYYMMDD.json を差し替える。15分毎にActionsで実行。"""
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from common import download_day, parse_b
from features import load_fan
from fetch_beforeinfo import fetch_beforeinfo
from predict_today import load_hist, load_models, races_to_rows, predict_races, write

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))
WINDOW_MIN = (-3, 45)  # 締切まで -3分(直後) 〜 45分前 を対象


def main():
    now = datetime.now(JST)
    ymd = now.strftime("%Y%m%d")
    path = ROOT / "docs" / "predictions" / f"{ymd}.json"
    if not path.exists():
        print("no prediction file for today; run predict_today first")
        return
    pred = json.loads(path.read_text())
    if not pred.get("venues"):
        print("no venues today")
        return

    targets = []
    for v in pred["venues"]:
        for r in v["races"]:
            if r.get("live"):
                continue
            dl = r.get("deadline")
            if not dl or ":" not in dl:
                continue
            h, m = map(int, dl.split(":"))
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            mins = (t - now).total_seconds() / 60
            if WINDOW_MIN[0] <= mins <= WINDOW_MIN[1]:
                targets.append((v["code"], r["no"]))
    if not targets:
        print("no races in deadline window")
        return
    print(f"window targets: {targets}", flush=True)

    live = {}
    for jcd, rno in targets:
        info = fetch_beforeinfo(ymd, jcd, rno)
        if info and any(x is not None for x in info["ex"].values()):
            live[(jcd, rno)] = info
            print(f"  {jcd}-{rno}R: ex ok, wind={info['wind']} wave={info['wave']}")
        else:
            print(f"  {jcd}-{rno}R: not published yet")
        time.sleep(0.5)
    if not live:
        print("no beforeinfo available; skip")
        return

    btxt = download_day("B", ymd)
    if btxt is None:
        print("B file unavailable")
        return
    races = [r for r in parse_b(btxt, ymd) if (r["venue"], r["race_no"]) in live]
    if not races:
        print("no matching races in B")
        return

    tgt = pd.DataFrame(races_to_rows(races, live=live))
    meta, models, sengen = load_models()
    hist = load_hist()
    fan = load_fan(ROOT / "data" / "fan")
    by_venue, _ = predict_races(tgt, hist, fan, models, sengen)

    n_upd = 0
    for v in pred["venues"]:
        new_list = by_venue.get(v["code"], [])
        for i, r in enumerate(v["races"]):
            for nr in new_list:
                if nr["no"] == r["no"]:
                    inf = live[(v["code"], r["no"])]
                    nr["live"] = True
                    nr["live_at"] = now.strftime("%H:%M")
                    nr["ex"] = {str(k): val for k, val in inf["ex"].items()}
                    nr["st_ex"] = {str(k): val for k, val in inf["st"].items()}
                    nr["course_ex"] = {str(k): val for k, val in inf["course"].items()}
                    nr["wind"] = inf["wind"]
                    nr["wave"] = inf["wave"]
                    v["races"][i] = nr
                    n_upd += 1
    pred["live_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    write(pred, ymd)
    print(f"live updated: {n_upd} races")


if __name__ == "__main__":
    main()

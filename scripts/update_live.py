# -*- coding: utf-8 -*-
"""Live updater: (1) fetch public odds for ALL pre-deadline races and attach to
picks/axis (odds are published throughout the sale window, independent of
exhibition data); (2) fetch exhibition (beforeinfo) + re-predict for races near
deadline; (3) fetch results of ANY finished race whose result is not yet stored
(no upper time limit). Runs every few minutes via GitHub Actions."""
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
from fetch_odds import fetch_odds
from fetch_result import fetch_result
from predict_today import load_hist, load_models, races_to_rows, predict_races, write

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))
# exhibition window (re-predict): from this many min before deadline, until N min after
EXHIBIT_MIN = (-3, 45)
# odds window: published during sale; start fetching this many min before deadline
ODDS_BEFORE_MAX = 120   # begin odds polling up to 120 min before deadline
ODDS_AFTER = 3          # stop a few min after deadline (no more odds changes)
ODDS_MAX_PER_RUN = 30   # cap odds fetches per cycle (rest handled next run)
RESULT_MAX_PER_RUN = 25 # cap result fetches per cycle (backlog drains over runs)


def _mins_to_deadline(now, dl):
    if not dl or ":" not in dl:
        return None
    h, m = map(int, dl.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return (t - now).total_seconds() / 60


def _axis_from_odds(nr, odds):
    """Compute axis (best single combo per ticket type) + combo labels + per-pick t3."""
    fav_lane = nr.get("fuku", {}).get("lane")
    combos = [p["c"] for p in nr.get("picks", [])]
    axis, axis_combo = {}, {}
    if combos:
        a, b, c = combos[0].split("-")
        s2 = "=".join(sorted([a, b]))
        s3 = "=".join(sorted([a, b, c]))
        axis = {"fuku": odds.get("fuku", {}).get(fav_lane),
                "k": odds.get("k", {}).get(s2),
                "f2": odds.get("f2", {}).get(s2),
                "t2": odds.get("t2", {}).get(f"{a}-{b}"),
                "f3": odds.get("f3", {}).get(s3),
                "t3": odds.get("t3", {}).get(combos[0])}
        axis_combo = {"fuku": str(fav_lane), "k": s2, "f2": s2,
                      "t2": f"{a}-{b}", "f3": s3, "t3": combos[0]}
    return {
        "fuku": odds.get("fuku", {}).get(fav_lane),
        "t3": {c2: odds.get("t3", {}).get(c2) for c2 in combos
               if odds.get("t3", {}).get(c2) is not None},
        "axis": axis, "axis_combo": axis_combo,
    }


def update_odds(pred, now, ymd) -> int:
    """Fetch public odds for every pre-deadline race (independent of exhibition)
    and merge into nr['odds'] so picks/axisTable show odds during the sale."""
    targets = []
    for v in pred["venues"]:
        for r in v["races"]:
            if r.get("result"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None:
                continue
            # within sale window: deadline-ODDS_BEFORE_MAX .. deadline+ODDS_AFTER
            if -ODDS_AFTER <= mins <= ODDS_BEFORE_MAX:
                targets.append((v["code"], r["no"]))
    if not targets:
        print("no races in odds window")
        return 0
    print(f"odds targets: {targets}", flush=True)

    n_odds = 0
    fetched = 0
    for v in pred["venues"]:
        for i, r in enumerate(v["races"]):
            if (v["code"], r["no"]) not in targets:
                continue
            if fetched >= ODDS_MAX_PER_RUN:
                print(f"odds cap {ODDS_MAX_PER_RUN} reached; rest next run")
                return n_odds
            fetched += 1
            odds = fetch_odds(ymd, v["code"], r["no"])
            time.sleep(0.4)
            if not odds or not (odds.get("t3") or odds.get("fuku")):
                continue
            merged = _axis_from_odds(r, odds)
            # keep existing live flags/exhibits; only refresh odds-derived fields
            ex_odds = r.get("odds", {})
            ex_odds.update(merged)
            r["odds"] = ex_odds
            n_odds += 1
            print(f"  odds {v['code']}-{r['no']}R: t3={len(odds.get('t3',{}))} fuku={len(odds.get('fuku',{}))}")
    return n_odds


def update_exhibits(pred, now, ymd) -> int:
    targets = []
    for v in pred["venues"]:
        for r in v["races"]:
            if r.get("live"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is not None and EXHIBIT_MIN[0] <= mins <= EXHIBIT_MIN[1]:
                targets.append((v["code"], r["no"]))
    if not targets:
        print("no races in exhibit window")
        return 0
    print(f"exhibit targets: {targets}", flush=True)

    live = {}
    for jcd, rno in targets:
        info = fetch_beforeinfo(ymd, jcd, rno)
        if info and any(x is not None for x in info["ex"].values()):
            odds = fetch_odds(ymd, jcd, rno) or {"fuku": {}, "t3": {}}
            info["odds"] = odds
            live[(jcd, rno)] = info
            print(f"  {jcd}-{rno}R: ex ok, wind={info['wind']} wave={info['wave']} odds3t={len(odds['t3'])}")
        else:
            print(f"  {jcd}-{rno}R: not published yet")
        time.sleep(0.5)
    if not live:
        return 0

    btxt = download_day("B", ymd)
    if btxt is None:
        print("B file unavailable")
        return 0
    races = [r for r in parse_b(btxt, ymd) if (r["venue"], r["race_no"]) in live]
    if not races:
        return 0

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
                    nr["odds"] = _axis_from_odds(nr, inf.get("odds", {}))
                    if r.get("result"):
                        nr["result"] = r["result"]
                    v["races"][i] = nr
                    n_upd += 1
    return n_upd


def update_results(pred, now, ymd) -> int:
    """Fetch results for ANY finished race (deadline passed) whose result is not
    yet stored. No upper time bound -- finished races keep getting retried until
    their result is fixed and recorded."""
    n_res = 0
    tried = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if r.get("result"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            # any race past its deadline (with small grace) and not yet recorded
            if mins is None or mins > -ODDS_AFTER:
                continue
            if tried >= RESULT_MAX_PER_RUN:
                print(f"result cap {RESULT_MAX_PER_RUN} reached; rest next run")
                return n_res
            tried += 1
            res = fetch_result(ymd, v["code"], r["no"])
            time.sleep(0.5)
            if not res:
                print(f"  result {v['code']}-{r['no']}R: not fixed yet")
                continue
            order = res["order"]
            top2 = set(map(int, order.split("-")[:2]))
            fuku_lane = r.get("fuku", {}).get("lane")
            picks = [p["c"] for p in r.get("picks", [])]
            boats = r.get("boats", [])
            top_boat = max(boats, key=lambda b: b["wp"])["lane"] if boats else None
            res["hit_win"] = top_boat == int(order.split("-")[0])
            res["hit_fuku"] = fuku_lane in top2
            res["hit_t1"] = bool(picks) and picks[0] == order
            res["hit_t6"] = order in picks[:6]
            res["hit_t10"] = order in picks[:10]
            r["result"] = res
            n_res += 1
            print(f"  result {v['code']}-{r['no']}R: {order} fuku={'HIT' if res['hit_fuku'] else 'MISS'}"
                  f"{' sengen' if r.get('sengen') else ''}")
    return n_res


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

    n_odds = update_odds(pred, now, ymd)
    n_upd = update_exhibits(pred, now, ymd)
    n_res = update_results(pred, now, ymd)
    if n_odds == 0 and n_upd == 0 and n_res == 0:
        print("nothing to update")
        return
    pred["live_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    write(pred, ymd)
    print(f"live updated: odds={n_odds} exhibits={n_upd} results={n_res}")


if __name__ == "__main__":
    main()

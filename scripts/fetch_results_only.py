# -*- coding: utf-8 -*-
"""Results-only updater: fetch race results for ANY finished race (deadline
passed) whose result is not yet stored, and attach hit flags. Deliberately does
NOT fetch odds or exhibition data, so it never blocks on the slow odds pages.
Runs every few minutes via GitHub Actions (results.yml)."""
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_result import fetch_result

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))
GRACE_MIN = 3            # ignore races until this many min past deadline
RESULT_MAX_PER_RUN = 40  # cap fetches per cycle; backlog drains over runs


def write(obj, ymd):
    d = ROOT / "docs" / "predictions"
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False)
    (d / f"{ymd}.json").write_text(txt)
    (d / "latest.json").write_text(txt)


def _mins_to_deadline(now, dl):
    if not dl or ":" not in dl:
        return None
    h, m = map(int, dl.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return (t - now).total_seconds() / 60


def update_results(pred, now, ymd) -> int:
    n_res = 0
    tried = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if r.get("result"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None or mins > -GRACE_MIN:
                continue
            if tried >= RESULT_MAX_PER_RUN:
                print(f"result cap {RESULT_MAX_PER_RUN} reached; rest next run")
                return n_res
            tried += 1
            res = fetch_result(ymd, v["code"], r["no"])
            time.sleep(0.4)
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

    n_res = update_results(pred, now, ymd)
    if n_res == 0:
        print("nothing to update")
        return
    pred["results_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    write(pred, ymd)
    print(f"results updated: {n_res}")


if __name__ == "__main__":
    main()

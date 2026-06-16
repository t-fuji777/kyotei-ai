# -*- coding: utf-8 -*-
"""Unified updater: in ONE pass over today's prediction file, attach race
RESULTS (for finished races) and refresh ODDS (for races on sale), then write
once. Single writer => no git rebase conflicts between separate workflows.

Why unified: odds fetching is slow (~10-15s/race from Actions IPs), and while it
runs a separate results workflow would commit the same latest.json, causing
merge conflicts on push. Doing both here and committing once avoids that."""
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_result import fetch_result
from fetch_odds import fetch_odds

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))

# ---- results config ----
GRACE_MIN = 3
RESULT_MAX_PER_RUN = 40
# ---- odds config ----
ODDS_BEFORE_MAX = 60    # only fetch odds for races within 60 min of deadline
ODDS_AFTER = 5
ODDS_MAX_PER_RUN = 12   # keep small so the whole run finishes in time


def _mins_to_deadline(now, dl):
    if not dl or ":" not in dl:
        return None
    h, m = map(int, dl.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return (t - now).total_seconds() / 60


def write(obj, ymd):
    d = ROOT / "docs" / "predictions"
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False)
    (d / f"{ymd}.json").write_text(txt)
    (d / "latest.json").write_text(txt)


def _axis_from_odds(nr, odds):
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


def do_results(pred, now, ymd) -> int:
    n = 0
    tried = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if r.get("result"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None or mins > -GRACE_MIN:
                continue
            if tried >= RESULT_MAX_PER_RUN:
                break
            tried += 1
            res = fetch_result(ymd, v["code"], r["no"])
            time.sleep(0.3)
            if not res:
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
            n += 1
            print(f"  result {v['code']}-{r['no']}R: {order}")
    return n


def _has_odds(r):
    """True if this race already has fetched 3t odds stored."""
    o = r.get("odds") or {}
    t3 = o.get("t3") or {}
    return len(t3) > 0


def do_odds(pred, now, ymd) -> int:
    # Fetch odds for EVERY race that doesn't yet have them, regardless of whether
    # the race has finished -- boatrace keeps the odds page up for the whole day,
    # so finished races still return their final (confirmed) odds. Skip only races
    # whose deadline is still far in the future (odds not meaningful yet) and races
    # that already have odds stored (so we never re-fetch and never overwrite).
    targets = []
    for v in pred["venues"]:
        for r in v["races"]:
            if _has_odds(r):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None:
                continue
            # too early: more than ODDS_BEFORE_MAX minutes before deadline
            if mins > ODDS_BEFORE_MAX:
                continue
            _p5 = sum((pk.get("p") or 0) for pk in (r.get("picks") or [])[:5])
            targets.append((_p5 < 0.40, mins, v["code"], r["no"]))
    if not targets:
        print("no races need odds")
        return 0
    targets.sort(key=lambda t: (t[0], t[1] < 0, abs(t[1])))
    targets = [(c, n) for _, _, c, n in targets[:ODDS_MAX_PER_RUN]]
    print(f"odds targets ({len(targets)}): {targets}",
          flush=True)
    n = 0
    fetched = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if (v["code"], r["no"]) not in targets:
                continue
            if fetched >= ODDS_MAX_PER_RUN:
                break
            fetched += 1
            odds = fetch_odds(ymd, v["code"], r["no"])
            time.sleep(0.3)
            if not odds or not (odds.get("t3") or odds.get("fuku")):
                print(f"  odds {v['code']}-{r['no']}R: none yet")
                continue
            merged = _axis_from_odds(r, odds)
            ex = r.get("odds", {})
            ex.update(merged)
            r["odds"] = ex
            n += 1
            print(f"  odds {v['code']}-{r['no']}R: t3={len(odds.get('t3',{}))}")
    return n


def main():
    now = datetime.now(JST)
    ymd = now.strftime("%Y%m%d")
    path = ROOT / "docs" / "predictions" / f"{ymd}.json"
    if not path.exists():
        print("no prediction file for today")
        return
    pred = json.loads(path.read_text())
    if not pred.get("venues"):
        print("no venues today")
        return

    n_res = do_results(pred, now, ymd)
    n_odds = do_odds(pred, now, ymd)
    if n_res == 0 and n_odds == 0:
        print("nothing to update")
        return
    if n_res:
        pred["results_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    if n_odds:
        pred["odds_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    write(pred, ymd)
    print(f"updated: results={n_res} odds={n_odds}")


if __name__ == "__main__":
    main()

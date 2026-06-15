# -*- coding: utf-8 -*-
"""Odds-only updater: fetch public odds for races and merge axis/axis_combo/t3
into each race's 'odds' field, so the UI shows odds next to picks. Runs on a
schedule via GitHub Actions (odds.yml). Independent of results/exhibition so a
slow odds page never blocks anything else.

Key fix vs the old in-live path: boatrace odds pages take ~10s to respond from
GitHub Actions IPs, so the fetch timeout must be generous (handled in
fetch_odds.py via ODDS_TIMEOUT below)."""
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fetch_odds import fetch_t3

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))

# Odds sale window relative to deadline (minutes). Negative mins = after deadline.
ODDS_BEFORE_MAX = 120  # start polling up to 2h before deadline
ODDS_AFTER = 5          # keep a few min after deadline, then result takes over
ODDS_MAX_PER_RUN = 60   # cap fetches per cycle; backlog drains over runs


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


def write(obj, ymd):
    d = ROOT / "docs" / "predictions"
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False)
    (d / f"{ymd}.json").write_text(txt)
    (d / "latest.json").write_text(txt)


def update_odds(pred, now, ymd) -> int:
    targets = []
    for v in pred["venues"]:
        for r in v["races"]:
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None:
                continue
            has_t3 = bool(r.get("odds", {}).get("t3"))
            closed = bool(r.get("result")) or mins < -ODDS_AFTER
            picks = r.get("picks", [])
            sengen = sum(p.get("p", 0) for p in picks[:5]) >= 0.40
            # sengen races: keep odds at ALL times (even after the race the
            # odds3t page still serves the final odds). non-sengen: only the
            # open window. closed races keep final odds -> fetch once then skip.
            if not (sengen or -ODDS_AFTER <= mins <= ODDS_BEFORE_MAX):
                continue
            if closed and has_t3:
                continue
            targets.append((v["code"], r["no"]))
    if not targets:
        print("no races in odds window")
        return 0
    print(f"odds targets ({len(targets)}): {targets}", flush=True)

    n_odds = 0
    fetched = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if (v["code"], r["no"]) not in targets:
                continue
            if fetched >= ODDS_MAX_PER_RUN:
                print(f"odds cap {ODDS_MAX_PER_RUN} reached; rest next run")
                return n_odds
            fetched += 1
            t3all = fetch_t3(ymd, v["code"], r["no"])
            time.sleep(0.6)
            if not t3all:
                print(f"  odds {v['code']}-{r['no']}R: none yet")
                continue
            combos = [p["c"] for p in r.get("picks", [])]
            ex_odds = r.get("odds", {})
            ex_odds["t3"] = {c: t3all[c] for c in combos if c in t3all}
            ex_odds["axis_t3"] = t3all.get(combos[0]) if combos else None
            r["odds"] = ex_odds
            n_odds += 1
            print(f"  odds {v['code']}-{r['no']}R: t3={len(ex_odds['t3'])}")
    return n_odds


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
    if n_odds == 0:
        print("nothing to update")
        return
    pred["odds_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    write(pred, ymd)
    print(f"odds updated: {n_odds}")


if __name__ == "__main__":
    main()

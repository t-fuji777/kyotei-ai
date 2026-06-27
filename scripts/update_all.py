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
from fetch_result import fetch_result, fetch_before_html, parse_before
from fetch_odds import fetch_odds, fetch_racename, fetch_t3

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))

# ---- results config ----
GRACE_MIN = 3
RESULT_MAX_PER_RUN = 150
# ---- odds config ----
ODDS_BEFORE_MAX = 60    # only fetch odds for races within 60 min of deadline
ODDS_AFTER = 5
ODDS_MAX_PER_RUN = 8   # keep small so the whole run finishes in time
# ---- morning provisional odds config ----
MORNING_ODDS_MAX_PER_RUN = 12       # lightweight t3-only sweep, far-out races
MORNING_ODDS_FROM_HHMM = (7, 45)    # advance (zen-uri) odds appear ~7:45 JST
SENGEN_REFETCH_MIN = 10             # re-fetch sengen candidates within N min of deadline (final 5x check)
SENGEN_TOP5P_MIN = 0.40             # a race is a sengen candidate when top-5 cumulative prob >= this


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
            if "order" not in res:
                r["result"] = res
                n += 1
                print(f"  result {v['code']}-{r['no']}R: {res.get('status', '?')}")
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
    # that already have odds stored (provisional ones are re-fetched once after deadline, then marked final).
    targets = []
    for v in pred["venues"]:
        for r in v["races"]:
            o = r.get("odds") or {}
            if o.get("final"):
                continue
            _p5 = sum((pk.get("p") or 0) for pk in (r.get("picks") or [])[:5])
            if o.get("t3") and not o.get("prov"):
                m0 = _mins_to_deadline(now, r.get("deadline"))
                if m0 is None:
                    continue
                # real odds stored and deadline still ahead: normally skip, but
                # re-fetch sengen candidates in the final minutes so the >=5x
                # judgment runs on near-final odds rather than the ~60-min value.
                if m0 >= 0 and not (_p5 >= SENGEN_TOP5P_MIN and m0 <= SENGEN_REFETCH_MIN):
                    continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None:
                continue
            # too early: more than ODDS_BEFORE_MAX minutes before deadline
            if mins > ODDS_BEFORE_MAX:
                continue
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
            ex.pop("prov", None)
            _m = _mins_to_deadline(now, r.get("deadline"))
            if _m is not None and _m < 0:
                ex["final"] = True
            r["odds"] = ex
            n += 1
            print(f"  odds {v['code']}-{r['no']}R: t3={len(odds.get('t3',{}))}")
    return n


def do_morning_odds(pred, now, ymd) -> int:
    # Early-morning provisional sweep. Once advance (zen-uri) odds are published
    # (~7:45 JST), fetch lightweight trifecta-only odds for EVERY race that has no
    # odds yet, no matter how far its deadline is, and mark them provisional.
    # do_odds replaces these with the full real odds (and clears the prov flag) as
    # each race enters its 60-min window, then marks them final after deadline.
    # Unpublished races return None here and are simply retried on the next pass.
    if (now.hour, now.minute) < MORNING_ODDS_FROM_HHMM:
        return 0
    targets = []
    for v in pred["venues"]:
        for r in v["races"]:
            if (r.get("odds") or {}).get("t3") or r.get("result"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None or mins <= ODDS_BEFORE_MAX:
                continue  # within-window / finished races are do_odds' job
            targets.append((v["code"], r["no"]))
    if not targets:
        return 0
    targets = targets[:MORNING_ODDS_MAX_PER_RUN]
    print(f"morning odds targets ({len(targets)}): {targets}", flush=True)
    n = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if (v["code"], r["no"]) not in targets:
                continue
            t3 = fetch_t3(ymd, v["code"], r["no"])
            time.sleep(0.3)
            if not t3:
                continue
            ex = r.get("odds", {})
            ex.update(_axis_from_odds(r, {"t3": t3}))
            ex["prov"] = True
            r["odds"] = ex
            n += 1
            print(f"  morning odds {v['code']}-{r['no']}R: t3={len(t3)} (prov)")
    return n


RACENAME_MAX_PER_RUN = 12


def do_racenames(pred, ymd) -> int:
    # B-program race names are truncated to ~6 chars; fetch the full name from
    # the official racelist page and overwrite. Names are day-invariant, so once
    # stored (rn_full flag) we never re-fetch.
    tried = updated = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if r.get("rn_full"):
                continue
            if tried >= RACENAME_MAX_PER_RUN:
                print(f"racenames: {updated} updated (cap)", flush=True)
                return updated
            nm = fetch_racename(ymd, v["code"], r["no"])
            tried += 1
            time.sleep(0.3)
            if nm:
                r["type"] = nm
                r["rn_full"] = True
                updated += 1
    print(f"racenames: {updated} updated, {tried} tried", flush=True)
    return updated


def _carryover(now):
    yest = (now - timedelta(days=1)).strftime("%Y%m%d")
    d = ROOT / "docs" / "predictions"
    yp = d / f"{yest}.json"
    if not yp.exists():
        return
    try:
        py = json.loads(yp.read_text())
    except Exception:
        return
    if not py.get("venues"):
        return
    if not any(not r.get("result") for v in py["venues"] for r in v.get("races", [])):
        return
    ny = do_results(py, now, yest)
    if not ny:
        return
    py["results_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    txt = json.dumps(py, ensure_ascii=False)
    yp.write_text(txt)
    lp = d / "latest.json"
    if lp.exists():
        try:
            if json.loads(lp.read_text()).get("date") == yest:
                lp.write_text(txt)
        except Exception:
            pass
    print(f"carryover {yest}: results={ny}")


ST_EX_LEAD = 25
ST_EX_MAX_PER_RUN = 15


def do_st_ex(pred, now, ymd) -> int:
    # Lightweight exhibition (ST / lap-time / weather) backfill so the frequent
    # results-only pass surfaces 展示反映 within ~90s instead of waiting for the
    # slow live re-prediction pass. No ML; only beforeinfo fetches. Bounded by
    # fetch count so it never blocks the results loop for long.
    n = 0
    tried = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if tried >= ST_EX_MAX_PER_RUN:
                break
            if r.get("result"):
                continue
            if r.get("st_ex") and r.get("weather"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None or mins > ST_EX_LEAD:
                continue
            try:
                bi = parse_before(fetch_before_html(ymd, v["code"], r["no"]))
            except Exception:
                continue
            tried += 1
            stx = bi.get("st", {})
            if stx:
                r["st_ex"] = {str(k): val for k, val in stx.items()}
                if not r.get("ex") and len(bi.get("ex", {})) == 6:
                    r["ex"] = bi["ex"]
                if bi.get("weather"):
                    r["weather"] = bi["weather"]
                n += 1
            time.sleep(0.3)
        if tried >= ST_EX_MAX_PER_RUN:
            break
    return n


def main():
    now = datetime.now(JST)
    ymd = now.strftime("%Y%m%d")
    results_only = "--results-only" in sys.argv
    _carryover(now)
    path = ROOT / "docs" / "predictions" / f"{ymd}.json"
    if not path.exists():
        print("no prediction file for today")
        return
    pred = json.loads(path.read_text())
    if not pred.get("venues"):
        print("no venues today")
        return

    n_res = do_results(pred, now, ymd)
    if results_only:
        n_stx = do_st_ex(pred, now, ymd)
        if n_res or n_stx:
            pred["results_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
            write(pred, ymd)
        print(f"results-only: results={n_res} st_ex={n_stx}")
        return
    n_odds = do_odds(pred, now, ymd)
    n_morn = do_morning_odds(pred, now, ymd)
    n_name = do_racenames(pred, ymd)
    if n_res == 0 and n_odds == 0 and n_morn == 0 and n_name == 0:
        print("nothing to update")
        return
    if n_res:
        pred["results_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    if n_odds or n_morn:
        pred["odds_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    write(pred, ymd)
    print(f"updated: results={n_res} odds={n_odds} morning={n_morn} names={n_name}")


if __name__ == "__main__":
    main()

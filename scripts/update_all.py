# -*- coding: utf-8 -*-
"""Unified updater: in ONE pass over today's prediction file, attach race
RESULTS (for finished races) and refresh ODDS (for races on sale), then write
once. Single writer => no git rebase conflicts between separate workflows.

Why unified: odds fetching is slow (~10-15s/race from Actions IPs), and while it
runs a separate results workflow would commit the same latest.json, causing
merge conflicts on push. Doing both here and committing once avoids that."""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import stamp_plans
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
SENGEN_REFETCH_MIN = 10             # re-fetch sengen/premier candidates within N min of deadline (final odds check)
CAND_TOP4P_MIN = 0.36               # top-4 cumulative prob threshold for the refetch heuristic; sengen
                                     # (top3p>=0.36) candidates are a subset of top4p>=0.36 candidates, so
                                     # this single top4p check covers both the sengen and premier tiers


def _mins_to_deadline(now, dl):
    if not dl or ":" not in dl:
        return None
    h, m = map(int, dl.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return (t - now).total_seconds() / 60


def _mins_to_deadline_on(now, dl, ymd):
    if not dl or ":" not in dl:
        return None
    h, m = map(int, dl.split(":"))
    base = datetime(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]), h, m, tzinfo=now.tzinfo)
    return (base - now).total_seconds() / 60


def _atomic_write_text(path: Path, txt: str) -> None:
    """Write text to path atomically via a temp file + os.replace, so a mid-write
    crash never leaves a truncated/corrupt file behind."""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(txt)
    os.replace(tmp, path)


def write(obj, ymd):
    d = ROOT / "docs" / "predictions"
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False)
    _atomic_write_text(d / f"{ymd}.json", txt)
    _atomic_write_text(d / "latest.json", txt)


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


STAMP_LEAD_MIN = 15  # 締切何分前からチェックポイント確定を打刻するか
# 締切から何分後まで打刻を許すか。古い作業ツリーのrunnerが何時間も後に
# 「締切15分前の確定」を名乗って打刻するのを防ぐ(2026-08-05に締切39分後の
# 打刻が発生)。この窓を過ぎたレースは do_results のフォールバックが拾う。
STAMP_LATE_MAX = 15


def do_stamps(pred, now) -> int:
    """締切15分前チェックポイント: まだ厳選(竹)が確定していない(r["tk"]無し)かつ
    結果未確定のレースのうち、締切前後STAMP_LEAD_MIN/STAMP_LATE_MAX分以内の
    ものへ、その時点のpicks/oddsで厳選スタンプ(r["tk"]/r["pt"]。mtは終売につき常に0)を
    first-winsで焼き込む。窓を外したものはdo_results側のフォールバックで結果
    確定時に焼く。"""
    n = 0
    now_hhmm = now.strftime("%H:%M")
    for v in pred["venues"]:
        for r in v["races"]:
            if "tk" in r:
                continue
            if r.get("result"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None or mins > STAMP_LEAD_MIN or mins < -STAMP_LATE_MAX:
                continue
            stamp_plans(r, v["code"], None, now_hhmm)
            n += 1
    if n:
        print(f"stamps: {n} race(s) checkpointed")
    return n


def do_results(pred, now, ymd, max_fetch=RESULT_MAX_PER_RUN) -> int:
    n = 0
    tried = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if r.get("result"):
                continue
            mins = _mins_to_deadline_on(now, r.get("deadline"), ymd)
            if mins is None or mins > -GRACE_MIN:
                continue
            if tried >= max_fetch:
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
            # 本命は保存済みの非丸めargmax(fuku.lane)を使う。boats.wp(3桁丸め)からの
            # 再計算はしない(丸め同値時に若い枠番優先バイアスが生じ複勝判定とも食い違うため)。
            top_boat = fuku_lane if fuku_lane is not None else (
                max(boats, key=lambda b: b["wp"])["lane"] if boats else None)
            res["hit_win"] = top_boat is not None and top_boat == int(order.split("-")[0])
            res["hit_fuku"] = fuku_lane in top2
            res["hit_t1"] = bool(picks) and picks[0] == order
            res["hit_t6"] = order in picks[:6]
            res["hit_t10"] = order in picks[:10]
            # 竹/松の該当可否を確定時点のrace(picks/odds)で焼き込む(遡及改変防止)。
            # first-winsのため、チェックポイント(do_stamps)で既に確定済みなら
            # ここでは何もしない(取りこぼした場合のみのフォールバック)。
            stamp_plans(r, v["code"], res, now.strftime("%H:%M"))
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
            # 結果が付いていても、t3が未取得 or 暫定(prov)のままなら暫定オッズ放置に
            # ならないよう取得対象に残す。確定t3(prov無し)が既にあればここでスキップ
            # (finalは上のo.get("final")で既に判定済み)。
            if r.get("result") and o.get("t3") and not o.get("prov"):
                continue
            _p4 = sum((pk.get("p") or 0) for pk in (r.get("picks") or [])[:4])
            if o.get("t3") and not o.get("prov"):
                m0 = _mins_to_deadline(now, r.get("deadline"))
                if m0 is None:
                    continue
                # real odds stored and deadline still ahead: normally skip, but
                # re-fetch sengen/premier candidates in the final minutes so the
                # final odds-band judgment runs on near-final odds rather than the
                # ~60-min value. top4p>=0.36 covers both tiers (see CAND_TOP4P_MIN).
                # races 1-4 are excluded from both tiers (5R以降のみ対象), so they
                # never qualify for this final-minutes re-fetch either.
                if m0 >= 0 and not (_p4 >= CAND_TOP4P_MIN and r["no"] >= 5 and m0 <= SENGEN_REFETCH_MIN):
                    continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None:
                continue
            # too early: more than ODDS_BEFORE_MAX minutes before deadline
            if mins > ODDS_BEFORE_MAX:
                continue
            targets.append((_p4 < CAND_TOP4P_MIN, mins, v["code"], r["no"]))
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


CARRYOVER_DAYS = 3                  # look back this many days (excluding today) for stale results
CARRYOVER_MAX_PER_RUN = RESULT_MAX_PER_RUN  # total fetches across all carryover days this run


def _carryover_one(now, ymd, budget):
    """Backfill missing results for a single past day by reusing do_results
    (which anchors deadlines to `ymd`), bounded by `budget` fetches. Returns
    the number of results fetched."""
    d = ROOT / "docs" / "predictions"
    yp = d / f"{ymd}.json"
    if not yp.exists() or budget <= 0:
        return 0
    try:
        py = json.loads(yp.read_text())
    except Exception:
        return 0
    if not py.get("venues"):
        return 0
    if not any(not r.get("result") for v in py["venues"] for r in v.get("races", [])):
        return 0
    n = do_results(py, now, ymd, max_fetch=budget)
    if not n:
        return 0
    py["results_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    txt = json.dumps(py, ensure_ascii=False)
    _atomic_write_text(yp, txt)
    lp = d / "latest.json"
    if lp.exists():
        try:
            if json.loads(lp.read_text()).get("date") == ymd:
                _atomic_write_text(lp, txt)
        except Exception:
            pass
    print(f"carryover {ymd}: results={n}")
    return n


def _carryover(now):
    """Backfill missing results for the last CARRYOVER_DAYS days (excluding
    today, which the main pass handles). Shares a single fetch budget across
    all days so overall load stays within the existing RESULT_MAX_PER_RUN
    envelope even when several past days still have gaps."""
    budget = CARRYOVER_MAX_PER_RUN
    for back in range(1, CARRYOVER_DAYS + 1):
        if budget <= 0:
            break
        ymd = (now - timedelta(days=back)).strftime("%Y%m%d")
        budget -= _carryover_one(now, ymd, budget)


ST_EX_LEAD = 25
ST_EX_MAX_PER_RUN = 15
# 決着後の取りこぼし回収: 展示は締切25分前〜結果確定までの間しか取りに行かないため、
# その窓でパイプラインが動かないと(実行の重なり/停止など)展示ST・展示タイムが
# 永久に欠落する。beforeinfoはレース後も参照できるので決着後に回収する。
# 1回の実行あたりの件数を絞り、締切から一定時間を過ぎたレースは諦める
# (恒久的に取得できないレースが毎回の枠を食い潰さないようにするため)。
ST_EX_BACKFILL_PER_RUN = 5
ST_EX_BACKFILL_MAX_AGE = 240


def _fill_st_ex(r, ymd, vcode) -> bool:
    """beforeinfoを取得して st_ex / ex / weather を埋める。埋まればTrue。
    取得・解析の例外は呼び出し元へ送出する(リトライ判断は呼び出し元の責務)。"""
    bi = parse_before(fetch_before_html(ymd, vcode, r["no"]))
    stx = bi.get("st", {})
    if not stx:
        return False
    r["st_ex"] = {str(k): val for k, val in stx.items()}
    if not r.get("ex") and len(bi.get("ex", {})) == 6:
        r["ex"] = bi["ex"]
    if bi.get("weather"):
        r["weather"] = bi["weather"]
    return True


def _st_ex_targets(pred, now):
    """展示取得の対象を優先順で返す。
    (1) 未決着かつ締切25分前以内(従来動作。速報性が最優先)
    (2) 決着済みで展示が欠落しているもの(取りこぼし回収。締切から4時間以内)"""
    live, backfill = [], []
    for v in pred["venues"]:
        for r in v["races"]:
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None:
                continue
            if r.get("result"):
                if not r.get("st_ex") and -ST_EX_BACKFILL_MAX_AGE <= mins < 0:
                    backfill.append((v["code"], r))
                continue
            if r.get("st_ex") and r.get("weather"):
                continue
            if mins <= ST_EX_LEAD:
                live.append((v["code"], r))
    return live[:ST_EX_MAX_PER_RUN] + backfill[:ST_EX_BACKFILL_PER_RUN]


def do_st_ex(pred, now, ymd) -> int:
    # Lightweight exhibition (ST / lap-time / weather) backfill so the frequent
    # results-only pass surfaces 展示反映 within ~90s instead of waiting for the
    # slow live re-prediction pass. No ML; only beforeinfo fetches. Bounded by
    # fetch count so it never blocks the results loop for long.
    n = 0
    consec_fail = 0
    for vcode, r in _st_ex_targets(pred, now):
        try:
            filled = _fill_st_ex(r, ymd, vcode)
        except Exception as e:
            print(f"  st_ex {vcode}-{r['no']}R fail: {e}")
            consec_fail += 1
            time.sleep(0.3)
            if consec_fail >= 5:
                print("st_ex: 5 consecutive failures; aborting pass")
                return n
            continue
        consec_fail = 0
        if filled:
            n += 1
        time.sleep(0.3)
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

    if results_only:
        # 毎分パスが最も確実に締切T-15のチェックポイントを捉えられるため、
        # 結果確定(do_results)の前にdo_stampsを呼ぶ。
        n_stp = do_stamps(pred, now)
        n_res = do_results(pred, now, ymd)
        n_stx = do_st_ex(pred, now, ymd)
        if n_res or n_stx or n_stp:
            pred["results_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
            write(pred, ymd)
        try:
            from notify import notify_events
            notify_events(pred, ymd)
        except Exception as e:
            print(f"notify skip: {e}")
        print(f"results-only: results={n_res} st_ex={n_stx} stamps={n_stp}")
        return
    # オッズ取得を結果確定より先に行う(確定時スタンプ(stamp_plans)が同一サイクルで
    # 取得した直前オッズを反映して判定できるように)。do_stampsはdo_odds直後・
    # do_resultsの前に置き、その時点の最新オッズでチェックポイント確定させる。
    n_odds = do_odds(pred, now, ymd)
    n_stp = do_stamps(pred, now)
    n_res = do_results(pred, now, ymd)
    n_morn = do_morning_odds(pred, now, ymd)
    n_name = do_racenames(pred, ymd)
    if n_res == 0 and n_odds == 0 and n_morn == 0 and n_name == 0 and n_stp == 0:
        print("nothing to update")
        try:
            from notify import notify_events
            notify_events(pred, ymd)
        except Exception as e:
            print(f"notify skip: {e}")
        return
    if n_res:
        pred["results_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    if n_odds or n_morn:
        pred["odds_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    write(pred, ymd)
    try:
        from notify import notify_events
        notify_events(pred, ymd)
    except Exception as e:
        print(f"notify skip: {e}")
    print(f"updated: results={n_res} odds={n_odds} morning={n_morn} names={n_name} stamps={n_stp}")


if __name__ == "__main__":
    main()

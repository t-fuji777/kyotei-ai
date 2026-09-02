# -*- coding: utf-8 -*-
"""当日予測: 本日の番組表(B)を取得し、3段モデルで全レースの3連単買い目
厳選(複勝90%+目標)判定を生成。出力: docs/predictions/YYYYMMDD.json と latest.json"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from common import download_day, parse_b, VENUES, is_sengen, SENGEN_TOP3P_MIN, SENGEN_MIN_ODDS, SENGEN_EXCLUDE_VENUES
from features import add_features, load_fan, FEATURES
from train import trifecta_probs
from fetch_result import fetch_before_html, parse_before

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))


def load_hist() -> pd.DataFrame:
    files = sorted(glob.glob(str(ROOT / "data" / "races" / "entries_*.csv.gz")))
    df = pd.concat([pd.read_csv(f, dtype={"date": str}) for f in files], ignore_index=True)
    return df[~df["abnormal"].isin(["K0", "K1"])]


def load_models():
    meta = json.loads((ROOT / "data" / "model" / "meta.json").read_text())
    models = {t: lgb.Booster(model_file=str(ROOT / "data" / "model" / f"model_{t}.txt"))
              for t in ("win", "top2", "top3")}
    # sengen(厳選)判定は common.is_sengen()に統一(top3p閾値+除外会場)。
    # ここで組み立てるsengen dictはload_models呼び出し元との互換のため残すが、
    # is_senの判定自体には使わない(meta由来のvenues許可リストは廃止)。
    sengen = {"top5_min": 0.40, "venues": list(range(1, 25)), **meta.get("sengen", {})}
    return meta, models, sengen


def confidence(top1p: float) -> str:
    if top1p >= 0.15:
        return "A"
    if top1p >= 0.08:
        return "B"
    return "C"


def races_to_rows(races, live=None):
    """parse_bのrace群 -> エントリ行リスト。live={(venue,rno):{ex,wind,wave,...}}で直前情報を注入"""
    rows = []
    for r in races:
        key = (r["venue"], r["race_no"])
        inf = (live or {}).get(key)
        for rc in r["racers"]:
            row = {"date": r["date"], "venue": r["venue"], "race_no": r["race_no"],
                   "race_type": r["race_type"], "deadline": r["deadline"],
                   "day_n": r.get("day_n"),
                   "lane": rc["lane"], "toban": rc["toban"], "name": rc["name"],
                   "age": rc["age"], "weight": rc["weight"], "class": rc["class"],
                   "nat_win": rc["nat_win"], "nat_in2": rc["nat_in2"],
                   "loc_win": rc["loc_win"], "loc_in2": rc["loc_in2"],
                   "motor_in2": rc["motor_in2"], "boat_in2": rc["boat_in2"]}
            if inf:
                row["ex_time"] = inf["ex"].get(rc["lane"])
                row["wind"] = inf.get("wind")
                row["wave"] = inf.get("wave")
            rows.append(row)
    return rows


def predict_races(tgt: pd.DataFrame, hist, fan, models, sengen):
    """エントリ行DF -> {venue_code: [race_obj,...]}, 厳選数"""
    tgt = add_features(hist, tgt, fan=fan)
    tgt["p_raw"] = models["win"].predict(tgt[FEATURES])
    tgt["p_norm"] = tgt.groupby(["venue", "race_no"])["p_raw"].transform(lambda s: s / s.sum())
    tgt["p_top2"] = models["top2"].predict(tgt[FEATURES])
    tgt["p_top3"] = models["top3"].predict(tgt[FEATURES])

    by_venue = {}
    n_sengen = 0
    for (venue, rno), g in tgt.groupby(["venue", "race_no"]):
        g = g.sort_values("lane")
        if len(g) != 6:
            print(f"SKIP {int(venue)}-{int(rno)}R: parsed {len(g)} boats (need 6)", flush=True)
            continue
        pw = g["p_norm"].to_numpy()
        pr = trifecta_probs(pw, g["p_top2"].to_numpy(), g["p_top3"].to_numpy())
        ranked = sorted(pr.items(), key=lambda x: -x[1])[:10]
        picks = [{"c": f"{a+1}-{b+1}-{c+1}", "p": round(float(v), 4)}
                 for (a, b, c), v in ranked]
        fav = int(np.argmax(pw))
        fav_p2 = float(g["p_top2"].to_numpy()[fav])
        # is_sen(厳選)は common.is_sengen()に統一:
        # 3連単上位3点(picks先頭3件, 買い目そのもの)の合算確率 >= 閾値 かつ除外会場でない
        # かつ5R以降(1-4Rはモデルの高確率帯が信用できないため対象外)
        top3p = sum(p["p"] for p in picks[:3])
        is_sen = is_sengen(top3p, venue, rno)
        n_sengen += int(is_sen)
        boats = []
        for _, x in g.iterrows():
            boats.append({"lane": int(x["lane"]), "name": x["name"], "cls": x["class"],
                          "wp": round(float(x["p_norm"]), 3)})
        day_n_val = g["day_n"].iloc[0] if "day_n" in g.columns else None
        race_obj = {"no": int(rno), "type": g["race_type"].iloc[0],
                    "deadline": g["deadline"].iloc[0],
                    "day_n": (int(day_n_val) if day_n_val is not None and not pd.isna(day_n_val) else None),
                    "boats": boats, "picks": picks,
                    "conf": confidence(picks[0]["p"]),
                    "fuku": {"lane": int(g["lane"].to_numpy()[fav]),
                             "p": round(fav_p2, 3)},
                    "sengen": is_sen}
        by_venue.setdefault(int(venue), []).append(race_obj)
    return by_venue, n_sengen


def _mins_to_deadline(now, dl):
    if not dl or ":" not in dl:
        return None
    hh, mm = map(int, dl.split(":"))
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (t - now).total_seconds() / 60


def predict_live(ymd, meta, models, sengen):
    """直前情報(展示タイム·風·波)を取得できたレースのみ再予測し、latest.jsonをin-place更新。
    既存の結果(result)·オッズ(odds)は保持。再学習不要(モデルは展示特徴を保有)。"""
    import time
    latest = ROOT / "docs" / "predictions" / "latest.json"
    if not latest.exists():
        print("live: latest.json なし (朝の予測が未実行)"); return
    old = json.loads(latest.read_text())
    if old.get("date") != ymd:
        print(f"live: latest日付 {old.get('date')} != {ymd}; skip"); return
    btxt = download_day("B", ymd)
    if not btxt:
        print("live: B(番組表)取得不可; skip"); return
    races = parse_b(btxt, ymd)
    card = {(r["venue"], r["race_no"]): r for r in races}
    now = datetime.now(JST)
    LEAD, GRACE, CAP = 20, 4, 80
    nbf = 0
    for v in old["venues"]:
        for r in v["races"]:
            if nbf >= 25:
                break
            if r.get("st_ex") and r.get("weather"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None or mins > LEAD:
                continue
            try:
                bi = parse_before(fetch_before_html(ymd, v["code"], r["no"]))
            except Exception as e:
                print(f"live st_ex {v['code']}-{r['no']}R fail: {e}")
                time.sleep(0.3)
                continue
            stx = bi.get("st", {})
            if stx:
                r["st_ex"] = {str(k): val for k, val in stx.items()}
                if not r.get("ex") and len(bi.get("ex", {})) == 6:
                    r["ex"] = bi["ex"]
                if bi.get("weather"):
                    r["weather"] = bi["weather"]
                nbf += 1
            time.sleep(0.3)
        if nbf >= 25:
            break
    if nbf:
        old["live_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
        write(old, ymd)
        print(f"st_ex backfill: {nbf} races")
    live = {}
    targets = []
    n_fetch = 0
    for v in old["venues"]:
        vc = v["code"]
        for r in v["races"]:
            if r.get("result"):
                continue
            if r.get("live") and r.get("st_ex"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None or mins > LEAD or mins < -GRACE:
                continue
            key = (vc, r["no"])
            if key not in card:
                continue
            if n_fetch >= CAP:
                break
            n_fetch += 1
            try:
                bi = parse_before(fetch_before_html(ymd, vc, r["no"]))
            except Exception as e:
                print(f"live before {vc}-{r['no']}R fail: {e}")
                continue
            if len(bi.get("ex", {})) != 6:
                continue
            live[key] = bi
            targets.append(card[key])
            time.sleep(0.3)
    if not targets:
        print(f"live: fetched={n_fetch} 展示の出たレースなし")
        return
    tgt = pd.DataFrame(races_to_rows(targets, live=live))
    hist = load_hist()
    fan = load_fan(ROOT / "data" / "fan")
    by_venue, _ = predict_races(tgt, hist, fan, models, sengen)
    idx = {(v["code"], r["no"]): r for v in old["venues"] for r in v["races"]}
    n_upd = 0
    for vc2, vraces in by_venue.items():
        for nr in vraces:
            r = idx.get((vc2, nr["no"]))
            if r is None:
                continue
            r["picks"] = nr["picks"]
            r["boats"] = nr["boats"]
            r["conf"] = nr["conf"]
            r["fuku"] = nr["fuku"]
            r["sengen"] = nr["sengen"]
            r["live"] = True
            bi = live.get((vc2, nr["no"]))
            if bi:
                r["ex"] = bi["ex"]
                r["st_ex"] = {str(k): val for k, val in bi.get("st", {}).items()}
                r["wind"] = bi["wind"]
                r["wave"] = bi["wave"]
                r["weather"] = bi.get("weather")
                r["live_at"] = now.strftime("%H:%M")
            n_upd += 1
    if n_upd:
        old["live_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
        write(old, ymd)
    print(f"live update: fetched={n_fetch} ready={len(targets)} updated={n_upd}")


# race-level fields holding observed data (results/odds/exhibition), attached by
# the update loop -- these must survive a same-day regeneration (daily's retrain).
# tk/mt/pt/rs/att はチェックポイント確定スタンプと見送り理由(first-wins・不変)のため、
# 同日再生成で必ず引き継ぐ(消えると所属確定が失われる)。att(注目/様子見バッジの打刻)は
# 2026-09-02導入。導入直後、この一覧への追加漏れでライブ再予測のたびにattが消えていた。
# os は判定時点の板(締切後に上書きされる odds.t3 と違い不変)。今はデータを貯め始めた
# 段階で、消費側は docs/index.html の表示のみ。集計側(recompute_sengen/update_results/
# build_calib)は依然 odds.t3 を読むため、そちらの移行は os が溜まってから行う。
_OBSERVED_FIELDS = ("result", "odds", "st_ex", "ex", "weather", "wind", "wave",
                    "tk", "mt", "pt", "rs", "os", "att")
# model-output fields; never re-issue them for a race already gone live
# (exhibition-based) or finished (its result was scored against those picks).
_PICK_FIELDS = ("picks", "boats", "conf", "fuku", "sengen", "live", "live_at")


def _merge_existing(out, ymd):
    """Carry accumulated per-race data forward from an existing same-day file so
    regenerating today's prediction (daily's 2nd/3rd run or the retrained model)
    never discards results/odds/exhibition the update loop attached, nor
    retroactively rewrites picks a finished/live race was already scored on."""
    fp = ROOT / "docs" / "predictions" / f"{ymd}.json"
    if not fp.exists():
        return
    try:
        old = json.loads(fp.read_text())
    except Exception:
        return
    idx = {(v["code"], r["no"]): r
           for v in old.get("venues", []) for r in v.get("races", [])}
    for v in out["venues"]:
        for r in v["races"]:
            o = idx.get((v["code"], r["no"]))
            if not o:
                continue
            for k in _OBSERVED_FIELDS:
                if k in o:
                    r[k] = o[k]
            if o.get("rn_full"):
                r["type"] = o.get("type", r["type"])
                r["rn_full"] = True
            # 竹/松の所属が確定("tk"あり)したレースも買い目を凍結する。確定は
            # その時点のpicksで判定しているため、後から買い目だけ差し替わると
            # 画面の買い目と確定根拠がずれる(実測で確定済みの3割で発生していた)。
            if o.get("live") or o.get("result") or "tk" in o:
                for k in _PICK_FIELDS:
                    if k in o:
                        r[k] = o[k]
    for k in ("results_updated_at", "odds_updated_at", "live_updated_at"):
        if k in old:
            out[k] = old[k]


def _existing_has_venues(ymd) -> bool:
    """既存の{ymd}.jsonがvenuesを持つか確認(朝から蓄積した予測·結果·オッズの
    全損防止用)。読めない/存在しない場合はFalse。"""
    fp = ROOT / "docs" / "predictions" / f"{ymd}.json"
    if not fp.exists():
        return False
    try:
        existing = json.loads(fp.read_text())
    except Exception:
        return False
    return bool(existing.get("venues"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(JST).strftime("%Y%m%d"))
    ap.add_argument("--live", action="store_true",
                    help="直前情報(展示)で展示の出たレースのみ再予測")
    a = ap.parse_args()
    ymd = a.date

    meta, models, sengen = load_models()

    if a.live:
        predict_live(ymd, meta, models, sengen)
        return

    btxt = download_day("B", ymd)
    out = {"date": ymd,
           "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
           "model_trained_at": meta["trained_at"],
           "sengen_cfg": {"top3p_min": SENGEN_TOP3P_MIN, "min_odds": SENGEN_MIN_ODDS,
                          "exclude_venues": sorted(SENGEN_EXCLUDE_VENUES)},
           "venues": []}
    if btxt is None:
        if _existing_has_venues(ymd):
            print(f"live: B(番組表)取得不可だが既存の{ymd}.jsonにvenuesあり; 上書きせずskip")
            return
        out["note"] = "本日の番組表が取得できませんでした(開催なし or 未公開)"
        write(out, ymd)
        return

    races = parse_b(btxt, ymd)
    if not races:
        if _existing_has_venues(ymd):
            print(f"live: 番組表の解析結果が空だが既存の{ymd}.jsonにvenuesあり; 上書きせずskip")
            return
        out["note"] = "番組表の解析結果が空でした"
        write(out, ymd)
        return

    tgt = pd.DataFrame(races_to_rows(races))
    hist = load_hist()
    fan = load_fan(ROOT / "data" / "fan")
    print(f"hist rows={len(hist)}, fan={len(fan)}, target races={len(races)}", flush=True)
    by_venue, n_sengen = predict_races(tgt, hist, fan, models, sengen)

    yusho = "\u512a\u52dd"  # 優勝 (championship final)
    for vcode in sorted(by_venue):
        vraces = sorted(by_venue[vcode], key=lambda r: r["no"])
        dns = [r.get("day_n") for r in vraces if r.get("day_n") is not None]
        day_n = dns[0] if dns else None
        is_final = any(yusho in str(r.get("type", "")) for r in vraces)
        out["venues"].append({"code": vcode, "name": VENUES[vcode],
                              "day_n": day_n, "is_final": is_final,
                              "races": vraces})
    _merge_existing(out, ymd)
    write(out, ymd)
    print(f"predicted: venues={len(out['venues'])} "
          f"races={sum(len(v['races']) for v in out['venues'])} sengen={n_sengen}")


def _atomic_write_text(path: Path, txt: str) -> None:
    """一時ファイルに書いてos.replaceで差し替える原子化write。
    途中クラッシュで破損ファイルが残ることを防ぐ(エンコーディング挙動は
    write_text無指定のまま変更しない)。"""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(txt)
    os.replace(tmp, path)


def write(obj, ymd, is_today=None):
    d = ROOT / "docs" / "predictions"
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False)
    _atomic_write_text(d / f"{ymd}.json", txt)
    # latest.json must only ever hold today's file; writing it for a past-date
    # (re)generation would roll the live site back to that day.
    if is_today is None:
        is_today = (ymd == datetime.now(JST).strftime("%Y%m%d"))
    if is_today:
        _atomic_write_text(d / "latest.json", txt)


if __name__ == "__main__":
    main()

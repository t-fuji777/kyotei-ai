# -*- coding: utf-8 -*-
"""当日予測: 本日の番組表(B)を取得し、3段モデルで全レースの3連単買い目
厳選(複勝90%+目標)判定を生成。出力: docs/predictions/YYYYMMDD.json と latest.json"""
import argparse
import glob
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from common import download_day, parse_b, VENUES
from features import add_features, load_fan, FEATURES
from train import trifecta_probs

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
    sengen = meta.get("sengen", {"p2_min": 0.89, "venues": list(range(1, 25))})
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
            continue
        pw = g["p_norm"].to_numpy()
        pr = trifecta_probs(pw, g["p_top2"].to_numpy(), g["p_top3"].to_numpy())
        ranked = sorted(pr.items(), key=lambda x: -x[1])[:10]
        picks = [{"c": f"{a+1}-{b+1}-{c+1}", "p": round(float(v), 4)}
                 for (a, b, c), v in ranked]
        fav = int(np.argmax(pw))
        fav_p2 = float(g["p_top2"].to_numpy()[fav])
        is_sen = bool(fav_p2 >= sengen["p2_min"] and int(venue) in sengen["venues"])
        n_sengen += int(is_sen)
        boats = []
        for _, x in g.iterrows():
            boats.append({"lane": int(x["lane"]), "name": x["name"], "cls": x["class"],
                          "wp": round(float(x["p_norm"]), 3)})
        race_obj = {"no": int(rno), "type": g["race_type"].iloc[0],
                    "deadline": g["deadline"].iloc[0],
                    "boats": boats, "picks": picks,
                    "conf": confidence(picks[0]["p"]),
                    "fuku": {"lane": int(g["lane"].to_numpy()[fav]),
                             "p": round(fav_p2, 3)},
                    "sengen": is_sen}
        by_venue.setdefault(int(venue), []).append(race_obj)
    return by_venue, n_sengen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(JST).strftime("%Y%m%d"))
    a = ap.parse_args()
    ymd = a.date

    meta, models, sengen = load_models()

    btxt = download_day("B", ymd)
    out = {"date": ymd,
           "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
           "model_trained_at": meta["trained_at"],
           "sengen_cfg": {"p2_min": sengen["p2_min"], "venues": sengen["venues"]},
           "venues": []}
    if btxt is None:
        out["note"] = "本日の番組表が取得できませんでした(開催なし or 未公開)"
        write(out, ymd)
        return

    races = parse_b(btxt, ymd)
    if not races:
        out["note"] = "番組表の解析結果が空でした"
        write(out, ymd)
        return

    tgt = pd.DataFrame(races_to_rows(races))
    hist = load_hist()
    fan = load_fan(ROOT / "data" / "fan")
    print(f"hist rows={len(hist)}, fan={len(fan)}, target races={len(races)}", flush=True)
    by_venue, n_sengen = predict_races(tgt, hist, fan, models, sengen)

    for vcode in sorted(by_venue):
        out["venues"].append({"code": vcode, "name": VENUES[vcode],
                              "races": sorted(by_venue[vcode], key=lambda r: r["no"])})
    write(out, ymd)
    print(f"predicted: venues={len(out['venues'])} "
          f"races={sum(len(v['races']) for v in out['venues'])} sengen={n_sengen}")


def write(obj, ymd):
    d = ROOT / "docs" / "predictions"
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False)
    (d / f"{ymd}.json").write_text(txt)
    (d / "latest.json").write_text(txt)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""学習: LightGBM 3段二値モデル(1着/2着内/3着内)で各艇の強さを推定し、
3段Plackett-Luce型で3連単120通りを確率化。validで厳選(複勝90%+)構成を決定し、
testでバックテストして docs/model_report.json に出力。"""
import glob
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from features import add_features, load_fan, FEATURES

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))
TARGETS = ("win", "top2", "top3")
PARAMS = dict(objective="binary", metric="binary_logloss", learning_rate=0.05,
              num_leaves=63, min_data_in_leaf=60, feature_fraction=0.9,
              bagging_fraction=0.8, bagging_freq=1, verbosity=-1, seed=42)


def load_entries() -> pd.DataFrame:
    files = sorted(glob.glob(str(ROOT / "data" / "races" / "entries_*.csv.gz")))
    if not files:
        raise SystemExit("no entries files. run build_dataset first")
    df = pd.concat([pd.read_csv(f, dtype={"date": str}) for f in files], ignore_index=True)
    df = df[~df["abnormal"].isin(["K0", "K1"])]
    df = df.drop_duplicates(subset=["date", "venue", "race_no", "lane"])
    print(f"loaded {len(df)} rows, {df['date'].nunique()} days, "
          f"{df['date'].min()} - {df['date'].max()}")
    return df


def race_groups(df):
    return df.groupby(["date", "venue", "race_no"], sort=False)


def trifecta_probs(pw, p2, p3):
    """3段強さ(1着確率正規化済pw, top2強さp2, top3強さp3) -> {(a,b,c): prob}"""
    s1 = np.clip(np.asarray(pw, float), 1e-9, None)
    s2 = np.clip(np.asarray(p2, float), 1e-9, None)
    s3 = np.clip(np.asarray(p3, float), 1e-9, None)
    out = {}
    t1 = s1.sum()
    for a in range(6):
        pa = s1[a] / t1
        d2 = s2.sum() - s2[a]
        for b in range(6):
            if b == a:
                continue
            pb = s2[b] / d2
            d3 = s3.sum() - s3[a] - s3[b]
            for c in range(6):
                if c in (a, b):
                    continue
                out[(a, b, c)] = pa * pb * (s3[c] / d3)
    return out


def race_eval_rows(part: pd.DataFrame):
    """レース単位の評価行: 本命・複勝確率・3連単上位・実着順・払戻"""
    rows = []
    for (date, venue, rno), g in race_groups(part):
        g = g.sort_values("lane")
        if len(g) != 6:
            continue
        pw = g["p_norm"].to_numpy()
        pr = trifecta_probs(pw, g["p_top2"].to_numpy(), g["p_top3"].to_numpy())
        ranked = sorted(pr.items(), key=lambda x: -x[1])
        fav = int(np.argmax(pw))
        pos = pd.to_numeric(g["pos"], errors="coerce").to_numpy()
        if np.isnan(pos).all():
            continue
        try:
            a = int(np.where(pos == 1)[0][0])
            b = int(np.where(pos == 2)[0][0])
            c = int(np.where(pos == 3)[0][0])
            actual = (a, b, c)
        except IndexError:
            actual = None
        pay = g["pay3t_amount"].dropna()
        rows.append(dict(
            date=date, venue=int(venue), rno=int(rno),
            fav=fav, fav_p2=float(g["p_top2"].to_numpy()[fav]),
            fav_pos=float(pos[fav]) if not np.isnan(pos[fav]) else 99.0,
            t1p=float(ranked[0][1]),
            hit_win=int(actual is not None and fav == actual[0]),
            hit_fuku=int(pos[fav] <= 2) if not np.isnan(pos[fav]) else 0,
            hit_t1=int(actual == ranked[0][0]) if actual else 0,
            hit_t6=int(actual in [k for k, _ in ranked[:6]]) if actual else 0,
            hit_t10=int(actual in [k for k, _ in ranked[:10]]) if actual else 0,
            hit_t18=int(actual in [k for k, _ in ranked[:18]]) if actual else 0,
            pay3t=float(pay.iloc[0]) if len(pay) else 0.0,
            in_t6=actual in [k for k, _ in ranked[:6]] if actual else False,
        ))
    return pd.DataFrame(rows)


def pick_sengen(valid_r: pd.DataFrame):
    """validで複勝的中率>=90%となる(閾値, 会場リスト)を選ぶ。カバレッジ最大化。"""
    best = None
    for thr in (0.83, 0.84, 0.85, 0.86, 0.87, 0.88, 0.89, 0.90):
        s = valid_r[valid_r["fav_p2"] >= thr]
        if len(s) < 50:
            continue
        bv = s.groupby("venue")["hit_fuku"].agg(["mean", "size"])
        venues = sorted(bv[(bv["mean"] >= 0.90) & (bv["size"] >= 10)].index.tolist())
        if not venues:
            continue
        ss = s[s["venue"].isin(venues)]
        rate = ss["hit_fuku"].mean()
        if rate >= 0.91 and (best is None or len(ss) > best["n"]):
            best = {"p2_min": thr, "venues": venues,
                    "n": int(len(ss)), "valid_rate": round(float(rate), 4)}
    if best is None:
        best = {"p2_min": 0.88, "venues": list(range(1, 25)), "n": 0, "valid_rate": None}
    return best


def report(r: pd.DataFrame, sengen):
    n = len(r)
    rep = {"races": int(n),
           "win_hit_rate": round(r["hit_win"].mean(), 4),
           "fuku_hit_rate": round(r["hit_fuku"].mean(), 4)}
    for k in (1, 6, 10, 18):
        rep[f"top{k}_hit_rate"] = round(r[f"hit_t{k}"].mean(), 4)
    rep["top6_roi"] = round(float(r.loc[r["in_t6"], "pay3t"].sum()) / max(n * 600, 1), 4)
    s = r[(r["fav_p2"] >= sengen["p2_min"]) & (r["venue"].isin(sengen["venues"]))]
    rep["sengen"] = {"races": int(len(s)),
                     "races_per_day": round(len(s) / max(r["date"].nunique(), 1), 1),
                     "fuku_hit_rate": round(float(s["hit_fuku"].mean()), 4) if len(s) else None}
    return rep


def main():
    df = load_entries()
    p = pd.to_numeric(df["pos"], errors="coerce")
    df["is_win"] = (p == 1).astype(int)
    df["is_top2"] = (p <= 2).astype(int)
    df["is_top3"] = (p <= 3).astype(int)
    fan = load_fan(ROOT / "data" / "fan")
    print(f"fan records: {len(fan)}", flush=True)
    print("building features (this may take a few minutes)...", flush=True)
    df = add_features(df, df, fan=fan)

    dates = np.sort(df["date"].unique())
    d_tr = dates[int(len(dates) * 0.80)]
    d_va = dates[int(len(dates) * 0.90)]
    tr = df[df["date"] < d_tr]
    va = df[(df["date"] >= d_tr) & (df["date"] < d_va)].copy()
    te = df[df["date"] >= d_va].copy()
    print(f"split: train<{d_tr} ({len(tr)}), valid<{d_va} ({len(va)}), test ({len(te)})")

    cat = ["venue", "lane", "class_i", "v_water"]
    models, iters = {}, {}
    for tgt in TARGETS:
        dtr = lgb.Dataset(tr[FEATURES], label=tr[f"is_{tgt}"], categorical_feature=cat)
        dva = lgb.Dataset(va[FEATURES], label=va[f"is_{tgt}"], categorical_feature=cat,
                          reference=dtr)
        m = lgb.train(PARAMS, dtr, num_boost_round=3000, valid_sets=[dva],
                      callbacks=[lgb.early_stopping(100), lgb.log_evaluation(0)])
        models[tgt] = m
        iters[tgt] = m.best_iteration
        print(f"{tgt}: best_iter={m.best_iteration} "
              f"ll={m.best_score['valid_0']['binary_logloss']:.5f}", flush=True)

    for part in (va, te):
        part["p_raw"] = models["win"].predict(part[FEATURES])
        part["p_norm"] = part.groupby(["date", "venue", "race_no"])["p_raw"].transform(
            lambda s: s / s.sum())
        part["p_top2"] = models["top2"].predict(part[FEATURES])
        part["p_top3"] = models["top3"].predict(part[FEATURES])

    va_r = race_eval_rows(va)
    te_r = race_eval_rows(te)
    sengen = pick_sengen(va_r)
    print("sengen config:", json.dumps(sengen, ensure_ascii=False))
    rep_test = report(te_r, sengen)
    print("TEST:", json.dumps(rep_test, ensure_ascii=False))

    # ベースライン: 会場xコース基礎率のみ
    te2 = te.copy()
    te2["p_norm"] = te2.groupby(["date", "venue", "race_no"])["v_lane_win365"].transform(
        lambda s: s.fillna(0.01).clip(0.001) / s.fillna(0.01).clip(0.001).sum())
    te2["p_top2"] = te2["v_lane_top2_365"].fillna(0.05).clip(0.001)
    te2["p_top3"] = te2["v_lane_top3_365"].fillna(0.1).clip(0.001)
    rep_base = report(race_eval_rows(te2), sengen)
    print("BASELINE:", json.dumps(rep_base, ensure_ascii=False))

    out = ROOT / "data" / "model"
    out.mkdir(parents=True, exist_ok=True)
    for tgt in TARGETS:
        models[tgt].save_model(str(out / f"model_{tgt}.txt"))
    imp = dict(zip(FEATURES, models["win"].feature_importance("gain").round(1).tolist()))
    meta = {
        "trained_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "rows_train": len(tr), "rows_valid": len(va), "rows_test": len(te),
        "period": [str(df["date"].min()), str(df["date"].max())],
        "best_iterations": iters,
        "sengen": {"p2_min": sengen["p2_min"], "venues": sengen["venues"],
                   "valid_rate": sengen["valid_rate"]},
        "test_metrics": rep_test, "baseline_metrics": rep_base,
        "feature_importance": dict(sorted(imp.items(), key=lambda x: -x[1])),
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    (ROOT / "docs" / "model_report.json").write_text(json.dumps(meta, ensure_ascii=False))
    print("models saved")


if __name__ == "__main__":
    main()

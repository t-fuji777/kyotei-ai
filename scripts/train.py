# -*- coding: utf-8 -*-
"""学習。MODEL_GEN で世代を選ぶ(予測側は読んだモデルの meta.json の世代で自動的に分かれる)。

世代1(MODEL_GEN = 1。現行・本番): LightGBM 3段二値モデル(1着/2着内/3着内)で各艇の強さを推定し、
3段Plackett-Luce型で3連単120通りを確率化。validで厳選(複勝90%+)構成を決定し、
testでバックテストして docs/model_report.json に出力。出力は data/model_build/model_{win,top2,top3}.txt + meta.json(形1)。

世代2(MODEL_GEN = 2。候補): 現行45 + 履歴108 = 153特徴量、条件づけ分解ロジットの LightGBM 1本(scripts/model_v2.py)。
出力は data/model_build/model.txt + meta.json(形2)と、較正の種 data/calib_seed_gen2.json。学習の最後に
自己検査(entries の最終日を当日とみなして予測の経路で作った値が、学習時の表と採点用の予測に一致すること)を
行い、外れたら失敗にして配布しない(meta.json を書かない)。

どちらの世代でも meta.json は最初に消して最後に書く(「一式が完成した」目印)。"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from features import add_features, load_fan, FEATURES, FEATURES_V2, LIVE_COLS_V2, add_features_v2
from features_hist import FEATURES_HIST_VERSION, ELO_CFG
from entries_io import load_entries as _load_entries_io
import model_v2

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))
# 学習する世代。1 = 現行(本番)、2 = 候補。切り替え(段階B)でここを 2 にする。
# train.yml の計測用の実行(dry_run)は環境変数 MODEL_GEN で上書きする(空なら定数のまま。resolve_model_gen)。
MODEL_GEN = 1
TARGETS = ("win", "top2", "top3")


def resolve_model_gen(env=None):
    """main() が学習する世代。環境変数 MODEL_GEN が "1" か "2" ならその値、空・無ければ定数 MODEL_GEN。
    それ以外("3"、"abc" など)は明確なメッセージで SystemExit(黙って世代1を学習しない)。
    import 時ではなく main() の中で読む: predict_today も train を import するので、import 時に落とすと
    不正な環境変数ひとつで予測まで巻き添えになる。env は試験用(省略時は os.environ)。"""
    v = str((os.environ if env is None else env).get("MODEL_GEN", "") or "").strip()
    if not v:
        return MODEL_GEN
    if v not in ("1", "2"):
        raise SystemExit(f"環境変数 MODEL_GEN は 1 か 2 か空にする(入力: {v!r})")
    return int(v)
PARAMS = dict(objective="binary", metric="binary_logloss", learning_rate=0.05,
              num_leaves=63, min_data_in_leaf=60, feature_fraction=0.9,
              bagging_fraction=0.8, bagging_freq=1, verbosity=-1, seed=42)
# 報告の「高確率帯」(上位5点の合計確率)のしきい値。世代2は確率の目盛りが上に伸びるので、件数を
# 今とそろえる値(0.47)にする(prob_consumers.md 6章)。判定には使わない(model_report.json の表示だけ)
SENGEN_TOP5_MIN_BY_GEN = {1: 0.40, 2: 0.47}
# 世代2の出力先に残っていてはいけない形1のファイル(形を替える時に消す。model_path.md 7章)
_FORMAT1_FILES = tuple(f"model_{t}.txt" for t in TARGETS)
_FORMAT2_FILES = ("model.txt",)
# (a,b,c)(0始まりの枠)→ model_v2.PERMS の位置。-1 は無い組み合わせ
_PERM_INDEX = -np.ones((6, 6, 6), dtype=np.int64)
_PERM_INDEX[model_v2.PERMS[:, 0], model_v2.PERMS[:, 1], model_v2.PERMS[:, 2]] = np.arange(len(model_v2.PERMS))


def load_entries(before_ymd=None) -> pd.DataFrame:
    """entries 全部(entries_io.load_entries。K0/K1 除外・重複除去・日付→会場→レース→枠の並び。
    今のファイルでは除外も並べ替えも何も動かさないので、世代1の学習は今と同じ行を同じ順で受け取る)。"""
    df = _load_entries_io(before_ymd)
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
            top5p=float(sum(p for _, p in ranked[:5])),
            hit_win=int(actual is not None and fav == actual[0]),
            hit_fuku=int(pos[fav] <= 2) if not np.isnan(pos[fav]) else 0,
            hit_t1=int(actual == ranked[0][0]) if actual else 0,
            hit_t5=int(actual in [k for k, _ in ranked[:5]]) if actual else 0,
            hit_t6=int(actual in [k for k, _ in ranked[:6]]) if actual else 0,
            hit_t10=int(actual in [k for k, _ in ranked[:10]]) if actual else 0,
            hit_t18=int(actual in [k for k, _ in ranked[:18]]) if actual else 0,
            pay3t=float(pay.iloc[0]) if len(pay) else 0.0,
            in_t6=actual in [k for k, _ in ranked[:6]] if actual else False,
        ))
    return pd.DataFrame(rows)


def pick_sengen(valid_r: pd.DataFrame):
    """高確率帯(3連単 上位5点の合算確率 top5p>=0.40)のTOP5的中率をvalidで測る、
    モデル単体の品質指標。

    注意: 商品の「厳選」(竹/松プラン)とは別物。竹はTOP3・3点300円(top3p>=0.36 +
    オッズ3.1倍以上 + 5R以降 + 除外会場)、松はTOP4・4点400円で、判定基準も購入点数も
    ここと一致しない。プランの実績は accuracy.json(的中実績タブ)が唯一の出所。
    キー名 sengen は既存 model_report.json との互換のため変更していない。"""
    thr = 0.40
    s = valid_r[valid_r["top5p"] >= thr]
    rate = float(s["hit_t5"].mean()) if len(s) else None
    return {"top5_min": thr, "n": int(len(s)),
            "valid_rate": round(rate, 4) if rate is not None else None}


def report(r: pd.DataFrame, sengen):
    n = len(r)
    rep = {"races": int(n),
           "win_hit_rate": round(r["hit_win"].mean(), 4),
           "fuku_hit_rate": round(r["hit_fuku"].mean(), 4)}
    for k in (1, 6, 10, 18):
        rep[f"top{k}_hit_rate"] = round(r[f"hit_t{k}"].mean(), 4)
    rep["top6_roi"] = round(float(r.loc[r["in_t6"], "pay3t"].sum()) / max(n * 600, 1), 4)
    rep["top5_hit_rate"] = round(r["hit_t5"].mean(), 4)
    rep["top5_roi"] = round(float(r.loc[r["hit_t5"] == 1, "pay3t"].sum()) / max(n * 500, 1), 4)
    s = r[r["top5p"] >= sengen["top5_min"]]
    rep["sengen"] = {"races": int(len(s)),
                     "races_per_day": round(len(s) / max(r["date"].nunique(), 1), 1),
                     "top5_min": sengen["top5_min"],
                     "top5_hit_rate": round(float(s["hit_t5"].mean()), 4) if len(s) else None}
    return rep


def main():
    if resolve_model_gen() == 2:
        main_v2()
        return
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

    # Pre-live robustness: mask ex features to NaN on 50% of training races
    # so the model stays calibrated for morning predictions without beforeinfo.
    EX_COLS = ["ex_time", "ex_rank", "ex_diff", "wind", "wave"]
    _rng = np.random.default_rng(42)
    _rid = tr.groupby(["date", "venue", "race_no"]).ngroup()
    _sel = _rng.random(int(_rid.max()) + 1)[_rid.values] < 0.5
    tr = tr.copy()
    tr.loc[_sel, EX_COLS] = np.nan
    print(f"ex-mask: {int(_sel.sum())}/{len(tr)} rows masked", flush=True)

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

    # 出力先は data/model_build(gitignore)。ここから scripts/model_store.py publish が Release へ
    # 配布する。data/model は凍結した予備で、学習では書き換えない。
    out = ROOT / "data" / "model_build"
    out.mkdir(parents=True, exist_ok=True)
    # meta.json は最後に書くので、これが「一式が完成した」目印になる。先に消しておけば、
    # 途中で落ちた時に古い meta と新しいモデルが混ざった状態を完成品と見誤らない。
    (out / "meta.json").unlink(missing_ok=True)
    # 形2(世代2)の出力が残っていれば消す(形を替えた直後に両方の形のファイルが混ざらないように)
    for n in _FORMAT2_FILES:
        (out / n).unlink(missing_ok=True)
    for tgt in TARGETS:
        models[tgt].save_model(str(out / f"model_{tgt}.txt"))
    imp = dict(zip(FEATURES, models["win"].feature_importance("gain").round(1).tolist()))
    meta = {
        "format": 1, "gen": 1,
        "trained_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "rows_train": len(tr), "rows_valid": len(va), "rows_test": len(te),
        "period": [str(df["date"].min()), str(df["date"].max())],
        "best_iterations": iters,
        "sengen": {"top5_min": sengen["top5_min"],
                   "valid_rate": sengen["valid_rate"]},
        "test_metrics": rep_test, "baseline_metrics": rep_base,
        "feature_importance": dict(sorted(imp.items(), key=lambda x: -x[1])),
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    (ROOT / "docs" / "model_report.json").write_text(json.dumps(meta, ensure_ascii=False))
    print("models saved")


# ============================================================ 世代2
def sort_entries(df: pd.DataFrame) -> pd.DataFrame:
    """日付→会場→レース番号→枠に並べて index を振り直す(entries_io.load_entries と同じ並び)。"""
    return df.sort_values(["date", "venue", "race_no", "lane"], kind="stable").reset_index(drop=True)


def six_row_mask(df: pd.DataFrame, log=print) -> np.ndarray:
    """sort_entries 済みの表で、全6艇(枠1〜6・登番あり)がそろったレースの行に True を立てる(行ごとの bool)。
    世代2の行列は (レース数, 6, 列数) に整形するので、行列に入れる行をここで決める。
    特徴量の計算には全行を渡す(ここで落とさない): 本番の predict_today.load_hist は6行でないレースを
    落とさず(build_hist はそのレースの履歴の値を NaN にするだけ)、現行45列の履歴(r_* / v_* など)も
    全行から数えるので、学習でも同じ入口にしないと6行でないレースが現れた日から学習と当日で別の履歴を
    見ることになる(設計 原則4)。今の entries は全レース6行で、何も落ちない。"""
    n = len(df)
    date = df["date"].astype(str).to_numpy()
    venue = pd.to_numeric(df["venue"], errors="coerce").to_numpy(np.float64)
    rno = pd.to_numeric(df["race_no"], errors="coerce").to_numpy(np.float64)
    lane = pd.to_numeric(df["lane"], errors="coerce").to_numpy(np.float64)
    toban = pd.to_numeric(df["toban"], errors="coerce").to_numpy(np.float64)
    if n == 0:
        return np.zeros(0, dtype=bool)
    new = np.ones(n, dtype=bool)
    new[1:] = (date[1:] != date[:-1]) | (venue[1:] != venue[:-1]) | (rno[1:] != rno[:-1])
    rid = np.cumsum(new) - 1
    nr = int(rid[-1]) + 1
    first = np.flatnonzero(new)
    pos_in = np.arange(n) - first[rid]
    rowok = (lane == pos_in + 1) & ~np.isnan(toban) & ~np.isnan(venue) & ~np.isnan(rno)
    size = np.bincount(rid, minlength=nr)
    raceok = (size == 6) & (np.bincount(rid, weights=rowok, minlength=nr) == 6)
    keep = raceok[rid]
    if not keep.all():
        log("6艇そろわない(欠け・重複・登番なし)レース %d 本・%d 行を学習の行列から除く(履歴の計算には残す)"
            % (int((~raceok).sum()), int((~keep).sum())))
    return keep


def six_row_races(df: pd.DataFrame, log=print) -> pd.DataFrame:
    """sort_entries → six_row_mask で絞った表(index を振り直す)。全行を保つ必要の無い所(テスト)で使う。
    学習(main_v2)は six_row_mask を使い、特徴量は全行から作って行列だけを絞る。"""
    d = sort_entries(df)
    keep = six_row_mask(d, log=log)
    return d if keep.all() else d[keep].reset_index(drop=True)


def entries_to_races(day_df: pd.DataFrame):
    """entries の1日分の行 → common.parse_b と同じ形の race dict の一覧(結果の列は持たない)。
    学習の最後の自己検査(その日を「当日」とみなして予測の経路を通す)とテストが使う。
    番組表由来の列は entries の値そのまま(整数の列は NaN なら 0。欠損の少数は NaN のまま)。"""
    def _i(v, default=0):
        try:
            return default if pd.isna(v) else int(v)
        except Exception:
            return default

    def _f(v):
        try:
            return float(v)
        except Exception:
            return float("nan")

    def _s(x, k):
        v = x.get(k, "")
        return "" if (v is None or (isinstance(v, float) and np.isnan(v))) else str(v)

    races = []
    for (venue, rno), g in day_df.groupby(["venue", "race_no"], sort=True):
        g = g.sort_values("lane")
        recs = g.to_dict("records")       # itertuples は列名 class(予約語)を別名にするので使わない
        racers = [{"lane": _i(x["lane"]), "toban": _i(x["toban"]), "name": _s(x, "name"), "age": _i(x["age"]),
                   "branch": _s(x, "branch"), "weight": _i(x["weight"]), "class": _s(x, "class"),
                   "nat_win": _f(x["nat_win"]), "nat_in2": _f(x["nat_in2"]),
                   "loc_win": _f(x["loc_win"]), "loc_in2": _f(x["loc_in2"]),
                   "motor_no": _i(x["motor_no"]), "motor_in2": _f(x["motor_in2"]),
                   "boat_no": _i(x["boat_no"]), "boat_in2": _f(x["boat_in2"])} for x in recs]
        races.append({"date": str(g["date"].iloc[0]), "venue": int(venue), "race_no": int(rno),
                      "race_type": str(g["race_type"].iloc[0]), "distance": _i(g["distance"].iloc[0]),
                      "deadline": str(g["deadline"].iloc[0]), "day_n": None, "racers": racers})
    return races


def entries_live_info(day_df: pd.DataFrame):
    """entries の1日分の行から、直前情報の形 {(venue, rno): {"ex": {lane: 展示タイム}, "wind", "wave", "absent": []}}
    (結果ファイル由来の確定値。自己検査とテストで「直前情報あり」の条件を作る)。展示タイムの無い艇は None。"""
    live = {}
    for (venue, rno), g in day_df.groupby(["venue", "race_no"], sort=True):
        g = g.sort_values("lane")
        ex = {}
        for ln, v in zip(g["lane"].to_numpy(), pd.to_numeric(g["ex_time"], errors="coerce").to_numpy()):
            ex[int(ln)] = None if np.isnan(v) else float(v)
        w = pd.to_numeric(g["wind"], errors="coerce").to_numpy()[0] if "wind" in g.columns else np.nan
        wv = pd.to_numeric(g["wave"], errors="coerce").to_numpy()[0] if "wave" in g.columns else np.nan
        live[(int(venue), int(rno))] = {"ex": ex, "wind": (None if np.isnan(w) else float(w)),
                                        "wave": (None if np.isnan(wv) else float(wv)), "absent": []}
    return live


def race_eval_rows_v2(dates, venues, rnos, pos6, pay, P, wp, p2):
    """世代2のレース単位の評価行(race_eval_rows と同じ列・同じ定義)。
    dates/venues/rnos/pay: (R,)、pos6: (R,6) 着順(NaN あり)、P: (R,120) 120通りの確率(PERMS の順)、
    wp: (R,6) 1着の確率、p2: (R,6) 2着以内の確率。買い目の並びは安定ソート(同率の順は trifecta_probs の
    辞書の順と同じ)。"""
    R = len(P)
    order = np.argsort(-P, axis=1, kind="stable")
    ranks = np.empty_like(order)
    ranks[np.arange(R)[:, None], order] = np.arange(120)[None, :]
    rows = []
    for i in range(R):
        pos = pos6[i]
        if np.isnan(pos).all():
            continue
        actual = None
        a = np.flatnonzero(pos == 1)
        b = np.flatnonzero(pos == 2)
        c = np.flatnonzero(pos == 3)
        if len(a) and len(b) and len(c):
            actual = (int(a[0]), int(b[0]), int(c[0]))
        rank_act = int(ranks[i, _PERM_INDEX[actual]]) if actual else 10 ** 6
        fav = int(np.argmax(wp[i]))
        rows.append(dict(
            date=dates[i], venue=int(venues[i]), rno=int(rnos[i]),
            fav=fav, fav_p2=float(p2[i, fav]),
            fav_pos=float(pos[fav]) if not np.isnan(pos[fav]) else 99.0,
            t1p=float(P[i, order[i, 0]]),
            top5p=float(P[i, order[i, :5]].sum()),
            hit_win=int(actual is not None and fav == actual[0]),
            hit_fuku=int(pos[fav] <= 2) if not np.isnan(pos[fav]) else 0,
            hit_t1=int(rank_act < 1), hit_t5=int(rank_act < 5), hit_t6=int(rank_act < 6),
            hit_t10=int(rank_act < 10), hit_t18=int(rank_act < 18),
            pay3t=float(pay[i]) if not np.isnan(pay[i]) else 0.0,
            in_t6=bool(rank_act < 6),
        ))
    return pd.DataFrame(rows)


def _race_first_valid(x6):
    """(R,6) の行ごとの最初の NaN でない値(無ければ NaN)。払戻などレース単位の列を6行から1つ取る。"""
    m = ~np.isnan(x6)
    j = m.argmax(1)
    return np.where(m.any(1), x6[np.arange(len(x6)), j], np.nan)


def _seed_rows(rnos, pos6, P, complete):
    """較正の種(build_calib.seed_from_predictions)に渡す行。試験期間の着順がそろったレースだけ。
    picks は上位5点(p は本番と同じ小数4桁)、order は結果 "a-b-c"。"""
    rows = []
    for i in np.flatnonzero(complete):
        order = np.argsort(-P[i], kind="stable")[:5]
        picks = [{"c": str(model_v2.COMBO_STR[j]), "p": round(float(P[i, j]), 4)} for j in order]
        a, b, c = (int(np.flatnonzero(pos6[i] == k)[0]) + 1 for k in (1, 2, 3))
        rows.append({"no": int(rnos[i]), "picks": picks, "order": f"{a}-{b}-{c}"})
    return rows


def self_check_v2(df, X6_te, date_r, venue_r, rno_r, booster, P_te, te_idx, fan, log=print,
                  rtol=1e-6, atol_floor=1e-9, p_tol=1e-6):
    """最初の朝の安全策(設計 4.9): entries の最終日 D を「当日」とみなし、本番の予測の経路
    (predict_today.build_feat_cache → live_matrix → model_v2.predict_P)で153列と120通りを作り、
    学習時の表の同じ行と、採点用の予測に一致することを確かめる。
    一致しなければ SystemExit(学習の失敗。meta.json を書かないので配布されない)。
    戻り値は meta.json に書く要約。
    df: entries の全行(sort_entries 済み。6艇そろわないレースの行も含んでよい = 本番の load_hist と同じ入口)。
    X6_te: 試験期間のレース(te_idx の順)の学習時の行列 (len(te_idx),6,153)(隠す前の値である必要は無い:
    試験期間は隠していない)。全期間の行列は学習の前に解放するので、試験期間の行だけを受け取る。
    date_r / venue_r / rno_r: 行列のレースごとの日付・会場・レース番号(全レース)。P_te: te_idx の採点用の予測。"""
    import predict_today as pt   # 循環 import(predict_today → train)を避けるため、ここで読む

    date_all = df["date"].astype(str).to_numpy()
    D = str(date_all.max())
    te_idx = np.asarray(te_idx, dtype=np.int64)
    pos_D = np.flatnonzero(np.asarray(date_r)[te_idx] == D)     # 試験期間の中の最終日のレース(X6_te の位置)
    n_D = int((np.asarray(date_r) == D).sum())
    if n_D == 0:
        raise SystemExit(f"self-check: 最終日 {D} に6艇そろったレースが無い")
    if len(pos_D) != n_D:
        raise SystemExit(f"self-check: 最終日 {D} が試験期間に入っていない")
    hist = df[date_all < D]
    day_df = df[date_all == D]
    races = entries_to_races(day_df)
    live = entries_live_info(day_df)
    t0 = time.time()
    cache = pt.build_feat_cache(D, races, hist, fan)          # 朝の経路(直前の9列は NaN)
    tgt = pd.DataFrame(pt.races_to_rows(races, live=live))
    X6l, keys, absent, _rows = pt.live_matrix(cache, tgt)     # 直前の経路(9列を埋める。6艇でないレースは SKIP)
    if X6l is None or len(keys) != len(pos_D):
        raise SystemExit(f"self-check: 予測の経路でレース数が合わない({0 if X6l is None else len(keys)} != {len(pos_D)})")
    key_to_pos = {(int(venue_r[te_idx[p]]), int(rno_r[te_idx[p]])): int(p) for p in pos_D}
    try:
        ref = np.array([key_to_pos[k] for k in keys])
    except KeyError as e:
        raise SystemExit(f"self-check: 予測の経路のレース {e} が学習時の表に無い")
    X_ref = np.asarray(X6_te)[ref]
    a = X6l.astype(np.float64)
    b = X_ref.astype(np.float64)
    nan_mis = int((np.isnan(a) != np.isnan(b)).sum())
    d = np.abs(a - b)
    d = np.where(np.isnan(d), 0.0, d)
    tol = rtol * np.maximum(np.abs(np.nan_to_num(a)), np.abs(np.nan_to_num(b))) + atol_floor
    bad = d > tol
    n_bad = int(bad.sum())
    worst = []
    if n_bad:
        for j in np.unique(np.nonzero(bad)[2]):
            worst.append((FEATURES_V2[j], float(d[:, :, j].max())))
    exact = int((d == 0).all(axis=(1, 2)).sum())
    P_live = model_v2.predict_P(booster, X6l)
    P_ref = np.asarray(P_te)[ref]
    dP = float(np.abs(P_live - P_ref).max())
    wp = model_v2.win_probs(P_live)
    structural = (np.allclose(P_live.sum(1), 1.0, atol=1e-9) and np.allclose(wp.sum(1), 1.0, atol=1e-9)
                  and np.isfinite(P_live).all() and (P_live >= 0).all())
    ok = nan_mis == 0 and n_bad == 0 and dP <= p_tol and structural
    summary = {"date": D, "races": int(len(keys)), "ok": bool(ok),
               "nan_mismatch": nan_mis, "cells_over_tol": n_bad, "races_bit_exact": exact,
               "max_abs_diff_features": float(d.max()), "max_abs_diff_P": dP,
               "rtol": rtol, "p_tol": p_tol, "seconds": round(time.time() - t0, 1)}
    log("self-check %s: races %d, NaN位置の不一致 %d, 許容超えの値 %d(列: %s), ビット一致のレース %d, "
        "P の差の最大 %.3g, 合計1 %s (%.1fs)" % (D, len(keys), nan_mis, n_bad, worst[:5], exact, dP, structural,
                                                   time.time() - t0))
    if not ok:
        raise SystemExit("self-check failed: 予測の経路で作った値が学習時の表・採点用の予測と合わない: %s" % summary)
    return summary


def main_v2(entries=None, rounds=model_v2.ROUNDS_V2, early_stop=model_v2.EARLY_STOP_V2, params=None,
            out_dir=None, report_path=None, seed_path=None, self_check=True, log=print):
    """世代2の学習(設計 4.1)。entries 読み込み → ラベル → add_features_v2(df, df, fan)(全行) →
    6艇そろったレースの行だけを行列にする(six_row_mask) →
    日付の前80%で学習・次の10%で打ち切り・最後の10%は報告だけ → 学習レースの半分で LIVE_COLS_V2 の9列を隠す
    (現行と同じ決め方: default_rng(42)、レース単位。隠すのは行列にした後) → 積み上げ → 全期間の行列を解放 →
    LightGBM のデータ化 → 積み上げ行列を解放 → 学習 → 試験期間(直前情報あり)の予測で報告 → model.txt →
    自己検査 → meta.json(最後)。
    メモリ: 全期間の行列(約1.0GB)は積み上げた直後に消し、試験期間の行(約0.1GB)だけを採点・自己検査のために
    別に持つ。積み上げは全期間の行列から位置で写す(X6[tr_idx] の写し 約0.8GB を作らない)ので、最大は
    データ化の最中の 積み上げ行列 約2.4GB + LightGBM のデータ(performance.md 2.5。約4GB が目安)。
    引数は試験用(entries の一部・木の本数・出力先の差し替え)。本番は引数なし。"""
    from build_calib import make_seed, SEED_PATH
    t0 = time.time()

    def say(msg):
        log("[%5.0fs] %s" % (time.time() - t0, msg))

    out = Path(out_dir) if out_dir else ROOT / "data" / "model_build"
    report_path = Path(report_path) if report_path else ROOT / "docs" / "model_report.json"
    seed_path = Path(seed_path) if seed_path else SEED_PATH
    F = len(FEATURES_V2)
    live_idx = [FEATURES_V2.index(c) for c in LIVE_COLS_V2]

    df = entries if entries is not None else load_entries()
    df = sort_entries(df)
    keep = six_row_mask(df, log=say)       # 行列に入れる行。特徴量は全行から作る(本番の load_hist と同じ入口)
    say(f"entries {len(df)} rows, {df['date'].nunique()} days, {df['date'].min()} - {df['date'].max()}"
        + ("" if keep.all() else f" (matrix rows {int(keep.sum())})"))
    fan = load_fan(ROOT / "data" / "fan")
    say(f"fan records: {len(fan)}")
    say("building features v2 (current 45 + history 108; this may take a few minutes)...")
    feats = add_features_v2(df, df, fan=fan)
    X = feats[FEATURES_V2].to_numpy(dtype=np.float32)
    # 採点・基準・自己検査に要る列だけ残して、表は解放する(全期間の表は数GBになる)
    vl = feats[["v_lane_win365", "v_lane_top2_365", "v_lane_top3_365"]].to_numpy(np.float64)
    del feats
    if not keep.all():
        X = X[keep]
        vl = vl[keep]
    say(f"features: X {X.shape} float32")
    date = df["date"].astype(str).to_numpy()[keep]
    venue = pd.to_numeric(df["venue"], errors="coerce").to_numpy(np.int64)[keep]
    rno = pd.to_numeric(df["race_no"], errors="coerce").to_numpy(np.int64)[keep]
    lane = pd.to_numeric(df["lane"], errors="coerce").to_numpy(np.int64)[keep]
    pos = pd.to_numeric(df["pos"], errors="coerce").to_numpy(np.float64)[keep]
    pay = (pd.to_numeric(df["pay3t_amount"], errors="coerce").to_numpy(np.float64) if "pay3t_amount" in df.columns
           else np.full(len(df), np.nan))[keep]

    N = len(X)
    R = N // 6
    assert N == R * 6 and (lane.reshape(R, 6) == np.arange(1, 7)[None, :]).all()
    date_r, venue_r, rno_r = date[::6], venue[::6], rno[::6]
    udates = np.sort(np.unique(date_r))
    d_tr = udates[int(len(udates) * 0.80)]
    d_va = udates[int(len(udates) * 0.90)]
    tr_r = date_r < d_tr
    va_r = (date_r >= d_tr) & (date_r < d_va)
    te_r = date_r >= d_va
    say(f"split: train<{d_tr} ({int(tr_r.sum()) * 6}), valid<{d_va} ({int(va_r.sum()) * 6}), test ({int(te_r.sum()) * 6})")

    # 直前情報の隠し: 学習レースの半分(現行と同じ乱数・レース単位)。隠すのは行列にした後(履歴は隠す前の表から)
    rng = np.random.default_rng(42)
    sel = rng.random(int(tr_r.sum())) < 0.5
    masked_r = np.zeros(R, dtype=bool)
    masked_r[np.flatnonzero(tr_r)] = sel
    mrows = (np.flatnonzero(masked_r)[:, None] * 6 + np.arange(6)[None, :]).ravel()
    X[np.ix_(mrows, live_idx)] = np.nan
    say(f"ex-mask: {int(masked_r.sum())} races / {len(mrows)} rows x {len(live_idx)} cols masked")

    pos6 = pos.reshape(R, 6)
    complete = ((pos6 == 1).sum(1) == 1) & ((pos6 == 2).sum(1) == 1) & ((pos6 == 3).sum(1) == 1)
    order = np.stack([(pos6 == k).argmax(1) for k in (1, 2, 3)], axis=1)
    X6 = X.reshape(R, 6, F)
    tr_idx = np.flatnonzero(tr_r & complete)
    va_idx = np.flatnonzero(va_r & complete)
    te_idx = np.flatnonzero(te_r)
    say(f"train races {len(tr_idx)} (complete), valid races {len(va_idx)} (complete), test races {len(te_idx)}")
    # 試験期間の行だけ別に持ち(採点・自己検査)、全期間の行列は積み上げた直後に解放する。積み上げは
    # 位置(idx)で写すので X6[tr_idx] の写しも作らない。データ化が済んだら積み上げ行列も解放する
    X6_te = X6[te_idx].copy()
    Xtr, ytr, btr = model_v2.stack_train(X6, order, idx=tr_idx)
    Xva, yva, bva = model_v2.stack_train(X6, order, idx=va_idx)
    del X, X6
    say(f"stacked: train {Xtr.shape} valid {Xva.shape} float32; full matrix released")
    dtr, dva = model_v2.make_datasets(Xtr, ytr, Xva, yva, params=params)
    del Xtr, Xva, ytr, yva
    say("lightgbm datasets constructed; stacked matrices released")
    booster = model_v2.fit_datasets(dtr, dva, btr, bva, params=params, rounds=rounds, early_stop=early_stop, log=say)
    del dtr, dva
    best = int(booster.best_iteration)
    valid_nll3 = booster.best_score.get("valid_0", {}).get("nll3")

    # 採点は試験期間の「直前情報あり」だけ(時間の節約。設計 4.1)
    t1 = time.time()
    P = model_v2.predict_P(booster, X6_te)
    wp = model_v2.win_probs(P)
    p2 = model_v2.top2_probs(P)
    pay_r = _race_first_valid(pay.reshape(R, 6))
    te_eval = race_eval_rows_v2(date_r[te_idx], venue_r[te_idx], rno_r[te_idx], pos6[te_idx], pay_r[te_idx], P, wp, p2)
    sengen = {"top5_min": SENGEN_TOP5_MIN_BY_GEN[2], "n": None, "valid_rate": None}
    rep_test = report(te_eval, sengen)
    say("scored test %d races (%.0fs)" % (len(te_idx), time.time() - t1))
    say("TEST: " + json.dumps(rep_test, ensure_ascii=False))

    # ベースライン: 会場xコース基礎率のみ(今と同じ式・同じ採点)
    te_rows = (te_idx[:, None] * 6 + np.arange(6)[None, :]).ravel()
    te2 = pd.DataFrame({"date": date[te_rows], "venue": venue[te_rows], "race_no": rno[te_rows], "lane": lane[te_rows],
                        "pos": pos[te_rows], "pay3t_amount": pay[te_rows],
                        "v_lane_win365": vl[te_rows, 0], "v_lane_top2_365": vl[te_rows, 1], "v_lane_top3_365": vl[te_rows, 2]})
    te2["p_norm"] = te2.groupby(["date", "venue", "race_no"])["v_lane_win365"].transform(
        lambda s: s.fillna(0.01).clip(0.001) / s.fillna(0.01).clip(0.001).sum())
    te2["p_top2"] = te2["v_lane_top2_365"].fillna(0.05).clip(0.001)
    te2["p_top3"] = te2["v_lane_top3_365"].fillna(0.1).clip(0.001)
    rep_base = report(race_eval_rows(te2), sengen)
    say("BASELINE: " + json.dumps(rep_base, ensure_ascii=False))
    del te2

    # 較正の種(設計 4.6): 試験期間の「直前情報あり」の予測のビン別の件数・的中数
    trained_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    seed_rows = _seed_rows(rno_r[te_idx], pos6[te_idx], P, complete[te_idx])
    seed = make_seed(seed_rows, built_from=f"train.py {trained_at} test {d_va}-{udates[-1]} live ({len(seed_rows)} races)")
    say(f"calib seed: {len(seed_rows)} races")

    # 特徴量の重要度(gain の絶対値・降順)。153列は feature_importance、段の特徴6列は stage_importance
    gain = np.asarray(booster.feature_importance("gain"), dtype=np.float64)
    imp = dict(zip(FEATURES_V2, gain[:F].round(1).tolist()))
    stage_imp = dict(zip(model_v2.STAGE_COLS, gain[F:].round(1).tolist()))

    # 出力先は data/model_build(gitignore)。meta.json は最初に消して最後に書く。形1のファイルが残っていれば消す
    out.mkdir(parents=True, exist_ok=True)
    (out / "meta.json").unlink(missing_ok=True)
    for n in _FORMAT1_FILES:
        (out / n).unlink(missing_ok=True)
    model_v2.save_model(booster, out / "model.txt")
    saved = lgb.Booster(model_file=str(out / "model.txt"))   # 自己検査は保存したファイルで行う
    say(f"model saved: {out / 'model.txt'} ({(out / 'model.txt').stat().st_size} bytes, {saved.num_trees()} trees)")

    chk = {"skipped": True}
    if self_check:
        chk = self_check_v2(df, X6_te, date_r, venue_r, rno_r, saved, P, te_idx, fan, log=say)

    meta = {
        "format": 2, "gen": 2,
        "trained_at": trained_at,
        "rows_train": int(tr_r.sum()) * 6, "rows_valid": int(va_r.sum()) * 6, "rows_test": int(te_r.sum()) * 6,
        "races_train": int(len(tr_idx)), "races_valid": int(len(va_idx)), "races_test": int(len(te_idx)),
        "period": [str(udates[0]), str(udates[-1])],
        "best_iterations": {"pl": best},
        "valid_nll3": (round(float(valid_nll3), 5) if valid_nll3 is not None else None),
        "features": list(FEATURES_V2),
        "stage_cols": list(model_v2.STAGE_COLS),
        "cat_idx": list(model_v2.CAT_IDX),
        "live_cols": list(LIVE_COLS_V2),
        "features_version": FEATURES_HIST_VERSION,
        "elo_cfg": dict(ELO_CFG),
        # モデルの設定だけ(num_threads など実行環境の項目は除く。dry_run で渡した値が「設定」として残らないように)
        "params": model_v2.model_params(params),
        "sengen": {"top5_min": sengen["top5_min"], "valid_rate": sengen["valid_rate"]},
        "test_metrics": rep_test, "baseline_metrics": rep_base,
        "feature_importance": dict(sorted(imp.items(), key=lambda x: -x[1])),
        "stage_importance": dict(sorted(stage_imp.items(), key=lambda x: -x[1])),
        "calib_seed": seed["bins"],
        "self_check": chk,
    }
    seed_path.parent.mkdir(parents=True, exist_ok=True)
    seed_path.write_text(json.dumps(seed, ensure_ascii=False), encoding="utf-8")
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    say(f"models saved (gen 2): {out / 'model.txt'}, meta.json, {seed_path.name}, {report_path.name}")
    return meta


if __name__ == "__main__":
    main()

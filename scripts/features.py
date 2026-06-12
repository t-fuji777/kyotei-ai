# -*- coding: utf-8 -*-
"""特徴量生成。hist(過去成績)から、target(予測対象行)の特徴量を計算。
リーク防止: 必ず対象レース日より前の日のデータのみ使用する。"""
import numpy as np
import pandas as pd

FEATURES = [
    "venue", "race_no", "lane", "age", "weight", "class_i",
    "nat_win", "nat_in2", "loc_win", "loc_in2", "motor_in2", "boat_in2",
    "r_n365", "r_win365", "r_top2_365", "r_top3_365",
    "r_n90", "r_win90",
    "r_avg_st180", "r_f365",
    "r_lane_n365", "r_lane_win365",
    "r_venue_n365", "r_venue_win365",
    "r_avgpos10",
    "r_makuri365",
    "v_lane_win365", "v_lane_top2_365", "v_lane_top3_365",
    "v_makuri365", "v_water", "v_tide",
    "f_noryoku", "f_win", "f_lane_in2", "f_lane_strank",
    "ex_time", "ex_rank", "ex_diff", "wind", "wave",
]
CLASS_MAP = {"A1": 4, "A2": 3, "B1": 2, "B2": 1}
MAKURI = ("まくり", "まくり差し", "捲り", "捲り差し")
# 場の静的属性(公式レース場データ準拠) 水質: 0=淡水 1=汽水 2=海水
WATER = {1: 0, 2: 0, 5: 0, 10: 0, 11: 0, 12: 0, 13: 0, 21: 0, 23: 0,
         3: 1, 6: 1, 7: 1, 9: 1, 22: 1,
         4: 2, 8: 2, 14: 2, 15: 2, 16: 2, 17: 2, 18: 2, 19: 2, 20: 2, 24: 2}
# 干満差の影響: 1=あり
TIDE = {3: 1, 4: 1, 8: 1, 14: 1, 15: 1, 16: 1, 17: 1, 18: 1, 19: 1, 20: 1, 22: 1, 24: 1}


def load_fan(fan_dir):
    """data/fan/fan_*.csv.gz を読み、(toban, year, ki) -> 行 の辞書を返す"""
    import glob as _glob
    idx = {}
    for f in sorted(_glob.glob(str(fan_dir) + "/fan_*.csv.gz")):
        df = pd.read_csv(f)
        for r in df.itertuples(index=False):
            idx[(int(r.toban), int(r.year), int(r.ki))] = r
    return idx


def _day_ord(dates):
    d = pd.to_datetime(dates.astype(str), format="%Y%m%d")
    return (d - pd.Timestamp("2000-01-01")).dt.days.to_numpy()


def add_features(hist: pd.DataFrame, target: pd.DataFrame, fan=None) -> pd.DataFrame:
    """hist: 結果(pos)付きentries。target: 特徴量を付けたい行(histと同一でも可)。
    fan: load_fan()の辞書(任意)。"""
    hist = hist.copy()
    target = target.copy()
    hist["day"] = _day_ord(hist["date"])
    target["day"] = _day_ord(target["date"])
    target["class_i"] = target["class"].map(CLASS_MAP).fillna(0).astype(int)

    h = hist.dropna(subset=["toban"]).copy()
    h["toban"] = h["toban"].astype(int)
    h["pos"] = pd.to_numeric(h["pos"], errors="coerce")
    h["is_win"] = (h["pos"] == 1).astype(np.int8)
    h["is_top2"] = (h["pos"] <= 2).astype(np.int8)
    h["is_top3"] = (h["pos"] <= 3).astype(np.int8)
    h["is_makuri_win"] = ((h["is_win"] == 1) &
                          h.get("kimarite", pd.Series(index=h.index, dtype=object))
                          .isin(MAKURI)).astype(np.int8)
    h["pos6"] = h["pos"].fillna(6).clip(1, 6)
    h["st_pos"] = np.where(pd.to_numeric(h["st"], errors="coerce") > 0,
                           pd.to_numeric(h["st"], errors="coerce"), np.nan)
    h["is_f"] = (pd.to_numeric(h["st"], errors="coerce") < 0).astype(np.int8)

    n = len(target)
    cols = {k: np.full(n, np.nan) for k in FEATURES if k.startswith(("r_", "v_"))}

    # ---- 選手別 ----
    tgt_idx = np.arange(n)
    tg = pd.DataFrame({
        "toban": target["toban"].astype(int).to_numpy(),
        "day": target["day"].to_numpy(),
        "lane": target["lane"].astype(int).to_numpy(),
        "venue": target["venue"].astype(int).to_numpy(),
        "i": tgt_idx,
    })
    hg = h.sort_values(["toban", "day"])
    for toban, g in tg.groupby("toban"):
        hh = hg[hg["toban"] == toban]
        days = hh["day"].to_numpy()
        if len(days) == 0:
            continue
        win = hh["is_win"].to_numpy(np.float64)
        top2 = hh["is_top2"].to_numpy(np.float64)
        top3 = hh["is_top3"].to_numpy(np.float64)
        pos6 = hh["pos6"].to_numpy(np.float64)
        stp = hh["st_pos"].to_numpy(np.float64)
        isf = hh["is_f"].to_numpy(np.float64)
        lanes = hh["lane"].astype(int).to_numpy()
        venues = hh["venue"].astype(int).to_numpy()

        c_one = np.concatenate([[0], np.cumsum(np.ones(len(days)))])
        c_win = np.concatenate([[0], np.cumsum(win)])
        c_mak = np.concatenate([[0], np.cumsum(hh["is_makuri_win"].to_numpy(np.float64))])
        c_top2 = np.concatenate([[0], np.cumsum(top2)])
        c_top3 = np.concatenate([[0], np.cumsum(top3)])
        c_st = np.concatenate([[0], np.cumsum(np.nan_to_num(stp))])
        c_stn = np.concatenate([[0], np.cumsum(~np.isnan(stp))])
        c_f = np.concatenate([[0], np.cumsum(isf)])

        for _, row in g.iterrows():
            td, i = row["day"], row["i"]
            e = np.searchsorted(days, td, "left")
            s365 = np.searchsorted(days, td - 365, "left")
            s180 = np.searchsorted(days, td - 180, "left")
            s90 = np.searchsorted(days, td - 90, "left")
            n365 = c_one[e] - c_one[s365]
            cols["r_n365"][i] = n365
            if n365 > 0:
                cols["r_win365"][i] = (c_win[e] - c_win[s365]) / n365
                cols["r_top2_365"][i] = (c_top2[e] - c_top2[s365]) / n365
                cols["r_top3_365"][i] = (c_top3[e] - c_top3[s365]) / n365
                cols["r_f365"][i] = c_f[e] - c_f[s365]
                w365 = c_win[e] - c_win[s365]
                if w365 > 0:
                    cols["r_makuri365"][i] = (c_mak[e] - c_mak[s365]) / w365
            n90 = c_one[e] - c_one[s90]
            cols["r_n90"][i] = n90
            if n90 > 0:
                cols["r_win90"][i] = (c_win[e] - c_win[s90]) / n90
            stn = c_stn[e] - c_stn[s180]
            if stn > 0:
                cols["r_avg_st180"][i] = (c_st[e] - c_st[s180]) / stn
            if e > 0:
                cols["r_avgpos10"][i] = pos6[max(0, e - 10):e].mean()
            # 同枠成績/同会場成績(365日)
            sl = slice(s365, e)
            lm = lanes[sl] == row["lane"]
            cols["r_lane_n365"][i] = lm.sum()
            if lm.sum() > 0:
                cols["r_lane_win365"][i] = win[sl][lm].mean()
            vm = venues[sl] == row["venue"]
            cols["r_venue_n365"][i] = vm.sum()
            if vm.sum() > 0:
                cols["r_venue_win365"][i] = win[sl][vm].mean()

    # ---- 会場xコース(枠)ベース率 ----
    hv = h.sort_values(["venue", "day"])
    for venue, g in tg.groupby("venue"):
        hh = hv[hv["venue"] == venue]
        if len(hh) == 0:
            continue
        days = hh["day"].to_numpy()
        # 会場まくり率(365日: 勝利レース中まくり系の割合)
        winm = (hh["is_win"] == 1).to_numpy()
        c_vw = np.concatenate([[0], np.cumsum(winm.astype(float))])
        c_vm = np.concatenate([[0], np.cumsum(hh["is_makuri_win"].to_numpy(np.float64))])
        for _, row in g.iterrows():
            td, i = row["day"], row["i"]
            e = np.searchsorted(days, td, "left")
            s = np.searchsorted(days, td - 365, "left")
            wn = c_vw[e] - c_vw[s]
            if wn > 0:
                cols["v_makuri365"][i] = (c_vm[e] - c_vm[s]) / wn
        for lane in range(1, 7):
            lm = (hh["lane"].astype(int) == lane).to_numpy()
            c_n = np.concatenate([[0], np.cumsum(lm.astype(float))])
            c_w = np.concatenate([[0], np.cumsum((lm & (hh["is_win"] == 1).to_numpy()).astype(float))])
            c_2 = np.concatenate([[0], np.cumsum((lm & (hh["is_top2"] == 1).to_numpy()).astype(float))])
            c_3 = np.concatenate([[0], np.cumsum((lm & (hh["is_top3"] == 1).to_numpy()).astype(float))])
            gg = g[g["lane"] == lane]
            for _, row in gg.iterrows():
                td, i = row["day"], row["i"]
                e = np.searchsorted(days, td, "left")
                s = np.searchsorted(days, td - 365, "left")
                nn = c_n[e] - c_n[s]
                if nn > 0:
                    cols["v_lane_win365"][i] = (c_w[e] - c_w[s]) / nn
                    cols["v_lane_top2_365"][i] = (c_2[e] - c_2[s]) / nn
                    cols["v_lane_top3_365"][i] = (c_3[e] - c_3[s]) / nn

    for k, v in cols.items():
        target[k] = v

    # ---- 場の静的属性 ----
    vno = target["venue"].astype(int)
    target["v_water"] = vno.map(WATER).fillna(0).astype(int)
    target["v_tide"] = vno.map(TIDE).fillna(0).astype(int)

    # ---- ファン手帳(期別成績) ----
    if fan:
        dt = pd.to_datetime(target["date"].astype(str), format="%Y%m%d")
        years = dt.dt.year.to_numpy()
        kis = np.where(dt.dt.month <= 6, 1, 2)
        tobans = target["toban"].astype(int).to_numpy()
        lanes = target["lane"].astype(int).to_numpy()
        fn = np.full(len(target), np.nan)
        fw = np.full(len(target), np.nan)
        fi = np.full(len(target), np.nan)
        fs = np.full(len(target), np.nan)
        for i in range(len(target)):
            r = fan.get((tobans[i], years[i], kis[i]))
            if r is None:
                continue
            fn[i] = getattr(r, "noryoku_now", np.nan)
            fw[i] = getattr(r, "win_rate", np.nan)
            ln = lanes[i]
            fi[i] = getattr(r, f"c{ln}_in2", np.nan)
            fs[i] = getattr(r, f"c{ln}_st_rank", np.nan)
        target["f_noryoku"] = fn
        target["f_win"] = fw
        target["f_lane_in2"] = fi
        target["f_lane_strank"] = fs
    else:
        for k in ("f_noryoku", "f_win", "f_lane_in2", "f_lane_strank"):
            target[k] = np.nan

    # ---- 直前情報(展示タイム・気象): 学習はK票確定値, 推論はbeforeinfo由来。未取得はNaN ----
    for c in ("ex_time", "wind", "wave"):
        if c not in target.columns:
            target[c] = np.nan
        target[c] = pd.to_numeric(target[c], errors="coerce")
    g = target.groupby(["date", "venue", "race_no"])["ex_time"]
    target["ex_rank"] = g.rank(method="min")
    target["ex_diff"] = target["ex_time"] - g.transform("min")
    return target

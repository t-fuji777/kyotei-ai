# -*- coding: utf-8 -*-
"""履歴の特徴量(scripts/features_hist.py)・読み込み(scripts/entries_io.py)・add_features_v2 の検算。

実行: リポジトリの根で  PYTHONUTF8=1 python -X utf8 tests/test_features_hist.py [--quick]
  --quick: 合成データの検算だけ(数秒)。省くと実データ(全168万行)の検算も行う(全期間の計算4回・約2〜3分・
  作業メモリ最大4GB台)。

実データの検算(設計書 5章-2 と担当の指示):
 (a) 実験の保存値 rebuild/history/features.pkl(float32 入口)と、CSV(float64 入口)から作った値を
     2026-10-01〜10-04 の行で比べ、差の最大が 1e-5 以下(入口の型の差は 5e-7 程度: port/performance.md 4.4)
 (b) ある日以降の結果を消した表から作った当日の行 = 全期間で作った同じ行(108列すべて不一致0。2日分)
 (c) 当日行を predict_today.races_to_rows の形(motor_no/boat_no/distance を足した形。結果の列なし)で作り、
     add_features_v2 の当日の経路を通しても同じ(朝 = 展示なし: 104列一致・4列 NaN)
 (d) live_hist_update(朝の中間値 + 展示)= 全期間で作った4列(ビット一致)
 (e) entries_io.load_entries が今の読み込み(train.load_entries / predict_today.load_hist)と同じ行・同じ順
実験の成果物の場所は環境変数 KYOTEI_REBUILD_DIR で変えられる(無ければ (a) は飛ばす)。
"""
import glob
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import features_hist as FH  # noqa: E402
import entries_io  # noqa: E402
from features import add_features, add_features_v2, FEATURES, FEATURES_V2, EX_COLS, LIVE_COLS_V2  # noqa: E402

REBUILD = Path(os.environ.get(
    "KYOTEI_REBUILD_DIR",
    r"C:\Users\TABF11~1.FUJ\AppData\Local\Temp\claude\C--Users-ta-fujino-Documents-kyotei-ai-main"
    r"\467be6e3-74fd-4564-9509-0bfe1e8aee91\scratchpad\rebuild"))
QUICK = "--quick" in sys.argv
RESULT_COLS = ["pos", "abnormal", "course", "st", "kimarite", "pay3t_combo", "pay3t_amount", "pay2t_combo", "pay2t_amount"]
LIVE_IN = ["ex_time", "wind", "wave"]
T0 = time.time()
_n_ok = 0


def say(msg):
    print("[%6.1fs] %s" % (time.time() - T0, msg), flush=True)


def ok(name):
    global _n_ok
    _n_ok += 1
    print("ok   " + name, flush=True)


def same_or_nan(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    return (a == b) | (np.isnan(a) & np.isnan(b))


def mismatch_cols(a, b, cols):
    eq = same_or_nan(a, b)
    return {c: int((~eq[:, j]).sum()) for j, c in enumerate(cols) if (~eq[:, j]).any()}


# ============================================================ 合成データ
def make_synthetic(n_days=60, venues=(3, 7), races_per_day=4, n_racers=40, seed=0, start="20250101"):
    """結果つきの小さな entries 風の表(全レース6行)。実データと同じ列名・型。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, periods=n_days, freq="D").strftime("%Y%m%d").tolist()
    tobans = 3000 + rng.permutation(2000)[:n_racers]
    strength = rng.normal(0, 1, n_racers)
    classes = np.array(["A1", "A2", "B1", "B2"])
    rtypes = np.array(["予選", "一般", "特選", "準優勝戦", "優勝戦"])
    kims = np.array(FH.KIM)
    rows = []
    for d in dates:
        for v in venues:
            for rno in range(1, races_per_day + 1):
                pick = rng.choice(n_racers, 6, replace=False)
                perf = strength[pick] + rng.normal(0, 1.5, 6) - 0.4 * np.arange(6)
                order = np.argsort(-perf)
                pos = np.empty(6)
                pos[order] = np.arange(1, 7)
                kim = kims[rng.integers(0, len(kims))]
                ex = np.round(6.6 + rng.normal(0, 0.08, 6), 2)
                st = np.round(rng.uniform(0.05, 0.25, 6), 2)
                abn = [np.nan] * 6
                if rng.random() < 0.08:                      # たまにフライング(着順なし)
                    k = int(rng.integers(0, 6))
                    pos[k] = np.nan
                    st[k] = -0.02
                    abn[k] = "F"
                wind, wave = float(rng.integers(0, 8)), float(rng.integers(0, 6))
                mi2 = np.round(rng.uniform(20, 50, 6), 2)
                for i in range(6):
                    rows.append(dict(
                        date=d, venue=v, race_no=rno, race_type=str(rtypes[rng.integers(0, len(rtypes))]),
                        distance=1800 if rng.random() > 0.1 else 1200,
                        deadline="%02d:%02d" % (10 + rno, 30), lane=i + 1, toban=int(tobans[pick[i]]),
                        name="x", age=30, branch="y", weight=52, class_=str(classes[rng.integers(0, 4)]),
                        nat_win=round(float(5 + strength[pick[i]]), 2), nat_in2=30.0, loc_win=5.0, loc_in2=30.0,
                        motor_no=int(rng.integers(1, 60)), motor_in2=float(mi2[i]),
                        boat_no=int(rng.integers(1, 60)), boat_in2=30.0,
                        pos=pos[i], abnormal=abn[i], course=float(i + 1), st=float(st[i]), ex_time=float(ex[i]),
                        kimarite=str(kim), wind=wind, wave=wave,
                        pay3t_combo="1-2-3", pay3t_amount=1000.0, pay2t_combo="1-2", pay2t_amount=300.0))
    df = pd.DataFrame(rows).rename(columns={"class_": "class"})
    # race_type をレース内で同じにする(実データと同じ)
    df["race_type"] = df.groupby(["date", "venue", "race_no"])["race_type"].transform("first")
    df["distance"] = df.groupby(["date", "venue", "race_no"])["distance"].transform("first")
    return df


def as_program_rows(day_df, with_live, add3=True):
    """CSV の当日行 → predict_today.races_to_rows と同じ形(番組表の辞書 → 行の辞書)。
    add3: 設計 4.3 の motor_no / boat_no / distance を足す(races_to_rows が既に足していれば何もしない)。
    結果の列は持たない。with_live なら ex_time / wind / wave を直前情報として入れる。"""
    try:
        from predict_today import races_to_rows
    except Exception as e:  # 他の担当が編集中などで読めない時は同じ形を手元で作る
        print("note: predict_today を読めない(%s)。races_to_rows と同じ形を手元で作る" % e, flush=True)
        races_to_rows = _races_to_rows_copy
    races, live = [], {}
    for (v, rno), g in day_df.groupby(["venue", "race_no"], sort=False):
        g = g.sort_values("lane")
        recs = g.to_dict("records")       # itertuples は列名 class(予約語)を別名にするので使わない
        racers = [dict(lane=int(x["lane"]), toban=int(x["toban"]), name=str(x.get("name", "")), age=int(x["age"]),
                       branch=str(x.get("branch", "")), weight=int(x["weight"]), **{"class": str(x["class"])},
                       nat_win=float(x["nat_win"]), nat_in2=float(x["nat_in2"]), loc_win=float(x["loc_win"]),
                       loc_in2=float(x["loc_in2"]), motor_no=int(x["motor_no"]), motor_in2=float(x["motor_in2"]),
                       boat_no=int(x["boat_no"]), boat_in2=float(x["boat_in2"]))
                  for x in recs]
        races.append(dict(date=str(g["date"].iloc[0]), venue=int(v), race_no=int(rno), race_type=str(g["race_type"].iloc[0]),
                          distance=int(g["distance"].iloc[0]), deadline=str(g["deadline"].iloc[0]), day_n=None, racers=racers))
        if with_live:
            live[(int(v), int(rno))] = {"ex": {int(x["lane"]): (None if pd.isna(x["ex_time"]) else float(x["ex_time"]))
                                              for x in recs},
                                        "wind": float(g["wind"].iloc[0]), "wave": float(g["wave"].iloc[0])}
    rows = races_to_rows(races, live=live if with_live else None)
    if add3:
        by = {(r["venue"], r["race_no"]): r for r in races}
        for row in rows:
            r = by[(row["venue"], row["race_no"])]
            rc = next(x for x in r["racers"] if x["lane"] == row["lane"])
            row.setdefault("distance", r["distance"])
            row.setdefault("motor_no", rc["motor_no"])
            row.setdefault("boat_no", rc["boat_no"])
    return pd.DataFrame(rows)


def _races_to_rows_copy(races, live=None):
    """predict_today.races_to_rows(HEAD d75435d46)の写し。predict_today を読めない時だけ使う。"""
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


def pick_rows(F, df, mask):
    return F.loc[df.index[mask]]


def test_synthetic():
    say("合成データの検算")
    df = make_synthetic()
    full = FH.build_hist(df, log=lambda m: None)
    assert list(full.columns) == FH.HIST_COLS + FH.HIST_AUX_COLS and len(full) == len(df)
    assert (full.index == df.index).all()
    assert all(full[c].dtype == np.float32 for c in FH.HIST_COLS) and full[FH.HIST_AUX_COLS[0]].dtype == np.float64
    # 中身の目安: 実力値は NaN にならない。h_elo_n は出走数、h_plw_p はレース内で合計1
    assert not full[["h_elo", "h_elola_exp", "h_plw_p", "h_elo_n"]].isna().any().any()
    assert np.allclose(full["h_plw_p"].to_numpy().reshape(-1, 6).sum(1), 1.0, atol=1e-5)
    assert np.allclose(full["h_elola_exp"].to_numpy().reshape(-1, 6).sum(1), 15.0, atol=1e-3)
    ok("build_hist: 108列 float32 + 中間値、同じ index、実力値の形")

    # 並び非依存: 混ぜた入力でも同じ index に同じ値
    rng = np.random.default_rng(3)
    dfs = df.iloc[rng.permutation(len(df))]
    sh = FH.build_hist(dfs, log=lambda m: None)
    assert (sh.index == dfs.index).all()
    assert not mismatch_cols(sh.sort_index()[FH.HIST_COLS].to_numpy(), full[FH.HIST_COLS].to_numpy(), FH.HIST_COLS)
    assert same_or_nan(sh.sort_index()["_aux_exh365"], full["_aux_exh365"]).all()
    ok("build_hist: 入力の並びに依存しない")

    # 6行そろわないレース: 1行欠け・重複・登番なし → そのレースは NaN、他の行は「そのレースが無い表」と同じ
    msgs = []
    key = (df["date"] == df["date"].iloc[600]) & (df["venue"] == df["venue"].iloc[600]) & (df["race_no"] == df["race_no"].iloc[600])
    bad1 = df[~(key & (df["lane"] == 3))]                      # 5行のレース
    dup = pd.concat([df, df.iloc[[1200]]], ignore_index=True)   # 重複で7行のレース
    kdup = (dup["date"] == dup["date"].iloc[1200]) & (dup["venue"] == dup["venue"].iloc[1200]) & (dup["race_no"] == dup["race_no"].iloc[1200])
    for d, k, label in ((bad1, key[~(key & (df["lane"] == 3))], "5行"), (dup, kdup, "重複7行")):
        r = FH.build_hist(d, log=msgs.append)
        assert r.loc[k.to_numpy(), FH.HIST_COLS].isna().all().all(), label
        ref = FH.build_hist(d[~k.to_numpy()], log=lambda m: None)
        got = r.loc[~k.to_numpy(), FH.HIST_COLS].to_numpy()
        assert not mismatch_cols(got, ref[FH.HIST_COLS].to_numpy(), FH.HIST_COLS), label
    assert any("6艇そろわない" in m for m in msgs), msgs
    ok("build_hist: 6行そろわないレースは NaN にして続行(件数をログ)、他の行は変わらない")

    # 当日行(結果の列なし)を末尾に足しても、結果つきで表の中にある時と同じ値
    D = df["date"].max()
    is_d = (df["date"] == D).to_numpy()
    hist = df[~is_d]
    today = df[is_d]
    live_rows = as_program_rows(today, with_live=True)
    am_rows = as_program_rows(today, with_live=False)
    for c in ("motor_no", "boat_no", "distance"):
        assert c in live_rows.columns
    assert not any(c in am_rows.columns for c in RESULT_COLS + LIVE_IN)
    ref = full.loc[is_d]
    key_ref = today[["venue", "race_no", "lane"]].to_numpy()
    for rows, label in ((live_rows, "展示あり"), (am_rows, "朝(展示なし)")):
        # 当日行は番組表の順(会場→レース番号)ではなく、わざと逆順に並べて渡す
        rows = rows.iloc[::-1].reset_index(drop=True)
        cat = pd.concat([hist, rows], ignore_index=True)
        r = FH.build_hist(cat, log=lambda m: None).iloc[len(hist):]
        rk = rows[["venue", "race_no", "lane"]].to_numpy()
        pos = {tuple(k): i for i, k in enumerate(map(tuple, key_ref))}
        ref_al = ref.iloc[[pos[tuple(k)] for k in rk]]
        if label == "展示あり":
            bad = mismatch_cols(r[FH.HIST_COLS].to_numpy(), ref_al[FH.HIST_COLS].to_numpy(), FH.HIST_COLS)
            assert not bad, bad
        else:
            nonlive = [c for c in FH.HIST_COLS if c not in FH.HIST_LIVE_COLS]
            bad = mismatch_cols(r[nonlive].to_numpy(), ref_al[nonlive].to_numpy(), nonlive)
            assert not bad, bad
            assert r[FH.HIST_LIVE_COLS].isna().all().all()
            # (d) 朝の中間値 + 展示 → 4列が全期間の値とビット一致(行の順は当日行のまま)
            ex = ref_al["h_ex_rel"].to_numpy().astype(np.float64) * 0  # 形だけ
            ex = rows.merge(today[["venue", "race_no", "lane", "ex_time"]], on=["venue", "race_no", "lane"], how="left")["ex_time"].to_numpy()
            upd = FH.live_hist_update(ex, r)
            assert list(upd.columns) == FH.HIST_LIVE_COLS and (upd.index == r.index).all()
            bad = mismatch_cols(upd.to_numpy(), ref_al[FH.HIST_LIVE_COLS].to_numpy(), FH.HIST_LIVE_COLS)
            assert not bad, bad
            # race_id を渡す形(行を混ぜても同じ)
            perm = rng.permutation(len(rows))
            rid = (np.arange(len(rows)) // 6)
            upd2 = FH.live_hist_update(ex[perm], r.iloc[perm], race_id=rid[perm])
            assert not mismatch_cols(upd2.to_numpy(), upd.iloc[perm].to_numpy(), FH.HIST_LIVE_COLS)
            # 展示が1艇だけ無い(欠場)・全艇無い → ある艇だけで平均 / 全部 NaN
            ex2 = ex.copy()
            ex2[0] = np.nan
            u = FH.live_hist_update(ex2, r)
            assert np.isnan(u["h_ex_rel"].iloc[0]) and not np.isnan(u["h_ex_rel"].iloc[1])
            assert abs(float(u["h_ex_rel"].iloc[1]) - float(ex2[1] - np.nanmean(ex2[:6]))) < 1e-6
            ex3 = ex.copy()
            ex3[:6] = np.nan
            assert FH.live_hist_update(ex3, r)[FH.HIST_LIVE_COLS].iloc[:6].isna().all().all()
    ok("build_hist: 当日行(races_to_rows の形・結果の列なし・逆順)でも全期間の値と一致、朝は4列 NaN")
    ok("live_hist_update: 朝の中間値 + 展示 = build_hist の4列(ビット一致)。race_id 指定・欠場・展示なしも")

    # 登番の上限を超えても例外にしない(配列を広げる)。会場コードの範囲外は止める
    big = df.copy()
    big.loc[big["toban"] == big["toban"].iloc[0], "toban"] = 12345
    msgs = []
    FH.build_hist(big, log=msgs.append)
    assert any("上限" in m for m in msgs), msgs
    # 1行だけ会場を変えると「6行そろわないレース」として NaN になる(止まらない)。レースごと範囲外なら止まる
    bad = df.copy()
    bad.loc[0, "venue"] = 40
    r = FH.build_hist(bad, log=lambda m: None)
    assert r.loc[:5, FH.HIST_COLS].isna().all().all() and not r.loc[6:, "h_elo"].isna().any()
    try:
        bad = df.copy()
        bad.loc[:5, "venue"] = 40
        FH.build_hist(bad, log=lambda m: None)
        raise AssertionError("会場コード 40 で止まらなかった")
    except ValueError:
        pass
    ok("build_hist: 登番 > 10000 は配列を広げて続行、会場コードの範囲外は分かる文言で止まる")

    # 入力に列が足りない(結果の列を全部落とす・motor_no なし)でも動く
    thin = df.drop(columns=RESULT_COLS + LIVE_IN + ["motor_no", "boat_no", "distance", "deadline", "race_type"])
    msgs = []
    r = FH.build_hist(thin, log=msgs.append)
    assert len(r) == len(thin) and any("NaN で補う" in m for m in msgs)
    assert (r["h_dist1200"] == 0).all() and r["h_dl_min"].isna().all()
    ok("build_hist: 無い列は NaN で補って動く(件数と列名をログ)")

    # add_features_v2: 学習(同じ表)と当日(hist + 当日行)
    out_tr = add_features_v2(df, df, fan=None)
    assert all(c in out_tr.columns for c in FEATURES_V2 + FH.HIST_AUX_COLS) and len(FEATURES_V2) == 153
    assert (out_tr.index == df.index).all()
    assert not mismatch_cols(out_tr[FH.HIST_COLS].to_numpy(), full[FH.HIST_COLS].to_numpy(), FH.HIST_COLS)
    assert same_or_nan(out_tr[FEATURES].to_numpy(dtype=np.float64), add_features(df, df)[FEATURES].to_numpy(dtype=np.float64)).all()
    # 当日: hist に当日以降の行が混ざっていても除く。target の 45 列は add_features(hist, target) と同じ
    tgt = live_rows.iloc[::-1].reset_index(drop=True)
    out_d = add_features_v2(df, tgt, fan=None)          # hist に当日の行(結果つき)が入ったまま渡す
    assert len(out_d) == len(tgt) and (out_d.index == tgt.index).all()
    rk = tgt[["venue", "race_no", "lane"]].to_numpy()
    pos = {tuple(k): i for i, k in enumerate(map(tuple, key_ref))}
    ref_al = ref.iloc[[pos[tuple(k)] for k in rk]]
    assert not mismatch_cols(out_d[FH.HIST_COLS].to_numpy(), ref_al[FH.HIST_COLS].to_numpy(), FH.HIST_COLS)
    assert same_or_nan(out_d[FEATURES].to_numpy(dtype=np.float64), add_features(hist, tgt)[FEATURES].to_numpy(dtype=np.float64)).all()
    assert set(LIVE_COLS_V2) == set(EX_COLS + FH.HIST_LIVE_COLS) and len(LIVE_COLS_V2) == 9
    # 直前情報を隠す前に履歴を作っている(= 当日行の展示が無くても過去の展示の列は変わらない)
    out_am = add_features_v2(hist, am_rows, fan=None)
    assert out_am[["ex_time", "ex_rank", "ex_diff", "wind", "wave"] + FH.HIST_LIVE_COLS].isna().all().all()
    assert not out_am[["h_exh365", "h_m_exrel120"]].isna().all().all()
    ok("add_features_v2: 学習(同じ表)と当日(hist + 当日行)で 153列、履歴は build_hist と一致、45列は add_features と一致")


# ============================================================ 実データ
def _load_entries_copy_train():
    """train.load_entries(HEAD d75435d46)の写し(比較の基準)。"""
    files = sorted(glob.glob(str(ROOT / "data" / "races" / "entries_*.csv.gz")))
    df = pd.concat([pd.read_csv(f, dtype={"date": str}) for f in files], ignore_index=True)
    df = df[~df["abnormal"].isin(["K0", "K1"])]
    return df.drop_duplicates(subset=["date", "venue", "race_no", "lane"])


def _load_hist_copy_predict():
    """predict_today.load_hist(HEAD d75435d46)の写し(比較の基準)。"""
    files = sorted(glob.glob(str(ROOT / "data" / "races" / "entries_*.csv.gz")))
    df = pd.concat([pd.read_csv(f, dtype={"date": str}) for f in files], ignore_index=True)
    return df[~df["abnormal"].isin(["K0", "K1"])]


def frames_equal(a, b):
    if a.shape != b.shape or list(a.columns) != list(b.columns):
        return False
    if not (a.index.to_numpy() == b.index.to_numpy()).all():
        return False
    return a.equals(b)


def test_entries_io():
    say("(e) entries_io.load_entries と今の読み込みの比較")
    mine = entries_io.load_entries()
    n = len(mine)
    key = mine[["date", "venue", "race_no", "lane"]].copy()
    key["d"] = key["date"].astype(int)
    arr = key[["d", "venue", "race_no", "lane"]].to_numpy()
    o = np.lexsort((arr[:, 3], arr[:, 2], arr[:, 1], arr[:, 0]))
    assert (o == np.arange(n)).all(), "並びが 日付→会場→レース→枠 でない"
    assert not mine.duplicated(subset=["date", "venue", "race_no", "lane"]).any()
    assert not mine["abnormal"].isin(["K0", "K1"]).any()
    say("  rows=%d days=%d %s-%s" % (n, mine["date"].nunique(), mine["date"].min(), mine["date"].max()))
    # 今の train.load_entries と同じ行・同じ順・同じ index・同じ型(2026年分だけでなく全部)
    ref = None
    try:
        import train
        ref = train.load_entries()
        label = "train.load_entries"
    except Exception as e:
        print("note: train を読めない(%s)。HEAD の写しと比べる" % e, flush=True)
    if ref is None or ref.shape != mine.shape:
        if ref is not None:
            print("note: train.load_entries の形が違う(%s)。HEAD の写しとも比べる" % (ref.shape,), flush=True)
        ref = _load_entries_copy_train()
        label = "train.load_entries(HEAD の写し)"
    assert frames_equal(mine, ref), "entries_io.load_entries != " + label
    ok("entries_io.load_entries == %s(全行・同じ順・同じ index・同じ型)" % label)
    del ref
    ref = None
    try:
        import predict_today
        ref = predict_today.load_hist()
        label = "predict_today.load_hist"
    except Exception as e:
        print("note: predict_today を読めない(%s)。HEAD の写しと比べる" % e, flush=True)
    if ref is None or ref.shape != mine.shape:
        ref = _load_hist_copy_predict()
        label = "predict_today.load_hist(HEAD の写し)"
    assert frames_equal(mine, ref), "entries_io.load_entries != " + label
    ok("entries_io.load_entries == %s(全行・同じ順)" % label)
    del ref
    # 2026年分だけの比較(指示どおり明示)
    m26 = mine[mine["date"] >= "20260101"]
    r26 = _load_entries_copy_train()
    r26 = r26[r26["date"] >= "20260101"]
    assert frames_equal(m26, r26)
    ok("entries_io.load_entries: 2026年分が今の読み込みと同じ行・同じ順")
    # before_ymd: その日より前だけ(年の後ろのファイルは読まない)
    b = entries_io.load_entries(before_ymd="20220101")
    assert frames_equal(b, mine[mine["date"] < "20220101"])
    b = entries_io.load_entries(before_ymd=20211001)
    assert frames_equal(b, mine[mine["date"] < "20211001"])
    ok("entries_io.load_entries(before_ymd): その日より前の行だけ")
    return mine


def test_real(df_all):
    LAST = "20261004"          # 実験のデータの最終日(features.pkl と比べられるのはここまで)
    keep = [c for c in df_all.columns if c not in ("name", "branch", "pay3t_combo", "pay3t_amount", "pay2t_combo", "pay2t_amount")]
    df = df_all.loc[df_all["date"] <= LAST, keep]
    del df_all
    N = len(df)
    say("実データ: %d 行 (<= %s)" % (N, LAST))
    cnt = df.groupby(["date", "venue", "race_no"], sort=False).size()
    say("  races=%d not6=%d" % (len(cnt), int((cnt != 6).sum())))
    del cnt

    # ---- 全期間(基準)
    t = time.time()
    F_full = FH.build_hist(df, log=lambda m: None)
    say("  build(全期間) %.1fs" % (time.time() - t))
    assert len(F_full) == N and (F_full.index == df.index).all()
    nan_rate = F_full[FH.HIST_COLS].isna().mean()
    say("  欠損率の目安: h_elo %.4f h_pl_top2_365 %.4f h_m_age %.4f h_s_pos %.4f h_pos5 %.4f h_vrt_win365 %.4f" % tuple(
        nan_rate[c] for c in ("h_elo", "h_pl_top2_365", "h_m_age", "h_s_pos", "h_pos5", "h_vrt_win365")))
    assert nan_rate["h_elo"] == 0 and 0.005 < nan_rate["h_pl_top2_365"] < 0.012 and 0.08 < nan_rate["h_m_age"] < 0.11 \
        and 0.17 < nan_rate["h_s_pos"] < 0.21, "欠損率が実験(candidate_spec.md 2章)の値から外れた"
    ok("build_hist(全期間): 欠損率が実験の記録と同じ水準")

    D1, D2, D3 = "20261004", "20261001", "20260928"
    days_a = ["20261001", "20261002", "20261003", "20261004"]
    date = df["date"].to_numpy()
    m_a = np.isin(date, days_a)
    ref_a = F_full.loc[m_a, FH.HIST_COLS].to_numpy(np.float32)
    ref_d = {D: F_full.loc[date == D] for D in (D1, D2, D3)}
    del F_full

    # ---- (a) 実験の保存値との一致
    fp = REBUILD / "history" / "features.pkl"
    cj = REBUILD / "candidate" / "cols.json"
    if cj.exists():
        c = json.loads(cj.read_text(encoding="utf-8"))
        assert c["hist"] == FH.HIST_COLS and c["cols"][45:153] == FH.HIST_COLS and c["cols"][:45] == FEATURES
        assert c["cols"][:153] == FEATURES_V2
        assert [x for x in c["ex_dep"] if not x.startswith("c_")] == LIVE_COLS_V2
        ok("(a) HIST_COLS / FEATURES_V2 / LIVE_COLS_V2 が candidate/cols.json の名前と順に一致")
    else:
        print("SKIP (a) cols.json が無い: %s" % cj, flush=True)
    if fp.exists():
        H = pd.read_pickle(fp)
        assert len(H) == N, "features.pkl の行数 %d != CSV(<= %s) %d。データの取り直し後は比べられない" % (len(H), LAST, N)
        bp = REBUILD / "base.pkl"
        if bp.exists():
            b = pd.read_pickle(bp)[["ymd", "venue", "race_no", "lane"]]
            kb = b.to_numpy(np.int64)
            del b
            km = np.stack([date.astype(np.int64), df["venue"].to_numpy(np.int64), df["race_no"].to_numpy(np.int64),
                           df["lane"].to_numpy(np.int64)], 1)
            assert (kb == km).all(), "base.pkl と CSV の行の並びが違う"
            del kb, km
            ok("(a) 実験の表(base.pkl)と CSV の行の並びが同じ")
        exp_a = H.loc[m_a, FH.HIST_COLS].to_numpy(np.float32)
        del H
        nan_mis = int((np.isnan(exp_a) != np.isnan(ref_a)).sum())
        d = np.abs(exp_a.astype(np.float64) - ref_a.astype(np.float64))
        d = np.where(np.isnan(d), 0.0, d)
        worst = sorted(((float(d[:, j].max()), c) for j, c in enumerate(FH.HIST_COLS)), reverse=True)[:5]
        n_exact = int(((exp_a == ref_a) | (np.isnan(exp_a) & np.isnan(ref_a))).sum())
        say("  (a) %d 行 x 108 列: NaN の位置の不一致 %d、差の最大 %.3g、完全一致 %d/%d、差の大きい列 %s" % (
            int(m_a.sum()), nan_mis, float(d.max()), n_exact, d.size, worst))
        assert nan_mis == 0 and float(d.max()) <= 1e-5
        ok("(a) 実験の保存値(float32 入口)と CSV(float64 入口)の値: %s の行で差の最大 %.2g <= 1e-5" % (
            "〜".join([days_a[0], days_a[-1]]), float(d.max())))
        del exp_a, d
    else:
        print("SKIP (a) features.pkl が無い: %s" % fp, flush=True)
    del ref_a

    # ---- (b) 結果を消した表から作った当日の行 = 全期間の同じ行(2日)
    for D in (D1, D2):
        t = time.time()
        sub = df[date <= D].copy()
        m = (sub["date"] == D).to_numpy()
        for c in ("pos", "abnormal", "course", "st", "kimarite"):
            sub.loc[m, c] = np.nan
        assert sub.loc[m, "pos"].isna().all() and sub.loc[m, "abnormal"].isna().all()
        Fd = FH.build_hist(sub, log=lambda m: None)
        got = Fd.loc[m, FH.HIST_COLS].to_numpy()
        ref = ref_d[D][FH.HIST_COLS].to_numpy()
        bad = mismatch_cols(got, ref, FH.HIST_COLS)
        say("  (b) %s: %d 行、%d 値、不一致 %s  (%.1fs)" % (D, int(m.sum()), got.size, bad or 0, time.time() - t))
        assert not bad, bad
        # 中間値(float64)は表が違うと最後の数ビットが動き得る(_Hist.wsum が全体の累積和の差で、他の選手の
        # 後の日の行が接頭の和に入るため)。float32 の108列はそれでも一致する(上の不一致0)。ここは相対 1e-9 で見る
        assert np.allclose(Fd.loc[m, "_aux_exh365"].to_numpy(), ref_d[D]["_aux_exh365"].to_numpy(), rtol=1e-9, atol=1e-12, equal_nan=True)
        del sub, Fd, got, ref
        ok("(b) %s 以降の結果を消した表から作った当日の行 = 全期間で作った同じ行(108列すべて不一致0)" % D)

    # ---- (c)(d) 当日行を races_to_rows の形(結果なし・朝 = 展示なし)で add_features_v2 に通す
    t = time.time()
    m3 = date == D3
    hist = df[date < D3]
    today = df[m3]
    am_rows = as_program_rows(today, with_live=False)
    assert not any(c in am_rows.columns for c in RESULT_COLS + LIVE_IN)
    # 番組表の順(会場ごと)ではなく、わざとレース番号→会場の順で渡す
    am_rows = am_rows.sort_values(["race_no", "venue", "lane"]).reset_index(drop=True)
    out = add_features_v2(hist, am_rows, fan=None)
    say("  (c) add_features_v2(hist<%s, %d 行の当日行) %.1fs" % (D3, len(am_rows), time.time() - t))
    assert len(out) == len(am_rows) and (out.index == am_rows.index).all()
    assert all(c in out.columns for c in FEATURES_V2 + FH.HIST_AUX_COLS)
    key_ref = {tuple(k): i for i, k in enumerate(map(tuple, today[["venue", "race_no", "lane"]].to_numpy()))}
    al = [key_ref[tuple(k)] for k in am_rows[["venue", "race_no", "lane"]].to_numpy()]
    ref3 = ref_d[D3].iloc[al]
    nonlive = [c for c in FH.HIST_COLS if c not in FH.HIST_LIVE_COLS]
    bad = mismatch_cols(out[nonlive].to_numpy(), ref3[nonlive].to_numpy(), nonlive)
    assert not bad, bad
    assert out[FH.HIST_LIVE_COLS].isna().all().all() and out[EX_COLS].isna().all().all()
    assert np.allclose(out["_aux_exh365"].to_numpy(), ref3["_aux_exh365"].to_numpy(), rtol=1e-9, atol=1e-12, equal_nan=True)
    ok("(c) %s: races_to_rows の形の当日行(結果なし・展示なし)で 104列が全期間の値と一致、直前の9列は NaN" % D3)
    # (d) 展示を入れて4列を作る = 全期間の4列。式と型は build_hist と同じなので、同じ表から作った値とはビット一致
    # (合成データの検算)。ここは「全期間の表」との比較なので、中間値の最後のビットの差(上の注記)が float32 の
    # 1ビットに出ることが原理上あり得る。1 ulp(相対 1.2e-7)以内を合格にし、完全一致の数も出す
    ex = today["ex_time"].to_numpy()[al]
    upd = FH.live_hist_update(ex, out)
    a, b = upd.to_numpy(np.float64), ref3[FH.HIST_LIVE_COLS].to_numpy(np.float64)
    n_exact = int(same_or_nan(a, b).sum())
    assert (np.isnan(a) == np.isnan(b)).all()
    assert np.allclose(a, b, rtol=1.2e-7, atol=0, equal_nan=True)
    say("  (d) %s: %d 値、完全一致 %d、差の最大 %.3g" % (D3, a.size, n_exact, float(np.nanmax(np.abs(a - b)))))
    ok("(d) %s: live_hist_update(朝の中間値 + 展示)= 全期間の4列(%d 値、1 ulp 以内、完全一致 %d)" % (D3, a.size, n_exact))
    # 保存の形(float32 の153列 + 中間値)から引いても同じ(こちらは同じ中間値からなのでビット一致)
    X = out[FEATURES_V2].to_numpy(np.float32)
    cache = {"h_m_exrel120": X[:, FEATURES_V2.index("h_m_exrel120")], "_aux_exh365": out["_aux_exh365"].to_numpy()}
    upd2 = FH.live_hist_update(ex, cache)
    assert not mismatch_cols(upd2.to_numpy(), upd.to_numpy(), FH.HIST_LIVE_COLS)
    ok("(d) 保存の形(float32 行列 + 中間値の dict)から作っても同じ(ビット一致)")


def main():
    test_synthetic()
    if QUICK:
        print("(--quick: 実データの検算は省いた)")
    else:
        df_all = test_entries_io()
        test_real(df_all)
    print("%d checks passed" % _n_ok)
    print("ALL OK")


if __name__ == "__main__":
    main()

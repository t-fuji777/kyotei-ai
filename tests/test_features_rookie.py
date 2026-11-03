# -*- coding: utf-8 -*-
"""履歴に無い選手(デビュー戦の新人)の特徴量が、学習時の表と予測の経路で同じになることの確認。

2026-11-01 の新人デビューから、daily の自己検査(train.py self_check)が毎回「NaN 位置の不一致」で落ちて
モデルが配布されなくなった。原因: features.add_features は hist に無い選手を飛ばして 45 列すべて NaN に
していたが、学習時の表(全行から作る)では自分の当日の行があるので「前日までの出走数」の4列
(r_n365 / r_n90 / r_lane_n365 / r_venue_n365)が 0 になる。予測の経路(hist = 前日まで)でも同じ 0 にした。
合成データで、新人の行の 45 列が両経路で一致する(値か、両方 NaN)ことを確かめる。実行: python tests/test_features_rookie.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from features import add_features, FEATURES  # noqa: E402
from test_features_hist import make_synthetic, same_or_nan  # noqa: E402

COUNT_COLS = ["r_n365", "r_n90", "r_lane_n365", "r_venue_n365"]


def main():
    df = make_synthetic(n_days=30, venues=(3,), races_per_day=3, n_racers=24, seed=1)
    D = df["date"].max()
    full = df.copy()
    is_day = full["date"] == D
    # 最終日の 2 レースに、履歴に1行も無い新人(登番 9001, 9002)を入れる。9001 は同じ日に2回走る(実データと同じ)
    day_idx = full.index[is_day]
    full.loc[day_idx[1], "toban"] = 9001      # 1R 2枠
    full.loc[day_idx[10], "toban"] = 9001     # 2R 5枠
    full.loc[day_idx[14], "toban"] = 9002     # 3R 3枠
    hist = full[~is_day]
    day = full[is_day].copy()
    assert not hist["toban"].isin([9001, 9002]).any()

    tr = add_features(full, full).loc[day.index, FEATURES].to_numpy(dtype=np.float64)   # 学習時の表(全行)
    pr = add_features(hist, day)[FEATURES].to_numpy(dtype=np.float64)                 # 予測の経路(履歴+当日)
    eq = same_or_nan(tr, pr)
    bad = {FEATURES[j]: int((~eq[:, j]).sum()) for j in range(len(FEATURES)) if (~eq[:, j]).any()}
    assert not bad, f"学習時の表と予測の経路で食い違う列: {bad}"

    rk = day["toban"].isin([9001, 9002]).to_numpy()
    assert rk.sum() == 3
    for c in COUNT_COLS:
        j = FEATURES.index(c)
        assert (pr[rk, j] == 0).all() and (tr[rk, j] == 0).all(), (c, pr[rk, j], tr[rk, j])
    for c in ("r_win365", "r_win90", "r_lane_win365", "r_venue_win365", "r_avgpos10", "r_avg_st180"):
        j = FEATURES.index(c)
        assert np.isnan(pr[rk, j]).all() and np.isnan(tr[rk, j]).all(), c
    # 新人でない行は今までどおり(出走数 > 0)
    j = FEATURES.index("r_n365")
    assert (pr[~rk, j] > 0).all()
    print("ok   新人(履歴に無い選手)の 45 列が学習時の表と予測の経路で一致し、出走数の4列は 0、率の列は NaN")
    print("ALL OK")


if __name__ == "__main__":
    main()

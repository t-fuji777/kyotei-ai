# -*- coding: utf-8 -*-
"""scripts/model_v2.py(世代2のモデル: 条件づけ分解ロジット)の検算。

実行: PYTHONUTF8=1 python -X utf8 tests/test_model_v2.py [--full]

(a) 保存済みの候補モデル(実験フォルダ rebuild/)で、156行の点数と120通りの確率が実験の保存値と一致する
    (差 1e-6 以下)。保存済みの点数から試験期間の成績(上位5点 0.3685・1着 0.5776・nll3 3.70691)を数え直す。
    --full を付けると試験期間の全レース(29,508)を自分で採点して同じ数字になることも確かめる(約1分)。
    実験フォルダは環境変数 KYOTEI_REBUILD_DIR(無ければ作業時の scratchpad の場所)。見つからなければ (a) を飛ばす。
(b) stack_train / stack_predict の行の並び・ラベル・段の特徴が実験の clib と一致する(小さな合成レース)。
(c) 独自目的関数の勾配・ヘシアン・評価関数が clib と一致する。
(d) 小さな合成データで fit が動き、早期打ち切りが効く。fit と fit_races(メモリ節約版)が同じ結果になり、
    保存して読み直しても点数が同じ。
(e) win_probs の合計1、top2_probs の合計2、top3_probs の合計3。
"""
import json
import os
import shutil
import sys
import time
import types
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import model_v2 as M  # noqa: E402

REBUILD = Path(os.environ.get(
    "KYOTEI_REBUILD_DIR",
    r"C:\Users\TABF11~1.FUJ\AppData\Local\Temp\claude\C--Users-ta-fujino-Documents-kyotei-ai-main"
    r"\467be6e3-74fd-4564-9509-0bfe1e8aee91\scratchpad\rebuild"))
FULL = "--full" in sys.argv
TMP = ROOT / f".tmp_model_v2_{os.getpid()}"       # 実行ごとに一意(同時に走る他の実行の一時フォルダを消さない)
T0 = time.time()


def say(msg):
    print("[%5.1fs] %s" % (time.time() - T0, msg), flush=True)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    say("ok  " + msg)


def maxdiff(a, b):
    return float(np.nanmax(np.abs(np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64))))


def eq_nan(a, b):
    return np.array_equal(np.asarray(a), np.asarray(b), equal_nan=True)


def perm_index(a, b, c):
    """PERMS の位置の閉じた式(candidate_spec.md 5.3)。"""
    return a * 20 + (b - (b > a)) * 4 + (c - (c > a) - (c > b))


# ------------------------------------------------------------ 合成レース
def synth_races(rng, R, F=M.N_FEAT, nan_rate=0.05):
    """枠1〜6の順の (R,6,F) float32 と、1〜3着の枠 (R,3)。強さは列6・列12と枠で決め、PL 型で着順を引く。"""
    X6 = rng.normal(size=(R, 6, F)).astype(np.float32)
    X6[rng.random(size=(R, 6, F)) < nan_rate] = np.nan            # 直前情報を隠した行のまね
    X6[:, :, 0] = rng.integers(1, 25, size=(R, 1))                 # venue
    X6[:, :, 1] = rng.integers(1, 13, size=(R, 1))                 # race_no
    X6[:, :, 2] = np.arange(1, 7)                                   # lane
    X6[:, :, 5] = rng.integers(0, 5, size=(R, 6))                   # class_i
    if F > 30:
        X6[:, :, 30] = rng.integers(0, 3, size=(R, 1))              # v_water
    s6 = np.nan_to_num(X6[:, :, 6].astype(np.float64))
    s12 = np.nan_to_num(X6[:, :, 12 if F > 12 else 6].astype(np.float64))
    strength = 1.2 * s6 + 0.6 * s12 - 0.25 * np.arange(6)
    full = np.argsort(-(strength + rng.gumbel(size=(R, 6))), axis=1)
    return X6, full[:, :3].astype(np.int64)


def race_rows_spec(x6):
    """candidate_spec.md 5.1 の 156 行(仕様書から独立に書いた参照)。"""
    F = x6.shape[1]
    out = np.empty((156, F + 6), dtype=np.float32)
    i = 0
    for lane in range(6):
        out[i, :F] = x6[lane]
        out[i, F:] = [1, 0, 0, np.nan, np.nan, lane]
        i += 1
    for a, b in M.PAIRS:
        out[i, :F] = x6[b]
        out[i, F:] = [2, a + 1, 0, b - a, np.nan, b - (a < b)]
        i += 1
    for a, b, c in M.PERMS:
        out[i, :F] = x6[c]
        out[i, F:] = [3, a + 1, b + 1, c - a, c - b, c - (a < c) - (b < c)]
        i += 1
    return out


def to_P_spec(S, absent6=None, drop=-60.0):
    """candidate_spec.md 5.2 の式(仕様書から独立に書いた参照)。"""
    S = np.asarray(S, dtype=np.float64)
    S1 = S[:, :6].copy()
    S2 = S[:, 6:36].copy().reshape(-1, 6, 5)
    S3 = S[:, 36:].copy().reshape(-1, 6, 5, 4)
    if absent6 is not None:
        S1[absent6] = drop
        S2[absent6[:, [b for a, b in M.PAIRS]].reshape(-1, 6, 5)] = drop
        S3[absent6[:, [c for a, b, c in M.PERMS]].reshape(-1, 6, 5, 4)] = drop

    def sm(x):
        x = x - x.max(-1, keepdims=True)
        p = np.exp(x)
        return p / p.sum(-1, keepdims=True)
    p1, p2, p3 = sm(S1), sm(S2), sm(S3)
    return (p1[:, :, None, None] * p2[:, :, :, None] * p3).reshape(-1, 120), p1


def import_clib():
    """実験の clib を読むだけで使う(import 時に runs/ の mkdir(exist_ok) をするだけで、他に書き込みは無い)。"""
    sys.path.insert(0, str(REBUILD / "candidate"))
    import clib  # noqa: E402
    return clib


# ============================================================ 定数
def test_constants():
    say("--- constants")
    P = M.PERMS
    check(P.shape == (120, 3) and len(set(map(tuple, P))) == 120, "PERMS: 120 distinct")
    check(tuple(P[0]) == (0, 1, 2) and tuple(P[1]) == (0, 1, 3) and tuple(P[-1]) == (5, 4, 3), "PERMS: first/last")
    check(all(perm_index(a, b, c) == i for i, (a, b, c) in enumerate(P)), "PERMS: closed-form position")
    check((M.PAIRS[M.PERM_PAIR] == P[:, :2]).all(), "PERM_PAIR maps each perm to its (a,b) pair")
    check((M.PAIRS.reshape(6, 5, 2)[:, :, 0] == np.arange(6)[:, None]).all(), "PAIRS: groups of 5 share a")
    check((P.reshape(30, 4, 3)[:, :, :2] == M.PAIRS[:, None, :]).all(), "PERMS: groups of 4 share (a,b) in PAIRS order")
    check(M.COMBO_STR[0] == "1-2-3" and M.COMBO_STR[-1] == "6-5-4" and len(M.COMBO_STR) == 120, "COMBO_STR 1-based")
    check(M.STAGE_COLS == ["stage", "win_lane", "sec_lane", "rel1", "rel2", "n_in"], "STAGE_COLS order")
    check(M.CAT_IDX == [0, 2, 5, 30, 154, 155], "CAT_IDX")
    want = dict(learning_rate=0.05, num_leaves=31, min_data_in_leaf=200, feature_fraction=0.6, lambda_l2=10.0,
                bagging_fraction=0.8, bagging_freq=1, seed=42)
    check(all(M.PARAMS_V2[k] == v for k, v in want.items()), "PARAMS_V2 values")
    check("num_threads" not in M.PARAMS_V2 and "objective" not in M.PARAMS_V2, "PARAMS_V2 has no num_threads/objective")
    check(M.ROUNDS_V2 == 4000 and M.EARLY_STOP_V2 == 100 and M.ABSENT_DROP == -60.0, "rounds / early stop / drop")
    # meta.json に書く「モデルの設定」: 実行環境の項目(num_threads / force_col_wise / verbosity)は入らない
    mp = M.model_params({"num_threads": 6, "verbosity": 1})
    check(all(k not in mp for k in M.RUNTIME_PARAM_KEYS) and all(mp[k] == v for k, v in want.items()),
          "model_params drops runtime keys (num_threads etc.) and keeps the model settings")
    check(M.model_params({"learning_rate": 0.1})["learning_rate"] == 0.1 and M.model_params(None) == M.model_params({"num_threads": 2}),
          "model_params keeps caller overrides of model settings, ignores num_threads")
    try:
        from train import trifecta_probs
        keys = list(trifecta_probs(np.ones(6), np.ones(6), np.ones(6)).keys())
        check(keys == [tuple(int(x) for x in p) for p in P], "PERMS order == train.trifecta_probs dict order")
    except ImportError as e:  # train.py は別の担当が編集中。読めない時だけ飛ばす
        say("SKIP train.trifecta_probs comparison (%s)" % e)


# ============================================================ (b) 行の積み上げ
def test_stacking():
    say("--- (b) stacking vs clib")
    rng = np.random.default_rng(1)
    R, F = 37, 9
    X6, order = synth_races(rng, R, F=F)
    X, y, blocks = M.stack_train(X6, order)
    check(X.shape == (R * 15, F + 6) and X.dtype == np.float32 and y.shape == (R * 15,), "stack_train shapes")
    check(blocks == [(0, R, 6), (R * 6, R, 5), (R * 11, R, 4)], "stack_train blocks")
    # 段ごとのラベルは各レースでちょうど1つ
    for s, n, k in blocks:
        check((y[s:s + n * k].reshape(n, k).sum(1) == 1).all(), "labels: one per race in stage k=%d" % k)
    # 1着の段の正解は a1、2着の段の行の艇は a1 を含まない、3着の段は a1・a2 を含まない
    lane1 = X[:R * 6, 2].reshape(R, 6) - 1
    check((lane1[np.arange(R), order[:, 0]] == order[:, 0]).all() and
          (y[:R * 6].reshape(R, 6).argmax(1) == order[:, 0]).all(), "stage1 label = winner")
    lane2 = X[R * 6:R * 11, 2].reshape(R, 5) - 1
    check((lane2 != order[:, 0][:, None]).all() and (lane2[np.arange(R), y[R * 6:R * 11].reshape(R, 5).argmax(1)] == order[:, 1]).all(),
          "stage2 rows exclude winner, label = second")
    lane3 = X[R * 11:, 2].reshape(R, 4) - 1
    check((lane3 != order[:, 0][:, None]).all() and (lane3 != order[:, 1][:, None]).all() and
          (lane3[np.arange(R), y[R * 11:].reshape(R, 4).argmax(1)] == order[:, 2]).all(),
          "stage3 rows exclude 1st/2nd, label = third")
    # 段の特徴の値(仕様 4.3 の表)
    st = X[:, F:]
    check((st[:R * 6, 0] == 1).all() and (st[:R * 6, 1] == 0).all() and (st[:R * 6, 2] == 0).all() and
          np.isnan(st[:R * 6, 3]).all() and np.isnan(st[:R * 6, 4]).all() and (st[:R * 6, 5] == lane1.ravel()).all(),
          "stage1 features: stage 1, win/sec 0, rel NaN, n_in = lane")
    w1 = np.repeat(order[:, 0], 5)
    check((st[R * 6:R * 11, 1] == w1 + 1).all() and (st[R * 6:R * 11, 3] == lane2.ravel() - w1).all() and
          np.isnan(st[R * 6:R * 11, 4]).all() and (st[R * 6:R * 11, 5] == lane2.ravel() - (w1 < lane2.ravel())).all(),
          "stage2 features: win_lane, rel1, n_in")
    w1 = np.repeat(order[:, 0], 4)
    w2 = np.repeat(order[:, 1], 4)
    l3 = lane3.ravel()
    check((st[R * 11:, 1] == w1 + 1).all() and (st[R * 11:, 2] == w2 + 1).all() and (st[R * 11:, 3] == l3 - w1).all() and
          (st[R * 11:, 4] == l3 - w2).all() and (st[R * 11:, 5] == l3 - (w1 < l3) - (w2 < l3)).all(),
          "stage3 features: win_lane, sec_lane, rel1, rel2, n_in")
    # (R*6,F) の2次元入力でも同じ
    X2, y2, b2 = M.stack_train(X6.reshape(R * 6, F), order)
    check(eq_nan(X, X2) and np.array_equal(y, y2) and b2 == blocks, "stack_train accepts (rows, F) input")
    # idx(位置)で積む = X6[idx] を渡して積む(写しを作らない経路。昇順でなくてもよい)
    sel = rng.permutation(R)[:21]
    Xi, yi, bi = M.stack_train(X6, order, idx=sel)
    Xs, ys, bs = M.stack_train(X6[sel], order[sel])
    check(eq_nan(Xi, Xs) and np.array_equal(yi, ys) and bi == bs == [(0, 21, 6), (21 * 6, 21, 5), (21 * 11, 21, 4)],
          "stack_train(idx=sel) == stack_train(X6[sel], order[sel])")
    try:
        M.stack_train(X6, order[sel], idx=sel)
        raise RuntimeError("no assert")
    except AssertionError:
        say("ok  stack_train(idx=...) requires order of the full length")
    # 予測の156行: 仕様書の参照と一致
    Xp = M.stack_predict(X6)
    check(Xp.shape == (R * 156, F + 6) and Xp.dtype == np.float32, "stack_predict shape")
    ref = np.concatenate([race_rows_spec(X6[i]) for i in range(R)])
    check(eq_nan(Xp, ref), "stack_predict == spec race_rows (all 156 rows x %d races)" % R)
    check(eq_nan(M.stack_predict(X6.reshape(R * 6, F)), ref), "stack_predict accepts (rows, F) input")
    # 6艇そろわない入力は assert で止まる
    try:
        M.stack_predict(X6.reshape(R * 6, F)[:-1])
        raise RuntimeError("no assert")
    except AssertionError:
        say("ok  stack_predict rejects rows not multiple of 6")
    try:
        M.stack_train(X6, np.array([[0, 0, 1]] * R))
        raise RuntimeError("no assert")
    except AssertionError:
        say("ok  stack_train rejects duplicate lanes in order")

    if not (REBUILD / "candidate" / "clib.py").exists():
        say("SKIP clib comparison (rebuild not found: %s)" % REBUILD)
        return
    clib = import_clib()
    D = types.SimpleNamespace(X=X6.reshape(R * 6, F), F=F, a1=order[:, 0], a2=order[:, 1], a3=order[:, 2])
    Xc, yc, bc = clib.stacked_set(D, np.arange(R))
    check(eq_nan(X, Xc) and np.array_equal(y, yc) and [tuple(b) for b in bc] == blocks,
          "stack_train == clib.stacked_set (rows, labels, blocks)")
    # 予測の行: clib.stacked_scores と同じ作り(_fill)で段ごとのかたまりを作り、レースごとの並びに直して比べる
    n = R
    out = np.empty((n * 156, F + 6), dtype=np.float32)
    o = 0
    rr = np.arange(n)
    for stage, cmb in ((1, None), (2, clib.PAIRS), (3, clib.PERMS)):
        k = 6 if cmb is None else len(cmb)
        race = np.repeat(rr, k)
        if stage == 1:
            lane, w1, w2 = np.tile(np.arange(6), n), None, None
        elif stage == 2:
            lane, w1, w2 = np.tile(cmb[:, 1], n), np.tile(cmb[:, 0], n), None
        else:
            lane, w1, w2 = np.tile(cmb[:, 2], n), np.tile(cmb[:, 0], n), np.tile(cmb[:, 1], n)
        clib._fill(out[o:o + n * k], D.X, race * 6 + lane, lane, stage, w1, w2)
        o += n * k
    per_race = np.concatenate([out[:n * 6].reshape(n, 6, -1), out[n * 6:n * 36].reshape(n, 30, -1),
                               out[n * 36:].reshape(n, 120, -1)], axis=1).reshape(n * 156, F + 6)
    check(eq_nan(Xp, per_race), "stack_predict == clib._fill construction (stacked_scores order)")
    check((clib.PERMS == M.PERMS).all() and (clib.PAIRS == M.PAIRS).all() and (clib.PERM_PAIR == M.PERM_PAIR).all(),
          "PERMS / PAIRS / PERM_PAIR == clib")


# ============================================================ (c) 目的関数
def test_objective():
    say("--- (c) objective vs clib")
    rng = np.random.default_rng(2)
    R, F = 23, 7
    X6, order = synth_races(rng, R, F=F)
    _, y, blocks = M.stack_train(X6, order)
    preds = rng.normal(size=len(y)) * 2.0
    ds = types.SimpleNamespace(get_label=lambda: y)
    g, h = M.make_objective(blocks)(preds, ds)
    name, val, hib = M.make_feval(blocks)(preds, ds)
    # 自前の式(段ごとのソフトマックス)
    g2 = np.empty_like(preds)
    h2 = np.empty_like(preds)
    tot = 0.0
    for s, n, k in blocks:
        z = preds[s:s + n * k].reshape(n, k)
        p = np.exp(z - z.max(1, keepdims=True))
        p /= p.sum(1, keepdims=True)
        yy = y[s:s + n * k].reshape(n, k)
        g2[s:s + n * k] = (p - yy).ravel()
        h2[s:s + n * k] = np.maximum(p * (1 - p), 1e-6).ravel()
        tot += float(-np.log((p * yy).sum(1)).mean())
    check(np.allclose(g, g2, atol=1e-12) and np.allclose(h, h2, atol=1e-12), "gradient p-y, hessian max(p(1-p),1e-6)")
    check(name == "nll3" and hib is False and abs(val - tot) < 1e-10, "feval nll3 = sum of 3 stage mean -log p")
    check((h >= 1e-6).all(), "hessian floor 1e-6")
    # 取り違え(別の行数)は止まる
    try:
        M.make_objective(blocks)(preds[:-1], types.SimpleNamespace(get_label=lambda: y[:-1]))
        raise RuntimeError("no assert")
    except AssertionError:
        say("ok  objective rejects preds whose length does not match blocks")
    if not (REBUILD / "candidate" / "clib.py").exists():
        say("SKIP clib comparison (rebuild not found)")
        return
    clib = import_clib()
    obj_c, fe_c = clib.block_objective({len(y): blocks})
    gc, hc = obj_c(preds, ds)
    _, vc, _ = fe_c(preds, ds)
    check(np.array_equal(g, gc) and np.array_equal(h, hc), "gradient/hessian bit-identical to clib.block_objective")
    check(val == vc, "feval identical to clib (%.12f)" % vc)


# ============================================================ (e) 1艇ごとの数字
def test_marginals(P=None, label="random"):
    say("--- (e) marginals (%s)" % label)
    if P is None:
        rng = np.random.default_rng(3)
        S = rng.normal(size=(50, 156)) * 1.5
        P, p1 = M.scores_to_P(S)
        check(maxdiff(M.win_probs(P), p1) < 1e-12, "win_probs(P) == p1 from scores_to_P")
    pw, p2, p3 = M.win_probs(P), M.top2_probs(P), M.top3_probs(P)
    check(maxdiff(P.sum(1), 1.0) < 1e-12, "P sums to 1")
    check(maxdiff(pw.sum(1), 1.0) < 1e-12, "win_probs sums to 1")
    check(maxdiff(p2.sum(1), 2.0) < 1e-12, "top2_probs sums to 2")
    check(maxdiff(p3.sum(1), 3.0) < 1e-12, "top3_probs sums to 3")
    check((pw <= p2 + 1e-15).all() and (p2 <= p3 + 1e-15).all(), "win <= top2 <= top3 per boat")
    # 足し上げの定義どおりか(ループで数える)
    pw2, p22, p32 = np.zeros_like(pw), np.zeros_like(pw), np.zeros_like(pw)
    for i, (a, b, c) in enumerate(M.PERMS):
        pw2[:, a] += P[:, i]
        p22[:, a] += P[:, i]
        p22[:, b] += P[:, i]
        p32[:, a] += P[:, i]
        p32[:, b] += P[:, i]
        p32[:, c] += P[:, i]
    check(maxdiff(pw, pw2) < 1e-14 and maxdiff(p2, p22) < 1e-14 and maxdiff(p3, p32) < 1e-14, "marginals == loop sums")
    if label == "random":
        # 欠場艇: 1艇を落とすとその艇を含む買い目がほぼ0になり、残りで合計1
        ab = np.zeros((50, 6), dtype=bool)
        ab[:, 3] = True
        Pa, p1a = M.scores_to_P(S, ab)
        has3 = (M.PERMS == 3).any(1)
        check(Pa[:, has3].max() < 1e-20 and maxdiff(Pa.sum(1), 1.0) < 1e-12 and p1a[:, 3].max() < 1e-20,
              "absent_mask drops lane 4 from all stages, P still sums to 1")
        check(maxdiff(M.scores_to_P(S, np.zeros((50, 6), dtype=bool))[0], P) == 0.0, "all-false absent_mask changes nothing")
        check(maxdiff(to_P_spec(S, ab)[0], Pa) < 1e-15, "scores_to_P with mask == spec to_P")


# ============================================================ (d) 合成データで学習
def test_fit():
    say("--- (d) fit on synthetic data")
    import lightgbm as lgb
    rng = np.random.default_rng(4)
    X6_tr, o_tr = synth_races(rng, 3000)
    X6_va, o_va = synth_races(rng, 600)
    Xtr, ytr, btr = M.stack_train(X6_tr, o_tr)
    Xva, yva, bva = M.stack_train(X6_va, o_va)
    rounds, es = 1500, M.EARLY_STOP_V2
    params = dict(num_threads=4)
    logs = []
    b1 = M.fit(Xtr, ytr, btr, Xva, yva, bva, params=params, rounds=rounds, early_stop=es, log=logs.append, log_every=50)
    for line in logs:
        say("    " + line)
    best = b1.best_iteration
    check(0 < best < rounds, "early stopping triggered: best_iteration %d (< %d)" % (best, rounds))
    check(best <= b1.num_trees() <= best + es, "trees kept %d within [best, best+%d]" % (b1.num_trees(), es))
    nll_es = b1.best_score["valid_0"]["nll3"]
    check(nll_es < np.log(120), "valid nll3 %.4f better than uniform %.4f" % (nll_es, np.log(120)))
    # 検証の nll3 を自分の predict_P から数え直す(段の積 = 3連単の確率なので一致するはず)
    P_va = M.predict_P(b1, X6_va)
    idx = perm_index(o_va[:, 0], o_va[:, 1], o_va[:, 2])
    nll_mine = float(-np.log(P_va[np.arange(len(P_va)), idx]).mean())
    check(abs(nll_mine - nll_es) < 1e-4, "nll3 from predict_P %.6f == early-stopping metric %.6f" % (nll_mine, nll_es))
    check(np.isfinite(P_va).all() and maxdiff(P_va.sum(1), 1.0) < 1e-12, "predict_P finite, sums to 1")
    test_marginals(P_va, label="fitted model")
    # 当たりの目は上位に来ているか(学習できている証拠)
    ranks = (P_va > P_va[np.arange(len(P_va)), idx][:, None]).sum(1) + 1
    check((ranks <= 5).mean() > 5.0 / 120 * 2, "top5 hit rate %.3f well above chance" % (ranks <= 5).mean())
    # fit_races(メモリ節約版)は同じ結果になる(Dataset を先に construct しても区切りが同じ)
    b2 = M.fit_races(X6_tr.copy(), o_tr, X6_va.copy(), o_va, params=params, rounds=rounds, early_stop=es, log=None)
    check(b2.best_iteration == best, "fit_races best_iteration == fit (%d)" % best)
    check(maxdiff(M.scores(b2, X6_va), M.scores(b1, X6_va)) == 0.0, "fit_races scores bit-identical to fit")
    # 全期間の行列 + 位置(tr_idx / va_idx)で学ぶ経路(train.main_v2 の作り: stack_train(idx) → make_datasets → fit_datasets)
    X6_all = np.concatenate([X6_va, X6_tr])            # 検証を前に置いて、位置の対応が自明でないようにする
    o_all = np.concatenate([o_va, o_tr])
    tr_i, va_i = np.arange(len(X6_va), len(X6_all)), np.arange(len(X6_va))
    b4 = M.fit_races(X6_all, o_all, X6_all, o_all, params=params, rounds=rounds, early_stop=es, log=None, tr_idx=tr_i, va_idx=va_i)
    check(b4.best_iteration == best and maxdiff(M.scores(b4, X6_va), M.scores(b1, X6_va)) == 0.0,
          "fit_races with tr_idx/va_idx on the full matrix == fit")
    Xa, ya, ba = M.stack_train(X6_all, o_all, idx=tr_i)
    Xb_, yb_, bb_ = M.stack_train(X6_all, o_all, idx=va_i)
    dtr, dva = M.make_datasets(Xa, ya, Xb_, yb_, params=params)
    del Xa, Xb_
    b5 = M.fit_datasets(dtr, dva, ba, bb_, params=params, rounds=rounds, early_stop=es, log=None)
    check(b5.best_iteration == best and maxdiff(M.scores(b5, X6_va), M.scores(b1, X6_va)) == 0.0,
          "stack_train(idx) -> make_datasets -> del -> fit_datasets == fit")
    try:
        M.fit_datasets(dtr, dva, bb_, ba, params=params, rounds=10, early_stop=5, log=None)
        raise RuntimeError("no assert")
    except AssertionError:
        say("ok  fit_datasets rejects swapped blocks")
    # 保存して読み直しても点数が同じ(打ち切りの本数で切って保存)
    TMP.mkdir(exist_ok=True)
    path = TMP / "synthetic_model.txt"
    try:
        M.save_model(b1, path)
        b3 = lgb.Booster(model_file=str(path))
        check(b3.num_trees() == best, "saved model has best_iteration trees (%d)" % b3.num_trees())
        check(maxdiff(M.scores(b3, X6_va), M.scores(b1, X6_va)) == 0.0, "reloaded model scores bit-identical")
        with open(path, "rb") as f:
            head = f.read(6)
        # predict_today.load_models と同じ検査(先頭が b"tree\r\n" なら CRLF で使えない)
        check(head[:5] == b"tree\n" and head != b"tree\r\n", "saved model starts with 'tree\\n' (LF, not CRLF)")
        check(b3.num_feature() == M.N_FEAT + M.N_STAGE, "num_feature 159")
    finally:
        path.unlink(missing_ok=True)
        if TMP.exists() and not any(TMP.iterdir()):
            TMP.rmdir()
    # 列数の違う行列をモデルに渡すと止まる
    try:
        M.scores(b1, X6_va[:, :, :-1])
        raise RuntimeError("no error")
    except ValueError:
        say("ok  scores rejects matrix whose width does not match the model")
    # 学習の入力の型の検査
    try:
        M.fit(Xtr.astype(np.float64), ytr, btr, Xva, yva, bva, log=None)
        raise RuntimeError("no assert")
    except AssertionError:
        say("ok  fit rejects float64 matrix")
    try:
        M.fit(Xtr, ytr, bva, Xva, yva, btr, log=None)
        raise RuntimeError("no assert")
    except AssertionError:
        say("ok  fit rejects swapped blocks (train/valid mix-up)")


# ============================================================ (a) 保存済みの候補モデル
def test_saved_model():
    say("--- (a) saved candidate model vs experiment")
    C = REBUILD / "candidate"
    need = [C / "cols.json", C / "meta.npz", C / "X_all.npy", C / "runs" / "F_s42_model.txt",
            C / "runs" / "F_s42.npz", C / "parts.pkl", REBUILD / "evalkit.py"]
    missing = [str(p) for p in need if not p.exists()]
    if missing:
        say("SKIP (a): rebuild files not found: %s" % missing)
        return False
    import lightgbm as lgb
    import pandas as pd
    clib = import_clib()
    sys.path.insert(0, str(REBUILD))
    import evalkit as K  # noqa: E402

    cj = json.loads((C / "cols.json").read_text(encoding="utf-8"))
    USE = cj["base"] + cj["hist"]
    F = len(USE)
    check(F == M.N_FEAT and cj["cols"][:F] == USE, "cols.json: base+hist = 153 = first 153 of X_all")
    check([USE.index(c) for c in cj["cat"]] + [F + 1, F + 2] == M.CAT_IDX, "CAT_IDX == cols.json cat + win/sec")
    EX = [USE.index(c) for c in cj["ex_dep"] if c in USE]
    check(EX == [36, 37, 38, 39, 40, 137, 138, 139, 140], "ex_dep positions")
    meta = np.load(C / "meta.npz")
    sr, sp, absent = meta["split_r"], meta["split"], meta["absent"]
    test_r = np.flatnonzero(sr == 2)
    first_test = int(test_r[0])
    ab6_all = absent.reshape(-1, 6)
    with_ab = test_r[ab6_all[test_r].any(1)][:100]
    sel = np.unique(np.concatenate([test_r[-300:], with_ab]))
    say("selected %d test races (%d with an absent boat)" % (len(sel), ab6_all[sel].any(1).sum()))
    Xm = np.load(C / "X_all.npy", mmap_mode="r")
    X6 = np.stack([np.asarray(Xm[r * 6:(r + 1) * 6, :F]) for r in sel])
    check(X6.dtype == np.float32 and X6.shape == (len(sel), 6, F), "X6 float32 (n,6,153)")
    check((X6[:, :, 2] == np.arange(1, 7)).all(), "rows are in lane order")
    booster = lgb.Booster(model_file=str(C / "runs" / "F_s42_model.txt"))
    check(booster.num_feature() == F + 6 and booster.num_trees() == 1248, "model: 159 features, 1248 trees")
    z = np.load(C / "runs" / "F_s42.npz")
    ab6 = ab6_all[sel]

    # 点数(直前情報あり・朝)
    t = time.time()
    S = M.scores(booster, X6)
    ref = z["S_2"][sel - first_test]
    d = maxdiff(S, ref)
    check(d <= 1e-6, "live scores: max|S - saved| = %.2e (%d rows, %.2fs)" % (d, len(sel) * 156, time.time() - t))
    X6m = X6.copy()
    X6m[:, :, EX] = np.nan
    dm = maxdiff(M.scores(booster, X6m), z["S_2_m"][sel - first_test])
    check(dm <= 1e-6, "morning scores (9 cols NaN): max|S - saved| = %.2e" % dm)
    # 2次元入力でも同じ
    check(maxdiff(M.scores(booster, X6.reshape(-1, F)), S) == 0.0, "scores with (rows,153) input identical")

    # 確率: 保存点数 → clib の式 と、自分の点数 → 自分の式 を比べる
    P_ref, p1_ref = clib.scores_to_P(ref, ab6)
    P, p1 = M.scores_to_P(S, ab6)
    dP = maxdiff(P, P_ref)
    check(dP <= 1e-6, "P with absent rule: max|P - clib(saved S)| = %.2e" % dP)
    check(maxdiff(p1, p1_ref) <= 1e-6, "p1 with absent rule matches clib")
    check(maxdiff(M.predict_P(booster, X6, absent_mask=ab6), P) == 0.0, "predict_P == scores_to_P(scores())")
    P_nr, _ = M.scores_to_P(S)
    check(maxdiff(P_nr, clib.scores_to_P(ref, None)[0]) <= 1e-6, "P without rule matches clib")
    check(maxdiff(to_P_spec(ref, ab6)[0], P_ref) < 1e-15, "clib.scores_to_P == spec to_P (sanity of references)")
    check(maxdiff(M.predict_P(booster, X6.reshape(-1, F)), P_nr) == 0.0, "predict_P accepts (rows,153)")
    # 欠場艇の行: その艇を含む買い目はほぼ0
    for i in np.flatnonzero(ab6.any(1))[:3]:
        lanes = np.flatnonzero(ab6[i])
        has = np.isin(M.PERMS, lanes).any(1)
        check(P[i, has].max() < 1e-20 and P_nr[i, has].max() > 1e-6,
              "race %d: absent lanes %s have ~0 probability with the rule" % (sel[i], (lanes + 1).tolist()))
    test_marginals(P, label="saved model, %d races" % len(sel))

    # 試験期間の成績(保存点数 → 自分の式 → 実験の物差し evalkit)。candidate_spec.md 4.6 / 7.1 の数字
    parts = pd.read_pickle(C / "parts.pkl")[2]
    ab6_test = absent[sp == 2].reshape(-1, 6)
    P_all, p1_all = M.scores_to_P(z["S_2"], ab6_test)
    ri = K.race_index(parts)
    sm = K.summary(K.evaluate(parts, p1_all.ravel(), P=P_all[ri["pos6"][:, 0] // 6], ri=ri))
    say("test (saved scores, rule): races %d t5 %.4f win %.4f t1 %.4f t10 %.4f roi5 %.4f nll3 %.5f"
        % (sm["races"], sm["t5_hit"], sm["win_hit"], sm["t1_hit"], sm["t10_hit"], sm["roi5"], sm["nll3"]))
    check(sm["races"] == 29216 and abs(sm["t5_hit"] - 0.3685) < 1e-6 and abs(sm["win_hit"] - 0.5776) < 1e-6
          and abs(sm["nll3"] - 3.70691) < 1e-6, "headline numbers reproduced: t5 0.3685 win 0.5776 nll3 3.70691")
    P_nr_all, p1_nr_all = M.scores_to_P(z["S_2"])
    sm2 = K.summary(K.evaluate(parts, p1_nr_all.ravel(), P=P_nr_all[ri["pos6"][:, 0] // 6], ri=ri))
    check(abs(sm2["t5_hit"] - 0.3679) < 1e-6 and abs(sm2["nll3"] - 3.70919) < 1e-6,
          "without rule: t5 0.3679 nll3 3.70919 reproduced")
    pw_all = M.win_probs(P_all)
    check(maxdiff(pw_all, p1_all) < 1e-12 and maxdiff(M.top2_probs(P_all).sum(1), 2.0) < 1e-12
          and maxdiff(M.top3_probs(P_all).sum(1), 3.0) < 1e-12, "marginals on all %d test races" % len(P_all))

    if FULL:
        # 試験期間の全レースを自分で採点する(約1分)
        t = time.time()
        rows = slice(first_test * 6, (int(test_r[-1]) + 1) * 6)
        X6_all = np.asarray(Xm[rows, :F])
        S_all = M.scores(booster, X6_all)
        d_all = maxdiff(S_all, z["S_2"])
        check(d_all <= 1e-6, "FULL: all %d test races scored, max|S - saved| = %.2e (%.0fs)"
              % (len(S_all), d_all, time.time() - t))
        P_my, p1_my = M.scores_to_P(S_all, ab6_test)
        sm3 = K.summary(K.evaluate(parts, p1_my.ravel(), P=P_my[ri["pos6"][:, 0] // 6], ri=ri))
        say("FULL test (my scores, rule): t5 %.4f win %.4f t1 %.4f t10 %.4f roi5 %.4f nll3 %.5f"
            % (sm3["t5_hit"], sm3["win_hit"], sm3["t1_hit"], sm3["t10_hit"], sm3["roi5"], sm3["nll3"]))
        check(abs(sm3["t5_hit"] - 0.3685) < 1e-6 and abs(sm3["win_hit"] - 0.5776) < 1e-6
              and abs(sm3["nll3"] - 3.70691) < 5e-6, "FULL: headline numbers from my own scoring")
    return True


def main():
    test_constants()
    test_stacking()
    test_objective()
    test_marginals()
    test_fit()
    ran_a = test_saved_model()
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    if not ran_a:
        say("NOTE: (a) saved-model comparison was skipped (set KYOTEI_REBUILD_DIR to the rebuild folder)")
    print("ALL OK")


if __name__ == "__main__":
    main()

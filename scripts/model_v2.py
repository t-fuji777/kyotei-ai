# -*- coding: utf-8 -*-
"""世代2のモデル: 条件づけ分解ロジット(1着の段・2着の段・3着の段を LightGBM 1本で学ぶ)。

実験 rebuild/candidate/clib.py と rebuild/audit/a05_retrain.py(1ファイルで学習を再現した参照実装)の移植。
特徴量の作り方(153列 = 現行45 + 履歴108)は features.py / features_hist.py の担当で、このモジュールは
「153列の行列を受け取ってから先」だけを持つ。

解き方:
  学習: 1レースを 1着の段6行 + 2着の段5行(1着を除く) + 3着の段4行(1・2着を除く) の15行に積み、
        各行に段の特徴6列(STAGE_COLS)を足して 159列にする。段ごとのレース内ソフトマックスを独自目的関数で学ぶ。
  予測: 1レースを 6 + 30 + 120 = 156行に積んで点数を出し、段ごとにソフトマックスして
        P(a,b,c) = p1[a] * p2[b|a] * p3[c|a,b] の120通りを直接組み立てる。

決まりごと:
  - 行列は float32、列は名前なしの位置渡し(学習と予測で同じ列の順を使う。列名は meta.json 側に持つ)。
  - 枠は内部では 0〜5 で数える(行列の並びは枠1〜6の順)。COMBO_STR だけ 1〜6 の表記。
  - 6艇そろわないレースは呼び出し側で除く(ここでは assert で守るだけ)。
  - 欠場艇(absent_mask)は呼び出し側が渡した時だけ点数を落とす。渡さなければ何もしない。
  - 学習用・検証用の段の区切り(blocks)は、行数で見分けずに明示的に渡す(実験は len(preds) で引いていた)。
"""
import time

import lightgbm as lgb
import numpy as np

# 120通りの並び = a, b, c の3重ループ順(train.trifecta_probs が辞書に入れる順と同じ)。
# 位置は a*20 + (b - (b > a))*4 + (c - (c > a) - (c > b))。先頭 (0,1,2)、末尾 (5,4,3)。
PERMS = np.array([(a, b, c) for a in range(6) for b in range(6) if b != a
                  for c in range(6) if c not in (a, b)], dtype=np.int64)
# 2着の段の並び(a を外側、b != a を内側に昇順)。PERMS の先頭2つを4個ずつまとめたものと同じ順。
PAIRS = np.array([(a, b) for a in range(6) for b in range(6) if b != a], dtype=np.int64)
# 各 PERM が属する PAIR の位置(2着の段の確率を120通りに配る時に使う)
PERM_PAIR = PERMS[:, 0] * 5 + (PERMS[:, 1] - (PERMS[:, 1] > PERMS[:, 0]))
COMBO_STR = np.array(["%d-%d-%d" % (a + 1, b + 1, c + 1) for a, b, c in PERMS])

# 段の特徴6列(行列の 153 列の後ろに足す。位置 153〜158)
STAGE_COLS = ["stage", "win_lane", "sec_lane", "rel1", "rel2", "n_in"]
N_STAGE = len(STAGE_COLS)
N_FEAT = 153                       # FEATURES_V2 の数(現行45 + 履歴108)
N_ROWS_TRAIN = 15                  # 1レースの学習行(6 + 5 + 4)
N_ROWS_PREDICT = 156               # 1レースの予測行(6 + 30 + 120)
# カテゴリ扱いの位置: venue, lane, class_i, v_water(現行と同じ4列)+ win_lane, sec_lane
CAT_IDX = [0, 2, 5, 30, N_FEAT + 1, N_FEAT + 2]

# LightGBM の設定(実験 clib.py の "tuned" + make_params)。num_threads は指定しない(= 全コア。
# 実験の 8 を持ち込むと 4 コアの Actions でスレッドの取り合いになる)。objective / metric は fit が足す。
PARAMS_V2 = dict(learning_rate=0.05, num_leaves=31, min_data_in_leaf=200, feature_fraction=0.6,
                 lambda_l2=10.0, bagging_fraction=0.8, bagging_freq=1, seed=42, verbosity=-1,
                 force_col_wise=True)
# 実行環境の項目(スレッド数・ヒストグラムの作り方・ログの量)。結果(木)には効かないので、meta.json に
# 「モデルの設定」として残さない(model_params)。dry_run や検証で num_threads を渡した時に、本番の
# 設定と食い違って見えないようにするため。
RUNTIME_PARAM_KEYS = ("num_threads", "num_thread", "nthread", "nthreads", "n_jobs",
                      "force_col_wise", "force_row_wise", "verbosity", "verbose")
ROUNDS_V2 = 4000                   # 最大の木の本数
EARLY_STOP_V2 = 100                # 早期打ち切りの待ち回数
ABSENT_DROP = -60.0                # 欠場艇の点数(通常の点数は -7〜+3 なので確率はほぼ0になる)


# ------------------------------------------------------------ 入力の形
def _as_x6(X):
    """(R,6,F) か (R*6,F) を (R,6,F) float32 にそろえる。6艇そろわないレースは呼び出し側で除く前提。"""
    X = np.asarray(X, dtype=np.float32)
    if X.ndim == 2:
        assert X.shape[0] % 6 == 0, "行数が6の倍数でない(6艇そろわないレースは呼び出し側で除く)"
        X = X.reshape(-1, 6, X.shape[1])
    assert X.ndim == 3 and X.shape[1] == 6, "入力は (レース数,6,列数) か (行数,列数)"
    return X


def _stage_feats(lane, stage, w1=None, w2=None):
    """段の特徴6列を (len(lane), 6) float32 で作る。lane / w1 / w2 は 0 始まりの枠(clib._fill と同じ式)。
    win_lane / sec_lane は 1〜6(無ければ 0)、rel1 / rel2 は枠番の差(無ければ NaN)、
    n_in は自分より内側に残っている艇の数。"""
    lane = np.asarray(lane, dtype=np.int64)
    out = np.empty((len(lane), N_STAGE), dtype=np.float32)
    out[:, 0] = stage
    out[:, 1] = 0 if w1 is None else w1 + 1
    out[:, 2] = 0 if w2 is None else w2 + 1
    out[:, 3] = np.nan if w1 is None else lane - w1
    out[:, 4] = np.nan if w2 is None else lane - w2
    n_in = lane.astype(np.float32)
    if w1 is not None:
        n_in = n_in - (w1 < lane)
    if w2 is not None:
        n_in = n_in - (w2 < lane)
    out[:, 5] = n_in
    return out


# 予測の156行の「行の艇」と段の特徴(全レース共通なので1回だけ作る)
_PRED_LANE = np.concatenate([np.arange(6), PAIRS[:, 1], PERMS[:, 2]])
_PRED_STAGE = np.concatenate([
    _stage_feats(np.arange(6), 1),
    _stage_feats(PAIRS[:, 1], 2, PAIRS[:, 0]),
    _stage_feats(PERMS[:, 2], 3, PERMS[:, 0], PERMS[:, 1]),
])
assert _PRED_LANE.shape == (N_ROWS_PREDICT,) and _PRED_STAGE.shape == (N_ROWS_PREDICT, N_STAGE)


# ------------------------------------------------------------ 行の積み上げ
def stack_train(X6, order, step=400000, idx=None):
    """学習用に1レース15行を積む(clib.stacked_set と同じ並び・同じ値)。

    X6: (R,6,F) float32(枠1〜6の順)。order: (R,3) 1着・2着・3着の枠(0〜5)。
    戻り値: X (R*15, F+6) float32, y (R*15,) float32, blocks [(開始行, レース数, 段の行数k), ...]。
    並びは「全レースの1着の段(R*6行)→ 全レースの2着の段(R*5行)→ 全レースの3着の段(R*4行)」。
    各段の中はレース順、レース内は残っている枠の昇順。blocks は目的関数・評価関数に渡す。
    idx: X6 の中で積むレースの位置(昇順でなくてもよい)。渡せば X6[idx] の写し(全期間の行列の8割 =
    約0.8GB)を作らずに、その位置の行を直接写す。order は X6 と同じ長さのまま渡す(ここで idx で選ぶ)。"""
    X6 = _as_x6(X6)
    order = np.asarray(order, dtype=np.int64)
    if idx is not None:
        idx = np.asarray(idx, dtype=np.int64)
        assert idx.ndim == 1 and (idx >= 0).all() and (idx < len(X6)).all(), "idx は X6 の中の位置"
        assert order.shape == (len(X6), 3), "idx を渡す時の order は X6 と同じ長さ"
        order = order[idx]
    R, _, F = X6.shape if idx is None else (len(idx), 6, X6.shape[2])
    assert order.shape == (R, 3), "order は (レース数,3)"
    assert ((order >= 0) & (order <= 5)).all(), "order の枠は 0〜5"
    so = np.sort(order, axis=1)
    assert (so[:, 1:] != so[:, :-1]).all(), "order の3艇は別々の枠"
    X = np.empty((R * N_ROWS_TRAIN, F + N_STAGE), dtype=np.float32)
    y = np.empty(R * N_ROWS_TRAIN, dtype=np.float32)
    rr = np.arange(R)
    blocks, off = [], 0
    for stage in (1, 2, 3):
        k = 7 - stage
        keep = np.ones((R, 6), dtype=bool)
        if stage >= 2:
            keep[rr, order[:, 0]] = False
        if stage >= 3:
            keep[rr, order[:, 1]] = False
        lanes = np.nonzero(keep)[1].reshape(R, k)
        ys = (lanes == order[:, stage - 1][:, None]).astype(np.float32)
        assert (ys.sum(1) == 1).all()
        lane = lanes.ravel()
        race = np.repeat(rr if idx is None else idx, k)      # X6 の中の行(idx があればその位置)
        w1 = np.repeat(order[:, 0], k) if stage >= 2 else None
        w2 = np.repeat(order[:, 1], k) if stage >= 3 else None
        m = R * k
        blk = X[off:off + m]
        # 一度に全行を取り出すと (m,F) の中間配列ができるので、行を区切って写す
        for s in range(0, m, step):
            blk[s:s + step, :F] = X6[race[s:s + step], lane[s:s + step]]
        blk[:, F:] = _stage_feats(lane, stage, w1, w2)
        y[off:off + m] = ys.ravel()
        blocks.append((off, R, k))
        off += m
    return X, y, blocks


def stack_predict(X6):
    """予測用に1レース156行を積む。戻り値 (R*156, F+6) float32。
    レースごとに連続した156行: 1着の段6行(枠0〜5)→ 2着の段30行(PAIRS の順。行は b の特徴量)→
    3着の段120行(PERMS の順。行は c の特徴量)。predict した点数を reshape(R,156) すれば
    S[:, :6] / S[:, 6:36] / S[:, 36:] がそれぞれの段になる。"""
    X6 = _as_x6(X6)
    R, _, F = X6.shape
    out = np.empty((R, N_ROWS_PREDICT, F + N_STAGE), dtype=np.float32)
    out[:, :, :F] = X6[:, _PRED_LANE, :]
    out[:, :, F:] = _PRED_STAGE[None, :, :]
    return out.reshape(R * N_ROWS_PREDICT, F + N_STAGE)


# ------------------------------------------------------------ 独自目的関数・評価関数
def _softmax(z, k):
    z = z.reshape(-1, k)
    z = z - z.max(1, keepdims=True)
    p = np.exp(z)
    return p / p.sum(1, keepdims=True)


def _check_blocks(blocks, n_rows):
    """blocks が n_rows 行をすき間なく覆うことを確かめる(学習用と検証用の取り違えをここで止める)。"""
    off = 0
    for s, n, k in blocks:
        assert s == off, "blocks の開始行が連続していない"
        off += n * k
    assert off == n_rows, "blocks の行数(%d)と行列の行数(%d)が合わない" % (off, n_rows)


def _block_probs(preds, blocks):
    for s, n, k in blocks:
        e = s + n * k
        yield s, e, n, k, _softmax(preds[s:e], k)


def make_objective(blocks):
    """段ごとのレース内ソフトマックスの目的関数。勾配 p - y、ヘシアン max(p(1-p), 1e-6)(clib と同じ式)。
    blocks は学習行列のもの(stack_train の戻り値)。"""
    def obj(preds, ds):
        _check_blocks(blocks, len(preds))
        y = ds.get_label()
        g = np.empty_like(preds)
        h = np.empty_like(preds)
        for s, e, n, k, p in _block_probs(preds, blocks):
            g[s:e] = (p - y[s:e].reshape(n, k)).ravel()
            h[s:e] = np.maximum(p * (1.0 - p), 1e-6).ravel()
        return g, h
    return obj


def make_feval(blocks):
    """評価 nll3 = 3段それぞれの「正解に付けた確率の -log の平均」の合計(= 当たりの3連単の -log の平均)。
    blocks は検証行列のもの。"""
    def fe(preds, ds):
        _check_blocks(blocks, len(preds))
        y = ds.get_label()
        tot = 0.0
        for s, e, n, k, p in _block_probs(preds, blocks):
            tot += -np.log(np.clip((p * y[s:e].reshape(n, k)).sum(1), 1e-12, None)).mean()
        return "nll3", float(tot), False
    return fe


# ------------------------------------------------------------ 学習
def _params(params):
    p = dict(PARAMS_V2)
    if params:
        p.update(params)
    return p


def model_params(params=None):
    """meta.json に「モデルの設定」として書く項目: _params(params) から実行環境の項目(RUNTIME_PARAM_KEYS)を
    除いたもの。学習の結果に効く項目(learning_rate など。呼び出し側の上書きも含む)だけが残る。"""
    return {k: v for k, v in _params(params).items() if k not in RUNTIME_PARAM_KEYS}


def _dataset_params(p):
    """Dataset を先に construct する時に渡す設定。objective(関数)と metric を除いた全部を渡す。
    LightGBM は min_data_in_leaf(feature_pre_filter が見る)や seed(bin の標本抽出)で区切りが変わるので、
    学習時と同じ設定で作らないと lgb.train が設定の食い違いを検出して止まる。"""
    return {k: v for k, v in p.items() if k not in ("objective", "metric")}


def _cat_idx(ncol, cat_idx):
    if cat_idx is None:
        assert ncol == N_FEAT + N_STAGE, \
            "列数が %d でない(CAT_IDX の前提)。別の列数なら cat_idx を渡す" % (N_FEAT + N_STAGE)
        cat_idx = CAT_IDX
    return [int(c) for c in cat_idx]


def _progress(log, every):
    def cb(env):
        it = env.iteration + 1
        if it % every == 0 and env.evaluation_result_list:
            log("  iter %d valid nll3 %.5f" % (it, env.evaluation_result_list[0][2]))
    cb.order = 5
    return cb


def _train(p, dtr, dva, blocks_tr, blocks_va, n_tr, n_va, rounds, early_stop, log, log_every):
    p = dict(p, objective=make_objective(blocks_tr), metric="None")
    callbacks = [lgb.early_stopping(early_stop, verbose=False), lgb.log_evaluation(0)]
    if log and log_every:
        callbacks.append(_progress(log, log_every))
    if log:
        log("model_v2.fit: rows train %d valid %d, rounds<=%d, early_stop %d" % (n_tr, n_va, rounds, early_stop))
    t0 = time.time()
    booster = lgb.train(p, dtr, num_boost_round=rounds, valid_sets=[dva], feval=make_feval(blocks_va),
                        callbacks=callbacks)
    if log:
        best = booster.best_iteration
        score = booster.best_score.get("valid_0", {}).get("nll3")
        log("model_v2.fit: best_iteration %d valid nll3 %s (%.0fs)%s"
            % (best, "%.5f" % score if score is not None else "-", time.time() - t0,
               "" if best > 0 else "  (打ち切りが効かずに上限まで回った)"))
    return booster


def fit(Xtr, ytr, blocks_tr, Xva, yva, blocks_va, params=None, rounds=ROUNDS_V2,
        early_stop=EARLY_STOP_V2, cat_idx=None, log=print, log_every=200):
    """積んだ行列から LightGBM を1本学ぶ。独自目的関数・独自評価関数・早期打ち切り(検証の nll3)。

    Xtr/ytr/blocks_tr: stack_train の戻り値(学習)。Xva/yva/blocks_va: 同(検証。1〜3着がそろったレースだけ)。
    params: PARAMS_V2 に上書きする項目(num_threads など)。cat_idx: 省略時は CAT_IDX(列数 159 が前提)。
    戻り値の Booster は best_iteration を持つ。保存は save_model(booster, path)(打ち切りの本数で切って保存)。
    メモリ: 渡した Xtr/Xva は LightGBM がデータを作り終えても呼び出し側の参照が残る(学習中 約2.4GB)。
    抑えたい時は fit_races を使う(中で積んで、データを作ったら消す)。"""
    assert Xtr.dtype == np.float32 and Xva.dtype == np.float32, "行列は float32"
    assert Xtr.ndim == 2 and Xva.ndim == 2 and Xtr.shape[1] == Xva.shape[1], "学習と検証の列数が違う"
    assert len(ytr) == len(Xtr) and len(yva) == len(Xva)
    _check_blocks(blocks_tr, len(ytr))
    _check_blocks(blocks_va, len(yva))
    p = _params(params)
    ci = _cat_idx(Xtr.shape[1], cat_idx)
    dtr = lgb.Dataset(Xtr, label=ytr, categorical_feature=ci, free_raw_data=True)
    dva = lgb.Dataset(Xva, label=yva, categorical_feature=ci, reference=dtr, free_raw_data=True)
    return _train(p, dtr, dva, blocks_tr, blocks_va, len(ytr), len(yva), rounds, early_stop, log, log_every)


def make_datasets(Xtr, ytr, Xva, yva, params=None, cat_idx=None):
    """積んだ行列から LightGBM の Dataset(学習・検証)を先に作って(construct)返す。
    データ化が済めば元の行列は要らないので、呼び出し側はこの直後に Xtr / Xva を del できる
    (学習中のメモリを積み上げ行列の分 約2.4GB 減らす。実験 clib.fit_stacked の del Xtr, Xva と同じ)。
    設定(params)は fit_datasets に渡すものと同じにすること(_dataset_params の注意)。"""
    assert Xtr.dtype == np.float32 and Xva.dtype == np.float32, "行列は float32"
    assert Xtr.ndim == 2 and Xva.ndim == 2 and Xtr.shape[1] == Xva.shape[1], "学習と検証の列数が違う"
    assert len(ytr) == len(Xtr) and len(yva) == len(Xva)
    p = _params(params)
    ci = _cat_idx(Xtr.shape[1], cat_idx)
    dp = _dataset_params(p)
    dtr = lgb.Dataset(Xtr, label=ytr, categorical_feature=ci, params=dp, free_raw_data=True).construct()
    dva = lgb.Dataset(Xva, label=yva, categorical_feature=ci, reference=dtr, params=dp,
                      free_raw_data=True).construct()
    return dtr, dva


def fit_datasets(dtr, dva, blocks_tr, blocks_va, params=None, rounds=ROUNDS_V2,
                 early_stop=EARLY_STOP_V2, log=print, log_every=200):
    """make_datasets で作った Dataset から学ぶ(fit / fit_races と同じ結果)。blocks は積んだ時のもの。"""
    n_tr, n_va = dtr.num_data(), dva.num_data()
    _check_blocks(blocks_tr, n_tr)
    _check_blocks(blocks_va, n_va)
    return _train(_params(params), dtr, dva, blocks_tr, blocks_va, n_tr, n_va, rounds, early_stop, log, log_every)


def fit_races(X6_tr, order_tr, X6_va, order_va, params=None, rounds=ROUNDS_V2,
              early_stop=EARLY_STOP_V2, cat_idx=None, log=print, log_every=200, tr_idx=None, va_idx=None):
    """レース単位の入力(X6 と着順)から積み上げと学習をまとめて行う版。結果は fit と同じ。
    LightGBM がデータを作り終えたら積んだ行列を消すので、学習中のメモリが fit より約2.4GB小さい
    (実験 clib.fit_stacked の del Xtr, Xva と同じ)。引数の X6 は呼び出し側で消す。
    tr_idx / va_idx を渡せば X6_tr / X6_va は全期間の行列のままでよく(同じ配列を2回渡す)、その位置の
    レースだけを写しを作らずに積む(stack_train の idx)。全期間の行列を学習中も持ち続けたくない時は、
    呼び出し側で stack_train → make_datasets → del → fit_datasets の順に分けて呼ぶ(train.main_v2)。"""
    Xtr, ytr, btr = stack_train(X6_tr, order_tr, idx=tr_idx)
    del X6_tr
    Xva, yva, bva = stack_train(X6_va, order_va, idx=va_idx)
    del X6_va
    dtr, dva = make_datasets(Xtr, ytr, Xva, yva, params=params, cat_idx=cat_idx)
    del Xtr, Xva
    return fit_datasets(dtr, dva, btr, bva, params=params, rounds=rounds, early_stop=early_stop,
                        log=log, log_every=log_every)


def _num_iteration(booster):
    """predict に渡す木の本数。学習直後は best_iteration、ファイルから読んだモデル(best_iteration が
    -1 や 0)は None(= 保存されている全部。保存は打ち切りの本数で切ってある)。"""
    bi = getattr(booster, "best_iteration", 0) or 0
    return int(bi) if bi > 0 else None


def save_model(booster, path):
    """打ち切りの本数で切って保存する(実験 c01_run.py と同じ)。読み直すと best_iteration は -1 になり、
    predict は保存された全部の木を使うので、学習直後と同じ点数になる。"""
    booster.save_model(str(path), num_iteration=_num_iteration(booster))


# ------------------------------------------------------------ 予測
def scores(booster, X6, chunk=3000):
    """生の点数 S (R,156) float32。S[:, :6] 1着の段、S[:, 6:36] 2着の段(PAIRS の順)、S[:, 36:] 3着の段(PERMS の順)。
    chunk レースずつ 156 行を積んで predict する(3000レースで約0.3GB)。"""
    X6 = _as_x6(X6)
    R, _, F = X6.shape
    nf = booster.num_feature()
    if nf != F + N_STAGE:
        raise ValueError("モデルの列数 %d と入力の列数 %d + 段の特徴 %d が合わない" % (nf, F, N_STAGE))
    ni = _num_iteration(booster)
    S = np.empty((R, N_ROWS_PREDICT), dtype=np.float32)
    for s in range(0, R, chunk):
        e = min(s + chunk, R)
        z = booster.predict(stack_predict(X6[s:e]), num_iteration=ni)
        S[s:e] = np.asarray(z).reshape(e - s, N_ROWS_PREDICT)
    return S


def scores_to_P(S, absent_mask=None, drop=ABSENT_DROP):
    """点数 S (R,156) → 120通りの確率 P (R,120)(PERMS の順。合計1)と p1 (R,6)(1着の段のソフトマックス)。
    absent_mask (R,6) の真偽を渡すと、その艇が「候補」になっている行の点数を各段で drop に置き換えてから
    ソフトマックスする(欠場艇の確率はほぼ0になり、残りの艇で正規化される)。渡さなければ何もしない。"""
    S = np.asarray(S, dtype=np.float64)
    assert S.ndim == 2 and S.shape[1] == N_ROWS_PREDICT, "S は (レース数,156)"
    S1, S2, S3 = S[:, :6].copy(), S[:, 6:36].copy(), S[:, 36:].copy()
    if absent_mask is not None:
        ab = np.asarray(absent_mask, dtype=bool)
        assert ab.shape == (len(S), 6), "absent_mask は (レース数,6)"
        S1[ab] = drop
        S2[ab[:, PAIRS[:, 1]]] = drop
        S3[ab[:, PERMS[:, 2]]] = drop
    p1 = _softmax(S1, 6)
    p2 = _softmax(S2, 5).reshape(-1, 30)
    p3 = _softmax(S3, 4).reshape(-1, 120)
    P = p1[:, PERMS[:, 0]] * p2[:, PERM_PAIR] * p3
    P /= P.sum(1, keepdims=True)
    return P, p1


def predict_P(booster, X6, absent_mask=None, chunk=3000):
    """153列の行列 X6((R,6,153) か (R*6,153)。枠1〜6の順、float32)→ 120通りの確率 (R,120)。
    列は PERMS の順(COMBO_STR が買い目の表記)。各レースの合計は1。
    absent_mask (R,6) は欠場艇の印(直前の再予測で呼び出し側が渡す。朝は渡さない)。"""
    S = scores(booster, X6, chunk=chunk)
    return scores_to_P(S, absent_mask)[0]


# 120通り → 1艇ごとの数字(P @ 指示行列)
_M1 = np.zeros((120, 6))
_M1[np.arange(120), PERMS[:, 0]] = 1.0
_M2 = _M1.copy()
_M2[np.arange(120), PERMS[:, 1]] = 1.0
_M3 = _M2.copy()
_M3[np.arange(120), PERMS[:, 2]] = 1.0


def win_probs(P):
    """1着の確率 (R,6)。6艇の合計は1(scores_to_P の p1 と同じ値)。"""
    return np.asarray(P, dtype=np.float64) @ _M1


def top2_probs(P):
    """2着以内の確率 (R,6)。6艇の合計はちょうど2(現行の二値モデルの p_top2 とは目盛りが違う)。"""
    return np.asarray(P, dtype=np.float64) @ _M2


def top3_probs(P):
    """3着以内の確率 (R,6)。6艇の合計はちょうど3。"""
    return np.asarray(P, dtype=np.float64) @ _M3

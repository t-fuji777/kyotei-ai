# -*- coding: utf-8 -*-
"""履歴の特徴量(h_ で始まる108列)。世代2のモデルが使う。

実験 rebuild/history/build_features.py の移植。違いは次の4点だけ(値は変えない)。
  - 入力の並びに依存しない。中で 日付→会場→レース番号→枠 に並べ替えて計算し、戻り値は入力と
    同じ index・同じ順にする。
  - 6行そろわないレース(重複・欠け)は例外にせず、その行を NaN にして続行する(件数をログ)。
  - 進入の癖(g3)は作らない(候補が使っていない。他の列はこの結果を参照しない)。
  - 内部の上限を広げた(登番 10000、会場 32、モーターの期 64、レース名 65536)。鍵の掛け数を
    広げただけなので値は同じ。超えた時は分かる文言で止める。

未来を使わない決まり(実験と同じ):
    どの列も「そのレースの日より前の日」の結果だけで作る(同じ日の他のレースの結果も使わない)。
    当日の番組表(選手・枠・モーター番号・締切・レース名など)と、そのレース自身の展示タイムは使う。
    実装は Hist(グループ×日 で並べた累積和 + searchsorted)で「day < 当日」を切り出す。
    実力値は日単位で進め、その日の予測には前日までの値を使う。
    したがって、過去の全行の後ろに当日の番組表の行(結果の列は無し)を足して build_hist に通せば、
    当日の行は「その日が結果つきで表の中にある時」と同じ値になる(tests/test_features_hist.py)。

当日の展示に依存するのは HIST_LIVE_COLS の4列だけ。朝に作った値(h_m_exrel120 と中間値
_aux_exh365)と展示タイムから live_hist_update() で作り直せる(全履歴を作り直さなくてよい)。
"""
import time
import warnings

import numpy as np
import pandas as pd

# 候補が使った順(rebuild/candidate/cols.json の位置45〜152)。学習と予測の列の位置になるので変えない。
HIST_COLS = [
    # g1 実力値 13
    "h_elo", "h_elo_fd", "h_elola", "h_elola_fd", "h_elo_n", "h_elola_rel", "h_elola_rank",
    "h_elola_exp", "h_elola_b1", "h_elola_maxoth", "h_plw", "h_plw_rel", "h_plw_p",
    # g2 選手×枠 26
    "h_pl_top2_365", "h_pl_top2_all", "h_pl_top3_365", "h_pl_top3_all", "h_pl_pos365", "h_pl_pos_all",
    "h_pl_n_all", "h_pl_win_all", "h_pl_st365", "h_pl_st_all",
    "h_pl_ksashi_all", "h_pl_kmakuri_all", "h_pl_kmz_all", "h_pl_knuki_all",
    "h_pl_lose_nige_all", "h_pl_lose_sashi_all", "h_pl_lose_makuri_all", "h_pl_lose_mz_all",
    "h_st_sd365", "h_st_all", "h_st_rel365", "h_st_rank365",
    "h_k_sashi365", "h_k_makuri365", "h_k_mz365", "h_k_nuki365",
    # g2b 1号艇(と2号艇)の選手の値 8
    "h_b1_win_all", "h_b1_n_all", "h_b1_lose_sashi_all", "h_b1_lose_makuri_all", "h_b1_lose_mz_all",
    "h_b1_st_all", "h_b1_win365", "h_b2_lose_nige_all",
    # g4 モーター・ボート 19
    "h_m_n60", "h_m_pos60", "h_m_top2_60", "h_m_exrel60", "h_m_res60",
    "h_m_n120", "h_m_pos120", "h_m_top2_120", "h_m_exrel120", "h_m_res120",
    "h_m_nep", "h_m_exrelep", "h_m_resep", "h_m_age", "h_m_res120_rel", "h_m_exrel120_rel",
    "h_b_n120", "h_b_res120", "h_b_exrel120",
    # g5 節間 11
    "h_s_day", "h_s_n", "h_s_pos", "h_s_win", "h_s_top3", "h_s_exrel", "h_s_st", "h_s_res",
    "h_s_pos_rel", "h_s_res_rel", "h_s_exrel_rel",
    # g6 近況 12
    "h_pos5", "h_pos20", "h_res10", "h_res30", "h_win_ew30", "h_pos_ew30", "h_win_ew90", "h_top3_ew90",
    "h_days_since", "h_last_pos", "h_n30", "h_days_since_venue",
    # g7 展示の癖 7
    "h_exh365", "h_exh_all", "h_exh_n365", "h_ex_rel", "h_ex_vs_usual", "h_ex_vs_motor", "h_ex_vs_exp",
    # g8 レースの属性 12
    "h_dl_min", "h_dow", "h_month", "h_dist1200", "h_race_natwin", "h_race_nA1", "h_b1_class",
    "h_vrt_n365", "h_vrt_win365", "h_vrt_top3_365", "h_vrn_win365", "h_vrn_top3_365",
]
assert len(HIST_COLS) == 108 and len(set(HIST_COLS)) == 108
# 当日の展示タイムに依存する4列。直前情報を隠す時は EX_COLS と一緒に NaN にする
HIST_LIVE_COLS = ["h_ex_rel", "h_ex_vs_usual", "h_ex_vs_motor", "h_ex_vs_exp"]
# live_hist_update に要る中間値。h_exh365 は float32 に丸める前の float64(丸めた値から作ると
# 最後の1ビットがずれ得る)。build_hist の戻り値に含める。当日分の保存(feat_cache)にも入れること
HIST_AUX_COLS = ["_aux_exh365"]
# build_hist が読む列。無い列は NaN で補う(date, venue, race_no, lane, toban は必須)
HIST_INPUT_COLS = ["date", "venue", "race_no", "lane", "toban", "class", "class_i",
                   "pos", "abnormal", "course", "st", "ex_time", "kimarite",
                   "motor_in2", "motor_no", "boat_no", "deadline", "distance", "nat_win", "race_type"]
# 特徴量コードの版。計算の式を変えたら上げる(meta.json / 当日分の保存の鍵に使う)
FEATURES_HIST_VERSION = "2026-10-06.1"

# 実力値の設定: rebuild/history/elo_cfg.json の chosen(学習期間だけで選んだ値)。外部ファイルは読まない
K_SLOW = 4.0
K_FAST = 16.0
KL_RATIO = 0.05
KW = 0.1
KLW_RATIO = 0.1
ELO_CFG = dict(K_slow=K_SLOW, K_fast=K_FAST, KL_ratio=KL_RATIO, Kw=KW, KLw_ratio=KLW_RATIO)
SCALE = 400.0 / np.log(10.0)

TOBAN_MAX = 10000          # 登番の配列の大きさ(今の最大 5472)。超えたら配列を広げる
VENUE_MAX = 32             # 会場コード < 32(今 24)
EPOCH_MAX = 64             # モーターの入れ替えの期 < 64(年1回。今 6前後)
RTYPE_MAX = 65536          # レース名の種類 < 65536(今 806種)
KIM = ["逃げ", "差し", "まくり", "まくり差し", "抜き", "恵まれ"]
CLASS_MAP = {"A1": 4, "A2": 3, "B1": 2, "B2": 1}
_COL_IDX = {c: j for j, c in enumerate(HIST_COLS)}


def _say(log, msg):
    if log is None:
        print("hist: " + msg, flush=True)
    else:
        log(msg)


def _day_ord(dates):
    """features._day_ord と同じ(2000-01-01 からの日数)。date は 'YYYYMMDD' の文字列。"""
    d = pd.to_datetime(pd.Series(dates).astype(str), format="%Y%m%d")
    return (d - pd.Timestamp("2000-01-01")).dt.days.to_numpy()


def _f64(df, col, sel):
    if col not in df.columns:
        return np.full(len(sel), np.nan)
    return pd.to_numeric(df[col], errors="coerce").to_numpy(np.float64)[sel]


def _obj(df, col, sel):
    if col not in df.columns:
        return np.full(len(sel), np.nan, dtype=object)
    return df[col].astype(object).to_numpy()[sel]


def _race_exrel(ex, race_id=None):
    """展示タイム − そのレースの展示タイムの平均(ある艇だけで平均。全艇無ければ NaN)。
    build_hist と live_hist_update の両方がこれを使う(式を1つにしてビット単位で合わせる)。"""
    ex = np.asarray(ex, dtype=np.float64)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        if race_id is None:
            if len(ex) % 6:
                raise ValueError("ex_time の行数が6の倍数でない(race_id を渡すか、6行ずつ並べる)")
            exm = np.nanmean(ex.reshape(-1, 6), axis=1)
            return ex - np.repeat(exm, 6)
        race_id = np.asarray(race_id)
        out = np.empty(len(ex))
        for r in np.unique(race_id):
            m = race_id == r
            out[m] = ex[m] - np.nanmean(ex[m])
        return out


def _live_cols(exrel, exh, mex):
    """当日の展示に依存する4列(HIST_LIVE_COLS)。exh = 選手の過去365日の exrel 平均(float64)、
    mex = h_m_exrel120(float32 の値を float64 にしたもの)。"""
    return {"h_ex_rel": exrel,
            "h_ex_vs_usual": exrel - exh,
            "h_ex_vs_motor": exrel - mex,
            "h_ex_vs_exp": exrel - np.nan_to_num(exh) - np.nan_to_num(mex)}


# ------------------------------------------------------------------ 実力値
def _elo_pair(P6, V, F6, M6, d_start, d_end, Kf, KL=0.0, lane_adj=False, n_toban=TOBAN_MAX, n_venue=VENUE_MAX):
    """6艇の総当たり(15組)で更新する Elo。日単位で進め、その日の行には前日までの値を入れる。
    lane_adj=True なら 会場×枠 の有利不利(これも前日までの結果から逐次学習)を引いて更新する。
    戻り値: pre (R,6) 前日までの値, expb (R,6) 期待される「負かす相手の数」, resid (R,6) 実際−期待,
            games (R,6) それまでの出走数"""
    R = len(P6)
    rating = np.zeros(n_toban)
    games = np.zeros(n_toban)
    L = np.zeros((n_venue, 6))
    pre = np.zeros((R, 6))
    pre_g = np.zeros((R, 6))
    expb = np.zeros((R, 6))
    resid = np.full((R, 6), np.nan)
    eye = np.eye(6, dtype=bool)
    for a, b in zip(d_start, d_end):
        pid = P6[a:b]
        r0 = rating[pid]
        pre[a:b] = r0
        pre_g[a:b] = games[pid]
        eff = r0 + L[V[a:b]] if lane_adj else r0
        E = 1.0 / (1.0 + np.exp(-(eff[:, :, None] - eff[:, None, :]) / SCALE))
        E[:, eye] = 0.0
        expb[a:b] = E.sum(2)
        f = F6[a:b]
        m = M6[a:b]
        mm = m[:, :, None] & m[:, None, :] & ~eye[None, :, :]
        with np.errstate(invalid="ignore"):
            S = (f[:, :, None] < f[:, None, :]) + 0.5 * (f[:, :, None] == f[:, None, :])
        g = np.where(mm, S - E, 0.0).sum(2)
        resid[a:b] = np.where(m, g, np.nan)
        # その日の全レースを計算し終えてから更新(同じ日に2回走る選手は2回分が足される。どちらも朝の値)
        np.add.at(rating, pid.ravel(), Kf * g.ravel())
        np.add.at(games, pid.ravel(), m.ravel().astype(np.float64))
        if lane_adj:
            np.add.at(L, V[a:b], KL * g)
    return pre, expb, resid, pre_g


def _elo_plw(P6, V, M6, Y6, d_start, d_end, Kw, KLw, n_toban=TOBAN_MAX, n_venue=VENUE_MAX):
    """1着だけを見る多項ロジットの逐次更新(強さ θ + 会場×枠)。p = softmax(θ + 枠)。"""
    R = len(P6)
    theta = np.zeros(n_toban)
    L = np.zeros((n_venue, 6))
    pre = np.zeros((R, 6))
    pout = np.zeros((R, 6))
    for a, b in zip(d_start, d_end):
        pid = P6[a:b]
        t0 = theta[pid]
        pre[a:b] = t0
        z = t0 + L[V[a:b]]
        ez = np.exp(z - z.max(1, keepdims=True))
        pout[a:b] = ez / ez.sum(1, keepdims=True)
        m = M6[a:b]
        y = Y6[a:b]
        ezm = np.where(m, ez, 0.0)
        tot = ezm.sum(1, keepdims=True)
        has = (y.sum(1) == 1) & (tot[:, 0] > 0)          # 1着がちょうど1艇いて、出走した艇がいるレースだけ
        pm = ezm / np.where(tot > 0, tot, 1.0)
        g = np.where(has[:, None] & m, y - pm, 0.0)
        np.add.at(theta, pid.ravel(), Kw * g.ravel())
        np.add.at(L, V[a:b], KLw * g)
    return pre, pout


# ------------------------------------------------------------------ 過去の集計
class _Hist:
    """グループ(選手、選手×枠 など)ごとに、当日より前の日の行だけを足し上げる道具。"""

    def __init__(self, gkey, day, mask):
        gkey = np.asarray(gkey, dtype=np.int64)
        key = gkey * 100000 + day
        idx = np.flatnonzero(mask)
        o = np.argsort(key[idx], kind="stable")       # 同じ日の中は元の並び(レース順)
        self.idx = idx[o]
        self.hk = key[self.idx]
        self.key = key
        self.e = np.searchsorted(self.hk, key, "left")              # 当日より前の行の終わり
        self.gs = np.searchsorted(self.hk, gkey * 100000, "left")   # グループの先頭

    def start(self, window):
        """当日から window 日前まで(当日は含まない)の先頭位置。"""
        return np.searchsorted(self.hk, self.key - window, "left")

    def lastn(self, n):
        return np.maximum(self.e - n, self.gs)

    def csum(self, v):
        c = np.zeros(len(self.idx) + 1)
        np.cumsum(np.asarray(v, dtype=np.float64)[self.idx], out=c[1:])
        return c

    def wsum(self, v, s):
        c = self.csum(v)
        return c[self.e] - c[s]

    def wsums(self, v, starts):
        c = self.csum(v)
        return [c[self.e] - c[s] for s in starts]


def _ratio(num, den, min_n=1):
    out = np.full(len(num), np.nan)
    ok = den >= min_n
    out[ok] = num[ok] / den[ok]
    return out


def _race_rel(x, R):
    x6 = x.reshape(R, 6)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        m = np.nanmean(x6, axis=1)
    return (x6 - m[:, None]).reshape(-1)


def _bcast(x, R, lane_idx):
    """レースの lane_idx 番目(0=1号艇)の値を6艇すべてに配る。"""
    return np.repeat(x.reshape(R, 6)[:, lane_idx], 6)


def _select_rows(df, log):
    """入力を 日付→会場→レース番号→枠 に並べ、6行そろった(枠1〜6・登番あり)レースの行位置を
    その順で返す。戻り値: (sel 元の行位置, day, n_bad_rows, n_bad_races)"""
    for c in ("date", "venue", "race_no", "lane", "toban"):
        if c not in df.columns:
            raise ValueError("build_hist: 列 %s が無い" % c)
    n = len(df)
    day = _day_ord(df["date"].to_numpy())
    venue = pd.to_numeric(df["venue"], errors="coerce").to_numpy(np.float64)
    race_no = pd.to_numeric(df["race_no"], errors="coerce").to_numpy(np.float64)
    lane = pd.to_numeric(df["lane"], errors="coerce").to_numpy(np.float64)
    toban = pd.to_numeric(df["toban"], errors="coerce").to_numpy(np.float64)
    keyok = ~(np.isnan(venue) | np.isnan(race_no) | np.isnan(lane) | np.isnan(day.astype(np.float64)))
    venue_i = np.where(keyok, venue, -1).astype(np.int64)
    race_i = np.where(keyok, race_no, -1).astype(np.int64)
    lane_i = np.where(keyok, lane, -1).astype(np.int64)
    day_i = np.where(keyok, day, -1).astype(np.int64)
    order = np.lexsort((lane_i, race_i, venue_i, day_i))
    d, v, r, ln = day_i[order], venue_i[order], race_i[order], lane_i[order]
    new = np.ones(n, dtype=bool)
    new[1:] = (d[1:] != d[:-1]) | (v[1:] != v[:-1]) | (r[1:] != r[:-1])
    rid = np.cumsum(new) - 1
    nr = int(rid[-1]) + 1 if n else 0
    size = np.bincount(rid, minlength=nr)
    first = np.flatnonzero(new)
    pos_in = np.arange(n) - first[rid]
    rowok = (ln == pos_in + 1) & keyok[order] & ~np.isnan(toban[order])
    raceok = (size == 6) & (np.bincount(rid, weights=rowok, minlength=nr) == 6)
    good = raceok[rid]
    sel = order[good]
    n_bad = int((~good).sum())
    if n_bad:
        _say(log, "6艇そろわない(重複・欠け・登番なし)レース %d 本・%d 行は NaN にして続行" % (int((~raceok).sum()), n_bad))
    return sel, day[sel], n_bad


def build_hist(df, log=None):
    """入力: 全期間の行(過去の entries + 必要なら当日の番組表の行。結果の無い行は結果の列が NaN か列ごと無い)。
    戻り値: DataFrame。HIST_COLS の108列(float32)+ HIST_AUX_COLS(float64)。入力と同じ行の並び・同じ index。
    6行そろわないレースの行は NaN(例外にしない)。date は 'YYYYMMDD' の文字列。"""
    t0 = time.time()

    def say(msg):
        _say(log, "[%5.1fs] %s" % (time.time() - t0, msg))

    N_in = len(df)
    out = np.full((N_in, len(HIST_COLS)), np.nan, dtype=np.float32)
    aux = np.full(N_in, np.nan)
    sel, day, _ = _select_rows(df, log)
    N = len(sel)
    if N == 0:
        _say(log, "6艇そろったレースが1つも無い。全行 NaN")
        res = pd.DataFrame(out, index=df.index, columns=HIST_COLS)
        res[HIST_AUX_COLS[0]] = aux
        return res
    R = N // 6

    missing = [c for c in HIST_INPUT_COLS if c not in df.columns and c not in ("class", "class_i")]
    if missing:
        say("入力に無い列を NaN で補う: %s" % missing)

    # ---- 元の列から計算に使う配列を作る(並べ替え済みの行だけ) ----
    lane = pd.to_numeric(df["lane"], errors="coerce").to_numpy(np.float64)[sel].astype(np.int64)
    venue = pd.to_numeric(df["venue"], errors="coerce").to_numpy(np.float64)[sel].astype(np.int64)
    race_no = pd.to_numeric(df["race_no"], errors="coerce").to_numpy(np.float64)[sel].astype(np.int64)
    toban = pd.to_numeric(df["toban"], errors="coerce").to_numpy(np.float64)[sel].astype(np.int64)
    if toban.min() < 0:
        raise ValueError("build_hist: 負の登番がある")
    n_toban = max(TOBAN_MAX, int(toban.max()) + 1)
    if n_toban > TOBAN_MAX:
        say("登番の最大 %d が上限 %d を超えた。配列を広げて続行" % (int(toban.max()), TOBAN_MAX))
    if venue.min() < 0 or venue.max() >= VENUE_MAX:
        raise ValueError("build_hist: 会場コードが 0〜%d の外(%d)" % (VENUE_MAX - 1, int(venue.max())))
    pos = _f64(df, "pos", sel)
    abn = df["abnormal"].notna().to_numpy()[sel] if "abnormal" in df.columns else np.zeros(N, dtype=bool)
    res = (~np.isnan(pos)) | abn                     # 出走して結果(着順 or F/失格/出遅れ)がある行
    pos6 = np.where(np.isnan(pos), 6.0, np.clip(pos, 1, 6))
    win = (pos == 1).astype(np.float64)
    top2 = (pos <= 2).astype(np.float64)
    top3 = (pos <= 3).astype(np.float64)
    fin = np.where(np.isnan(pos), np.where(abn, 7.0, np.nan), pos)   # 完走しなかった艇は最下位扱い
    st = _f64(df, "st", sel)
    st_pos = np.where(st > 0, st, np.nan)            # 現行と同じく、正のスタートタイミングだけを平均に使う
    ex = _f64(df, "ex_time", sel)
    kstr = _obj(df, "kimarite", sel)
    kc = np.full(N, -1, dtype=np.int64)
    for i, nm in enumerate(KIM):
        kc[kstr == nm] = i
    del kstr
    exrel = _race_exrel(ex)                          # 展示タイム − レース平均
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        stm = np.nanmean(st_pos.reshape(R, 6), axis=1)
    strel = st_pos - np.repeat(stm, 6)               # スタート − レース平均
    s6 = st_pos.reshape(R, 6)
    with np.errstate(invalid="ignore"):
        rk = 1.0 + (s6[:, None, :] < s6[:, :, None]).sum(2)
    strank = np.where(np.isnan(s6), np.nan, rk).reshape(-1)
    del s6, rk
    day_r = day.reshape(R, 6)[:, 0]
    _, d_start = np.unique(day_r, return_index=True)
    d_end = np.append(d_start[1:], R)
    venue_r = venue.reshape(R, 6)[:, 0]
    if "class" in df.columns:
        ci = df["class"].map(CLASS_MAP).fillna(0).astype(int).to_numpy(np.float64)[sel]
    elif "class_i" in df.columns:
        ci = _f64(df, "class_i", sel)
    else:
        say("class も class_i も無い。h_race_nA1 / h_b1_class は NaN")
        ci = np.full(N, np.nan)
    ones = np.ones(N)

    def put(name, arr):
        a32 = np.asarray(arr, dtype=np.float32)
        out[sel, _COL_IDX[name]] = a32
        return a32

    # ============ 1) 実力値 ============
    P6 = toban.reshape(R, 6)
    F6 = fin.reshape(R, 6)
    M6 = res.reshape(R, 6)
    pre_p, _, _, games = _elo_pair(P6, venue_r, F6, M6, d_start, d_end, K_SLOW, lane_adj=False, n_toban=n_toban)
    pre_pf, _, _, _ = _elo_pair(P6, venue_r, F6, M6, d_start, d_end, K_FAST, lane_adj=False, n_toban=n_toban)
    pre_l, expb_l, resid_l, _ = _elo_pair(P6, venue_r, F6, M6, d_start, d_end, K_SLOW, KL=K_SLOW * KL_RATIO,
                                          lane_adj=True, n_toban=n_toban)
    pre_lf, _, _, _ = _elo_pair(P6, venue_r, F6, M6, d_start, d_end, K_FAST, KL=K_FAST * KL_RATIO,
                                lane_adj=True, n_toban=n_toban)
    th, pw_pl = _elo_plw(P6, venue_r, M6, win.reshape(R, 6), d_start, d_end, KW, KW * KLW_RATIO, n_toban=n_toban)
    elo = pre_p.reshape(-1)
    elola = pre_l.reshape(-1)
    resid = resid_l.reshape(-1)                      # 実際に負かした数 − 期待(枠の有利不利を引いた後)
    put("h_elo", elo)
    put("h_elo_fd", pre_pf.reshape(-1) - elo)                           # 速い版 − 遅い版 = 最近の上り下り
    put("h_elola", elola)
    put("h_elola_fd", pre_lf.reshape(-1) - elola)
    put("h_elo_n", games.reshape(-1))
    put("h_elola_rel", _race_rel(elola, R))
    e6 = pre_l
    rk = 1.0 + (e6[:, None, :] > e6[:, :, None]).sum(2)
    put("h_elola_rank", rk.reshape(-1))
    put("h_elola_exp", expb_l.reshape(-1))                              # 枠込みで期待される「負かす相手の数」
    put("h_elola_b1", _bcast(elola, R, 0))                              # 1号艇の選手の値
    mo = np.where(np.eye(6, dtype=bool)[None, :, :], -np.inf, e6[:, None, :]).max(2)
    put("h_elola_maxoth", mo.reshape(-1))                               # 相手5艇の最大
    put("h_plw", th.reshape(-1))
    put("h_plw_rel", _race_rel(th.reshape(-1), R))
    put("h_plw_p", pw_pl.reshape(-1))                                   # 逐次ロジットの1着確率(枠込み)
    del pre_p, pre_pf, pre_l, pre_lf, expb_l, resid_l, games, th, pw_pl, e6, rk, mo, F6, M6
    say("g1 rating done")

    # ============ 2) 選手×枠の詳しい成績 ============
    Hpl = _Hist(toban * 8 + lane, day, res)
    s365, sall = Hpl.start(365), Hpl.gs
    n365, nall = Hpl.wsums(ones, [s365, sall])
    a, b = Hpl.wsums(top2, [s365, sall])
    put("h_pl_top2_365", _ratio(a, n365))
    put("h_pl_top2_all", _ratio(b, nall))
    a, b = Hpl.wsums(top3, [s365, sall])
    put("h_pl_top3_365", _ratio(a, n365))
    put("h_pl_top3_all", _ratio(b, nall))
    a, b = Hpl.wsums(pos6, [s365, sall])
    put("h_pl_pos365", _ratio(a, n365))
    put("h_pl_pos_all", _ratio(b, nall))
    f_pl_n_all = put("h_pl_n_all", nall)
    f_pl_win_all = put("h_pl_win_all", _ratio(Hpl.wsum(win, sall), nall))
    stv, stc = np.nan_to_num(st_pos), (~np.isnan(st_pos)).astype(np.float64)
    a, b = Hpl.wsums(stv, [s365, sall])
    c, d = Hpl.wsums(stc, [s365, sall])
    put("h_pl_st365", _ratio(a, c))
    f_pl_st_all = put("h_pl_st_all", _ratio(b, d))
    # この枠での勝ち方(決まり手別の勝ち / この枠の出走)
    for code, nm in ((1, "sashi"), (2, "makuri"), (3, "mz"), (4, "nuki")):
        put("h_pl_k%s_all" % nm, _ratio(Hpl.wsum(win * (kc == code), sall), nall))
    # この枠で負けた時、レースの決まり手は何だったか(1号艇なら弱点、2号艇の「逃げ」は逃がし率)
    lose = 1.0 - win
    f_lose = {}
    for code, nm in ((0, "nige"), (1, "sashi"), (2, "makuri"), (3, "mz")):
        f_lose[nm] = put("h_pl_lose_%s_all" % nm, _ratio(Hpl.wsum(lose * (kc == code), sall), nall))
    f_b1_win365 = _ratio(Hpl.wsum(win, s365), n365)
    del Hpl, a, b, c, d, n365, nall, s365, sall, lose
    # 選手全体: スタートのばらつき・相対スタート・スタート順・決まり手別
    Hp = _Hist(toban, day, res)
    p365, pall = Hp.start(365), Hp.gs
    pn365 = Hp.wsum(ones, p365)
    sc = Hp.wsum(stc, p365)
    sm = _ratio(Hp.wsum(stv, p365), sc)
    sq = _ratio(Hp.wsum(stv * stv, p365), sc)
    sd = np.sqrt(np.clip(sq - sm * sm, 0, None))
    sd[sc < 5] = np.nan
    put("h_st_sd365", sd)
    put("h_st_all", _ratio(Hp.wsum(stv, pall), Hp.wsum(stc, pall)))
    rv, rc = np.nan_to_num(strel), (~np.isnan(strel)).astype(np.float64)
    put("h_st_rel365", _ratio(Hp.wsum(rv, p365), Hp.wsum(rc, p365)))
    kv, kcn = np.nan_to_num(strank), (~np.isnan(strank)).astype(np.float64)
    put("h_st_rank365", _ratio(Hp.wsum(kv, p365), Hp.wsum(kcn, p365)))
    outer = (lane >= 2).astype(np.float64)
    on365 = Hp.wsum(outer, p365)
    for code, nm in ((1, "sashi"), (2, "makuri"), (3, "mz")):
        put("h_k_%s365" % nm, _ratio(Hp.wsum(win * (kc == code) * outer, p365), on365))
    put("h_k_nuki365", _ratio(Hp.wsum(win * (kc == 4), p365), pn365))
    del sc, sm, sq, sd, rv, rc, kv, kcn, outer, on365, pn365, strel, strank
    # 2b) 1号艇(と2号艇)の選手の値をレースの全艇に配る(float32 に丸めた値を元にする。実験と同じ)
    put("h_b1_win_all", _bcast(f_pl_win_all.astype(np.float64), R, 0))
    put("h_b1_n_all", _bcast(f_pl_n_all.astype(np.float64), R, 0))
    put("h_b1_lose_sashi_all", _bcast(f_lose["sashi"].astype(np.float64), R, 0))
    put("h_b1_lose_makuri_all", _bcast(f_lose["makuri"].astype(np.float64), R, 0))
    put("h_b1_lose_mz_all", _bcast(f_lose["mz"].astype(np.float64), R, 0))
    put("h_b1_st_all", _bcast(f_pl_st_all.astype(np.float64), R, 0))
    put("h_b1_win365", _bcast(f_b1_win365, R, 0))
    put("h_b2_lose_nige_all", _bcast(f_lose["nige"].astype(np.float64), R, 1))
    del f_pl_win_all, f_pl_n_all, f_lose, f_pl_st_all, f_b1_win365
    say("g2 lane done")

    # (g3 進入の癖は作らない)

    # ============ 4) モーターの実績 ============
    mi2 = _f64(df, "motor_in2", sel)
    # 入れ替え日の推定: 会場×日で「2連率0の行の割合」が半分超の日。年1回なので前の入れ替えから150日超だけ採る
    vd = pd.DataFrame({"v": venue, "d": day, "z": (mi2 == 0).astype(np.float64)}).groupby(["v", "d"])["z"].mean().reset_index()
    epoch = np.zeros(N, dtype=np.int64)
    ep_age = np.full(N, np.nan)
    for v, g in vd[vd["z"] > 0.5].groupby("v"):
        ds = g["d"].to_numpy()
        starts = [ds[0]]
        for x in ds[1:]:
            if x - starts[-1] > 150:
                starts.append(x)
        starts = np.array(starts)
        mv = venue == v
        k = np.searchsorted(starts, day[mv], "right")
        epoch[mv] = k
        ep_age[mv] = np.where(k > 0, day[mv] - starts[np.clip(k - 1, 0, None)], np.nan)
    del vd, mi2
    if epoch.max() >= EPOCH_MAX:
        raise ValueError("build_hist: モーターの入れ替えの期が %d 以上になった(上限 %d)" % (int(epoch.max()), EPOCH_MAX))
    motor_no = _f64(df, "motor_no", sel)
    boat_no = _f64(df, "boat_no", sel)
    n_mnan = int(np.isnan(motor_no).sum() + np.isnan(boat_no).sum())
    if n_mnan:
        say("motor_no / boat_no が無い行 %d。番号 0(存在しない番号)として扱う(当日行には番組表の値を入れること)" % n_mnan)
    motor_no = np.nan_to_num(motor_no, nan=0.0).astype(np.int64)
    boat_no = np.nan_to_num(boat_no, nan=0.0).astype(np.int64)
    mk = (venue * EPOCH_MAX + epoch) * 1024 + np.clip(motor_no, 0, 1023)
    Hm = _Hist(mk, day, res)
    exv, exc = np.nan_to_num(exrel), (~np.isnan(exrel)).astype(np.float64)
    rsv = np.nan_to_num(resid)
    for w, tag in ((60, "60"), (120, "120"), (None, "ep")):
        s = Hm.gs if w is None else Hm.start(w)
        n = Hm.wsum(ones, s)
        put("h_m_n" + tag, n)
        if w is not None:
            put("h_m_pos" + tag, _ratio(Hm.wsum(pos6, s), n))
            put("h_m_top2_" + tag, _ratio(Hm.wsum(top2, s), n))
        fx = put("h_m_exrel" + tag, _ratio(Hm.wsum(exv, s), Hm.wsum(exc, s)))
        fr = put("h_m_res" + tag, _ratio(Hm.wsum(rsv, s), n))      # 乗り手の実力と枠から期待される成績との差
        if tag == "120":
            f_m_exrel120, f_m_res120 = fx, fr
    put("h_m_age", ep_age)
    put("h_m_res120_rel", _race_rel(f_m_res120.astype(np.float64), R))
    put("h_m_exrel120_rel", _race_rel(f_m_exrel120.astype(np.float64), R))
    del Hm, mk, epoch, ep_age, motor_no, f_m_res120
    Hb = _Hist(venue * 1024 + np.clip(boat_no, 0, 1023), day, res)
    b120 = Hb.start(120)
    bn = Hb.wsum(ones, b120)
    put("h_b_n120", bn)
    put("h_b_res120", _ratio(Hb.wsum(rsv, b120), bn))
    put("h_b_exrel120", _ratio(Hb.wsum(exv, b120), Hb.wsum(exc, b120)))
    del Hb, b120, bn, boat_no
    say("g4 motor done")

    # ============ 5) 節間の調子 ============
    # 節の区切りの推定: 選手→日付→元の並び に並べ、選手が変わる / 会場が変わる / 4日超あく で新しい節
    o = np.lexsort((np.arange(N), day, toban))
    tb, dy, vn = toban[o], day[o], venue[o]
    new = np.ones(N, dtype=bool)
    new[1:] = (tb[1:] != tb[:-1]) | (vn[1:] != vn[:-1]) | ((dy[1:] - dy[:-1]) > 4)
    sid_s = np.cumsum(new) - 1
    sid = np.empty(N, dtype=np.int64)
    sid[o] = sid_s
    sday = np.empty(N)
    sday[o] = dy - dy[new][sid_s]
    del o, tb, dy, vn, new, sid_s
    Hs = _Hist(sid, day, res)
    sn = Hs.wsum(ones, Hs.gs)
    put("h_s_day", sday)                                                # 節の何日目か(0=初日)
    put("h_s_n", sn)
    f_s_pos = put("h_s_pos", _ratio(Hs.wsum(pos6, Hs.gs), sn))
    put("h_s_win", _ratio(Hs.wsum(win, Hs.gs), sn))
    put("h_s_top3", _ratio(Hs.wsum(top3, Hs.gs), sn))
    f_s_exrel = put("h_s_exrel", _ratio(Hs.wsum(exv, Hs.gs), Hs.wsum(exc, Hs.gs)))
    put("h_s_st", _ratio(Hs.wsum(stv, Hs.gs), Hs.wsum(stc, Hs.gs)))
    f_s_res = put("h_s_res", _ratio(Hs.wsum(rsv, Hs.gs), sn))
    put("h_s_pos_rel", _race_rel(f_s_pos.astype(np.float64), R))
    put("h_s_res_rel", _race_rel(f_s_res.astype(np.float64), R))
    put("h_s_exrel_rel", _race_rel(f_s_exrel.astype(np.float64), R))
    del Hs, sid, sday, sn, f_s_pos, f_s_exrel, f_s_res, stv, stc
    say("g5 series done")

    # ============ 6) 近況 ============
    for n_ in (5, 20):
        s = Hp.lastn(n_)
        put("h_pos%d" % n_, _ratio(Hp.wsum(pos6, s), Hp.e - s))
    for n_ in (10, 30):
        s = Hp.lastn(n_)
        put("h_res%d" % n_, _ratio(Hp.wsum(rsv, s), Hp.e - s))
    hd = day[Hp.idx].astype(np.float64)
    hg = toban[Hp.idx]
    has = Hp.e > Hp.gs
    last = np.clip(Hp.e - 1, 0, None)
    d0 = float(day.min())
    # 重み exp(−(当日 − その走の日)/tau) の平均。exp((日 − 最初の日)/tau) の累積和の比で書くと最初の日に依らない
    for tau, vals in ((30.0, (("win", win), ("pos", pos6))), (90.0, (("win", win), ("top3", top3)))):
        wgt = np.exp((hd - d0) / tau)
        cw = pd.Series(wgt).groupby(hg).cumsum().to_numpy()
        for nm, v in vals:
            cwv = pd.Series(wgt * v[Hp.idx]).groupby(hg).cumsum().to_numpy()
            o6 = np.full(N, np.nan)
            o6[has] = cwv[last[has]] / cw[last[has]]
            put("h_%s_ew%d" % (nm, int(tau)), o6)
        del wgt, cw, cwv
    o6 = np.full(N, np.nan)
    o6[has] = day[has] - hd[last[has]]
    put("h_days_since", o6)
    o6 = np.full(N, np.nan)
    o6[has] = pos6[Hp.idx][last[has]]
    put("h_last_pos", o6)
    put("h_n30", Hp.wsum(ones, Hp.start(30)))
    del Hp, hd, hg, has, last, rsv, resid
    Hpv = _Hist(toban * 32 + venue, day, res)
    hasv = Hpv.e > Hpv.gs
    o6 = np.full(N, np.nan)
    o6[hasv] = day[hasv] - day[Hpv.idx][np.clip(Hpv.e - 1, 0, None)[hasv]]
    put("h_days_since_venue", o6)
    del Hpv, hasv, o6
    say("g6 recent done")

    # ============ 7) 展示タイムの癖 ============
    He = _Hist(toban, day, ~np.isnan(exrel))
    e365 = He.start(365)
    en = He.wsum(ones, e365)
    exh = _ratio(He.wsum(exv, e365), en)
    put("h_exh365", exh)
    aux[sel] = exh                                                      # float64 のまま保存(live_hist_update 用)
    put("h_exh_all", _ratio(He.wsum(exv, He.gs), He.wsum(ones, He.gs)))
    put("h_exh_n365", en)
    mex = f_m_exrel120.astype(np.float64)
    for nm, arr in _live_cols(exrel, exh, mex).items():                 # 当日の展示を使う4列
        put(nm, arr)
    del He, e365, en, exh, mex, exv, exc, f_m_exrel120
    say("g7 exhabit done")

    # ============ 8) レースの属性 ============
    dl = _obj(df, "deadline", sel)
    u, inv = np.unique(dl.astype(str), return_inverse=True)
    mins = np.array([(int(x.split(":")[0]) * 60 + int(x.split(":")[1])) if ":" in x else np.nan for x in u], dtype=np.float64)
    put("h_dl_min", mins[inv])
    dts = pd.to_datetime(df["date"].astype(str).to_numpy()[sel], format="%Y%m%d")
    put("h_dow", dts.dayofweek.to_numpy())
    put("h_month", dts.month.to_numpy())
    put("h_dist1200", (_f64(df, "distance", sel) == 1200).astype(np.float64))
    nat = _f64(df, "nat_win", sel)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        put("h_race_natwin", np.repeat(np.nanmean(nat.reshape(R, 6), axis=1), 6))
    put("h_race_nA1", np.repeat((ci.reshape(R, 6) == 4).sum(1), 6))
    put("h_b1_class", _bcast(ci, R, 0))
    rt_code = pd.factorize(pd.Series(_obj(df, "race_type", sel)))[0].astype(np.int64) + 1   # 欠損は 0
    if rt_code.max() >= RTYPE_MAX:
        raise ValueError("build_hist: レース名の種類が %d 以上(上限 %d)" % (int(rt_code.max()), RTYPE_MAX))
    Hrt = _Hist((venue * RTYPE_MAX + rt_code) * 8 + lane, day, res)
    r365 = Hrt.start(365)
    rn = Hrt.wsum(ones, r365)
    put("h_vrt_n365", rn)                                               # 会場×レース名×枠 の過去365日
    put("h_vrt_win365", _ratio(Hrt.wsum(win, r365), rn))
    put("h_vrt_top3_365", _ratio(Hrt.wsum(top3, r365), rn))
    del Hrt, r365, rn, rt_code
    Hrn = _Hist((venue * 16 + race_no) * 8 + lane, day, res)
    q365 = Hrn.start(365)
    qn = Hrn.wsum(ones, q365)
    put("h_vrn_win365", _ratio(Hrn.wsum(win, q365), qn))                # 会場×レース番号×枠 の過去365日
    put("h_vrn_top3_365", _ratio(Hrn.wsum(top3, q365), qn))
    del Hrn, q365, qn
    say("g8 race done (rows %d, races %d)" % (N, R))

    feats = pd.DataFrame(out, index=df.index, columns=HIST_COLS)
    feats[HIST_AUX_COLS[0]] = aux
    return feats


def _col(base, name):
    if isinstance(base, pd.DataFrame):
        return base[name].to_numpy() if name in base.columns else None
    if isinstance(base, dict):
        return np.asarray(base[name]) if name in base else None
    raise TypeError("base は DataFrame か dict")


def live_hist_update(ex_time_by_row, base, race_id=None):
    """当日の展示に依存する4列(HIST_LIVE_COLS)を、朝に作った値と展示タイムから作る。
    ex_time_by_row: 行ごとの展示タイム(無い艇は NaN)。base: 同じ行の h_m_exrel120 と _aux_exh365 を持つ
    DataFrame か dict(build_hist の戻り値や、当日分の保存から切り出した行)。
    race_id が無ければ 6行ずつが1レース(レース内の順は問わない)。
    戻り値: 4列の DataFrame(float32。base が DataFrame ならその index)。
    build_hist と同じ式・同じ型で計算するので、全履歴から作り直した値とビット単位で一致する。"""
    ex = np.asarray(ex_time_by_row, dtype=np.float64)
    mex = _col(base, "h_m_exrel120")
    if mex is None:
        raise ValueError("live_hist_update: base に h_m_exrel120 が無い")
    mex = mex.astype(np.float32).astype(np.float64)          # 保存は float32。build_hist も float32 の値から作る
    exh = _col(base, HIST_AUX_COLS[0])
    if exh is None:
        exh = _col(base, "h_exh365")
        if exh is None:
            raise ValueError("live_hist_update: base に %s も h_exh365 も無い" % HIST_AUX_COLS[0])
        print("hist: live_hist_update は float32 の h_exh365 から作る(%s が無い。最後の1ビットがずれ得る)" % HIST_AUX_COLS[0], flush=True)
    exh = np.asarray(exh, dtype=np.float64)
    if not (len(ex) == len(mex) == len(exh)):
        raise ValueError("live_hist_update: 行数が合わない")
    exrel = _race_exrel(ex, race_id)
    cols = {k: np.asarray(v, dtype=np.float32) for k, v in _live_cols(exrel, exh, mex).items()}
    idx = base.index if isinstance(base, pd.DataFrame) else None
    return pd.DataFrame(cols, index=idx, columns=HIST_LIVE_COLS)

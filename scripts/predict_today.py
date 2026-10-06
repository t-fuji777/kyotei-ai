# -*- coding: utf-8 -*-
"""当日予測: 本日の番組表(B)を取得し、モデルで全レースの3連単買い目と厳選の判定を生成。
出力: docs/predictions/YYYYMMDD.json と latest.json

モデルの世代(gen)は読み込んだモデルの meta.json で決まる(コードの定数は見ない):
  世代1(形1: model_win/top2/top3.txt): 現行45特徴量・二値3本 + 3段の式。今までどおり。
  世代2(形2: model.txt): 153特徴量(現行45 + 履歴108)・条件づけ分解ロジット1本で120通りを直接出す。
各レースには「その買い目を作ったモデルの世代」g を書き、しきい値(厳選)と較正表はレースの g で引く
(common.sengen_cfg_for / race_gen)。トップレベルの model_gen は朝の予測を行ったモデルの世代(参考情報)。

世代2の当日分の特徴量は data/feat_cache/<ymd>.npz に保存する(git には入れない)。朝の予測が作り、直前の
再予測は保存を読んで直前に変わる9列(LIVE_COLS_V2)だけ埋めて予測する(履歴108列を毎回作り直さない)。
保存が無い・入力の指紋が合わない時はその場で1回作って保存する。

失敗に強く: 候補のモデルごとに「読む → 予測する」までを try に入れ、例外なら次の候補(別の形・世代の
予備を含む)へ落ちる。全候補で失敗した時だけ止まる。"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from common import download_day, parse_b, VENUES, is_sengen, sengen_cfg_for, race_gen
from common import SENGEN_TOP3P_MIN, SENGEN_MIN_ODDS, SENGEN_EXCLUDE_VENUES  # noqa: F401 (互換のため残す)
from features import add_features, load_fan, FEATURES, FEATURES_V2, LIVE_COLS_V2, add_features_v2
from features_hist import HIST_LIVE_COLS, HIST_AUX_COLS, FEATURES_HIST_VERSION, ELO_CFG, live_hist_update
from entries_io import load_entries, entries_files
from train import trifecta_probs
import model_v2
import fetch_result
from fetch_result import fetch_before_html, parse_before, ex_complete, absent_lanes

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))
# 世代2の当日分の特徴量の保存先(.gitignore 済み)。テストは差し替える
FEAT_CACHE_DIR = ROOT / "data" / "feat_cache"
FEAT_CACHE_VERSION = 1          # 保存の形を変えたら上げる(古い保存は指紋が合わず作り直される)
_V2_IDX = {c: i for i, c in enumerate(FEATURES_V2)}
_LIVE_IDX = [_V2_IDX[c] for c in LIVE_COLS_V2]


def load_hist(before_ymd=None) -> pd.DataFrame:
    """entries 全部(entries_io.load_entries。今までの読み込みと同じ行・同じ順)。
    before_ymd があればその日より前だけ(世代2で過去の日を作り直す時に、対象日以降の行を履歴に入れないため)。"""
    return load_entries(before_ymd)


def _model_candidates():
    """使うモデルの候補を、優先順に [(ディレクトリ, 種別)] で返す。

    置き場と選び方は scripts/model_store.py を参照(この場で学習した data/model_build、
    Release から取得した data/model_live、git の予備 data/model のうち、検査を通るものを
    学習時刻の新しい順に)。参照先(data/model/live_pointer.json)が無い間は予備だけになる。
    取得や候補選びの不具合では予測を止めない。
    """
    try:
        import model_store
    except Exception as e:
        print(f"model: model_store を読み込めない({type(e).__name__}: {e})。git の data/model を使う", flush=True)
        return [(ROOT / "data" / "model", "frozen")]
    try:
        model_store.fetch()
    except Exception as e:
        print(f"model: 取得の不具合({type(e).__name__}: {e})。予測は止めず、手元にあるモデルで続行する", flush=True)
    try:
        return [(d, kind) for d, _meta, kind in model_store.candidates()]
    except Exception as e:
        print(f"model: 候補を選べない({type(e).__name__}: {e})。git の data/model を使う", flush=True)
        return [(ROOT / "data" / "model", "frozen")]


def model_gen(meta) -> int:
    """meta.json の世代(無ければ1)。"""
    try:
        g = (meta or {}).get("gen")
        return 1 if g is None else int(g)
    except Exception:
        return 1


def model_format(meta) -> int:
    """meta.json の形(無ければ1)。model_store があればその判定(知らない形は None)、無ければ自前。"""
    try:
        import model_store
        return model_store.model_format(meta)
    except ImportError:
        f = (meta or {}).get("format", 1)
        return f if (isinstance(f, int) and not isinstance(f, bool) and f in (1, 2)) else None


_FORMAT_FILES = {1: ("model_win.txt", "model_top2.txt", "model_top3.txt"), 2: ("model.txt",)}


def _crlf_files(d, meta, fmt):
    """改行が CRLF のモデルファイル(その形の全ファイルを見る)。model_store.crlf_model_files と同じ検査。"""
    try:
        import model_store
        return model_store.crlf_model_files(d, meta)
    except ImportError:
        bad = []
        for n in _FORMAT_FILES.get(fmt, ()):
            with open(Path(d) / n, "rb") as f:
                if f.read(6) == b"tree\r\n":
                    bad.append(n)
        return bad


def check_meta_v2(meta, booster):
    """形2の一式の読み込み時の検査(設計 3.3 末尾)。合わなければ ValueError(その候補を使わない)。
    列名の一覧・段の特徴・カテゴリの位置・列数は、特徴量を作るコードと一致していなければ予測が狂う。
    実力値の設定(elo_cfg)も同じ。特徴量コードの版(features_version)の違いは警告だけ(値の式を変えた時に
    上げる版なので、違えば学習し直しが要るが、その日の予測を止めるほどではない)。"""
    feats = meta.get("features")
    if list(feats or []) != list(FEATURES_V2):
        raise ValueError("meta の features(%s個)が FEATURES_V2(%d個)と違う" % (
            len(feats) if isinstance(feats, list) else "?", len(FEATURES_V2)))
    if list(meta.get("stage_cols") or []) != list(model_v2.STAGE_COLS):
        raise ValueError("meta の stage_cols が model_v2.STAGE_COLS と違う")
    if [int(x) for x in (meta.get("cat_idx") or [])] != list(model_v2.CAT_IDX):
        raise ValueError("meta の cat_idx が model_v2.CAT_IDX と違う")
    nf = booster.num_feature()
    if nf != len(FEATURES_V2) + model_v2.N_STAGE:
        raise ValueError("モデルの列数 %d != %d + %d" % (nf, len(FEATURES_V2), model_v2.N_STAGE))
    ec = meta.get("elo_cfg")
    if isinstance(ec, dict):
        for k, v in ELO_CFG.items():
            if k not in ec or abs(float(ec[k]) - float(v)) > 1e-12:
                raise ValueError("meta の elo_cfg(%s)がコードの設定(%s)と違う" % (ec, ELO_CFG))
    fv = meta.get("features_version")
    if fv is not None and str(fv) != FEATURES_HIST_VERSION:
        print(f"model: 注意: meta の features_version {fv} != コード {FEATURES_HIST_VERSION}(学習し直しが要る)", flush=True)


def iter_models():
    """候補のモデルを優先順に読み、読めたものから順に (meta, models, sengen) を返す生成器。
    形1なら models は {"win","top2","top3"}、形2なら {"pl"}。meta["gen"] / meta["format"] を必ず入れる。
    読めない候補(壊れている・CRLF・形2の meta と列が合わない)は飛ばす。"""
    for d, kind in _model_candidates():
        try:
            d = Path(d)
            m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            fmt = model_format(m)
            if fmt is None:
                raise ValueError("知らない形(format=%r)" % (m.get("format"),))
            # 改行が CRLF のモデル(Windows の作業ツリーにある git の予備など)を LightGBM に渡すと、
            # 例外にならずプロセスごと落ちる。渡す前に弾く(Actions 上のファイルは LF なので該当しない)。
            bad = _crlf_files(d, m, fmt)
            if bad:
                print(f"model: {kind} は改行が CRLF なので使えない({', '.join(bad)})。次の候補へ", flush=True)
                continue
            if fmt == 2:
                b = {"pl": lgb.Booster(model_file=str(d / "model.txt"))}
                check_meta_v2(m, b["pl"])
            else:
                b = {t: lgb.Booster(model_file=str(d / f"model_{t}.txt"))
                     for t in ("win", "top2", "top3")}
            m["gen"] = model_gen(m)
            m["format"] = fmt
            print(f"model: {kind} trained_at={m['trained_at']}", flush=True)
        except Exception as e:
            print(f"model: {kind} を読めない({type(e).__name__}: {e})。次の候補へ", flush=True)
            continue
        # sengen(厳選)判定は common.is_sengen()に統一(top3p閾値+除外会場)。
        # ここで組み立てるsengen dictはload_models呼び出し元との互換のため残すが、
        # is_senの判定自体には使わない(meta由来のvenues許可リストは廃止)。
        sengen = {"top5_min": 0.40, "venues": list(range(1, 25)), **m.get("sengen", {})}
        yield m, b, sengen


def load_models():
    """最初に読めた候補の (meta, models, sengen)。どれも読めなければ SystemExit。"""
    for meta, models, sengen in iter_models():
        return meta, models, sengen
    raise SystemExit("使えるモデルが無い(data/model_build / data/model_live / data/model のどれも読めない)")


def confidence(top1p: float) -> str:
    if top1p >= 0.15:
        return "A"
    if top1p >= 0.08:
        return "B"
    return "C"


def races_to_rows(races, live=None):
    """parse_bのrace群 -> エントリ行リスト。live={(venue,rno):{ex,wind,wave,absent,...}}で直前情報を注入。
    distance / motor_no / boat_no は世代2の履歴の特徴量(h_dist1200 / h_m_* / h_b_*)に要る
    (世代1の特徴量は使わない列なので世代1の結果は変わらない)。absent は欠場艇の印(直前の再予測だけ)。"""
    rows = []
    for r in races:
        key = (r["venue"], r["race_no"])
        inf = (live or {}).get(key)
        for rc in r["racers"]:
            row = {"date": r["date"], "venue": r["venue"], "race_no": r["race_no"],
                   "race_type": r["race_type"], "deadline": r["deadline"],
                   "day_n": r.get("day_n"), "distance": r.get("distance"),
                   "lane": rc["lane"], "toban": rc["toban"], "name": rc["name"],
                   "age": rc["age"], "weight": rc["weight"], "class": rc["class"],
                   "nat_win": rc["nat_win"], "nat_in2": rc["nat_in2"],
                   "loc_win": rc["loc_win"], "loc_in2": rc["loc_in2"],
                   "motor_no": rc.get("motor_no"), "motor_in2": rc["motor_in2"],
                   "boat_no": rc.get("boat_no"), "boat_in2": rc["boat_in2"]}
            if inf:
                row["ex_time"] = inf["ex"].get(rc["lane"])
                row["wind"] = inf.get("wind")
                row["wave"] = inf.get("wave")
                row["absent"] = rc["lane"] in (inf.get("absent") or [])
            rows.append(row)
    return rows


def _absent_flags(tgt):
    """行ごとの欠場の印(bool)。列が無ければ全部 False。"""
    if "absent" not in tgt.columns:
        return np.zeros(len(tgt), dtype=bool)
    return tgt["absent"].map(lambda v: bool(v) if isinstance(v, (bool, np.bool_)) else False).to_numpy(dtype=bool)


def _race_header(g):
    day_n_val = g["day_n"].iloc[0] if "day_n" in g.columns else None
    return {"no": int(g["race_no"].iloc[0]), "type": g["race_type"].iloc[0],
            "deadline": g["deadline"].iloc[0],
            "day_n": (int(day_n_val) if day_n_val is not None and not pd.isna(day_n_val) else None)}


# ---------------------------------------------------------------- 世代2: 当日分の特徴量の保存(feat_cache)
def feat_cache_path(ymd):
    return Path(FEAT_CACHE_DIR) / f"{ymd}.npz"


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def card_fingerprint(races):
    """番組表の行の指紋(会場・レース・艇の全項目。並びに依らない)。"""
    rs = sorted(races, key=lambda r: (int(r["venue"]), int(r["race_no"])))
    txt = json.dumps(rs, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()


# 特徴量を作るコード(この中身が変われば保存の値も変わり得る)。指紋に sha256 を入れる
_FEAT_CODE_FILES = ("features.py", "features_hist.py")


def feat_code_fingerprint():
    """特徴量を作るファイルの [名前, sha256]。手で上げる FEATURES_HIST_VERSION だけに頼らず、式を直した
    commit が入った日に、開催中ループ(周回ごとに git reset --hard でコードを取り直す)が古いコードで作った
    保存(.gitignore で残る)を使い続けないため。数十KB のハッシュなので毎分呼んでも無視できる。"""
    out = []
    for n in _FEAT_CODE_FILES:
        p = Path(__file__).parent / n
        out.append([n, _sha256_file(p) if p.exists() else None])
    return out


def input_fingerprint(ymd, races):
    """保存の鍵(JSON 文字列)。日付・特徴量コードの版と中身(sha256)・entries 各ファイルの sha256・
    ファン手帳の一覧・番組表の指紋。daily が前日の結果を直して entries が変わった時や、番組表が変わった時、
    特徴量のコードが変わった時に古い保存を使い続けないため。"""
    fan_dir = ROOT / "data" / "fan"
    fan_files = sorted(str(p) for p in fan_dir.glob("fan_*.csv.gz")) if fan_dir.exists() else []
    fp = {"v": FEAT_CACHE_VERSION, "date": str(ymd), "features_version": FEATURES_HIST_VERSION,
          "n_features": len(FEATURES_V2), "feat_code": feat_code_fingerprint(),
          "entries": [[Path(f).name, _sha256_file(f)] for f in entries_files()],
          "fan": [[Path(f).name, _sha256_file(f)] for f in fan_files],
          "card": card_fingerprint(races)}
    return json.dumps(fp, sort_keys=True)


def _cache_dict(rows_df, feats, fingerprint=None):
    """行の表(races_to_rows の形)と add_features_v2 の戻り値から、保存の中身(dict)を作る。
    153列は float32、直前に変わる9列は NaN(朝の状態)、中間値 _aux_exh365 は float64。"""
    X = feats[FEATURES_V2].to_numpy(dtype=np.float32, copy=True)
    X[:, _LIVE_IDX] = np.nan
    return {"X": X,
            "aux": feats[HIST_AUX_COLS[0]].to_numpy(dtype=np.float64, copy=True),
            "venue": rows_df["venue"].to_numpy(dtype=np.int64),
            "race_no": rows_df["race_no"].to_numpy(dtype=np.int64),
            "lane": rows_df["lane"].to_numpy(dtype=np.int64),
            "toban": pd.to_numeric(rows_df["toban"], errors="coerce").fillna(-1).to_numpy(dtype=np.int64),
            "fingerprint": fingerprint}


def build_feat_cache(ymd, races, hist, fan, fingerprint=None):
    """当日の全レースの153列(直前の9列は NaN)を作る。履歴(hist)の後ろに当日の全行を足して build_hist を
    1回通す(学習と同じ関数・同じ入口)。対象レースだけでなく当日の全レースを渡すこと(モーターの入れ替え
    判定がその日・その会場の全行で決まるため)。"""
    rows_df = pd.DataFrame(races_to_rows(races))
    feats = add_features_v2(hist, rows_df, fan=fan)
    return _cache_dict(rows_df, feats, fingerprint)


def save_feat_cache(ymd, cache):
    p = feat_cache_path(ymd)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp{os.getpid()}")
    np.savez_compressed(tmp, X=cache["X"], aux=cache["aux"], venue=cache["venue"], race_no=cache["race_no"],
                        lane=cache["lane"], toban=cache["toban"], cols=np.array(FEATURES_V2),
                        fingerprint=np.array(cache["fingerprint"] or ""))
    # np.savez_compressed は拡張子 .npz を足す
    src = tmp if tmp.exists() else Path(str(tmp) + ".npz")
    os.replace(src, p)
    return p


def load_feat_cache(ymd, fingerprint):
    """保存を読む。無い・読めない・指紋が合わない・列が合わない時は None。"""
    p = feat_cache_path(ymd)
    if not p.exists():
        return None
    try:
        with np.load(p, allow_pickle=False) as z:
            if str(z["fingerprint"].item()) != fingerprint:
                print("feat_cache: 入力の指紋が合わない(entries・番組表・コードの版が変わった)。作り直す", flush=True)
                return None
            if list(z["cols"]) != list(FEATURES_V2) or z["X"].shape[1] != len(FEATURES_V2):
                print("feat_cache: 列が合わない。作り直す", flush=True)
                return None
            return {k: z[k].copy() for k in ("X", "aux", "venue", "race_no", "lane", "toban")} | {"fingerprint": fingerprint}
    except Exception as e:
        print(f"feat_cache: 読めない({type(e).__name__}: {e})。作り直す", flush=True)
        return None


def get_feat_cache(ymd, races, hist=None, fan=None, save=True):
    """保存があって指紋が合えばそれを読み、無ければその場で作って保存する(約35秒・約4.7GB。performance.md 2.2)。"""
    import time
    fp = input_fingerprint(ymd, races)
    c = load_feat_cache(ymd, fp)
    if c is not None:
        print(f"feat_cache: 保存を使う {feat_cache_path(ymd).name} ({len(c['X'])} 行)", flush=True)
        return c
    t0 = time.time()
    if hist is None:
        hist = load_hist(before_ymd=ymd)
    if fan is None:
        fan = load_fan(ROOT / "data" / "fan")
    c = build_feat_cache(ymd, races, hist, fan, fp)
    msg = ""
    if save:
        try:
            save_feat_cache(ymd, c)
            msg = f" 保存 {feat_cache_path(ymd).name}"
        except Exception as e:
            msg = f" 保存できない({type(e).__name__}: {e})"
    print(f"feat_cache: 作った({len(c['X'])} 行, {time.time() - t0:.1f}s){msg}", flush=True)
    return c


def live_matrix(cache, tgt):
    """保存(または同じ形の dict)の行に、対象レースの直前情報9列(LIVE_COLS_V2)を埋めて (R,6,153) を作る。

    tgt: races_to_rows の形の行(対象レース。ex_time / wind / wave / absent は直前情報があれば入っている)。
    ex_rank(レース内の順位 method=min)/ ex_diff(最速との差)は features.add_features と同じ式、
    履歴の4列は features_hist.live_hist_update(build_hist と同じ式)で作る。
    戻り値: (X6, keys, absent, rows)。keys は (venue, race_no) の一覧(会場→レース番号の順)、
    absent は (R,6) の真偽(欠場艇が1つも無ければ None)、rows は keys の順・枠順に並べた行の表。
    6艇そろわないレースは SKIP(今までと同じ)。保存に無いレース・登番の違うレースは ValueError。"""
    key2row = {(int(v), int(r), int(ln)): i
               for i, (v, r, ln) in enumerate(zip(cache["venue"], cache["race_no"], cache["lane"]))}
    t = tgt.sort_values(["venue", "race_no", "lane"], kind="stable").reset_index(drop=True)
    groups, keys, idx_all = [], [], []
    for (venue, rno), g in t.groupby(["venue", "race_no"], sort=True):
        if len(g) != 6 or list(g["lane"].astype(int)) != [1, 2, 3, 4, 5, 6]:
            print(f"SKIP {int(venue)}-{int(rno)}R: parsed {len(g)} boats (need 6)", flush=True)
            continue
        idx = [key2row.get((int(venue), int(rno), int(ln))) for ln in g["lane"]]
        if any(i is None for i in idx):
            raise ValueError(f"feat_cache に {int(venue)}-{int(rno)}R の行が無い")
        tb = pd.to_numeric(g["toban"], errors="coerce").fillna(-1).to_numpy(dtype=np.int64)
        if (cache["toban"][idx] != tb).any():
            raise ValueError(f"feat_cache の登番が番組表と違う {int(venue)}-{int(rno)}R")
        groups.append(g)
        keys.append((int(venue), int(rno)))
        idx_all.extend(idx)
    if not groups:
        return None, [], None, None
    rows = pd.concat(groups, ignore_index=True)
    idx_all = np.asarray(idx_all, dtype=np.int64)
    R = len(keys)
    X = cache["X"][idx_all].astype(np.float32, copy=True)
    aux = np.asarray(cache["aux"], dtype=np.float64)[idx_all]

    def col(name):
        if name not in rows.columns:
            return np.full(len(rows), np.nan)
        return pd.to_numeric(rows[name], errors="coerce").to_numpy(dtype=np.float64)

    ex = col("ex_time")
    rid = np.repeat(np.arange(R), 6)
    s = pd.Series(ex)
    gb = s.groupby(rid)
    X[:, _V2_IDX["ex_time"]] = ex
    X[:, _V2_IDX["ex_rank"]] = gb.rank(method="min").to_numpy(dtype=np.float64)
    X[:, _V2_IDX["ex_diff"]] = (s - gb.transform("min")).to_numpy(dtype=np.float64)
    X[:, _V2_IDX["wind"]] = col("wind")
    X[:, _V2_IDX["wave"]] = col("wave")
    upd = live_hist_update(ex, {"h_m_exrel120": X[:, _V2_IDX["h_m_exrel120"]], HIST_AUX_COLS[0]: aux})
    for c in HIST_LIVE_COLS:
        X[:, _V2_IDX[c]] = upd[c].to_numpy()
    ab = _absent_flags(rows).reshape(R, 6)
    return X.reshape(R, 6, len(FEATURES_V2)), keys, (ab if ab.any() else None), rows


# ---------------------------------------------------------------- 予測
def _predict_races_v1(tgt, hist, fan, models, cfg):
    """世代1(現行45特徴量・二値3本 + 3段の式)。今までのコードのまま(is_sengen に cfg、各レースに g=1、
    欠場艇の強さを 1e-9 にする行を足しただけ。欠場の印が無ければ結果は今と同じ)。"""
    tgt = add_features(hist, tgt, fan=fan)
    tgt["p_raw"] = models["win"].predict(tgt[FEATURES])
    tgt["p_top2"] = models["top2"].predict(tgt[FEATURES])
    tgt["p_top3"] = models["top3"].predict(tgt[FEATURES])
    ab = _absent_flags(tgt)
    if ab.any():
        # 欠場艇(直前情報で展示タイムが空・is-miss)は3つの強さを 1e-9 にして買い目から外す(設計 4.5)
        tgt.loc[ab, ["p_raw", "p_top2", "p_top3"]] = 1e-9
    tgt["p_norm"] = tgt.groupby(["venue", "race_no"])["p_raw"].transform(lambda s: s / s.sum())

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
        # かつ5R以降(1-4Rはモデルの高確率帯が信用できないため対象外)。しきい値は世代の cfg
        top3p = sum(p["p"] for p in picks[:3])
        is_sen = is_sengen(top3p, venue, rno, cfg)
        n_sengen += int(is_sen)
        boats = []
        for _, x in g.iterrows():
            boats.append({"lane": int(x["lane"]), "name": x["name"], "cls": x["class"],
                          "wp": round(float(x["p_norm"]), 3)})
        race_obj = _race_header(g)
        race_obj.update({"boats": boats, "picks": picks,
                         "conf": confidence(picks[0]["p"]),
                         "fuku": {"lane": int(g["lane"].to_numpy()[fav]),
                                  "p": round(fav_p2, 3)},
                         "sengen": is_sen, "g": 1})
        by_venue.setdefault(int(venue), []).append(race_obj)
    return by_venue, n_sengen


def _predict_races_v2(tgt, hist, fan, models, cfg, cache=None):
    """世代2(153列 → 156行 → 120通り)。cache(当日分の保存)があればその行に直前の9列を埋めて使う。
    無ければ add_features_v2(hist, tgt, fan) でその場で作る(tgt はその日の全レースの行であること)。
    picks は上位10点(p は小数4桁)、boats[].wp は1着の確率(小数3桁。合計1)、fuku は1着確率が最大の艇と
    その艇が2着以内に入る確率、conf は今と同じしきい値、sengen は is_sengen(top3p, venue, rno, cfg)。"""
    booster = models["pl"]
    if cache is None:
        feats = add_features_v2(hist, tgt, fan=fan)
        cache = _cache_dict(tgt, feats)
        del feats
    X6, keys, absent, rows = live_matrix(cache, tgt)
    by_venue = {}
    n_sengen = 0
    if X6 is None:
        return by_venue, n_sengen
    P = model_v2.predict_P(booster, X6, absent_mask=absent)
    wp = model_v2.win_probs(P)
    p2 = model_v2.top2_probs(P)
    for i, (venue, rno) in enumerate(keys):
        g = rows.iloc[i * 6:(i + 1) * 6]
        order = np.argsort(-P[i], kind="stable")[:10]
        picks = [{"c": str(model_v2.COMBO_STR[j]), "p": round(float(P[i, j]), 4)} for j in order]
        fav = int(np.argmax(wp[i]))
        top3p = sum(p["p"] for p in picks[:3])
        is_sen = is_sengen(top3p, venue, rno, cfg)
        n_sengen += int(is_sen)
        boats = [{"lane": int(x["lane"]), "name": x["name"], "cls": x["class"],
                  "wp": round(float(wp[i, k]), 3)} for k, (_, x) in enumerate(g.iterrows())]
        race_obj = _race_header(g)
        race_obj.update({"boats": boats, "picks": picks,
                         "conf": confidence(picks[0]["p"]),
                         "fuku": {"lane": int(g["lane"].to_numpy()[fav]),
                                  "p": round(float(p2[i, fav]), 3)},
                         "sengen": is_sen, "g": 2})
        by_venue.setdefault(int(venue), []).append(race_obj)
    return by_venue, n_sengen


def predict_races(tgt: pd.DataFrame, hist, fan, models, sengen, meta=None, cfg=None, cache=None):
    """エントリ行DF -> {venue_code: [race_obj,...]}, 厳選数。世代(meta["gen"]。meta が無ければ models の形)で分かれる。
    cfg は厳選のしきい値(common.sengen_cfg_for。無ければ世代の表)。cache は世代2の当日分の保存(無ければその場で作る)。
    sengen は呼び出し元との互換のために受け取るだけ(判定は is_sengen)。"""
    gen = model_gen(meta) if meta is not None else (2 if "pl" in models else 1)
    if cfg is None:
        cfg = sengen_cfg_for(gen)
    if gen == 2:
        return _predict_races_v2(tgt, hist, fan, models, cfg, cache=cache)
    return _predict_races_v1(tgt, hist, fan, models, cfg)


def predict_live_rows(ymd, card_races, tgt, meta, models, sengen):
    """直前の再予測の共通部分(predict_live と update_live が使う)。
    世代1: 履歴を読んで今までどおり predict_races。世代2: 当日分の保存(無ければ作る)から9列だけ更新して予測。
    card_races はその日の番組表の全レース(保存を作る時に要る)、tgt は対象レースの行(直前情報入り)。"""
    gen = model_gen(meta)
    cfg = sengen_cfg_for(gen)
    if gen == 2:
        cache = get_feat_cache(ymd, card_races)
        return predict_races(tgt, None, None, models, sengen, meta=meta, cfg=cfg, cache=cache)
    hist = load_hist()
    fan = load_fan(ROOT / "data" / "fan")
    return predict_races(tgt, hist, fan, models, sengen, meta=meta, cfg=cfg)


def _mins_to_deadline(now, dl):
    if not dl or ":" not in dl:
        return None
    hh, mm = map(int, dl.split(":"))
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (t - now).total_seconds() / 60


def _live_window_has_targets(ymd):
    """--live-window の事前チェック。モデル・番組表の読込前に latest.json だけで
    対象の有無を判定し、対象ゼロの毎分呼び出しを数十msで終わらせる。
    対象: 未決着・未打刻(tk無し)・未live で締切20分前以内のレース。"""
    latest = ROOT / "docs" / "predictions" / "latest.json"
    if not latest.exists():
        return False
    try:
        old = json.loads(latest.read_text())
    except Exception:
        return False
    if old.get("date") != ymd or not old.get("venues"):
        return False
    now = datetime.now(JST)
    for v in old["venues"]:
        for r in v["races"]:
            if r.get("result") or "tk" in r:
                continue
            if r.get("live") and r.get("st_ex"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is not None and 0 <= mins <= 20:
                return True
    return False


def predict_live(ymd, meta=None, models=None, sengen=None, window=False):
    """直前情報(展示タイム·風·波)を取得できたレースのみ再予測し、latest.jsonをin-place更新。
    既存の結果(result)·オッズ(odds)は保持。再学習不要(モデルは展示特徴を保有)。
    window=True(--live-window): 毎分呼び出しの軽量モード。打刻(T-15)前の締切20分前以内・
    未liveのレースだけを対象にし、展示公開(実測で締切約20分前)から打刻までの約5分の窓を
    取り切る。従来は10分間隔の重い周回頼みで、打刻の約8割が展示なしの予測のまま確定していた。
    meta/models を省くと候補のモデルを順に試す(読む → 予測するまでを候補ごとに try。例外なら次の候補へ)。
    欠場艇(fetch_result.ABSENT_RULE): 欠場の印の艇を除く全艇に展示タイムがあれば再予測し、欠場艇を
    買い目から外して race["absent"] に枠を残す。r["ex"] は取れた艇の分だけ保存する。"""
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
    # windowモードは再予測対象だけに絞る(展示バックフィルは毎分のdo_st_exが担う)
    for v in ([] if window else old["venues"]):
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
                if not r.get("ex") and ex_complete(bi):
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
            # 打刻済み(tk有り)=販売確定。以後picksを差し替えない(締切15分前確定の約束)。
            # 従来はこのガードが無く、打刻後のライブ再予測で買い目が入れ替わる事故が
            # 松候補の26%で起きていた(2026-09-01の精査で判明)。
            if "tk" in r:
                continue
            if r.get("live") and r.get("st_ex"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None or mins > LEAD or mins < -GRACE:
                continue
            if window and mins < 0:
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
            # 展示タイムが6艇そろう(欠場の印の艇は除く)まで待つ。欠場艇の規則は fetch_result.ex_complete
            if not ex_complete(bi):
                continue
            ab = absent_lanes(bi)
            if ab:
                print(f"live {vc}-{r['no']}R: 欠場 {ab}(展示 {len(bi.get('ex', {}))} 艇)。欠場艇を外して再予測", flush=True)
            bi["absent"] = ab
            live[key] = bi
            targets.append(card[key])
            time.sleep(0.3)
    if not targets:
        print(f"live: fetched={n_fetch} 展示の出たレースなし")
        return
    tgt = pd.DataFrame(races_to_rows(targets, live=live))
    cands = [(meta, models, sengen)] if models is not None else iter_models()
    by_venue = None
    for m, b, sg in cands:
        try:
            by_venue, _ = predict_live_rows(ymd, races, tgt, m, b, sg)
            meta = m
            break
        except Exception as e:
            print(f"live: 世代{model_gen(m)}のモデル(trained_at={m.get('trained_at')})で再予測に失敗"
                  f"({type(e).__name__}: {e})。次の候補へ", flush=True)
            continue
    if by_venue is None:
        raise SystemExit("live: どの候補のモデルでも再予測できない")
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
            r["g"] = nr.get("g", 1)
            r["live"] = True
            bi = live.get((vc2, nr["no"]))
            if bi:
                r["ex"] = bi["ex"]
                r["st_ex"] = {str(k): val for k, val in bi.get("st", {}).items()}
                r["wind"] = bi["wind"]
                r["wave"] = bi["wave"]
                r["weather"] = bi.get("weather")
                r["live_at"] = now.strftime("%H:%M")
                if bi.get("absent"):
                    r["absent"] = list(bi["absent"])
                else:
                    r.pop("absent", None)
            n_upd += 1
    if n_upd:
        old["live_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
        old["live_model_trained_at"] = meta.get("trained_at")
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
                    "tk", "mt", "pt", "rs", "os", "att", "qc", "qp", "ph", "pr")
# model-output fields; never re-issue them for a race already gone live
# (exhibition-based) or finished (its result was scored against those picks).
# g(買い目を作ったモデルの世代)と absent(買い目から外した欠場艇)は買い目と一緒に保つ
_PICK_FIELDS = ("picks", "boats", "conf", "fuku", "sengen", "live", "live_at", "g", "absent")


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
                # 買い目を引き継いだら世代の印もその買い目のもの(印の無い古いファイルは世代1)。
                # 欠場の印は引き継いだ買い目に無ければ消す(新しい race_obj には付かない)
                r["g"] = race_gen(o)
                if "absent" not in o:
                    r.pop("absent", None)
    for k in ("results_updated_at", "odds_updated_at", "live_updated_at", "live_model_trained_at"):
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


def predict_day(ymd, races, candidates):
    """朝の予測の本体: 候補のモデルを順に試し、最初に予測まで通った候補の (meta, by_venue, n_sengen) を返す。
    世代2は当日分の特徴量を作って保存(feat_cache)してから予測する。どの候補でも通らなければ SystemExit。"""
    tgt = pd.DataFrame(races_to_rows(races))
    fan = load_fan(ROOT / "data" / "fan")
    hist = None
    for meta, models, sengen in candidates:
        gen = model_gen(meta)
        try:
            cfg = sengen_cfg_for(gen)
            if gen == 2:
                cache = get_feat_cache(ymd, races, fan=fan)
                print(f"target races={len(races)}, feat rows={len(cache['X'])}, fan={len(fan)}", flush=True)
                by_venue, n_sengen = predict_races(tgt, None, fan, models, sengen, meta=meta, cfg=cfg, cache=cache)
            else:
                if hist is None:
                    hist = load_hist()
                print(f"hist rows={len(hist)}, fan={len(fan)}, target races={len(races)}", flush=True)
                by_venue, n_sengen = predict_races(tgt, hist, fan, models, sengen, meta=meta, cfg=cfg)
            return meta, by_venue, n_sengen
        except Exception as e:
            print(f"predict: 世代{gen}のモデル(trained_at={meta.get('trained_at')})で予測に失敗"
                  f"({type(e).__name__}: {e})。次の候補へ", flush=True)
            continue
    raise SystemExit("どの候補のモデルでも予測できない(読めるモデルが無いか、全候補で予測に失敗)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(JST).strftime("%Y%m%d"))
    ap.add_argument("--live", action="store_true",
                    help="直前情報(展示)で展示の出たレースのみ再予測")
    ap.add_argument("--live-window", action="store_true",
                    help="毎分呼び出し用の軽量ライブ再予測: 打刻(T-15)前・締切20分前以内・未liveのレースのみ")
    a = ap.parse_args()
    ymd = a.date

    if a.live_window:
        # 対象が無ければモデルも番組表も読まずに即終了(毎分呼ばれるため)
        if not _live_window_has_targets(ymd):
            print("live-window: 対象なし")
            return
        predict_live(ymd, window=True)
        return

    if a.live:
        predict_live(ymd)
        return

    # 先頭の候補だけ先に読む(番組表が取れない時の当日ファイルにも model_trained_at を書くため)。
    # 残りの候補は予測に失敗した時だけ順に読む
    cands = iter_models()
    first = next(cands, None)
    if first is None:
        raise SystemExit("使えるモデルが無い(data/model_build / data/model_live / data/model のどれも読めない)")
    meta = first[0]
    gen = model_gen(meta)

    btxt = download_day("B", ymd)
    out = {"date": ymd,
           "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
           "model_trained_at": meta["trained_at"],
           "model_gen": gen,
           "sengen_cfg": sengen_cfg_for(gen),
           "venues": []}
    if btxt is None:
        if _existing_has_venues(ymd):
            print(f"live: B(番組表)取得不可だが既存の{ymd}.jsonにvenuesあり; 上書きせずskip")
            return
        # 早朝(10時前)の未取得は「未公開」とみなし、空ファイルを書かずに終了する。
        # dailyを番組表公開(当日早朝)の直後に着弾させる設計のため、公開前に着弾した回が
        # 空の予測を公開すると、アプリが前日表示より悪い「レース無し」表示になり、
        # auto-updateの自己復旧(当日ファイルの有無で判定)も働かなくなる。次の回が拾う。
        # --dateは既定値が本日なので値の有無では判別できない。明示指定(手動の日付指定
        # 再生成)の時だけは従来通り書く。
        if datetime.now(JST).hour < 10 and "--date" not in sys.argv:
            print(f"B(番組表)未公開({ymd}): 早朝のため空ファイルを書かずに終了。次の回で再試行")
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

    def _chain():
        yield first
        yield from cands

    meta, by_venue, n_sengen = predict_day(ymd, races, _chain())
    gen = model_gen(meta)
    out["model_trained_at"] = meta["trained_at"]
    out["model_gen"] = gen
    out["sengen_cfg"] = sengen_cfg_for(gen)

    yusho = "優勝"  # 優勝 (championship final)
    for vcode in sorted(by_venue):
        vraces = sorted(by_venue[vcode], key=lambda r: r["no"])
        dns = [r.get("day_n") for r in vraces if r.get("day_n") is not None]
        day_n = dns[0] if dns else None
        # 最終日 = 優勝戦のある日。「準優勝戦」「準々優勝戦」も「優勝」を含むので、先に取り除いて
        # から判定する。以前は含むだけで最終日としており、準優勝戦の日(最終日の前日など)にも
        # 「最終日」と表示していた(2026-06-13〜10-03 で最終日とした528会場日のうち約250が該当)。
        is_final = any(yusho in str(r.get("type", "")).replace("準々優勝", "").replace("準優勝", "")
                       for r in vraces)
        out["venues"].append({"code": vcode, "name": VENUES[vcode],
                              "day_n": day_n, "is_final": is_final,
                              "races": vraces})
    _merge_existing(out, ymd)
    write(out, ymd)
    print(f"predicted: venues={len(out['venues'])} "
          f"races={sum(len(v['races']) for v in out['venues'])} sengen={n_sengen} gen={gen}")


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

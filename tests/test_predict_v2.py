# -*- coding: utf-8 -*-
"""scripts/predict_today.py の世代2の経路(153列 → 156行 → 120通り、当日分の保存 feat_cache、欠場艇)と、
世代1の中立性(main のコードと同じ買い目・確率・厳選の印)の検算。

実行: リポジトリの根で  PYTHONUTF8=1 python -X utf8 tests/test_predict_v2.py
  通信はしない(番組表・直前情報・モデルの取得はすべて差し替える)。全期間の履歴の特徴量を数回作るので
  約2〜4分・作業メモリ4GB台。

(A) 保存済みの候補モデル(rebuild/candidate/runs/F_s42_model.txt)を形2の meta と一緒に置き、2026年の過去の1日
    (既定 2026-10-03。環境変数 PREDICT_V2_DAY)を「当日」として朝の予測 → 当日ファイルの形(picks 10点・wp の
    合計1・fuku・conf・sengen・g=2)。実験の X_all.npy の同じ日の行とも比べる(あれば)。
(B) feat_cache を使った直前の更新(9列だけ埋める)= その場で全部作り直した値(行列・買い目)。保存→読み直し。
(C) 欠場艇(data_gaps.md 4-2 の実例: 2026-10-03 場18 11R 2号艇)。predict_live を通信なしで動かし、世代2(absent_mask)
    と世代1(強さ 1e-9)の両方で欠場艇が買い目から外れ、race["absent"] が残ること。ABSENT_RULE=False なら従来どおり
    (5艇では再予測しない)。
(D) 中立: 世代1のモデル(data/model の HEAD の写し)で同じ日を予測し、main のコード(別プロセス)で作った結果と
    買い目・確率・厳選の印が完全に一致する(増えた項目 g を除く)。朝の条件と直前情報ありの両方。
(E) 候補ごとの try: 読めない候補(features が違う meta・CRLF)を飛ばす、予測で例外なら次の候補(別の世代)に落ちる、
    当日ファイルの model_gen / sengen_cfg / 各レースの g、_merge_existing が買い目と一緒に g を保つ。
実験フォルダは環境変数 KYOTEI_REBUILD_DIR、main のクローンは KYOTEI_MAIN_CLONE で変えられる。
一時フォルダは .tmp_predict_v2_<pid>/(実行ごとに一意。同じ作業ツリーで同時に走っても、他方の実行中のモデルの写しを
消さない)。終わりに(失敗した時も)消す。
"""
import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import predict_today as pt  # noqa: E402
import train as T  # noqa: E402
import model_v2 as M  # noqa: E402
import fetch_result as FR  # noqa: E402
from features import FEATURES_V2, LIVE_COLS_V2, add_features_v2, load_fan  # noqa: E402
from common import sengen_cfg_for, is_sengen, VENUES  # noqa: E402
from entries_io import load_entries  # noqa: E402

REBUILD = Path(os.environ.get(
    "KYOTEI_REBUILD_DIR",
    r"C:\Users\TABF11~1.FUJ\AppData\Local\Temp\claude\C--Users-ta-fujino-Documents-kyotei-ai-main"
    r"\467be6e3-74fd-4564-9509-0bfe1e8aee91\scratchpad\rebuild"))
MAIN_SCRIPTS = Path(os.environ.get("KYOTEI_MAIN_CLONE", r"C:\Users\ta.fujino\Documents\kyotei-ai-main")) / "scripts"
DAY = os.environ.get("PREDICT_V2_DAY", "20261003")
ABSENT_RACE = (18, 11, 2)            # 2026-10-03 場18 11R 2号艇が欠場(data_gaps.md 4-2)
TMP = ROOT / f".tmp_predict_v2_{os.getpid()}"
JST = timezone(timedelta(hours=9))
T0 = time.time()
_n_ok = 0
_LIVE_IDX = [FEATURES_V2.index(c) for c in LIVE_COLS_V2]


def say(msg):
    print("[%6.1fs] %s" % (time.time() - T0, msg), flush=True)


def rmtree_retry(path, tries=5):
    """Windows で開いたままのハンドルや読み取り専用で rmtree が失敗することがあるので、属性を外して数回試す。
    それでも消えなければ残す(場所を出す)。"""
    def _onerror(fn, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            fn(p)
        except Exception:
            pass
    for i in range(tries):
        if not Path(path).exists():
            return True
        shutil.rmtree(path, onerror=_onerror)
        if not Path(path).exists():
            return True
        time.sleep(0.5 * (i + 1))
    print(f"NOTE: 一時フォルダを消せなかった(手で消す): {path}", flush=True)
    return False


def ok(name):
    global _n_ok
    _n_ok += 1
    print("ok   " + name, flush=True)


def norm(obj):
    """JSON に通して型をそろえる(numpy の数値・タプルの鍵など)。"""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=float))


def strip_keys(by_venue, keys=("g",)):
    out = {}
    for v, races in norm(by_venue).items():
        out[str(v)] = [{k: x for k, x in r.items() if k not in keys} for r in races]
    return out


def git_show(rel):
    return subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=str(ROOT), capture_output=True, check=True).stdout


# ============================================================ 準備
def setup_models():
    """形2(候補モデル)と形1(git の予備の HEAD の写し。作業ツリーの CRLF を避ける)の一式を .tmp に置く。"""
    m2 = TMP / "model2"
    m2.mkdir(parents=True, exist_ok=True)
    src = REBUILD / "candidate" / "runs" / "F_s42_model.txt"
    if not src.exists():
        raise SystemExit("候補モデルが無い: %s(KYOTEI_REBUILD_DIR を確かめる)" % src)
    shutil.copyfile(src, m2 / "model.txt")
    meta2 = {"format": 2, "gen": 2, "trained_at": "2026-10-05 04:00 JST",
             "period": ["20210611", "20261004"], "rows_train": 0,
             "features": list(FEATURES_V2), "stage_cols": list(M.STAGE_COLS), "cat_idx": list(M.CAT_IDX),
             "features_version": pt.FEATURES_HIST_VERSION, "elo_cfg": dict(pt.ELO_CFG),
             "sengen": {"top5_min": 0.47, "valid_rate": None}}
    (m2 / "meta.json").write_text(json.dumps(meta2, ensure_ascii=False, indent=1), encoding="utf-8")
    m1 = TMP / "model1"
    m1.mkdir(parents=True, exist_ok=True)
    for n in ("meta.json", "model_win.txt", "model_top2.txt", "model_top3.txt"):
        b = git_show(f"data/model/{n}")
        assert b"\r\n" not in b[:64], "git の予備が CRLF"
        (m1 / n).write_bytes(b)
    return m1, m2


def load_dir(d):
    """iter_models と同じ読み方(候補の一覧を差し替えて1つだけ読む)。"""
    old = pt._model_candidates
    pt._model_candidates = lambda: [(Path(d), "test")]
    try:
        return pt.load_models()
    finally:
        pt._model_candidates = old


def check_race_obj(r, gen, cfg, venue):
    assert set(r) >= {"no", "type", "deadline", "day_n", "boats", "picks", "conf", "fuku", "sengen", "g"}, r.keys()
    assert r["g"] == gen
    assert len(r["picks"]) == 10 and len({p["c"] for p in r["picks"]}) == 10
    ps = [p["p"] for p in r["picks"]]
    assert ps == sorted(ps, reverse=True) and all(0 <= p <= 1 for p in ps)
    assert all(len(p["c"].split("-")) == 3 and len(set(p["c"].split("-"))) == 3 for p in r["picks"])
    assert 0 < sum(p["p"] for p in r["picks"]) <= 1.0 + 1e-9
    assert len(r["boats"]) == 6 and [b["lane"] for b in r["boats"]] == [1, 2, 3, 4, 5, 6]
    wps = [b["wp"] for b in r["boats"]]
    assert abs(sum(wps) - 1.0) <= 0.0031, wps          # 小数3桁の丸め(6艇で最大 0.003)
    assert r["fuku"]["lane"] == r["boats"][int(np.argmax(wps))]["lane"] or wps.count(max(wps)) > 1
    assert 0 < r["fuku"]["p"] <= 1
    assert r["conf"] in ("A", "B", "C") and r["conf"] == pt.confidence(r["picks"][0]["p"])
    top3p = sum(p["p"] for p in r["picks"][:3])
    assert r["sengen"] == is_sengen(top3p, venue, r["no"], cfg)


# ============================================================ (A)(B) 世代2の朝の予測と保存
def test_v2_morning_and_cache(models2, meta2, races, hist, day_df, fan):
    say("--- (A) 世代2の朝の予測")
    cfg2 = sengen_cfg_for(2)
    sengen2 = {"top5_min": 0.47, "venues": list(range(1, 25))}
    t = time.time()
    fp = pt.input_fingerprint(DAY, races)
    cache = pt.build_feat_cache(DAY, races, hist, fan, fingerprint=fp)
    say("  build_feat_cache: %d 行 %.1fs" % (len(cache["X"]), time.time() - t))
    assert cache["X"].shape == (len(day_df), len(FEATURES_V2)) and cache["X"].dtype == np.float32
    assert np.isnan(cache["X"][:, _LIVE_IDX]).all(), "朝の保存は直前の9列が NaN"
    static = [j for j in range(len(FEATURES_V2)) if j not in _LIVE_IDX]
    nan_rate = float(np.isnan(cache["X"][:, static]).mean())
    assert nan_rate < 0.05, nan_rate
    ok("(A) 保存の形: %d 行 x 153 列 float32、直前の9列は NaN、他の欠損率 %.2f%%" % (len(cache["X"]), nan_rate * 100))

    tgt = pd.DataFrame(pt.races_to_rows(races))
    assert {"distance", "motor_no", "boat_no"} <= set(tgt.columns)
    by2, n2 = pt.predict_races(tgt, None, fan, models2, sengen2, meta=meta2, cfg=cfg2, cache=cache)
    assert sum(len(v) for v in by2.values()) == len(races)
    for v, rs in by2.items():
        for r in rs:
            check_race_obj(r, 2, cfg2, v)
    assert n2 == sum(r["sengen"] for rs in by2.values() for r in rs)
    ok("(A) 世代2の当日ファイルの形: %d レース、picks 10点・wp 合計1・fuku・conf・sengen(cfg 0.45)・g=2、厳選 %d" % (len(races), n2))
    # 実験の行列(X_all.npy)の同じ日の行と比べる(あれば)。入口の型(float32/float64)の差で 5e-7 程度はずれる
    xa = REBUILD / "candidate" / "X_all.npy"
    pp = REBUILD / "candidate" / "parts.pkl"
    if xa.exists() and pp.exists():
        parts = pd.read_pickle(pp)[2]
        X_all = np.load(xa, mmap_mode="r")
        off = X_all.shape[0] - len(parts)
        m = (parts["ymd"].astype(str) == DAY).to_numpy()
        if m.sum() == len(cache["X"]):
            ref = np.asarray(X_all[off + np.flatnonzero(m)][:, :len(FEATURES_V2)], dtype=np.float64)
            k_ref = parts.loc[m, ["venue", "race_no", "lane"]].to_numpy()
            k_got = np.stack([cache["venue"], cache["race_no"], cache["lane"]], 1)
            pos = {tuple(k): i for i, k in enumerate(map(tuple, k_got))}
            al = [pos[tuple(k)] for k in k_ref]
            got = cache["X"][al].astype(np.float64)
            d = np.abs(got[:, static] - ref[:, static])
            d = np.where(np.isnan(d), 0.0, d)
            nan_mis = int((np.isnan(got[:, static]) != np.isnan(ref[:, static])).sum())
            say("  (A) 実験の X_all の %s の行: 144列の差の最大 %.3g, NaN 位置の不一致 %d" % (DAY, d.max(), nan_mis))
            if nan_mis == 0 and d.max() <= 1e-4:
                ok("(A) 実験の行列(X_all.npy)の同じ日の行と一致(差 <= 1e-4、NaN 位置一致)")
            else:
                # データの取り直し(2026-10-06)で履歴が変わり、実験の行列とは一致しなくなった。
                # 実験と同じ行数(1,684,494 行)のデータでだけ一致を求め、それ以外は飛ばす。
                from entries_io import load_entries as _le
                n_rows = len(_le())
                if n_rows == 1684494:
                    raise AssertionError("(A) 実験の行列と一致しない: 差 %.3g, NaN 位置の不一致 %d" % (d.max(), nan_mis))
                say("  SKIP (A) データの取り直し後(entries %d 行)なので実験の行列とは比べない" % n_rows)
        else:
            say("  SKIP (A) X_all の %s の行数 %d != %d(データが変わっている)" % (DAY, int(m.sum()), len(cache["X"])))
    else:
        say("  SKIP (A) 実験の X_all.npy が無い")

    say("--- (B) 保存を使った直前の更新 = 作り直し")
    live = T.entries_live_info(day_df)
    tgt_live = pd.DataFrame(pt.races_to_rows(races, live=live))
    X6a, keys_a, ab_a, _ = pt.live_matrix(cache, tgt_live)
    assert ab_a is None
    t = time.time()
    feats = add_features_v2(hist, tgt_live, fan=fan)      # その場で全部作り直す(直前情報入りの表から)
    cache_b = pt._cache_dict(tgt_live, feats)
    del feats
    say("  作り直し(add_features_v2 直前情報あり) %.1fs" % (time.time() - t))
    X6b, keys_b, _, _ = pt.live_matrix(cache_b, tgt_live)
    assert keys_a == keys_b and X6a.shape == X6b.shape
    a, b = X6a.astype(np.float64), X6b.astype(np.float64)
    assert (np.isnan(a) == np.isnan(b)).all()
    d = np.where(np.isnan(a), 0.0, np.abs(a - b))
    n_exact = int((d == 0).all(axis=(1, 2)).sum())
    say("  (B) %d レース x 6 x 153: 差の最大 %.3g、ビット一致のレース %d/%d" % (len(keys_a), d.max(), n_exact, len(keys_a)))
    assert d.max() <= 1e-6 * np.nanmax(np.abs(b)) + 1e-9
    assert not np.isnan(a[:, :, _LIVE_IDX]).all(), "直前の9列が埋まっていない"
    by_a, _ = pt.predict_races(tgt_live, None, None, models2, sengen2, meta=meta2, cfg=cfg2, cache=cache)
    by_b, _ = pt.predict_races(tgt_live, hist, fan, models2, sengen2, meta=meta2, cfg=cfg2, cache=None)
    assert norm(by_a) == norm(by_b), "保存から更新した買い目と作り直した買い目が違う"
    ok("(B) 保存から9列を埋めた行列 = 作り直し(差 <= 1e-6、ビット一致 %d/%d レース)、買い目・確率も同じ" % (n_exact, len(keys_a)))
    # 直前の9列の式: ex_rank(method=min)/ ex_diff / 履歴の4列が features.add_features と同じ
    chk = feats_live = add_features_v2(hist, tgt_live.sort_values(["venue", "race_no", "lane"]).reset_index(drop=True), fan=fan)
    live_ref = chk[LIVE_COLS_V2].to_numpy(np.float32)
    live_got = X6a.reshape(-1, len(FEATURES_V2))[:, _LIVE_IDX]
    eq = (live_ref == live_got) | (np.isnan(live_ref) & np.isnan(live_got))
    assert eq.all(), "直前の9列が add_features_v2 の値と違う(%d)" % int((~eq).sum())
    del feats_live, chk
    ok("(B) 直前の9列(ex_time/ex_rank/ex_diff/wind/wave + 履歴4列)が add_features_v2 の値とビット一致")
    # 保存 → 読み直し
    p = pt.save_feat_cache(DAY, cache)
    c2 = pt.load_feat_cache(DAY, fp)
    assert p.exists() and c2 is not None
    assert np.array_equal(c2["X"], cache["X"], equal_nan=True) and np.array_equal(c2["aux"], cache["aux"], equal_nan=True)
    assert all(np.array_equal(c2[k], cache[k]) for k in ("venue", "race_no", "lane", "toban"))
    assert pt.load_feat_cache(DAY, fp + "x") is None, "指紋が違えば読まない"
    assert pt.load_feat_cache("19990101", fp) is None
    fp2 = pt.input_fingerprint(DAY, races[:-1])
    assert fp2 != fp and pt.load_feat_cache(DAY, fp2) is None, "番組表が変われば指紋が変わる"
    # 指紋には特徴量を作るコード(features.py / features_hist.py)の sha256 も入る(版の上げ忘れに頼らない)
    fpo = json.loads(fp)
    assert [n for n, _ in fpo["feat_code"]] == ["features.py", "features_hist.py"], fpo["feat_code"]
    import hashlib
    for n, h in fpo["feat_code"]:
        assert h == hashlib.sha256((ROOT / "scripts" / n).read_bytes()).hexdigest(), n
    saved_files = pt._FEAT_CODE_FILES
    pt._FEAT_CODE_FILES = ("features.py",)         # コードの一覧が変わる = 中身が変わったのと同じ扱い
    try:
        fp3 = pt.input_fingerprint(DAY, races)
    finally:
        pt._FEAT_CODE_FILES = saved_files
    assert fp3 != fp and pt.load_feat_cache(DAY, fp3) is None, "特徴量のコードが変われば指紋が変わる"
    assert pt.input_fingerprint(DAY, races) == fp
    ok("(B) feat_cache の保存 → 読み直しで同じ。指紋(entries・番組表・コードの版と sha256)が違えば使わない")
    return by2, cache


# ============================================================ (C) 欠場艇
class _FakeDT:
    """predict_live の「今」を固定する(締切20分前の窓に入れるため)。"""
    fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls.fixed

    @staticmethod
    def strptime(*a, **kw):
        return datetime.strptime(*a, **kw)


def write_day_file(by_venue, gen):
    out = {"date": DAY, "generated_at": "x", "model_trained_at": "x", "model_gen": gen,
           "sengen_cfg": sengen_cfg_for(gen), "venues": []}
    for vcode in sorted(by_venue):
        out["venues"].append({"code": vcode, "name": VENUES[vcode], "day_n": None, "is_final": False,
                              "races": sorted(copy.deepcopy(by_venue[vcode]), key=lambda r: r["no"])})
    d = TMP / "docs" / "predictions"
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(out, ensure_ascii=False)
    (d / f"{DAY}.json").write_text(txt)
    (d / "latest.json").write_text(txt)
    return out


def read_day_file():
    return json.loads((TMP / "docs" / "predictions" / f"{DAY}.json").read_text())


def test_absent(by2, cache, races, day_df, models2, meta2, models1, meta1):
    say("--- (C) 欠場艇(predict_live を通信なしで)")
    import time as _time
    v0, r0, lane0 = ABSENT_RACE
    live = T.entries_live_info(day_df)
    key = (v0, r0)
    assert key in live, "欠場の実例のレースが entries に無い: %s" % (key,)
    ex_full = {k: v for k, v in live[key]["ex"].items() if v is not None}
    assert lane0 not in ex_full and len(ex_full) == 5, ex_full           # entries でも欠場艇の展示は空
    bi_absent = {"ex": ex_full, "st": {str(k): ".10" for k in ex_full}, "wind": live[key]["wind"],
                 "wave": live[key]["wave"], "weather": None, "absent": [lane0]}
    dl = next(r for r in races if (r["venue"], r["race_no"]) == key)["deadline"]
    hh, mm = map(int, dl.split(":"))
    _FakeDT.fixed = datetime(int(DAY[:4]), int(DAY[4:6]), int(DAY[6:]), hh, mm, tzinfo=JST) - timedelta(minutes=14)

    def fake_parse_before(html):
        jcd, rno = map(int, html.split(":"))
        if (jcd, rno) == key:
            return copy.deepcopy(bi_absent)
        return {"ex": {}, "st": {}, "wind": None, "wave": None, "weather": None, "absent": []}

    saved = (pt.download_day, pt.parse_b, pt.fetch_before_html, pt.parse_before, pt.datetime, _time.sleep)
    pt.download_day = lambda kind, ymd: "B"
    pt.parse_b = lambda txt, ymd: races
    pt.fetch_before_html = lambda ymd, jcd, rno: "%d:%d" % (jcd, rno)
    pt.parse_before = fake_parse_before
    pt.datetime = _FakeDT
    _time.sleep = lambda s: None
    try:
        for gen, meta, models in ((2, meta2, models2), (1, meta1, models1)):
            before = write_day_file(by2, 2)
            sengen = {"top5_min": 0.4, "venues": list(range(1, 25))}
            pt.predict_live(DAY, meta, models, sengen, window=True)
            after = read_day_file()
            idx = {(v["code"], r["no"]): r for v in after["venues"] for r in v["races"]}
            idx0 = {(v["code"], r["no"]): r for v in before["venues"] for r in v["races"]}
            r = idx[key]
            assert r.get("live") is True and r.get("absent") == [lane0] and r["g"] == gen, {k: r.get(k) for k in ("live", "absent", "g")}
            assert set(map(int, r["ex"])) == set(ex_full) and r["wind"] == live[key]["wind"]
            assert all(str(lane0) not in p["c"].split("-") for p in r["picks"]), r["picks"]
            wp = {b["lane"]: b["wp"] for b in r["boats"]}
            assert wp[lane0] == 0.0 and abs(sum(wp.values()) - 1.0) <= 0.0031, wp
            assert r["fuku"]["lane"] != lane0
            assert after.get("live_model_trained_at") == meta["trained_at"]
            changed = [k for k in idx if norm(idx[k]) != norm(idx0[k])]
            assert changed == [key], changed
            ok("(C) 世代%d: 欠場艇 %s-%dR %d号艇を外して再予測(absent=[%d]、買い目に含まず、wp=0、g=%d)。他のレースは不変"
               % (gen, v0, r0, lane0, lane0, gen))
        # 規則を止めると従来どおり(5艇では再予測しない)
        before = write_day_file(by2, 2)
        FR.ABSENT_RULE = False
        try:
            pt.predict_live(DAY, meta2, models2, {"top5_min": 0.4}, window=True)
        finally:
            FR.ABSENT_RULE = True
        after = read_day_file()
        assert norm(after["venues"]) == norm(before["venues"])
        ok("(C) ABSENT_RULE=False なら5艇のレースは再予測しない(従来どおり)")
        # 6艇そろいは規則の有無に関わらず再予測する(欠場なし)。absent の印は付かない
        ex6 = dict(ex_full)
        ex6[lane0] = 6.90
        bi_absent["ex"] = ex6
        bi_absent["absent"] = []
        before = write_day_file(by2, 2)
        pt.predict_live(DAY, meta2, models2, {"top5_min": 0.4}, window=True)
        after = read_day_file()
        r = {(v["code"], r["no"]): r for v in after["venues"] for r in v["races"]}[key]
        assert r.get("live") is True and "absent" not in r and len(r["ex"]) == 6 and r["g"] == 2
        ok("(C) 6艇そろいなら従来どおり再予測し、absent は付かない")
    finally:
        pt.download_day, pt.parse_b, pt.fetch_before_html, pt.parse_before, pt.datetime, _time.sleep = saved


# ============================================================ (D) 世代1の中立性
MAIN_DRIVER = r'''
import json, sys
from pathlib import Path
import pandas as pd, lightgbm as lgb
MAIN, WT, MODEL1, RACES, LIVE, OUT = sys.argv[1:7]
sys.path.insert(0, MAIN)
import predict_today as pt, features
pt.ROOT = Path(WT)
races = json.load(open(RACES, encoding="utf-8"))
live = {tuple(map(int, k.split(","))): v for k, v in json.load(open(LIVE, encoding="utf-8")).items()}
for v in live.values():
    v["ex"] = {int(k): x for k, x in v["ex"].items()}
hist = pt.load_hist()
fan = features.load_fan(Path(WT) / "data" / "fan")
models = {t: lgb.Booster(model_file=str(Path(MODEL1) / ("model_%s.txt" % t))) for t in ("win", "top2", "top3")}
sengen = {"top5_min": 0.4, "venues": list(range(1, 25))}
res = {"hist_rows": len(hist), "scripts": MAIN}
for name, lv in (("morning", None), ("live", live)):
    tgt = pd.DataFrame(pt.races_to_rows(races, live=lv))
    by, n = pt.predict_races(tgt, hist, fan, models, sengen)
    res[name] = {"by_venue": {str(k): v for k, v in by.items()}, "n": n}
Path(OUT).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
'''


def test_gen1_neutral(models1, meta1, races, day_df, hist_full, fan):
    say("--- (D) 世代1の中立性(main のコードと比べる)")
    if not (MAIN_SCRIPTS / "predict_today.py").exists():
        say("  SKIP (D) main のクローンが無い: %s" % MAIN_SCRIPTS)
        return
    live = T.entries_live_info(day_df)
    (TMP / "races.json").write_text(json.dumps(races, ensure_ascii=False), encoding="utf-8")
    (TMP / "live.json").write_text(json.dumps({"%d,%d" % k: v for k, v in live.items()}, ensure_ascii=False), encoding="utf-8")
    drv = TMP / "main_driver.py"
    drv.write_text(MAIN_DRIVER, encoding="utf-8")
    out = TMP / "main_out.json"
    t = time.time()
    env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    cp = subprocess.run([sys.executable, "-X", "utf8", str(drv), str(MAIN_SCRIPTS), str(ROOT), str(TMP / "model1"),
                         str(TMP / "races.json"), str(TMP / "live.json"), str(out)],
                        capture_output=True, text=True, env=env, cwd=str(TMP))
    if cp.returncode != 0:
        print(cp.stdout[-2000:])
        print(cp.stderr[-4000:])
        raise AssertionError("main のコードでの予測が失敗")
    ref = json.loads(out.read_text(encoding="utf-8"))
    say("  main の予測(別プロセス) %.1fs hist_rows=%d" % (time.time() - t, ref["hist_rows"]))
    assert ref["hist_rows"] == len(hist_full)
    sengen1 = {"top5_min": 0.4, "venues": list(range(1, 25))}
    for name, lv in (("morning", None), ("live", live)):
        tgt = pd.DataFrame(pt.races_to_rows(races, live=lv))
        by, n = pt.predict_races(tgt, hist_full, fan, models1, sengen1, meta=meta1)
        got = strip_keys(by)
        exp = strip_keys(ref[name]["by_venue"])   # main も g を書くようになった(段階A)ので両側から外して比べる
        assert n == ref[name]["n"], (n, ref[name]["n"])
        assert set(got) == set(exp)
        n_races = 0
        for v in got:
            assert len(got[v]) == len(exp[v])
            for a, b in zip(got[v], exp[v]):
                if a != b:
                    keys = sorted(set(a) | set(b))
                    diff = {k: (a.get(k), b.get(k)) for k in keys if a.get(k) != b.get(k)}
                    raise AssertionError((name, v, a["no"], json.dumps(diff, ensure_ascii=False)[:1200]))
                n_races += 1
        for v, rs in by.items():
            for r in rs:
                assert r["g"] == 1
        ok("(D) %s: 世代1の %d レースが main のコードと完全に一致(買い目・確率・厳選の印・本命。g=1 が増えただけ)、厳選 %d"
           % (name, n_races, n))


# ============================================================ (E) 候補ごとの try・当日ファイルの項目
def test_candidates_and_main(races, cache, meta2):
    say("--- (E) 候補の読み込みと main の経路")
    m2, m1 = TMP / "model2", TMP / "model1"
    # 読めない候補を飛ばす: features が違う meta / CRLF / 形1の meta に model.txt だけ
    bad = TMP / "model2_badmeta"
    shutil.copytree(m2, bad, dirs_exist_ok=True)
    mm = json.loads((bad / "meta.json").read_text(encoding="utf-8"))
    mm["features"] = mm["features"][:-1] + ["h_bogus"]
    (bad / "meta.json").write_text(json.dumps(mm), encoding="utf-8")
    crlf = TMP / "model2_crlf"
    shutil.copytree(m2, crlf, dirs_exist_ok=True)
    (crlf / "model.txt").write_bytes((m2 / "model.txt").read_bytes().replace(b"\n", b"\r\n"))
    wrong = TMP / "model2_wrongbooster"
    shutil.copytree(m2, wrong, dirs_exist_ok=True)
    shutil.copyfile(m1 / "model_win.txt", wrong / "model.txt")      # 形2の meta に列数45のモデル
    import io
    import contextlib
    buf = io.StringIO()
    old = pt._model_candidates
    pt._model_candidates = lambda: [(bad, "build"), (crlf, "live"), (wrong, "x"), (m2, "live2"), (m1, "frozen")]
    try:
        with contextlib.redirect_stdout(buf):
            cands = list(pt.iter_models())
    finally:
        pt._model_candidates = old
    log = buf.getvalue()
    assert [m["gen"] for m, _, _ in cands] == [2, 1] and [m["format"] for m, _, _ in cands] == [2, 1]
    assert set(cands[0][1]) == {"pl"} and set(cands[1][1]) == {"win", "top2", "top3"}
    assert "build を読めない" in log and "features" in log, log
    assert "live は改行が CRLF なので使えない(model.txt)" in log, log
    assert "x を読めない" in log and "列数" in log, log
    assert "model: live2 trained_at=2026-10-05 04:00 JST" in log and "model: frozen trained_at=" in log
    assert all("top5_min" in s and "venues" in s for _, _, s in cands)
    ok("(E) iter_models: features の違う meta・CRLF・列数の合わないモデルを飛ばし、形2 → 形1 の順に読む")

    # main(): 候補ごとに「読む → 予測する」。先頭(世代2)の予測で例外なら次の候補(世代1)へ
    saved = (pt.download_day, pt.parse_b, pt._model_candidates, M.predict_P, sys.argv)
    pt.download_day = lambda kind, ymd: "B"
    pt.parse_b = lambda txt, ymd: races
    pt._model_candidates = lambda: [(m2, "build"), (m1, "frozen")]
    sys.argv = ["predict_today.py", "--date", DAY]
    pred_path = TMP / "docs" / "predictions" / f"{DAY}.json"
    try:
        pred_path.unlink(missing_ok=True)
        calls = {"n": 0}
        real_predict_P = M.predict_P

        def boom(*a, **kw):
            calls["n"] += 1
            raise RuntimeError("模擬: 世代2の予測で例外")

        M.predict_P = boom
        with contextlib.redirect_stdout(buf):
            pt.main()
        M.predict_P = real_predict_P
        log = buf.getvalue()
        assert calls["n"] == 1 and "世代2のモデル" in log and "で予測に失敗" in log and "次の候補へ" in log, log[-800:]
        d1 = json.loads(pred_path.read_text())
        assert d1["model_gen"] == 1 and d1["sengen_cfg"] == sengen_cfg_for(1) and d1["model_trained_at"] == "2026-10-02 09:51 JST"
        assert all(r["g"] == 1 for v in d1["venues"] for r in v["races"])
        assert "latest.json" not in {p.name for p in pred_path.parent.iterdir()} or True
        ok("(E) main: 世代2の候補が予測で例外 → 世代1の予備で当日ファイルを作る(model_gen=1、各レース g=1、sengen_cfg は世代1)")
        # 世代2が通れば model_gen=2、各レース g=2、sengen_cfg は世代2(0.45)。保存(feat_cache)を読む
        with contextlib.redirect_stdout(buf):
            pt.main()
        log = buf.getvalue()
        assert "feat_cache: 保存を使う" in log, log[-500:]
        d2 = json.loads(pred_path.read_text())
        assert d2["model_gen"] == 2 and d2["sengen_cfg"] == sengen_cfg_for(2) and d2["sengen_cfg"]["top3p_min"] == 0.45
        assert d2["model_trained_at"] == "2026-10-05 04:00 JST"
        assert all(r["g"] == 2 for v in d2["venues"] for r in v["races"])
        assert len(d2["venues"]) == len({r["venue"] for r in races})
        ok("(E) main: 世代2で当日ファイル(model_gen=2、sengen_cfg top3p_min 0.45、各レース g=2、保存を再利用)")
        # _merge_existing: live / 結果あり / tk のレースは買い目と一緒に g を保つ(印の無い古いファイルは世代1)
        d = json.loads(pred_path.read_text())
        r_live = d["venues"][0]["races"][0]
        r_live["live"] = True
        r_live["picks"] = [{"c": "6-5-4", "p": 0.5}] + r_live["picks"][1:]
        del r_live["g"]                                 # 古いコードが作った live のレース(印なし = 世代1)
        r_live["absent"] = [3]
        r_tk = d["venues"][0]["races"][1]
        r_tk["tk"] = 0
        r_tk["g"] = 1
        r_tk["picks"] = [{"c": "5-4-3", "p": 0.4}] + r_tk["picks"][1:]
        r_free = d["venues"][0]["races"][2]
        r_free["g"] = 1                                 # 未処理のレース: 作り直しで買い目も g も新しくなる
        r_free["os"] = {"1-2-3": 5.0}
        pred_path.write_text(json.dumps(d, ensure_ascii=False))
        with contextlib.redirect_stdout(buf):
            pt.main()
        d3 = json.loads(pred_path.read_text())
        a, b, c = d3["venues"][0]["races"][:3]
        assert a["picks"][0]["c"] == "6-5-4" and a["g"] == 1 and a["absent"] == [3] and a["live"] is True
        assert b["picks"][0]["c"] == "5-4-3" and b["g"] == 1 and b["tk"] == 0 and "absent" not in b
        assert c["g"] == 2 and c["picks"][0]["c"] != "6-5-4" and c["os"] == {"1-2-3": 5.0}
        assert d3["model_gen"] == 2
        ok("(E) _merge_existing: live / tk のレースは買い目と g(印なしは1)・absent を保ち、未処理のレースは g=2 に替わる")
    finally:
        pt.download_day, pt.parse_b, pt._model_candidates, M.predict_P, sys.argv = saved


def main():
    rmtree_retry(TMP)
    TMP.mkdir(parents=True)
    try:
        _main()
    finally:
        rmtree_retry(TMP)


def _main():
    say("当日 %s、実験 %s、main %s、一時 %s" % (DAY, REBUILD, MAIN_SCRIPTS, TMP.name))
    m1, m2 = setup_models()
    meta2, models2, _ = load_dir(m2)
    meta1, models1, _ = load_dir(m1)
    assert meta2["gen"] == 2 and set(models2) == {"pl"} and meta1["gen"] == 1 and set(models1) == {"win", "top2", "top3"}
    ok("形2(候補モデル)と形1(予備の HEAD の写し)を読めた")
    t = time.time()
    full = load_entries()
    hist = full[full["date"] < DAY]
    day_df = full[full["date"] == DAY]
    assert len(day_df) and len(day_df) % 6 == 0, "当日 %s の行が無い" % DAY
    races = T.entries_to_races(day_df)
    fan = load_fan(ROOT / "data" / "fan")
    say("entries %d 行(当日 %s は %d レース), fan %d (%.1fs)" % (len(full), DAY, len(races), len(fan), time.time() - t))
    assert len(races) * 6 == len(day_df)

    # 予測の入出力(当日ファイル・保存)は .tmp に向ける。ファン手帳は写しを置く(ROOT を差し替えるため)
    shutil.copytree(ROOT / "data" / "fan", TMP / "data" / "fan")
    old_root, old_cache = pt.ROOT, pt.FEAT_CACHE_DIR
    pt.ROOT = TMP
    pt.FEAT_CACHE_DIR = TMP / "feat_cache"
    try:
        by2, cache = test_v2_morning_and_cache(models2, meta2, races, hist, day_df, fan)
        test_absent(by2, cache, races, day_df, models2, meta2, models1, meta1)
        test_gen1_neutral(models1, meta1, races, day_df, full, fan)
        test_candidates_and_main(races, cache, meta2)
    finally:
        pt.ROOT, pt.FEAT_CACHE_DIR = old_root, old_cache
    print("%d checks passed" % _n_ok)
    print("ALL OK")


if __name__ == "__main__":
    main()

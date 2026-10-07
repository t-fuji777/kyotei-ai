# -*- coding: utf-8 -*-
"""scripts/train.py の世代2(main_v2)の検算。

実行: リポジトリの根で  PYTHONUTF8=1 python -X utf8 tests/test_train_v2.py [--quick]
  (0) 環境変数 MODEL_GEN の読み方(resolve_model_gen): 空・無し=定数、"1"/"2"=その値、それ以外は SystemExit。
      import 時には読まない(train.MODEL_GEN は定数のまま)。
  (1) 合成データ(tests/test_features_hist.make_synthetic)で自己検査 self_check_v2 が動き、予測の経路の値を
      わざとずらすと止まる(SystemExit)。数秒。
  (1b) 6艇そろわないレースを混ぜた合成データで main_v2 を通す: そのレースの行は行列から外れるが特徴量の
      入口(全行)には残り、最終日の自己検査は6艇そろったレースだけで通る。meta.params に num_threads が残らない。
      較正の種は試験期間の後半(日付の中央値以降)の着順ありレースだけ(件数と built_from。check_seed)。
  (2) entries の一部(既定 2025-04-01 以降。環境変数 TRAIN_V2_FROM)で main_v2 を木を少なくして通し、
      出力(model.txt / meta.json / 較正の種 / 報告)の項目と自己検査が動くこと、読み込み側(predict_today.check_meta_v2 /
      model_store.valid_dir)がその一式を受け付けることを確かめる。較正の種は (1b) と同じ規則で件数を照合する。
      約3〜6分・作業メモリ3GB台。--quick で省く。
出力先はすべて .tmp_train_v2_<pid>/(本番の data/model_build・docs/model_report.json・data/calib_seed_gen2.json には
触れない。同じ作業ツリーで同時に走っても互いの一時フォルダを消さない)。
"""
import json
import os
import re
import shutil
import stat
import sys
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import train as T  # noqa: E402
import model_v2 as M  # noqa: E402
import predict_today as pt  # noqa: E402
import build_calib as BC  # noqa: E402
from features import FEATURES_V2, LIVE_COLS_V2, add_features_v2  # noqa: E402
from entries_io import load_entries  # noqa: E402
from test_features_hist import make_synthetic  # noqa: E402

QUICK = "--quick" in sys.argv
FROM = os.environ.get("TRAIN_V2_FROM", "20250401")
TMP = ROOT / f".tmp_train_v2_{os.getpid()}"      # 実行ごとに一意(同時に走る他の実行の一時フォルダを消さない)
T0 = time.time()
_n_ok = 0
GEN1_METRIC_KEYS = {"races", "win_hit_rate", "fuku_hit_rate", "top1_hit_rate", "top6_hit_rate", "top10_hit_rate",
                    "top18_hit_rate", "top6_roi", "top5_hit_rate", "top5_roi", "sengen"}


def say(msg):
    print("[%6.1fs] %s" % (time.time() - T0, msg), flush=True)


def ok(name):
    global _n_ok
    _n_ok += 1
    print("ok   " + name, flush=True)


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


def booster_for_synthetic(F):
    """合成データ用の小さな世代2のモデル(列数 F + 6)。自己検査の値の一致を見るだけなので木は少なくてよい。"""
    rng = np.random.default_rng(0)
    R = 300
    X6 = rng.normal(size=(R, 6, F)).astype(np.float32)
    X6[:, :, 0] = 3
    X6[:, :, 1] = 1
    X6[:, :, 2] = np.arange(1, 7)
    X6[:, :, 5] = rng.integers(0, 5, size=(R, 6))
    X6[:, :, 30] = 1
    order = np.stack([rng.permutation(6)[:3] for _ in range(R)])
    return M.fit_races(X6[:240], order[:240], X6[240:], order[240:], rounds=12, early_stop=5, log=lambda m: None)


def _n_complete_races(six):
    """6艇そろったレースの表のうち、1〜3着が1艇ずつ付いたレース(main_v2 の complete)の数。"""
    pos = pd.to_numeric(six["pos"], errors="coerce")
    keys = [six["date"].astype(str), six["venue"], six["race_no"]]
    c = pd.concat([(pos == k).groupby(keys).sum().rename(k) for k in (1, 2, 3)], axis=1)
    return int((c == 1).all(axis=1).sum())


def seed_expectation(df):
    """較正の種の期待値を entries から main_v2 とは別の道で数える。規則(2026-10-07):
    行列に入る(6艇そろった)レースの日付の後ろ10%が試験期間、その日付の中央値(偶数個なら後ろ側)以降が
    種の期間で、種の件数はその期間の着順のそろったレース数。採点(test_metrics)は試験期間全体のままなので、
    前半の日付が種に入っていないことは「種の件数 < 試験期間全体の着順ありレース数」で見る。"""
    six = T.six_row_races(df, log=lambda m: None)
    date = six["date"].astype(str)
    dates = np.sort(date.unique())
    d_va = dates[int(len(dates) * 0.90)]
    te_dates = dates[dates >= d_va]
    d_seed = te_dates[len(te_dates) // 2]
    return {"d_va": str(d_va), "d_seed": str(d_seed), "d_last": str(dates[-1]), "test_days": int(len(te_dates)),
            "n_seed": _n_complete_races(six[date >= d_seed]), "n_test_complete": _n_complete_races(six[date >= d_va])}


def check_seed(seed_path, meta, df, label):
    """種のファイルが build_calib の読める形で、meta.calib_seed と同じ中身、件数と built_from が
    「試験期間の後半だけ」の規則どおりであることを確かめる。"""
    seed = BC.load_seed(seed_path)
    assert seed is not None and seed["gen"] == 2 and seed["bins"] == meta["calib_seed"]
    assert BC.validate_seed(seed) is None
    for key, tbl in seed["bins"].items():
        assert all(h <= n for n, h in tbl)
    n_seed = BC.seed_total_races(seed["bins"])
    exp = seed_expectation(df)
    assert exp["test_days"] >= 2 and exp["n_seed"] > 0, exp                # 規則を見るには試験期間が2日以上要る
    assert n_seed == exp["n_seed"], (n_seed, exp)                           # 後半の着順ありレース数と一致
    assert n_seed < exp["n_test_complete"] <= meta["races_test"], (n_seed, exp)   # 前半は入っていない
    assert "train.py" in seed["built_from"]
    assert f"test の後半 {exp['d_seed']}-{exp['d_last']}" in seed["built_from"], seed["built_from"]
    assert f"({n_seed} races" in seed["built_from"] and f"test {exp['d_va']}-{exp['d_last']}" in seed["built_from"], seed["built_from"]
    ok("%s 較正の種: build_calib.load_seed が受け付け、meta.calib_seed と同じ。試験期間 %d 日のうち後半 %s-%s の"
       "着順ありレース %d 件だけ(試験期間全体は %d 件)、built_from に期間と件数" % (
           label, exp["test_days"], exp["d_seed"], exp["d_last"], n_seed, exp["n_test_complete"]))
    return seed


# ============================================================ (0) MODEL_GEN の読み方
def test_resolve_model_gen():
    say("--- (0) 環境変数 MODEL_GEN")
    # 定数は 2(段階B。2026-10-07 に世代2へ切り替え、翌朝の学習から新しいモデル)
    assert T.MODEL_GEN == 2, "定数は 2(段階B)"
    assert T.resolve_model_gen({}) == 2 and T.resolve_model_gen({"MODEL_GEN": ""}) == 2 and T.resolve_model_gen({"MODEL_GEN": "  "}) == 2
    assert T.resolve_model_gen({"MODEL_GEN": "1"}) == 1 and T.resolve_model_gen({"MODEL_GEN": "2"}) == 2
    assert T.resolve_model_gen({"MODEL_GEN": " 2 "}) == 2
    for bad in ("3", "abc", "0", "1.0", "-1"):
        try:
            T.resolve_model_gen({"MODEL_GEN": bad})
            raise RuntimeError("no exit")
        except SystemExit as e:
            assert "MODEL_GEN" in str(e) and bad in str(e), str(e)
    # import 時には読まない(不正な値でも train / predict_today が import できる)
    env = dict(os.environ, MODEL_GEN="abc", PYTHONUTF8="1")
    env.pop("PYTHONPATH", None)
    import subprocess
    cp = subprocess.run([sys.executable, "-X", "utf8", "-c",
                         "import sys; sys.path.insert(0, %r); import train, predict_today; print('GEN', train.MODEL_GEN)" % str(ROOT / "scripts")],
                        capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert cp.returncode == 0 and "GEN 2" in cp.stdout, (cp.returncode, cp.stdout[-300:], cp.stderr[-800:])
    ok("(0) MODEL_GEN: 空=定数(2)、1/2=その値、それ以外は SystemExit。import 時には読まない(MODEL_GEN=abc でも predict_today が読める)")


# ============================================================ (1) 合成データで自己検査
def test_self_check_synthetic():
    say("--- (1) 合成データで自己検査")
    df = T.six_row_races(make_synthetic(n_days=40, venues=(3, 7), races_per_day=3, n_racers=30, seed=3), log=say)
    feats = add_features_v2(df, df, fan=None)
    X = feats[FEATURES_V2].to_numpy(np.float32)
    del feats
    date_r = df["date"].astype(str).to_numpy()[::6]
    venue_r = pd.to_numeric(df["venue"]).to_numpy(np.int64)[::6]
    rno_r = pd.to_numeric(df["race_no"]).to_numpy(np.int64)[::6]
    R = len(X) // 6
    X6 = X.reshape(R, 6, -1)
    booster = booster_for_synthetic(len(FEATURES_V2))
    te_idx = np.arange(R)                                   # 全レースを「試験期間」とみなす
    P_te = M.predict_P(booster, X6)
    log = []
    s = T.self_check_v2(df, X6, date_r, venue_r, rno_r, booster, P_te, te_idx, None, log=log.append)
    assert s["ok"] and s["nan_mismatch"] == 0 and s["cells_over_tol"] == 0 and s["max_abs_diff_P"] <= 1e-6, s
    assert s["races"] == int((date_r == date_r.max()).sum()) and s["races_bit_exact"] >= 1
    ok("(1) 合成データ: 予測の経路(build_feat_cache → live_matrix → predict_P)の値が学習時の表・採点用の予測と一致 %s"
       % {k: s[k] for k in ("races", "races_bit_exact", "max_abs_diff_features", "max_abs_diff_P")})
    # 試験期間の行だけを渡しても同じ(本番は全期間の行列を解放した後に、試験期間の行だけで自己検査する)
    te2 = np.flatnonzero(date_r >= np.sort(np.unique(date_r))[-5])
    s2 = T.self_check_v2(df, X6[te2], date_r, venue_r, rno_r, booster, P_te[te2], te2, None, log=log.append)
    assert s2["ok"] and s2["races"] == s["races"] and s2["max_abs_diff_P"] <= 1e-6, s2
    ok("(1) 試験期間の行(X6_te)だけを渡した自己検査も同じ結果")
    # 学習時の表の値をわざとずらす → 止まる(配布しない)
    Xb = X6.copy()
    races_D = np.flatnonzero(date_r == date_r.max())
    j = FEATURES_V2.index("h_elola_exp")
    Xb[races_D[0], :, j] += 0.01
    try:
        T.self_check_v2(df, Xb, date_r, venue_r, rno_r, booster, P_te, te_idx, None, log=log.append)
        raise RuntimeError("no exit")
    except SystemExit as e:
        assert "self-check failed" in str(e), str(e)
    ok("(1) 値をずらすと self-check が SystemExit で止まる(学習の失敗 = 配布しない)")
    # 採点用の予測だけがずれても止まる
    Pb = P_te.copy()
    Pb[-1, 0] += 1e-4
    try:
        T.self_check_v2(df, X6, date_r, venue_r, rno_r, booster, Pb, te_idx, None, log=log.append)
        raise RuntimeError("no exit")
    except SystemExit as e:
        assert "self-check failed" in str(e)
    ok("(1) 採点用の予測がずれても止まる")
    # 最終日が試験期間に無ければ止まる
    try:
        T.self_check_v2(df, X6[:-1], date_r, venue_r, rno_r, booster, P_te[:-1], te_idx[:-1], None, log=log.append)
        raise RuntimeError("no exit")
    except SystemExit as e:
        assert "試験期間" in str(e)
    ok("(1) 最終日が試験期間に入っていなければ止まる")
    # 補助: entries_to_races / entries_live_info の形は parse_b / races_to_rows と合う
    date = df["date"].astype(str).to_numpy()
    day = df[date == date.max()]
    races = T.entries_to_races(day)
    assert len(races) * 6 == len(day) and all(set(r) == {"date", "venue", "race_no", "race_type", "distance", "deadline", "day_n", "racers"} for r in races)
    assert all(set(x) == {"lane", "toban", "name", "age", "branch", "weight", "class", "nat_win", "nat_in2", "loc_win",
                          "loc_in2", "motor_no", "motor_in2", "boat_no", "boat_in2"} for r in races for x in r["racers"])
    live = T.entries_live_info(day)
    rows = pd.DataFrame(pt.races_to_rows(races, live=live))
    assert len(rows) == len(day) and rows["ex_time"].notna().sum() == day["ex_time"].notna().sum()
    ok("(1) entries_to_races / entries_live_info が parse_b・直前情報の形になる")


# ============================================================ (1b) 6艇そろわないレースを混ぜた main_v2
def test_main_v2_with_incomplete_race():
    say("--- (1b) 6艇そろわないレースを混ぜた合成データで main_v2")
    full = make_synthetic(n_days=40, venues=(3, 7), races_per_day=3, n_racers=30, seed=5)
    date = full["date"].astype(str)
    D = date.max()
    # 最終日(自己検査の「当日」)の 場7 2R の6号艇を落とす → 5行のレース。学習期間の 場3 1R(最初の日)も1行落とす
    drop = full.index[(date == D) & (full["venue"] == 7) & (full["race_no"] == 2) & (full["lane"] == 6)].tolist()
    drop += full.index[(date == date.min()) & (full["venue"] == 3) & (full["race_no"] == 1) & (full["lane"] == 3)].tolist()
    assert len(drop) == 2
    df = full.drop(index=drop).sample(frac=1.0, random_state=1)        # 並びも崩しておく(sort_entries が直す)
    msgs = []
    keep = T.six_row_mask(T.sort_entries(df), log=msgs.append)
    assert int((~keep).sum()) == 10 and msgs and "2 本・10 行" in msgs[0], (int((~keep).sum()), msgs)
    assert len(T.six_row_races(df)) == len(df) - 10
    ok("(1b) six_row_mask: 6艇そろわない2レースの10行だけ False(six_row_races も同じ行を落とす)")
    build = TMP / "build_syn"
    build.mkdir(parents=True, exist_ok=True)
    meta = T.main_v2(entries=df, rounds=12, early_stop=5, params={"num_threads": 2},
                     out_dir=build, report_path=TMP / "report_syn.json", seed_path=TMP / "seed_syn.json", log=say)
    assert meta["rows_train"] + meta["rows_valid"] + meta["rows_test"] == len(df) - 10, "行列は6艇そろったレースだけ"
    assert meta["self_check"]["ok"] is True and meta["self_check"]["date"] == D
    assert meta["self_check"]["races"] == 5, meta["self_check"]            # 最終日 6 レースのうち6艇そろった 5 レース
    assert meta["self_check"]["nan_mismatch"] == 0 and meta["self_check"]["cells_over_tol"] == 0
    assert "num_threads" not in meta["params"] and "verbosity" not in meta["params"] and "force_col_wise" not in meta["params"]
    assert meta["params"]["learning_rate"] == 0.05 and meta["params"]["num_leaves"] == 31 and meta["params"]["seed"] == 42
    assert meta["params"] == M.model_params({"num_threads": 2}) == M.model_params(None)
    ok("(1b) main_v2: 5行のレースは行列から外れ、全行を特徴量の入口に残したまま最終日の自己検査(5/6 レース)が通る。"
       "meta.params に num_threads / verbosity / force_col_wise は残らない")
    # 較正の種は試験期間(40日の後ろ4日)の後半2日だけ。最終日の5行のレースは行列に無いので種にも入らない
    check_seed(TMP / "seed_syn.json", meta, df, "(1b)")


# ============================================================ (2) entries の一部で main_v2
def test_main_v2_subset():
    say("--- (2) entries の一部で main_v2(木は少なく)")
    t = time.time()
    df = load_entries()
    sub = df[df["date"] >= FROM].reset_index(drop=True)
    del df
    say("  entries %s 以降 %d 行 %d 日 (%.1fs)" % (FROM, len(sub), sub["date"].nunique(), time.time() - t))
    build = TMP / "build"
    build.mkdir(parents=True, exist_ok=True)
    for n in ("model_win.txt", "model_top2.txt", "model_top3.txt"):      # 形1の残骸は消されること
        (build / n).write_bytes(b"tree\nstale\n")
    (build / "meta.json").write_text("{}")
    report = TMP / "model_report.json"
    seed_path = TMP / "calib_seed_gen2.json"
    meta = T.main_v2(entries=sub, rounds=60, early_stop=15, out_dir=build, report_path=report, seed_path=seed_path, log=say)
    say("  main_v2 done (%.0fs total)" % (time.time() - t))
    # 出力ファイル
    assert (build / "model.txt").exists() and (build / "meta.json").exists()
    assert not any((build / n).exists() for n in ("model_win.txt", "model_top2.txt", "model_top3.txt")), "形1の残骸が残っている"
    m_file = json.loads((build / "meta.json").read_text(encoding="utf-8"))
    assert m_file == json.loads(report.read_text(encoding="utf-8")) == json.loads(json.dumps(meta))
    ok("(2) 出力: model.txt + meta.json(形1のファイルは消える)、docs の報告は meta と同じ中身")
    # meta の項目(設計 4.1 / 4.9)
    assert meta["format"] == 2 and meta["gen"] == 2
    assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} JST$", meta["trained_at"])
    assert meta["period"] == [str(sub["date"].min()), str(sub["date"].max())]
    assert meta["rows_train"] % 6 == 0 and meta["rows_train"] + meta["rows_valid"] + meta["rows_test"] == len(sub)
    assert meta["races_train"] <= meta["rows_train"] // 6 and meta["races_test"] == meta["rows_test"] // 6
    assert set(meta["best_iterations"]) == {"pl"} and 1 <= meta["best_iterations"]["pl"] <= 60
    assert meta["features"] == list(FEATURES_V2) and meta["stage_cols"] == M.STAGE_COLS and meta["cat_idx"] == M.CAT_IDX
    assert meta["live_cols"] == LIVE_COLS_V2 and meta["features_version"] == pt.FEATURES_HIST_VERSION
    assert meta["elo_cfg"] == dict(pt.ELO_CFG)
    assert meta["sengen"] == {"top5_min": 0.47, "valid_rate": None}
    assert meta["params"] == M.model_params(None) and "num_threads" not in meta["params"]
    for k in ("test_metrics", "baseline_metrics"):
        assert set(meta[k]) == GEN1_METRIC_KEYS, (k, set(meta[k]) ^ GEN1_METRIC_KEYS)
        assert set(meta[k]["sengen"]) == {"races", "races_per_day", "top5_min", "top5_hit_rate"}
        # 採点の分母は着順の付いたレース(全艇 NaN の中止レースは数えない。現行 race_eval_rows と同じ)
        assert meta[k]["sengen"]["top5_min"] == 0.47 and 0 < meta[k]["races"] <= meta["races_test"]
        assert meta[k]["races"] == meta["test_metrics"]["races"]
        assert 0 <= meta[k]["top5_hit_rate"] <= 1 and 0 <= meta[k]["win_hit_rate"] <= 1
    assert meta["test_metrics"]["top5_hit_rate"] > meta["baseline_metrics"]["top5_hit_rate"], "木60本でも基準には勝つはず"
    imp = meta["feature_importance"]
    assert list(imp) and set(imp) == set(FEATURES_V2) and list(imp.values()) == sorted(imp.values(), reverse=True)
    assert all(v >= 0 for v in imp.values()) and set(meta["stage_importance"]) == set(M.STAGE_COLS)
    assert meta["self_check"]["ok"] is True and meta["self_check"]["date"] == str(sub["date"].max())
    assert meta["self_check"]["nan_mismatch"] == 0 and meta["self_check"]["cells_over_tol"] == 0
    ok("(2) meta の項目: format/gen/trained_at/rows_*/period/best_iterations{pl}/features/stage_cols/cat_idx/"
       "features_version/elo_cfg/params(実行環境の項目なし)/sengen(0.47)/test_metrics・baseline_metrics(今と同じ項目)/"
       "feature_importance(153・降順)/self_check")
    say("  TEST %s" % json.dumps(meta["test_metrics"], ensure_ascii=False))
    say("  self_check %s" % json.dumps(meta["self_check"], ensure_ascii=False))
    # 較正の種(build_calib が読める形)。試験期間の後半(日付の中央値以降)の着順ありレースだけから作られる
    seed = check_seed(seed_path, meta, sub, "(2)")
    say("  seed built_from: %s" % seed["built_from"])
    # 読み込み側が受け付ける
    booster = lgb.Booster(model_file=str(build / "model.txt"))
    assert booster.num_feature() == len(FEATURES_V2) + M.N_STAGE and booster.num_trees() == meta["best_iterations"]["pl"]
    pt.check_meta_v2(m_file, booster)
    old = pt._model_candidates
    pt._model_candidates = lambda: [(build, "build")]
    try:
        m, models, sengen = pt.load_models()
    finally:
        pt._model_candidates = old
    assert m["gen"] == 2 and m["format"] == 2 and set(models) == {"pl"} and sengen["top5_min"] == 0.47
    try:
        import model_store as ms
        vm = ms.valid_dir(build)
        assert vm is not None and ms.model_format(vm) == 2 and ms.crlf_model_files(build) == []
        ok("(2) model_store.valid_dir が形2の一式として受け付ける(model.txt %d バイト)" % (build / "model.txt").stat().st_size)
    except ImportError:
        say("  SKIP model_store の検査(読めない)")
    ok("(2) predict_today.check_meta_v2 / load_models がその一式を読む(gen 2・{pl}・木 %d 本)" % booster.num_trees())


def main():
    rmtree_retry(TMP)
    TMP.mkdir(parents=True)
    try:
        test_resolve_model_gen()
        test_self_check_synthetic()
        test_main_v2_with_incomplete_race()
        if QUICK:
            print("(--quick: entries での main_v2 は省いた)")
        else:
            test_main_v2_subset()
        print("%d checks passed" % _n_ok)
        print("ALL OK")
    finally:
        rmtree_retry(TMP)


if __name__ == "__main__":
    main()

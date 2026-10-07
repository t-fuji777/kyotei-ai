# -*- coding: utf-8 -*-
"""3連単の板(全120通り)の記録 scripts/boards.py と、update_all.py からの呼び出しの回帰テスト(通信なし・時計は差し替え)。

確かめること:
  boards.py     o の並びが model_v2.PERMS / COMBO_STR と同じで、無い買い目は null / 重複の抑制(同じ板は続けて書かない)/
                "final" は1レース1回 / kind ごとに行が分かれる / フォルダの作成と追記 / 壊れた入力でも例外が出ない /
                日付違いの板は書かない / 1行の大きさの目安
  update_all.py do_odds の後に板が記録される("pre" と "final")/ 朝の暫定は "morning" / 判定直前の取り直し
                (_make_refresher)で "judge" / 取り直しを省いた時は judge の行が増えない(通信を増やさない)/
                RECORD_BOARDS=False で止まる / 記録が失敗しても取得・判定は続く / 既存の保存(os・jt・odds.t3)は変わらない
update_all 側の仕組みは tests/test_update_all_stamps.py の偽の fetch と同じ(あちらは変えずに、ここに写しを持つ)。
一時フォルダは .tmp_boards_<pid>/(.gitignore の .tmp_*/)。

実行: python tests/test_boards.py   (Windows では PYTHONUTF8=1 を付ける)"""
import json
import math
import os
import shutil
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.modules["notify"] = types.SimpleNamespace(notify_events=lambda *a, **k: None)
import boards as B
import update_all as U

JST = timezone(timedelta(hours=9))
YMD = "20261001"
START = datetime(2026, 10, 1, 12, 0, 0, tzinfo=JST)
NOW = datetime(2026, 10, 1, 12, 3, 4, tzinfo=JST)
COMBOS = ["1-2-3", "1-3-2", "2-1-3", "1-2-4", "1-4-2", "2-1-4"]
HIGH = {c: 9.9 for c in COMBOS}
LOW = {c: 2.0 for c in COMBOS}
TMP = ROOT / f".tmp_boards_{os.getpid()}"      # 実行ごとに一意(同時に走る他の実行の一時フォルダを消さない)
_n_tmp = [0]


def fresh_root():
    _n_tmp[0] += 1
    d = TMP / f"r{_n_tmp[0]}"
    d.mkdir(parents=True, exist_ok=True)
    B.reset()
    return d


def rows(root, ymd=YMD):
    p = Path(root) / "data" / "boards" / f"{ymd}.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def check_row(row, deadline=None):
    """行の形: 鍵・120個の o・t の形。deadline を渡せば m が「t から締切までの分(floor)」と一致すること。"""
    assert set(row) == {"t", "v", "r", "k", "m", "o"}, row.keys()
    assert len(row["o"]) == 120 and row["k"] in B.KINDS and len(row["t"]) == 8 and row["t"][2] == ":"
    assert isinstance(row["v"], int) and isinstance(row["r"], int)
    if deadline:
        h, m = map(int, deadline.split(":"))
        th, tm, ts = map(int, row["t"].split(":"))
        mins = ((h * 60 + m) * 60 - (th * 3600 + tm * 60 + ts)) / 60
        assert row["m"] == math.floor(mins), (row["m"], mins, row["t"], deadline)


try:
    # ---------------------------------------------------------------- 1) 並び
    try:
        import model_v2 as M
        assert B.COMBOS == [str(x) for x in M.COMBO_STR], "boards.COMBOS が model_v2.COMBO_STR と違う"
        assert [tuple(int(x) - 1 for x in c.split("-")) for c in B.COMBOS] == [tuple(p) for p in M.PERMS.tolist()]
        print("1 ok: COMBOS == model_v2.COMBO_STR (PERMS order)")
    except ImportError as e:
        print(f"1 skipped: model_v2 を読めない({e})。並びは式で確かめる")
    assert len(B.COMBOS) == 120 and len(set(B.COMBOS)) == 120
    assert B.COMBOS[0] == "1-2-3" and B.COMBOS[1] == "1-2-4" and B.COMBOS[4] == "1-3-2" and B.COMBOS[-1] == "6-5-4"
    for i, c in enumerate(B.COMBOS):
        a, b, c3 = (int(x) - 1 for x in c.split("-"))
        assert i == a * 20 + (b - (b > a)) * 4 + (c3 - (c3 > a) - (c3 > b)), c
    print("1b ok: position formula a*20 + (b-(b>a))*4 + (c-(c>a)-(c>b))")

    # ---------------------------------------------------------------- 2) board_list: 無い買い目は None
    o = B.board_list({"1-2-3": 9.9, "6-5-4": "12.5", "2-1-4": None, "9-9-9": 1.0, "1-2-4": "abc",
                      "1-3-2": float("nan"), "1-4-2": True})
    assert o[0] == 9.9 and o[-1] == 12.5 and sum(x is not None for x in o) == 2, o
    assert B.board_list(None) == [None] * 120 and B.board_list([1, 2, 3]) == [None] * 120 and B.board_list({}) == [None] * 120
    print("2 ok: board_list maps by combo, null for missing/invalid")

    # ---------------------------------------------------------------- 3) record の基本と m の丸め(floor。締切後は負)
    root = fresh_root()
    assert B.record(YMD, 12, 5, HIGH, "pre", 12.7, NOW, root=root) is True
    R = rows(root)
    assert len(R) == 1 and R[0]["t"] == "12:03:04" and R[0]["v"] == 12 and R[0]["r"] == 5 and R[0]["k"] == "pre" and R[0]["m"] == 12
    assert R[0]["o"][0] == 9.9 and R[0]["o"][B.COMBOS.index("2-1-4")] == 9.9 and sum(x is not None for x in R[0]["o"]) == 6
    check_row(R[0])
    assert B.record(YMD, 12, 6, HIGH, "final", -0.5, NOW, root=root) and rows(root)[-1]["m"] == -1, "締切の直後は -1(負)"
    assert B.record(YMD, 12, 7, HIGH, "pre", 0.2, NOW, root=root) and rows(root)[-1]["m"] == 0
    assert B.record(YMD, 12, 8, HIGH, "pre", None, NOW, root=root) and rows(root)[-1]["m"] is None
    assert B.record(YMD, "12", "9", HIGH, "pre", 5, NOW, root=root) and rows(root)[-1]["v"] == 12 and rows(root)[-1]["r"] == 9
    print("3 ok: one line per board; m = floor(minutes to deadline)")

    # ---------------------------------------------------------------- 4) 重複の抑制: 同じ (ymd,v,r,k) で同じ板は書かない
    root = fresh_root()
    assert B.record(YMD, 1, 5, HIGH, "pre", 50, NOW, root=root) is True
    assert B.record(YMD, 1, 5, dict(HIGH), "pre", 49, NOW + timedelta(minutes=1), root=root) is False, "同じ板は書かない"
    assert len(rows(root)) == 1
    changed = dict(HIGH); changed["1-2-3"] = 8.8
    assert B.record(YMD, 1, 5, changed, "pre", 48, NOW, root=root) is True, "板が動けば書く"
    assert B.record(YMD, 1, 5, HIGH, "pre", 47, NOW, root=root) is True, "直前の板と比べる(戻っても書く)"
    assert B.record(YMD, 1, 5, HIGH, "judge", 13, NOW, root=root) is True, "kind が違えば同じ板でも書く"
    assert B.record(YMD, 1, 6, HIGH, "pre", 47, NOW, root=root) is True, "レースが違えば書く"
    assert B.record(YMD, 2, 5, HIGH, "pre", 47, NOW, root=root) is True, "会場が違えば書く"
    assert [r_["k"] for r_ in rows(root)] == ["pre", "pre", "pre", "judge", "pre", "pre"]
    print("4 ok: identical consecutive boards are suppressed per (ymd, v, r, k)")

    # ---------------------------------------------------------------- 5) final は1レース1回まで
    root = fresh_root()
    assert B.record(YMD, 1, 5, HIGH, "final", -3, NOW, root=root) is True
    assert B.record(YMD, 1, 5, LOW, "final", -4, NOW, root=root) is False, "板が違っても final は2回書かない"
    assert B.record(YMD, 1, 5, LOW, "pre", -4, NOW, root=root) is True, "final の後でも他の kind は書ける"
    assert B.record(YMD, 1, 6, LOW, "final", -4, NOW, root=root) is True, "別のレースの final は書く"
    assert [(r_["r"], r_["k"]) for r_ in rows(root)] == [(5, "final"), (5, "pre"), (6, "final")]
    print("5 ok: final at most once per race")

    # ---------------------------------------------------------------- 6) kind ごとの行・フォルダ作成・追記
    root = fresh_root()
    assert not (root / "data").exists()
    for k in B.KINDS:
        assert B.record(YMD, 3, 7, HIGH, k, 10, NOW, root=root) is True
    assert (root / "data" / "boards").is_dir()
    R = rows(root)
    assert [r_["k"] for r_ in R] == list(B.KINDS) and all(r_["v"] == 3 and r_["r"] == 7 for r_ in R)
    # 追記: 別の日のファイルは別、同じ日のファイルには足される
    assert B.record("20261002", 3, 7, HIGH, "pre", 10, NOW + timedelta(days=1), root=root) is True
    assert len(rows(root)) == 4 and len(rows(root, "20261002")) == 1
    txt = (root / "data" / "boards" / f"{YMD}.jsonl").read_text(encoding="utf-8")
    assert txt.endswith("\n") and txt.count("\n") == 4, "1行1板・改行終わり"
    print("6 ok: one row per kind; folder created; append")

    # ---------------------------------------------------------------- 7) 壊れた入力でも例外が出ない(False が返り、行は増えない)
    root = fresh_root()
    bad = [
        (YMD, 1, 5, None, "pre", 10, NOW),                 # 板が無い
        (YMD, 1, 5, {}, "pre", 10, NOW),                   # 空の板
        (YMD, 1, 5, "1-2-3:9.9", "pre", 10, NOW),          # dict でない
        (YMD, 1, 5, {"x": object()}, "pre", 10, NOW),      # 値が数でない
        (YMD, 1, 5, HIGH, "bogus", 10, NOW),               # 知らない kind
        (YMD, "abc", 5, HIGH, "pre", 10, NOW),             # 会場が数でない
        (YMD, 1, None, HIGH, "pre", 10, NOW),              # レースが無い
        (YMD, 1, 5, HIGH, "pre", "soon", NOW),             # 分が数でない
        (YMD, 1, 5, HIGH, "pre", 10, "12:00"),             # 時刻が datetime でない
        (None, 1, 5, HIGH, "pre", 10, NOW),                # 日付が無い
    ]
    for args in bad:
        assert B.record(*args, root=root) is False, args
    assert rows(root) == []
    # 書く先がフォルダにできない(ファイルがある)場合も例外を出さない
    blocked = fresh_root()
    (blocked / "data").write_text("not a dir", encoding="utf-8")
    assert B.record(YMD, 1, 5, HIGH, "pre", 10, NOW, root=blocked) is False
    print("7 ok: broken inputs never raise")

    # ---------------------------------------------------------------- 8) 日付違い: now の日が ymd と違えば書かない
    root = fresh_root()
    assert B.record(YMD, 1, 5, HIGH, "pre", 10, datetime(2026, 10, 7, 12, 0, tzinfo=JST), root=root) is False
    assert rows(root) == [] and not (root / "data" / "boards").exists()
    print("8 ok: a board dated another day is not written")

    # ---------------------------------------------------------------- 9) 大きさの目安(1行約700バイト)
    root = fresh_root()
    full = {c: round(1.5 + i * 7.3, 1) for i, c in enumerate(B.COMBOS)}      # 1.5〜870.2 倍
    assert B.record(YMD, 24, 12, full, "final", -2, NOW, root=root) is True
    size = len((root / "data" / "boards" / f"{YMD}.jsonl").read_bytes())
    assert 500 <= size <= 900, size
    print(f"9 ok: a full 120-value row is {size} bytes")

    # ================================================================ update_all からの呼び出し
    def race(no, deadline, odds=None, live=False, top3p=0.45):
        p = top3p / 3
        r = {"no": no, "deadline": deadline, "type": "x", "rn_full": True,
             "picks": [{"c": c, "p": (p if i < 3 else 0.02)} for i, c in enumerate(COMBOS)],
             "boats": [], "fuku": {"lane": 1}}
        if odds is not None:
            r["odds"] = {"t3": dict(odds)}
        if live:
            r["live"] = True
            r["st_ex"] = {"1": ".10"}
            r["weather"] = {"sky": "x"}
        return r

    def run(races, argv, fetched_odds=None, fetched_t3=None, switches=None, start=START):
        """tests/test_update_all_stamps.py の run の写し(公開・通知の細部は省く)。races を1会場(コード1)に載せて
        main() を1回走らせる。本オッズ1レース=50秒、t3 1ページ=10秒として時計を進める。
        戻り値: (レース番号→レース, 板の行, 取得の記録)。板は tmp/data/boards に書かれる(U.ROOT を差し替えるため)。"""
        tmp = fresh_root()
        d = tmp / "docs" / "predictions"
        d.mkdir(parents=True)
        pred = {"date": YMD, "generated_at": "x", "venues": [{"code": 1, "name": "t", "races": races}]}
        (d / f"{YMD}.json").write_text(json.dumps(pred, ensure_ascii=False), encoding="utf-8")
        clock = {"t": start}
        calls = {"odds": [], "t3": []}

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["t"]

        def adv(s):
            clock["t"] = clock["t"] + timedelta(seconds=s)

        def f_odds(ymd, jcd, rno):
            adv(50)
            calls["odds"].append(rno)
            o = (fetched_odds or {}).get(rno)
            return {"fuku": {1: "1.0-1.5"}, "t3": dict(o), "f3": {}, "t2": {}, "f2": {}, "k": {}} if o else None

        def f_t3(ymd, jcd, rno):
            adv(10)
            calls["t3"].append(rno)
            o = (fetched_t3 or {}).get(rno)
            return dict(o) if o else None

        def f_none(*a):
            adv(10)
            return None

        def f_before(*a):
            adv(10)
            return ""

        names = ("ROOT", "datetime", "fetch_odds", "fetch_result", "fetch_t3", "fetch_racename", "fetch_before_html",
                 "time", "subprocess", "RECORD_BOARDS", "ODDS_REFRESH_FROM")
        saved = {k: getattr(U, k) for k in names}
        old_env, old_argv = os.environ.pop("GITHUB_WORKFLOW", None), sys.argv
        try:
            U.ROOT = tmp
            U.datetime = FakeDT
            U.fetch_odds = f_odds
            U.fetch_result = f_none
            U.fetch_t3 = f_t3
            U.fetch_racename = f_none
            U.fetch_before_html = f_before
            U.time = types.SimpleNamespace(sleep=lambda s: adv(s),
                                           monotonic=lambda: (clock["t"] - start).total_seconds())
            U.subprocess = types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(returncode=0))
            for k, v in (switches or {}).items():
                setattr(U, k, v)
            sys.argv = ["update_all.py"] + argv
            U.main()
        finally:
            for k, v in saved.items():
                setattr(U, k, v)
            sys.argv = old_argv
            if old_env is not None:
                os.environ["GITHUB_WORKFLOW"] = old_env
        out = json.loads((d / f"{YMD}.json").read_text(encoding="utf-8"))
        return {r["no"]: r for r in out["venues"][0]["races"]}, rows(tmp), calls

    ON = {"ODDS_REFRESH_FROM": YMD}
    OFF = {"ODDS_REFRESH_FROM": "29991231"}

    # 10) full: do_odds の後に板が記録される。締切後の取得は "final"、締切前は "pre"、朝の暫定(fetch_t3)は "morning"。
    #     5R: 締切11:50(過ぎている)・オッズ無し → 本オッズを取って final。6R: 締切12:50 → pre。20R: 締切14:20(60分より先)→ morning。
    R, BR, calls = run([race(5, "11:50"), race(6, "12:50"), race(20, "14:20")], [],
                       fetched_odds={5: HIGH, 6: HIGH}, fetched_t3={20: LOW}, switches=OFF)
    assert calls["odds"] == [5, 6] and calls["t3"] == [20], calls
    assert [(b["v"], b["r"], b["k"]) for b in BR] == [(1, 5, "final"), (1, 6, "pre"), (1, 20, "morning")], BR
    for b, dl in zip(BR, ("11:50", "12:50", "14:20")):
        check_row(b, dl)
    assert BR[0]["m"] < 0 < BR[1]["m"] < 60 < BR[2]["m"], [b["m"] for b in BR]
    assert BR[0]["o"][0] == 9.9 and sum(x is not None for x in BR[0]["o"]) == 6, "板は全120通り(無い買い目は null)"
    assert BR[2]["o"][0] == 2.0 and BR[2]["o"][B.COMBOS.index("2-1-4")] == 2.0
    # 既存の保存は変わらない: 当日ファイルの odds.t3 は買い目の分だけ、final / prov の付け方もそのまま
    assert R[5]["odds"]["t3"] == HIGH and R[5]["odds"].get("final") is True and "prov" not in R[5]["odds"]
    assert R[6]["odds"]["t3"] == HIGH and "final" not in R[6]["odds"] and "prov" not in R[6]["odds"]
    assert R[20]["odds"]["t3"] == LOW and R[20]["odds"].get("prov") is True
    assert R[5]["odds"]["axis"]["t3"] == 9.9 and R[20]["odds"]["axis"]["t3"] == 2.0
    print("10 ok: do_odds -> final/pre rows, morning sweep -> morning row:", [(b["r"], b["k"], b["m"]) for b in BR])

    # 10b) RECORD_BOARDS=False で止まる(取得・保存は従来どおり)
    R, BR, calls = run([race(5, "11:50"), race(6, "12:50"), race(20, "14:20")], [],
                       fetched_odds={5: HIGH, 6: HIGH}, fetched_t3={20: LOW}, switches=dict(OFF, RECORD_BOARDS=False))
    assert BR == [] and calls["odds"] == [5, 6] and R[5]["odds"]["t3"] == HIGH and R[20]["odds"].get("prov") is True
    print("10b ok: kill switch")

    # 10c) 取得できなかった(None)レースの行は無い
    R, BR, calls = run([race(5, "11:50"), race(6, "12:50")], [], fetched_odds={6: HIGH}, switches=OFF)
    assert [(b["r"], b["k"]) for b in BR] == [(6, "pre")] and "odds" not in R[5]
    print("10c ok: no row when the fetch returned nothing")

    # 11) --results-only: 判定直前の取り直し(_make_refresher の fetch_t3)で "judge" が記録される。
    #     保存済みは高い板、取り直した板は 2.0 倍 → 判定は取り直した板(見送り)。板の行は取り直した板の全120通り。
    R, BR, calls = run([race(5, "12:14", HIGH)], ["--results-only"], fetched_t3={5: LOW}, switches=ON)
    assert calls["t3"] == [5] and [(b["v"], b["r"], b["k"]) for b in BR] == [(1, 5, "judge")], (calls, BR)
    check_row(BR[0], "12:14")
    assert 0 <= BR[0]["m"] <= 15 and BR[0]["o"][0] == 2.0 and sum(x is not None for x in BR[0]["o"]) == 6
    # 既存の判定・保存は変わらない(tests/test_update_all_stamps.py 9a と同じ)
    assert R[5].get("tk") == 0 and "3.1倍未満 2.0倍" in (R[5].get("rs") or ""), (R[5].get("tk"), R[5].get("rs"))
    assert R[5]["os"]["1-2-3"] == 2.0 and R[5]["odds"]["t3"] == LOW and R[5]["odds"].get("jt") == BR[0]["t"]
    print("11 ok: judge-time refetch -> judge row at m =", BR[0]["m"])

    # 11b) 取り直しが対象日より前(取り直さない)なら judge の行は無い。取り直しに失敗した時も無い
    R, BR, calls = run([race(5, "12:14", HIGH)], ["--results-only"], fetched_t3={5: LOW}, switches=OFF)
    assert calls["t3"] == [] and BR == [] and R[5].get("tk") == 1
    R, BR, calls = run([race(5, "12:14", HIGH)], ["--results-only"], fetched_t3={}, switches=ON)
    assert calls["t3"] == [5] and BR == [] and R[5].get("tk") == 1 and "jt" not in R[5]["odds"]
    print("11b ok: no judge row without a refetched board")

    # 12) full: 本オッズを取ったばかりの候補は判定直前に取り直さない(通信を増やさない)→ 行は "pre" だけで judge は無い。
    #     取っていない候補(8R: 保存済み・締切12:16)は取り直す → judge の行が増える。
    R, BR, calls = run([race(5, "12:14"), race(8, "12:16", HIGH), race(6, "12:50")], [],
                       fetched_odds={5: LOW, 6: HIGH}, fetched_t3={8: LOW}, switches=ON)
    assert calls["odds"] == [5, 6] and calls["t3"] == [8], calls
    assert sorted((b["r"], b["k"]) for b in BR) == [(5, "pre"), (6, "pre"), (8, "judge")], BR
    assert R[5].get("tk") == 0 and R[5]["odds"].get("jt") and R[8].get("tk") == 0 and R[8]["odds"].get("jt")
    print("12 ok: one row per fetch; no extra request, no extra row")

    # 13) 記録が失敗(例外)しても取得・判定・保存は続く
    saved_record = B.record

    def boom(*a, **k):
        raise RuntimeError("disk full")

    B.record = boom
    try:
        R, BR, calls = run([race(5, "12:14", HIGH), race(6, "12:50")], [], fetched_odds={6: HIGH}, fetched_t3={5: LOW}, switches=ON)
    finally:
        B.record = saved_record
    assert BR == [] and calls["odds"] == [6] and calls["t3"] == [5]
    assert R[6]["odds"]["t3"] == HIGH and R[5].get("tk") == 0 and R[5]["os"]["1-2-3"] == 2.0
    print("13 ok: a failing recorder does not stop odds, judgment or writing")

    # 14) 本番の定数: 記録は入っている
    assert U.RECORD_BOARDS is True and U.boards is B
    print("14 ok: RECORD_BOARDS is on")
    print("ALL OK")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

# -*- coding: utf-8 -*-
"""update_all.py の打刻タイミングの回帰テスト(ネットワーク不使用・時計は差し替え)。
実行: python tests/test_update_all_stamps.py   (Windows では PYTHONUTF8=1 を付ける)"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.modules["notify"] = types.SimpleNamespace(notify_events=lambda *a, **k: None)
import update_all as U

JST = timezone(timedelta(hours=9))
YMD = "20261001"
START = datetime(2026, 10, 1, 12, 0, 0, tzinfo=JST)
COMBOS = ["1-2-3", "1-3-2", "2-1-3", "1-2-4", "1-4-2", "2-1-4"]


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


HIGH = {c: 9.9 for c in COMBOS}
LOW = {c: 2.0 for c in COMBOS}


def run(races, argv, fetched_odds=None, env_workflow=None, break_stamps=False, switches=None):
    """races を1会場(コード1)に載せて main() を1回走らせる。取得1ページ=10秒として時計を進める。"""
    tmp = Path(tempfile.mkdtemp())
    d = tmp / "docs" / "predictions"
    d.mkdir(parents=True)
    pred = {"date": YMD, "generated_at": "x", "venues": [{"code": 1, "name": "t", "races": races}]}
    (d / f"{YMD}.json").write_text(json.dumps(pred, ensure_ascii=False), encoding="utf-8")
    clock = {"t": START}
    calls = {"odds": [], "publish": 0}

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

    def f_none(*a):
        adv(10)
        return None

    def f_before(*a):
        adv(10)
        return ""

    saved = {k: getattr(U, k) for k in ("ROOT", "datetime", "fetch_odds", "fetch_result", "fetch_t3",
                                         "fetch_racename", "fetch_before_html", "time", "subprocess",
                                         "do_stamps", "MIDRUN_STAMP", "MIDRUN_PUBLISH", "TAIL_STAMP")}
    old_env = os.environ.get("GITHUB_WORKFLOW")
    old_argv = sys.argv
    try:
        U.ROOT = tmp
        U.datetime = FakeDT
        U.fetch_odds = f_odds
        U.fetch_result = f_none
        U.fetch_t3 = f_none
        U.fetch_racename = f_none
        U.fetch_before_html = f_before
        U.time = types.SimpleNamespace(sleep=lambda s: adv(s))

        def fake_run(cmd, **kw):
            calls["publish"] += 1
            return types.SimpleNamespace(returncode=0)
        U.subprocess = types.SimpleNamespace(run=fake_run)
        if break_stamps:
            def bad(*a, **k):
                raise RuntimeError("boom")
            U.do_stamps = bad
        for k, v in (switches or {}).items():
            setattr(U, k, v)
        if env_workflow is None:
            os.environ.pop("GITHUB_WORKFLOW", None)
        else:
            os.environ["GITHUB_WORKFLOW"] = env_workflow
        sys.argv = ["update_all.py"] + argv
        U.main()
    finally:
        for k, v in saved.items():
            setattr(U, k, v)
        sys.argv = old_argv
        if old_env is None:
            os.environ.pop("GITHUB_WORKFLOW", None)
        else:
            os.environ["GITHUB_WORKFLOW"] = old_env
    out = json.loads((d / f"{YMD}.json").read_text(encoding="utf-8"))
    return {r["no"]: r for r in out["venues"][0]["races"]}, out, calls


# 1) full の取得中に締切15分前を跨いだレースは、その場(次の取得の前)で打刻される
#    5R: 締切12:16 → T-15 は 12:01。オッズ取得対象は 6R と 7R(各50秒)。
R, out, calls = run([race(5, "12:16", HIGH), race(6, "12:50"), race(7, "12:55")], [],
                    fetched_odds={6: HIGH, 7: HIGH})
assert calls["odds"] == [6, 7], calls
assert R[5].get("tk") == 1 and R[5].get("pt") == "12:01", R[5].get("pt")
assert "tk" not in R[6] and "tk" not in R[7]
assert out["results_updated_at"].endswith("JST") and out["results_updated_at"].count(":") == 2
print("1 ok: mid-run stamp at", R[5]["pt"])

# 1b) 止めた場合(MIDRUN_STAMP=False)は従来どおり: 開始時刻で判定するので full の中では打刻されない
R, out, calls = run([race(5, "12:16", HIGH), race(6, "12:50"), race(7, "12:55")], [],
                    fetched_odds={6: HIGH, 7: HIGH}, switches={"MIDRUN_STAMP": False})
assert "tk" not in R[5]
print("1b ok: kill switch restores the old behaviour")

# 2) この実行でオッズを取る予定のレースは、取り終えるまで打刻しない(取得したオッズで判定する)
#    5R: 締切12:14(開始時点で既に T-15 を過ぎている)・オッズ未取得 → 取得対象。
#    取得したオッズは全点2.0倍 → 厳選は不成立(tk=0, 理由つき)。取得前に打刻すると
#    オッズ無しで素通りして tk=1 になってしまう。
R, out, calls = run([race(5, "12:14"), race(6, "12:50")], [], fetched_odds={5: LOW, 6: HIGH})
assert R[5].get("tk") == 0 and "3.1倍未満" in (R[5].get("rs") or ""), (R[5].get("tk"), R[5].get("rs"))
assert R[5]["os"]["1-2-3"] == 2.0
print("2 ok: pending race judged with the fetched odds, pt", R[5]["pt"])

# 2b) 取得に失敗した取得待ちレースは、既存のオッズで打刻される(待ち続けない)。
#     既存のオッズが無い場合は「3.1倍以上」を確かめられないので成立させない(オッズ未取得)。
R, out, calls = run([race(5, "12:14"), race(6, "12:50")], [], fetched_odds={6: HIGH})
assert R[5].get("tk") == 0 and R[5].get("rs") == "オッズ未取得", (R[5].get("tk"), R[5].get("rs"))
assert R[5].get("os") == {c: None for c in COMBOS[:4]}
R, out, calls = run([race(5, "12:14", HIGH), race(6, "12:50")], [], fetched_odds={6: HIGH})
assert "tk" not in R[5] or R[5].get("tk") == 1  # 既存の本オッズがあれば取得対象にならず、そのオッズで成立
print("2b ok")

# 2c) 上位3点のうち1点だけオッズが無い場合も成立させない
part = dict(HIGH); del part["1-3-2"]
R, out, calls = run([race(5, "12:14", part), race(6, "12:50")], [], fetched_odds={6: HIGH})
assert R[5].get("tk") == 0 and R[5].get("rs") == "オッズ未取得", (R[5].get("tk"), R[5].get("rs"))
print("2c ok")

# 3) --results-only の末尾: 取得中に T-15 を跨いだレースのうち、買い目もオッズも動かないものだけ打刻
#    1R(締切11:50)が結果待ちで fetch_result が1回(10秒)走る。5R/6R/7R は締切12:15 → T-15 は 12:00:00...
#    開始を T-15 の5秒前に合わせるため締切を 12:15 とし、開始時刻 12:00:00 の5秒前相当にずらす。
START = datetime(2026, 10, 1, 11, 59, 55, tzinfo=JST)
prov = race(7, "12:15", HIGH, live=True)
prov["odds"]["prov"] = True
done = race(1, "11:40", HIGH)
done["tk"] = 0
done["att"] = 0
R, out, calls = run([done, race(5, "12:15", HIGH, live=True), race(6, "12:15", HIGH, live=False), prov],
                    ["--results-only"])
assert R[5].get("tk") == 1 and R[5].get("pt") == "12:00", R[5]
assert "tk" not in R[6], "展示未反映のレースは次の周回(--live-window の後)に回す"
assert "tk" not in R[7], "本オッズ未取得(暫定)のレースは次の周回に回す"
print("3 ok: tail stamp only for settled races")
R, out, calls = run([done, race(5, "12:15", HIGH, live=True)], ["--results-only"], switches={"TAIL_STAMP": False})
assert "tk" not in R[5]
print("3b ok: kill switch")
START = datetime(2026, 10, 1, 12, 0, 0, tzinfo=JST)

# 4) 合間の打刻が例外を出しても、オッズ取得は最後まで行い、ファイルへ書く
R, out, calls = run([race(5, "12:16", HIGH), race(6, "12:50"), race(7, "12:55")], [],
                    fetched_odds={6: HIGH, 7: HIGH}, break_stamps=True)
assert calls["odds"] == [6, 7] and R[6]["odds"]["t3"] and R[7]["odds"]["t3"]
print("4 ok: stamp failure does not block odds")

# 5) その場の公開は auto-update の中でだけ、打刻のあった合間ごとに1回
args = dict(fetched_odds={6: HIGH, 7: HIGH})
ON = {"MIDRUN_PUBLISH": True}
_, _, calls = run([race(5, "12:16", HIGH), race(6, "12:50"), race(7, "12:55")], [], env_workflow="auto-update",
                  switches=ON, **args)
assert calls["publish"] == 1, calls
_, _, calls = run([race(5, "12:16", HIGH), race(6, "12:50"), race(7, "12:55")], [], env_workflow="all-update",
                  switches=ON, **args)
assert calls["publish"] == 0
_, _, calls = run([race(5, "12:16", HIGH), race(6, "12:50"), race(7, "12:55")], [], env_workflow=None,
                  switches=ON, **args)
assert calls["publish"] == 0
_, _, calls = run([race(5, "12:16", HIGH), race(6, "12:50"), race(7, "12:55")], [], env_workflow="auto-update",
                  switches={"MIDRUN_PUBLISH": False}, **args)
assert calls["publish"] == 0
print("5 ok: publish only inside the auto-update loop")
print("ALL OK")

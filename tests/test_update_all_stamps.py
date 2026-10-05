# -*- coding: utf-8 -*-
"""update_all.py の打刻タイミングと、通知の呼び出し方の回帰テスト(ネットワーク不使用・時計は差し替え)。
通知(notify.notify_events)は偽物に差し替え、「いつ・何回呼ばれたか」と「失敗しても打刻・取得が
続くか」を見る。通知の中身は tests/test_notify_push.py で確かめる。
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


def run(races, argv, fetched_odds=None, env_workflow=None, break_stamps=False, switches=None,
        notify=None, notify_cost=0, publish_raises=False, fetched_t3=None):
    """races を1会場(コード1)に載せて main() を1回走らせる。取得1ページ=10秒として時計を進める。
    notify: notify_events の代わりに呼ぶ関数(pred, ymd)。戻り値がそのまま返る(真=未送信が残った)。
    notify_cost: 通知1回にかかる秒数(時計を進める)。publish_raises: 合間の公開が例外を出す。
    calls["log"] に、公開・通知・取得の起きた順を残す。"""
    tmp = Path(tempfile.mkdtemp())
    d = tmp / "docs" / "predictions"
    d.mkdir(parents=True)
    pred = {"date": YMD, "generated_at": "x", "venues": [{"code": 1, "name": "t", "races": races}]}
    (d / f"{YMD}.json").write_text(json.dumps(pred, ensure_ascii=False), encoding="utf-8")
    clock = {"t": START}
    calls = {"odds": [], "publish": 0, "publish_kw": None, "notify": 0, "log": [], "tk_on_disk": []}

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["t"]

    def adv(s):
        clock["t"] = clock["t"] + timedelta(seconds=s)

    def tk_on_disk():
        """いまディスク上の当日ファイルで tk=1 になっているレース番号。"""
        cur = json.loads((d / f"{YMD}.json").read_text(encoding="utf-8"))
        return sorted(r["no"] for r in cur["venues"][0]["races"] if r.get("tk") == 1)

    def f_odds(ymd, jcd, rno):
        adv(50)
        calls["odds"].append(rno)
        calls["log"].append(f"odds:{rno}")
        o = (fetched_odds or {}).get(rno)
        return {"fuku": {1: "1.0-1.5"}, "t3": dict(o), "f3": {}, "t2": {}, "f2": {}, "k": {}} if o else None

    def f_result(ymd, jcd, rno):
        adv(10)
        calls["log"].append(f"result:{rno}")
        return None

    def f_t3(ymd, jcd, rno):
        adv(10)
        calls["log"].append(f"t3:{rno}")
        o = (fetched_t3 or {}).get(rno)
        if isinstance(o, Exception):
            raise o
        return dict(o) if o else None

    def f_none(*a):
        adv(10)
        return None

    def f_before(*a):
        adv(10)
        return ""

    def fake_notify(pred_, ymd_):
        calls["notify"] += 1
        calls["log"].append("notify")
        adv(notify_cost)
        return notify(pred_, ymd_) if notify else None

    saved = {k: getattr(U, k) for k in ("ROOT", "datetime", "fetch_odds", "fetch_result", "fetch_t3",
                                         "fetch_racename", "fetch_before_html", "time", "subprocess",
                                         "do_stamps", "MIDRUN_STAMP", "MIDRUN_PUBLISH", "TAIL_STAMP",
                                         "EARLY_NOTIFY", "ODDS_REFRESH_FROM")}
    saved_notify = sys.modules["notify"].notify_events
    old_env = os.environ.get("GITHUB_WORKFLOW")
    old_argv = sys.argv
    try:
        U.ROOT = tmp
        U.datetime = FakeDT
        U.fetch_odds = f_odds
        U.fetch_result = f_result
        U.fetch_t3 = f_t3
        U.fetch_racename = f_none
        U.fetch_before_html = f_before
        U.time = types.SimpleNamespace(sleep=lambda s: adv(s),
                                       monotonic=lambda: (clock["t"] - START).total_seconds())
        sys.modules["notify"].notify_events = fake_notify

        def fake_run(cmd, **kw):
            calls["publish"] += 1
            calls["publish_kw"] = kw
            calls["log"].append("publish")
            calls["tk_on_disk"].append(tk_on_disk())
            if publish_raises:
                raise RuntimeError("git push stuck")
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
        sys.modules["notify"].notify_events = saved_notify
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
assert calls["publish_kw"]["timeout"] == 30, "公開(push)を待つのは30秒まで。詰まっても通知をそれ以上遅らせない"
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

# 6) --results-only で厳選が確定(この実行の打刻で tk=1)したら、結果・展示の取得より前に
#    「書く → 公開 → 通知」。1R は結果待ち(fetch_result が走る)。5R は締切12:14 で、開始時点(12:00)に打刻される。
def waiting():
    r = race(1, "11:40", HIGH)
    r["tk"] = 0
    r["att"] = 0
    return r


R, out, calls = run([waiting(), race(5, "12:14", HIGH, live=True)], ["--results-only"],
                    env_workflow="auto-update", switches=ON)
log = calls["log"]
assert R[5].get("tk") == 1 and R[5].get("pt") == "12:00"
assert log[:2] == ["publish", "notify"] and "result:1" in log[2:], log
assert calls["tk_on_disk"] == [[5]], "公開の時点で、確定(tk=1)がファイルに書かれている"
assert log.count("notify") == 2 and log[-1] == "notify", "末尾の通知(やり直しと結果の通知)は残す"
print("6 ok: results-only publishes and notifies before fetching results:", log)

# 6b) auto-update の外(公開しない場面)でも、通知は取得より前
_, _, calls = run([waiting(), race(5, "12:14", HIGH, live=True)], ["--results-only"])
assert calls["publish"] == 0 and calls["log"][0] == "notify" and "result:1" in calls["log"][1:], calls["log"]
print("6b ok")

# 6c) 打刻があっても厳選が成立していない(tk=0)なら、前倒しの公開・通知はしない(末尾の1回だけ)
R, out, calls = run([waiting(), race(5, "12:14", LOW, live=True)], ["--results-only"],
                    env_workflow="auto-update", switches=ON)
assert R[5].get("tk") == 0
assert calls["publish"] == 0 and calls["log"].count("notify") == 1 and calls["log"][-1] == "notify", calls["log"]
print("6c ok: no early publish for tk=0 stamps")

# 6d) 前の周回で確定済みの厳選(tk=1)があるだけでは、前倒しはしない(この実行で新しく確定した時だけ)
old = race(4, "12:05", HIGH, live=True)
old["tk"] = 1
old["att"] = 1
R, out, calls = run([waiting(), old, race(5, "12:14", LOW, live=True)], ["--results-only"],
                    env_workflow="auto-update", switches=ON)
assert R[4]["tk"] == 1 and R[5].get("tk") == 0
assert calls["publish"] == 0 and calls["log"].count("notify") == 1, calls["log"]
print("6d ok: only newly confirmed races trigger the early notify")

# 6e) 止めた場合(EARLY_NOTIFY=False)は従来どおり: 通知は末尾の1回だけ
_, _, calls = run([waiting(), race(5, "12:14", HIGH, live=True)], ["--results-only"],
                  env_workflow="auto-update", switches={"MIDRUN_PUBLISH": True, "EARLY_NOTIFY": False})
assert calls["publish"] == 0 and calls["log"].count("notify") == 1 and calls["log"][-1] == "notify", calls["log"]
print("6e ok: kill switch")


# 7) 通知や公開が失敗しても、打刻・結果の取得・ファイルへの書き込みは続く
def boom(pred, ymd):
    raise RuntimeError("notify exploded")


R, out, calls = run([waiting(), race(5, "12:14", HIGH, live=True)], ["--results-only"],
                    env_workflow="auto-update", switches=ON, notify=boom, publish_raises=True)
assert R[5].get("tk") == 1 and "result:1" in calls["log"], calls["log"]
assert calls["log"][:2] == ["publish", "notify"], "公開が失敗しても通知は試す"
print("7 ok: results-only survives notify/publish failures")

# 7b) full: 合間の通知が例外を出しても、以後の合間打刻は続く(11R は 12:02 に合間で打刻される)
far = [race(n, "14:%02d" % n) for n in (20, 21, 22, 23)]        # 朝オッズの対象(取得のたびに合間が来る)
R, out, calls = run([race(5, "12:16", HIGH), race(11, "12:17", HIGH), race(6, "12:50"), race(7, "12:55")] + far, [],
                    fetched_odds={6: HIGH, 7: HIGH}, notify=boom)
assert calls["odds"] == [6, 7] and R[6]["odds"]["t3"] and R[7]["odds"]["t3"]
assert R[5].get("pt") == "12:01" and R[11].get("tk") == 1 and R[11].get("pt") == "12:02", (R[5].get("pt"), R[11].get("pt"))
assert [x for x in calls["log"] if x.startswith("t3:")] == ["t3:20", "t3:21", "t3:22", "t3:23"]
print("7b ok: a failing notify does not stop mid-run stamping")

# 8) full: 合間の通知が送れずに残ったら、次の合間で最大3回まで試し直す(打刻が無い合間でも)
#    通知1回に30秒(notify 側の上限)かかっても、オッズと朝オッズの取得は全部行われる。
R, out, calls = run([race(5, "12:16", HIGH), race(6, "12:50"), race(7, "12:55")] + far, [],
                    fetched_odds={6: HIGH, 7: HIGH}, notify=lambda p, y: True, notify_cost=30)
log = calls["log"]
assert calls["odds"] == [6, 7] and [x for x in log if x.startswith("t3:")] == ["t3:20", "t3:21", "t3:22", "t3:23"]
assert R[5].get("tk") == 1
# 打刻の合間で1回 + やり直し3回 + full の末尾で1回
assert calls["notify"] == 1 + U.MIDRUN_NOTIFY_RETRY + 1 == 5, log
assert log[log.index("notify"):][:5] == ["notify", "notify", "t3:20", "notify", "t3:21"], log
print("8 ok: mid-run notify is retried at the next ticks, at most 3 times:", log)

# 8b) 送れたら、やり直さない(打刻の合間で1回 + 末尾で1回)
_, _, calls = run([race(5, "12:16", HIGH), race(6, "12:50"), race(7, "12:55")] + far, [],
                  fetched_odds={6: HIGH, 7: HIGH}, notify=lambda p, y: False)
assert calls["notify"] == 2, calls["log"]
# やり直しの1回目で送れたら、そこで止める
answers = [True, False, False, False, False]
_, _, calls = run([race(5, "12:16", HIGH), race(6, "12:50"), race(7, "12:55")] + far, [],
                  fetched_odds={6: HIGH, 7: HIGH}, notify=lambda p, y: answers.pop(0))
assert calls["notify"] == 3, calls["log"]
print("8b ok: no retry after a successful send")

# 9) 厳選の判定の直前に、候補のオッズを取り直す(対象日以降だけ)
ON = {"ODDS_REFRESH_FROM": YMD}       # このテストの日付から有効にする
OFF = {"ODDS_REFRESH_FROM": "29991231"}
START = datetime(2026, 10, 1, 12, 0, 0, tzinfo=JST)
t3s = lambda log: [x for x in log if x.startswith("t3:")]

# 9a) 軽い周回: 保存済みは高いオッズ(古い板)、取り直した板は 2.0 倍 → 取り直した板で判定して見送り
R, out, calls = run([race(5, "12:14", HIGH)], ["--results-only"], fetched_t3={5: LOW}, switches=ON)
assert t3s(calls["log"]) == ["t3:5"], calls["log"]
assert R[5].get("tk") == 0 and "3.1倍未満 2.0倍" in (R[5].get("rs") or ""), (R[5].get("tk"), R[5].get("rs"))
assert R[5]["os"]["1-2-3"] == 2.0 and R[5]["odds"]["t3"]["1-2-3"] == 2.0 and R[5]["odds"].get("jt")
print("9a ok: the candidate is judged with odds refreshed right before the stamp")

# 9b) 取り直した板で条件を満たせば厳選。目安(pr)は付けない(幅が古い板の変動で作られているため)
R, out, calls = run([race(5, "12:14", LOW)], ["--results-only"], fetched_t3={5: HIGH}, switches=ON)
assert R[5].get("tk") == 1 and R[5]["os"]["1-2-3"] == 9.9 and "pr" not in R[5], R[5]
print("9b ok: selected with refreshed odds; no payout estimate")

# 9c) 対象日より前は取り直さない(従来どおり保存済みのオッズで判定し、目安も付く)
R, out, calls = run([race(5, "12:14", HIGH)], ["--results-only"], fetched_t3={5: LOW}, switches=OFF)
assert t3s(calls["log"]) == [] and R[5].get("tk") == 1 and R[5].get("pr") and "jt" not in R[5]["odds"]
print("9c ok: no refresh before the switch date")

# 9d) 取り直しに失敗(取れない・例外)しても打刻は行い、保存済みのオッズで判定する
for bad in (None, RuntimeError("timeout")):
    R, out, calls = run([race(5, "12:14", HIGH)], ["--results-only"], fetched_t3={5: bad}, switches=ON)
    assert R[5].get("tk") == 1 and "jt" not in R[5]["odds"] and R[5].get("pr"), (bad, R[5])
print("9d ok: a failed refresh falls back to the stored odds")

# 9e) 候補でないレース(確率条件を満たさない・4R以前)は取り直さない(要求を増やさない)
R, out, calls = run([race(5, "12:14", HIGH, top3p=0.30), race(4, "12:14", HIGH)], ["--results-only"],
                    fetched_t3={5: LOW, 4: LOW}, switches=ON)
assert t3s(calls["log"]) == [] and R[5].get("tk") == 0 and R[4].get("tk") == 0
print("9e ok: non-candidates are not refetched")

# 9f) 取り直した板に無い買い目(欠場など)は、古い板の値で通さない → オッズ未取得で見送り
part = dict(HIGH); del part["1-3-2"]
R, out, calls = run([race(5, "12:14", HIGH)], ["--results-only"], fetched_t3={5: part}, switches=ON)
assert R[5].get("tk") == 0 and R[5].get("rs") == "オッズ未取得", (R[5].get("tk"), R[5].get("rs"))
print("9f ok: a pick missing from the refreshed board is not passed with stale odds")

# 9g) 締切を過ぎてからの打刻では取り直さない(締切後の板は確定オッズ)
START = datetime(2026, 10, 1, 12, 20, 0, tzinfo=JST)
R, out, calls = run([race(5, "12:14", HIGH)], ["--results-only"], fetched_t3={5: LOW}, switches=ON)
assert t3s(calls["log"]) == [] and R[5].get("ph") == 1, (calls["log"], R[5])
START = datetime(2026, 10, 1, 12, 0, 0, tzinfo=JST)
print("9g ok: no refresh for a post-deadline stamp")

# 9h) full: この実行で本オッズを取ったばかりのレースは二重に取り直さない。取っていない候補は取り直す
#     5R: オッズ未取得で取得対象(取得した板は 2.0 倍)。8R: 保存済みは高い・取得対象外・締切12:16(T-15=12:01)
R, out, calls = run([race(5, "12:14"), race(8, "12:16", HIGH), race(6, "12:50")], [],
                    fetched_odds={5: LOW, 6: HIGH}, fetched_t3={8: LOW}, switches=ON)
assert calls["odds"] == [5, 6] and t3s(calls["log"]) == ["t3:8"], calls["log"]
assert R[5].get("tk") == 0 and "jt" not in R[5]["odds"], "取ったばかりの本オッズで判定する(取り直さない)"
assert R[8].get("tk") == 0 and R[8]["odds"].get("jt") and R[8]["os"]["1-2-3"] == 2.0
print("9h ok: full pass refreshes only candidates it did not just fetch")
print("ALL OK")

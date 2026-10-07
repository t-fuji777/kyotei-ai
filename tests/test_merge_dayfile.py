# -*- coding: utf-8 -*-
"""scripts/merge_dayfile.py(当日ファイルの JSON 単位の merge)と、auto-update.yml の commit_push に
入れた呼び出しの形を確かめる。通信なし。

確かめること:
  - 2026-10-06 の事故の形: theirs = daily の新しい予測(+ その時点で引き継いだ観測)、
    ours = 周回の古い予測 + その後の結果/オッズ/打刻/展示の反映 → merge は「daily の買い目 + 周回の観測」。
  - 打刻済み(tk)・結果付きのレースの買い目は ours(一式。g は ours の世代、absent は ours に無ければ消える)。
  - live の優先: ours だけ live → ours。両方 live → live_at の新しい方(同じ・無ければ theirs)。
  - 観測項目ごとの規則: 打刻系 first-wins(theirs)・result は order のある方・odds は final → 判定直前の取り直し
    (jt のある方・両方なら新しい方)→ 本オッズ > 暫定 → theirs・展示系は情報量(艇数)の多い方・
    ours にしか無い項目/レース/会場は足す・theirs だけのものはそのまま。
  - トップレベル: date/generated_at/model_trained_at/model_gen/sengen_cfg は theirs、
    *_updated_at と live_model_trained_at は新しい方(片方しか無ければある方)。
  - auto-update.yml: commit_push は docs/predictions に加えて、有る時だけ data/boards(3連単の板の記録)を add する。
  - 同じものどうしは恒等、merge の結果をもう1回 merge しても変わらない(push が断られた回の再 rebase で再び掛かる)。
  - CLI: 1行 JSON(ensure_ascii=False・改行なし)、--latest は date が同じ時だけ写す、壊れた入力は終了コード 2 で何も書かない。
  - 実在の当日ファイル(docs/predictions/20261006.json)から作った2版でも通る。
  - auto-update.yml: commit_push の中で BASE を rebase の前に取り、rebase の後・push の前に merge_day_files を呼ぶ。
    self_heal / publish_stamps.sh は対象外のまま。

使い方: リポジトリの根で  PYTHONUTF8=1 python -X utf8 tests/test_merge_dayfile.py
一時フォルダは .tmp_merge_dayfile_<pid>/(.gitignore の .tmp_*/)。終わったら消す。"""
import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import merge_dayfile as M  # noqa: E402

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

TMP = ROOT / f".tmp_merge_dayfile_{os.getpid()}"
PY = [sys.executable, "-X", "utf8"]
ENV = dict(os.environ, PYTHONUTF8="1")
YMD = "20261006"
COMBOS = ["1-2-3", "1-3-2", "1-2-4", "1-4-2", "2-1-3", "2-3-1"]


# ---------------------------------------------------------------- 合成データ
def picks(tag):
    """買い目。tag ごとに順番と確率を変え、どちらの版の買い目かを見分けられるようにする。"""
    cs = COMBOS if tag == "new" else COMBOS[::-1]
    return [{"c": c, "p": round(0.12 - 0.01 * i + (0.001 if tag == "new" else 0), 4)} for i, c in enumerate(cs)]


def race(no, deadline, tag, **extra):
    r = {"no": no, "type": "一般戦", "deadline": deadline, "day_n": 2,
         "boats": [{"lane": i, "name": f"{tag}選手{i}", "cls": "B1", "wp": round(0.4 - 0.05 * i, 3)} for i in range(1, 7)],
         "picks": picks(tag), "conf": "B" if tag == "new" else "C",
         "fuku": {"lane": 1, "p": 0.6 if tag == "new" else 0.5}, "sengen": tag == "new"}
    r.update(extra)
    return r


def dayfile(venues, **top):
    d = {"date": YMD, "generated_at": "2026-10-06 08:05 JST", "model_trained_at": "2026-10-06 08:04 JST",
         "model_gen": 2, "sengen_cfg": {"top3p_min": 0.46, "min_odds": 3.1, "min_rno": 5, "exclude_venues": [3, 4, 14]},
         "venues": venues}
    d.update(top)
    return d


def venue(code, name, races, **extra):
    v = {"code": code, "name": name, "day_n": 2, "is_final": False, "races": races}
    v.update(extra)
    return v


def idx(d):
    return {(v["code"], r["no"]): r for v in d["venues"] for r in v["races"]}


PROV = {"t3": {c: 0.0 for c in COMBOS}, "prov": True}
REAL = {"fuku": "1.0-1.2", "t3": {c: 5.0 + i for i, c in enumerate(COMBOS)}, "axis": {}, "axis_combo": {}}
FINAL = dict(REAL, final=True)
RESULT = {"order": "1-2-3", "kimarite": "逃げ", "ninki": 1, "pay3t": 560}
ST6 = {str(i): ".1%d" % i for i in range(1, 7)}
EX6 = {str(i): 6.7 + i / 100 for i in range(1, 7)}
WEATHER = {"temp": 24.0, "sky": "晴", "wspd": 2, "wdir": 8, "wtemp": 22.0, "wave": 1}


# ---------------------------------------------------------------- 1) 2026-10-06 の事故の形
def test_incident_shape():
    """daily(08:05)が push した新しい予測を、周回(08:06)の古い予測 + 観測で丸ごと上書きした形。
    merge は daily の買い目を土台に周回の観測を重ね、打刻済み・結果付き・live の買い目だけ周回を残す。"""
    # theirs = daily: 新しいモデル(本日学習)。_merge_existing で引き継いだ暫定オッズが2レース分
    theirs = dayfile([
        venue(2, "戸田", [race(1, "10:47", "new", odds=copy.deepcopy(PROV)), race(2, "11:15", "new", odds=copy.deepcopy(PROV)),
                        race(3, "11:43", "new"), race(4, "12:11", "new", g=2)]),
        venue(5, "多摩川", [race(1, "10:50", "new"), race(2, "11:20", "new"), race(3, "11:50", "new")]),
    ], odds_updated_at="2026-10-06 07:46 JST")
    # ours = 周回: 前日モデルの買い目。その後の観測(本オッズ・展示・live 再予測・打刻・結果)を持つ
    ours = dayfile([
        venue(2, "戸田", [
            race(1, "10:47", "old", odds=copy.deepcopy(REAL), st_ex=ST6, ex=EX6, weather=WEATHER, wind=2, wave=1,
                 live=True, live_at="10:30", tk=0, pt="10:32", os={"1-2-3": 12.3}, mt=0, att=0),
            race(2, "11:15", "old", odds=dict(PROV, t3={c: 1.0 for c in COMBOS}), tk=1, pt="11:00", os={"1-2-3": 4.4}, mt=0, att=1),
            race(3, "11:43", "old", odds=copy.deepcopy(FINAL), result=copy.deepcopy(RESULT)),
            race(4, "12:11", "old"),
        ]),
        venue(5, "多摩川", [race(1, "10:50", "old", odds=copy.deepcopy(PROV)), race(2, "11:20", "old"),
                          race(4, "12:20", "old")]),
        venue(7, "蒲郡", [race(1, "15:00", "old")]),
    ], generated_at="2026-10-06 07:21 JST", model_trained_at="2026-10-05 04:55 JST", model_gen=1,
        sengen_cfg={"top3p_min": 0.36, "min_odds": 3.1, "exclude_venues": [3, 4, 14]},
        odds_updated_at="2026-10-06 08:01 JST", results_updated_at="2026-10-06 08:06:32 JST")
    o0, t0 = copy.deepcopy(ours), copy.deepcopy(theirs)

    m = M.merge_dayfile(ours, theirs)
    assert ours == o0 and theirs == t0, "入力を書き換えている"

    # トップレベル: 土台は theirs。*_updated_at は新しい方(片方しか無ければある方)
    assert m["model_trained_at"] == "2026-10-06 08:04 JST", m["model_trained_at"]
    assert m["generated_at"] == theirs["generated_at"] and m["model_gen"] == 2 and m["sengen_cfg"] == theirs["sengen_cfg"]
    assert m["odds_updated_at"] == "2026-10-06 08:01 JST" and m["results_updated_at"] == "2026-10-06 08:06:32 JST"
    assert m["date"] == YMD

    mi, ti, oi = idx(m), idx(theirs), idx(ours)
    # (2,1) 打刻済み + live: 買い目は ours。観測はすべて ours(theirs には暫定オッズしか無い)
    r = mi[(2, 1)]
    assert r["picks"] == oi[(2, 1)]["picks"] and r["boats"] == oi[(2, 1)]["boats"] and r["conf"] == "C"
    assert r["live"] is True and r["live_at"] == "10:30" and r["g"] == 1, "印の無い買い目は世代1"
    assert r["odds"] == REAL, "本オッズ(prov 無し)が暫定(prov)に勝つ"
    assert r["st_ex"] == ST6 and r["ex"] == EX6 and r["weather"] == WEATHER and r["wind"] == 2 and r["wave"] == 1
    assert (r["tk"], r["pt"], r["os"], r["mt"], r["att"]) == (0, "10:32", {"1-2-3": 12.3}, 0, 0)
    # (2,2) 打刻済み(tk=1)・live 無し: 買い目は ours。両方暫定のオッズは theirs
    r = mi[(2, 2)]
    assert r["picks"] == oi[(2, 2)]["picks"] and r["tk"] == 1 and r["att"] == 1 and "live" not in r
    assert r["odds"] == PROV, "両方とも暫定なら theirs"
    # (2,3) 結果付き(tk 無し): 買い目は ours。結果と確定オッズは ours
    r = mi[(2, 3)]
    assert r["picks"] == oi[(2, 3)]["picks"] and r["result"] == RESULT and r["odds"] == FINAL
    # (2,4) 何も無い: 買い目は theirs(daily の新しい予測)。theirs の g=2 もそのまま
    r = mi[(2, 4)]
    assert r["picks"] == ti[(2, 4)]["picks"] and r["boats"] == ti[(2, 4)]["boats"] and r["conf"] == "B" and r["g"] == 2
    # (5,1) ours に暫定オッズだけ: 買い目は theirs、オッズは ours(theirs に無い)
    r = mi[(5, 1)]
    assert r["picks"] == ti[(5, 1)]["picks"] and r["odds"] == PROV
    assert mi[(5, 2)]["picks"] == ti[(5, 2)]["picks"]
    # theirs にしか無いレース(5,3)はそのまま。ours にしか無いレース(5,4)・会場(7)は足される。並びは番号順
    assert mi[(5, 3)] == ti[(5, 3)]
    assert mi[(5, 4)] == oi[(5, 4)]
    assert [r["no"] for r in m["venues"][1]["races"]] == [1, 2, 3, 4]
    assert [v["code"] for v in m["venues"]] == [2, 5, 7] and m["venues"][2] == ours["venues"][2]
    # 会場の属性は theirs
    assert m["venues"][0]["name"] == "戸田" and m["venues"][0]["day_n"] == 2
    # 打刻・結果・live の無い 4 レース(2-4, 5-1, 5-2 と theirs 側だけの 5-3)が daily の買い目。
    # 事故ではこれが周回の古い買い目で上書きされていた
    n_daily = sum(1 for k in ti if k in mi and mi[k]["picks"] == ti[k]["picks"] and ti[k]["picks"] != oi.get(k, {}).get("picks"))
    assert n_daily == 4, n_daily
    print("  (1) 2026-10-06 の事故の形: daily の買い目 + 周回の観測 OK")


# ---------------------------------------------------------------- 2) 打刻済み・結果付きの買い目
def test_stamped_keeps_ours_picks():
    # 両方に打刻がある(daily が引き継いだ): 打刻系は first-wins で theirs、買い目は ours の一式
    t = race(5, "12:00", "new", tk=1, pt="11:44", os={"1-2-3": 3.3}, mt=0, att=0, absent=[3], g=2, live=True, live_at="11:40")
    o = race(5, "12:00", "old", tk=1, pt="11:45", os={"1-2-3": 3.4}, mt=0, att=1, g=2)
    m = M.merge_dayfile(dayfile([venue(1, "桐生", [o])]), dayfile([venue(1, "桐生", [t])]))
    r = idx(m)[(1, 5)]
    assert r["picks"] == o["picks"] and r["boats"] == o["boats"] and r["sengen"] is False
    assert (r["tk"], r["pt"], r["os"], r["att"]) == (1, "11:44", {"1-2-3": 3.3}, 0), "打刻系は先に push された側(theirs)"
    assert "absent" not in r and "live" not in r and "live_at" not in r, "買い目一式は ours に無い印を消す"
    assert r["g"] == 2
    # ours に世代の印が無ければ世代1(印を付け始める前の買い目)
    o2 = race(5, "12:00", "old", tk=0)
    r = idx(M.merge_dayfile(dayfile([venue(1, "桐生", [o2])]), dayfile([venue(1, "桐生", [t])])))[(1, 5)]
    assert r["picks"] == o2["picks"] and r["g"] == 1
    # 結果だけ(tk 無し)でも凍結
    o3 = race(5, "12:00", "old", result=copy.deepcopy(RESULT))
    t3 = race(5, "12:00", "new")
    r = idx(M.merge_dayfile(dayfile([venue(1, "桐生", [o3])]), dayfile([venue(1, "桐生", [t3])])))[(1, 5)]
    assert r["picks"] == o3["picks"] and r["result"] == RESULT
    # 取得済みのレース名(rn_full)は ours から引き継ぐ(_merge_existing と同じ)
    o4 = race(6, "12:30", "old", type="第12回 ○○杯 予選", rn_full=True)
    t4 = race(6, "12:30", "new", type="予選")
    r = idx(M.merge_dayfile(dayfile([venue(1, "桐生", [o4])]), dayfile([venue(1, "桐生", [t4])])))[(1, 6)]
    assert r["type"] == "第12回 ○○杯 予選" and r["rn_full"] is True and r["picks"] == t4["picks"]
    print("  (2) 打刻済み・結果付きの買い目は ours の一式、打刻系は theirs OK")


# ---------------------------------------------------------------- 3) live の優先
def test_live_rules():
    def merged(o, t):
        return idx(M.merge_dayfile(dayfile([venue(1, "桐生", [o])]), dayfile([venue(1, "桐生", [t])])))[(1, 7)]

    # ours だけ live(朝の予測のまま theirs) → ours
    o = race(7, "13:00", "old", live=True, live_at="12:40", st_ex=ST6, g=1)
    t = race(7, "13:00", "new", g=2)
    r = merged(o, t)
    assert r["picks"] == o["picks"] and r["live"] is True and r["live_at"] == "12:40" and r["g"] == 1
    # 両方 live: ours が新しい → ours
    t2 = race(7, "13:00", "new", live=True, live_at="12:38")
    r = merged(o, t2)
    assert r["picks"] == o["picks"] and r["live_at"] == "12:40"
    # 両方 live: theirs が新しい → theirs
    t3 = race(7, "13:00", "new", live=True, live_at="12:41")
    r = merged(o, t3)
    assert r["picks"] == t3["picks"] and r["live_at"] == "12:41"
    # 同じ live_at → theirs。ours に live_at が無ければ theirs
    t4 = race(7, "13:00", "new", live=True, live_at="12:40")
    assert merged(o, t4)["picks"] == t4["picks"]
    o5 = race(7, "13:00", "old", live=True)
    assert merged(o5, t4)["picks"] == t4["picks"]
    # theirs だけ live(ours は何も無い) → theirs のまま
    o6 = race(7, "13:00", "old")
    r = merged(o6, t4)
    assert r["picks"] == t4["picks"] and r["live"] is True
    print("  (3) live の優先 OK")


# ---------------------------------------------------------------- 4) 観測項目ごとの規則
def test_observed_rules():
    def merged(o_extra, t_extra):
        # update で重ねる(deadline など race() の引数と同じ名前の項目も差し替えられるように)
        o = race(8, "14:00", "old")
        o.update(o_extra)
        t = race(8, "14:00", "new")
        t.update(t_extra)
        return idx(M.merge_dayfile(dayfile([venue(1, "桐生", [o])]), dayfile([venue(1, "桐生", [t])])))[(1, 8)]

    # result: order のある方。両方あれば theirs
    assert merged({"result": RESULT}, {"result": {"order": None, "note": "未確定"}})["result"] == RESULT
    r2 = dict(RESULT, order="2-1-3")
    assert merged({"result": RESULT}, {"result": r2})["result"] == r2
    assert merged({"result": {"order": ""}}, {"result": RESULT})["result"] == RESULT
    # odds: final が立っている方(両方なら theirs)。どちらも final でなければ 本オッズ > 暫定、それ以外 theirs
    t_final = dict(FINAL, fuku="2.0-2.5")
    assert merged({"odds": REAL}, {"odds": t_final})["odds"] == t_final
    assert merged({"odds": FINAL}, {"odds": REAL})["odds"] == FINAL
    assert merged({"odds": FINAL}, {"odds": t_final})["odds"] == t_final
    assert merged({"odds": REAL}, {"odds": PROV})["odds"] == REAL
    assert merged({"odds": PROV}, {"odds": REAL})["odds"] == REAL
    t_real2 = dict(REAL, fuku="3.0-3.5")
    assert merged({"odds": REAL}, {"odds": t_real2})["odds"] == t_real2
    assert merged({"odds": PROV}, {"odds": dict(PROV, t3={c: 9.0 for c in COMBOS})})["odds"]["t3"]["1-2-3"] == 9.0
    # odds: 判定直前に取り直した板(jt)のある方。両方なら jt の新しい方(同じなら theirs)。final には負ける
    o_judge = dict(REAL, t3={c: 2.0 for c in COMBOS}, jt="10:31:02")
    assert merged({"odds": o_judge}, {"odds": REAL})["odds"] == o_judge, "ours だけ jt → ours(古い本オッズで戻さない)"
    assert merged({"odds": o_judge}, {"odds": PROV})["odds"] == o_judge
    assert merged({"odds": REAL}, {"odds": o_judge})["odds"] == o_judge, "theirs だけ jt → theirs"
    assert merged({"odds": dict(PROV, jt="10:31:02")}, {"odds": REAL})["odds"]["jt"] == "10:31:02", "jt は本オッズ>暫定より先"
    t_judge = dict(o_judge, jt="10:40:00", fuku="2.0-2.5")
    assert merged({"odds": o_judge}, {"odds": t_judge})["odds"] == t_judge, "両方 jt → 新しい方(theirs)"
    assert merged({"odds": t_judge}, {"odds": o_judge})["odds"] == t_judge, "両方 jt → 新しい方(ours)"
    assert merged({"odds": dict(o_judge, fuku="9.9")}, {"odds": o_judge})["odds"] == o_judge, "jt が同じなら theirs"
    assert merged({"odds": o_judge}, {"odds": t_final})["odds"] == t_final, "final が jt に勝つ"
    assert merged({"odds": dict(o_judge, final=True)}, {"odds": t_judge})["odds"]["final"] is True
    # 展示系: 艇数(要素数)の多い方。同じなら theirs。風・波はスカラー(有無で比べ、両方あれば theirs)
    st5 = {k: v for k, v in ST6.items() if k != "6"}
    assert merged({"st_ex": ST6}, {"st_ex": st5})["st_ex"] == ST6
    assert merged({"st_ex": st5}, {"st_ex": ST6})["st_ex"] == ST6
    st6b = dict(ST6, **{"1": ".05"})
    assert merged({"st_ex": ST6}, {"st_ex": st6b})["st_ex"] == st6b
    ex5 = {k: v for k, v in EX6.items() if k != "3"}
    assert merged({"ex": EX6}, {"ex": ex5})["ex"] == EX6
    w_small = {"sky": "曇"}
    assert merged({"weather": WEATHER}, {"weather": w_small})["weather"] == WEATHER
    assert merged({"weather": w_small}, {"weather": WEATHER})["weather"] == WEATHER
    assert merged({"wind": 3}, {"wind": None})["wind"] == 3
    assert merged({"wind": 3}, {"wind": 4})["wind"] == 4
    assert merged({"wave": 2}, {})["wave"] == 2
    # 打刻系: 両方あれば theirs。theirs に無ければ ours
    r = merged({"tk": 1, "pt": "13:45", "qc": "a", "qp": 1, "ph": 2, "pr": 3, "rs": "x"},
               {"tk": 0, "pt": "13:44"})
    assert (r["tk"], r["pt"]) == (0, "13:44") and (r["qc"], r["qp"], r["ph"], r["pr"], r["rs"]) == ("a", 1, 2, 3, "x")
    # 観測でも買い目でもない項目(deadline/type/day_n)は theirs
    r = merged({"deadline": "14:05", "day_n": 9}, {})
    assert r["deadline"] == "14:00" and r["day_n"] == 2
    print("  (4) 観測項目ごとの規則 OK")


# ---------------------------------------------------------------- 5) トップレベルと恒等・再 merge
def test_top_level_and_idempotent():
    o = dayfile([venue(1, "桐生", [race(1, "10:00", "old", tk=0)])],
                generated_at="2026-10-06 07:21 JST", model_trained_at="2026-10-05 04:55 JST", model_gen=1,
                results_updated_at="2026-10-06 09:00:00 JST", odds_updated_at="2026-10-06 09:01 JST",
                live_updated_at="2026-10-06 08:50 JST", live_model_trained_at="2026-10-05 04:55 JST", note="ours の注記")
    t = dayfile([venue(1, "桐生", [race(1, "10:00", "new")])],
                results_updated_at="2026-10-06 09:02:00 JST", odds_updated_at="2026-10-06 08:59 JST",
                live_model_trained_at="2026-10-06 08:04 JST")
    m = M.merge_dayfile(o, t)
    assert m["generated_at"] == "2026-10-06 08:05 JST" and m["model_trained_at"] == "2026-10-06 08:04 JST" and m["model_gen"] == 2
    assert m["results_updated_at"] == "2026-10-06 09:02:00 JST", "新しい方(theirs)"
    assert m["odds_updated_at"] == "2026-10-06 09:01 JST", "新しい方(ours)"
    assert m["live_updated_at"] == "2026-10-06 08:50 JST", "theirs に無ければ ours"
    assert m["live_model_trained_at"] == "2026-10-06 08:04 JST", "新しい方(theirs)"
    assert "note" not in m, "土台は theirs(列挙した項目以外の ours だけのトップレベル項目は写さない)"
    # live_model_trained_at は新しい方: 周回が本日学習のモデルで live 再予測した後に daily の版(前日の値を
    # _merge_existing で引き継いだ)と merge しても前日の値に戻らない(戻ると healthcheck の W5 が誤って立つ)。
    # theirs に無ければ ours
    o_live = dict(o, live_model_trained_at="2026-10-06 08:04 JST")
    t_old = dict(t, live_model_trained_at="2026-10-05 04:55 JST")
    m2 = M.merge_dayfile(o_live, t_old)
    assert m2["live_model_trained_at"] == "2026-10-06 08:04 JST", "新しい方(ours)"
    assert not (m2["live_model_trained_at"][:10] < m2["model_trained_at"][:10]), "W5(live のモデルが朝より古い)が立つ"
    t_none = {k: v for k, v in t.items() if k != "live_model_trained_at"}
    assert M.merge_dayfile(o, t_none)["live_model_trained_at"] == "2026-10-05 04:55 JST", "theirs に無ければ ours"
    assert "live_model_trained_at" not in M.merge_dayfile({k: v for k, v in o.items() if k != "live_model_trained_at"}, t_none)
    # 恒等: 同じものどうし
    assert M.merge_dayfile(t, t) == t
    # 再 merge しても変わらない(push が断られて次の attempt でもう1回 rebase + merge が掛かる)
    assert M.merge_dayfile(m, t) == m
    # 壊れた入力
    for bad_o, bad_t in ((dict(o, date="20261007"), t), (o, dict(t, date=None)), (dict(o, date=""), dict(t, date="")),
                         ([], t), (o, "x")):
        try:
            M.merge_dayfile(bad_o, bad_t)
        except ValueError:
            pass
        else:
            raise AssertionError("壊れた入力で ValueError にならない: %r / %r" % (type(bad_o), type(bad_t)))
    print("  (5) トップレベル・恒等・再 merge・壊れた入力 OK")


# ---------------------------------------------------------------- 6) CLI
def run_cli(*args):
    return subprocess.run(PY + [str(ROOT / "scripts" / "merge_dayfile.py"), *map(str, args)],
                          cwd=str(ROOT), env=ENV, capture_output=True, text=True, encoding="utf-8")


def test_cli():
    TMP.mkdir(exist_ok=True)
    o = dayfile([venue(1, "桐生", [race(1, "10:00", "old", tk=0), race(2, "10:30", "old")])],
                generated_at="2026-10-06 07:21 JST", model_trained_at="2026-10-05 04:55 JST")
    t = dayfile([venue(1, "桐生", [race(1, "10:00", "new"), race(2, "10:30", "new")])])
    fo, ft, fl = TMP / f"{YMD}.json", TMP / "theirs.json", TMP / "latest.json"
    fo.write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
    ft.write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
    fl.write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
    cp = run_cli(fo, ft, fo, "--latest", fl)
    assert cp.returncode == 0, (cp.returncode, cp.stdout, cp.stderr)
    raw = fo.read_bytes()
    assert b"\n" not in raw and raw.decode("utf-8") == json.dumps(M.merge_dayfile(o, t), ensure_ascii=False), "1行 JSON・改行なし・ensure_ascii=False"
    assert "桐生".encode("utf-8") in raw and b"\\u" not in raw
    assert fl.read_bytes() == raw, "latest.json(date が同じ)にも写す"
    m = json.loads(raw.decode("utf-8"))
    assert m["model_trained_at"] == "2026-10-06 08:04 JST"
    assert idx(m)[(1, 1)]["picks"] == picks("old") and idx(m)[(1, 2)]["picks"] == picks("new")
    # --latest の date が違えば触らない。--latest が無いファイルなら無視
    fl.write_text(json.dumps(dict(o, date="20261005"), ensure_ascii=False), encoding="utf-8")
    before = fl.read_bytes()
    cp = run_cli(fo, ft, fo, "--latest", fl)
    assert cp.returncode == 0 and fl.read_bytes() == before
    cp = run_cli(fo, ft, fo, "--latest", TMP / "nothing.json")
    assert cp.returncode == 0 and not (TMP / "nothing.json").exists()
    # 書き先を別にできる
    fout = TMP / "out.json"
    cp = run_cli(fo, ft, fout)
    assert cp.returncode == 0 and fout.read_bytes() == raw
    # 壊れた入力は終了コード 2、書き先は触らない
    fo.write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
    before = fo.read_bytes()
    (TMP / "broken.json").write_text("{not json", encoding="utf-8")
    cp = run_cli(fo, TMP / "broken.json", fo)
    assert cp.returncode == 2 and fo.read_bytes() == before, (cp.returncode, cp.stderr)
    cp = run_cli(TMP / "broken.json", ft, fo)
    assert cp.returncode == 2 and fo.read_bytes() == before
    (TMP / "other.json").write_text(json.dumps(dict(t, date="20261007"), ensure_ascii=False), encoding="utf-8")
    cp = run_cli(fo, TMP / "other.json", fo, "--latest", fl)
    assert cp.returncode == 2 and fo.read_bytes() == before and "date" in cp.stderr
    cp = run_cli(fo, TMP / "missing.json", fo)
    assert cp.returncode == 2 and fo.read_bytes() == before
    (TMP / "list.json").write_text("[1, 2]", encoding="utf-8")
    cp = run_cli(fo, TMP / "list.json", fo)
    assert cp.returncode == 2 and fo.read_bytes() == before
    assert not list(TMP.glob("*.tmp*")), "一時ファイルが残っている"
    print("  (6) CLI(1行 JSON・--latest・終了コード 2)OK")


# ---------------------------------------------------------------- 7) 実在の当日ファイルから作った2版
def test_real_file():
    """docs/predictions/20261006.json(1日分の結果・オッズ・打刻・展示がそろった版)から、
    theirs = daily の予測(買い目はこのファイルのもの、観測は 11:30 までのレースの暫定オッズだけ = 引き継いだ分)、
    ours = 周回(買い目は前日モデル = 並びを1つずらしたもの、観測は 11:00 までのレースの全部 + 12:00 までの暫定オッズ)
    を作って merge する。期待: 11:00 まで = ours の買い目と観測、11:00〜11:30 = daily の買い目 + 両方暫定なので theirs の
    オッズ、11:30〜12:00 = daily の買い目 + ours だけのオッズ、12:00 以降 = daily の買い目だけ。"""
    fp = ROOT / "docs" / "predictions" / f"{YMD}.json"
    if not fp.exists():
        print(f"  (7) {fp.name} が無いので省く")
        return
    R = json.loads(fp.read_text(encoding="utf-8"))
    obs_keys = set(M._OBSERVED_FIELDS)
    pick_keys = set(M._PICK_FIELDS)

    def prov_of(r):
        t3 = (r.get("odds") or {}).get("t3") or {}
        return {"t3": {c: 0.0 for c in list(t3)[:6]}, "prov": True}

    theirs = {k: copy.deepcopy(v) for k, v in R.items() if k not in ("venues", "results_updated_at", "live_updated_at", "live_model_trained_at")}
    theirs["odds_updated_at"] = "2026-10-06 10:20 JST"
    theirs["venues"] = []
    ours = {k: copy.deepcopy(v) for k, v in R.items() if k != "venues"}
    ours["generated_at"] = "2026-10-06 07:21 JST"
    ours["model_trained_at"] = "2026-10-05 04:55 JST"
    ours["odds_updated_at"] = "2026-10-06 11:50 JST"
    ours["venues"] = []
    n_old = n_live = n_both_prov = 0
    for v in R["venues"]:
        tv = {k: copy.deepcopy(x) for k, x in v.items() if k != "races"}
        ov = copy.deepcopy(tv)
        tv["races"], ov["races"] = [], []
        for r in v["races"]:
            dl = r["deadline"]
            tr = {k: copy.deepcopy(x) for k, x in r.items() if k not in obs_keys and k not in ("live", "live_at", "absent", "rn_full")}
            if dl <= "11:30" and r.get("odds"):
                tr["odds"] = prov_of(r)
            tv["races"].append(tr)
            orc = copy.deepcopy(r)
            # 前日モデルの買い目: 並びを1つずらす(確率は同じ)。選手名に印を付けて見分ける
            orc["picks"] = r["picks"][1:] + r["picks"][:1]
            orc["boats"] = [dict(b, name=b["name"] + "(旧)") for b in r["boats"]]
            orc.pop("g", None)
            if dl <= "11:00":
                n_old += 1
                if r.get("live"):
                    n_live += 1
            else:
                for k in list(orc):
                    if k in obs_keys or k in ("live", "live_at", "absent"):
                        orc.pop(k)
                if dl <= "12:00" and r.get("odds"):
                    orc["odds"] = prov_of(r)
                    if dl <= "11:30":
                        n_both_prov += 1
            ov["races"].append(orc)
        theirs["venues"].append(tv)
        ours["venues"].append(ov)
    assert n_old and n_live and n_both_prov, (n_old, n_live, n_both_prov)

    m = M.merge_dayfile(ours, theirs)
    assert m["model_trained_at"] == R["model_trained_at"] and m["generated_at"] == R["generated_at"]
    assert m["results_updated_at"] == R["results_updated_at"] and m["live_updated_at"] == R["live_updated_at"]
    assert m["odds_updated_at"] == "2026-10-06 11:50 JST"
    assert [v["code"] for v in m["venues"]] == [v["code"] for v in R["venues"]]
    ri, oi, ti, mi = idx(R), idx(ours), idx(theirs), idx(m)
    assert set(mi) == set(ri)
    n_theirs_picks = 0
    for k, r in mi.items():
        dl = r["deadline"]
        o, t = oi[k], ti[k]
        assert set(r) >= set(t), k
        if dl <= "11:00":
            # 周回の観測(結果・オッズ・展示・打刻・live)は全部 ours。買い目も ours(打刻済み・結果付き)
            for f in obs_keys | {"live", "live_at", "absent"}:
                assert r.get(f) == o.get(f), (k, f)
            assert r["picks"] == o["picks"] and r["boats"][0]["name"].endswith("(旧)")
            assert r["g"] == M.race_gen(o) == 1
            assert r["rn_full"] == o["rn_full"] and r["type"] == o["type"]
        else:
            n_theirs_picks += 1
            for f in pick_keys:
                assert r.get(f) == t.get(f), (k, f)
            assert r["picks"] == ri[k]["picks"] and not r["boats"][0]["name"].endswith("(旧)")
            if dl <= "11:30":
                assert r.get("odds") == t.get("odds"), k
            elif dl <= "12:00":
                assert r.get("odds") == o.get("odds"), k
            else:
                assert "odds" not in r
            for f in ("result", "tk", "st_ex", "ex", "live"):
                assert f not in r, (k, f)
    assert n_theirs_picks == len(mi) - n_old
    # 1行 JSON に書いて読み戻せる。再 merge で変わらない
    txt = M.dumps(m)
    assert "\n" not in txt and json.loads(txt) == m
    assert M.merge_dayfile(m, theirs) == m and M.merge_dayfile(theirs, theirs) == theirs
    print(f"  (7) 実在の当日ファイルの2版: {len(mi)} レース(ours の買い目 {n_old}・うち live {n_live}、daily の買い目 {n_theirs_picks})OK")


# ---------------------------------------------------------------- 8) auto-update.yml の commit_push
def _func_body(run, name):
    """bash の関数 name() { ... } の中身(入れ子の { } を数えて終わりを見つける)。"""
    i = run.index(name + "() {")
    depth, j = 0, i
    while j < len(run):
        if run[j] == "{":
            depth += 1
        elif run[j] == "}":
            depth -= 1
            if depth == 0:
                return run[i:j + 1]
        j += 1
    raise AssertionError(f"{name} の終わりが見つからない")


def test_workflow():
    path = ROOT / ".github" / "workflows" / "auto-update.yml"
    text = path.read_text(encoding="utf-8")
    if yaml is None:
        run = text
        print("  (PyYAML 無し: auto-update.yml は文字列の確認だけ)")
    else:
        doc = yaml.safe_load(text)
        run = doc["jobs"]["loop"]["steps"][-1]["run"]
    cp = _func_body(run, "commit_push")
    mf = _func_body(run, "merge_day_files")
    sh = _func_body(run, "self_heal")
    # commit_push: BASE を rebase の前に取り、rebase の後・push の前に merge_day_files を呼ぶ(失敗しても止めない)
    i_fetch = cp.index("git fetch origin main || true")
    i_base = cp.index('BASE=$(git merge-base HEAD origin/main 2>/dev/null || true)')
    i_rebase = cp.index("git rebase -X theirs origin/main ||")
    i_merge = cp.index('merge_day_files "$BASE" || true')
    i_push = cp.index("git push origin HEAD:main")
    assert i_fetch < i_base < i_rebase < i_merge < i_push, "commit_push: fetch → BASE → rebase → merge → push の順でない"
    assert cp.count("merge_day_files") == 1 and "git reset --hard origin/main || true; return;" in cp, "既存の rebase 失敗時の同期が変わっている"
    # commit_push: 3連単の板の記録(data/boards。scripts/boards.py)も送る。フォルダは最初の板を書いた時にできるので
    # 有る時だけ add する(`git add docs/predictions data/boards` と1行にすると、無い日は fatal: pathspec で
    # bash -e の周回ごと止まる)。add は差分の判定(git diff --cached)より前
    i_add = cp.index("git add docs/predictions")
    i_boards = cp.index("if [ -d data/boards ]; then git add data/boards; fi")
    i_cached = cp.index("git diff --cached --quiet")
    assert i_add < i_boards < i_cached, "commit_push: data/boards の add(有る時だけ)が docs/predictions の次・差分の判定の前にない"
    assert "git add docs/predictions data/boards" not in run, "data/boards を無条件に add している(無い日に fatal)"
    assert "data/boards" not in mf and "data/boards" not in sh, "data/boards の add は commit_push だけ"
    # merge_day_files: 上流が BASE 以降に変えた当日ファイルだけ・手元が触っていないものは飛ばす・latest.json に写す・commit
    assert '[ -n "$1" ] || return 0' in mf, "BASE が空なら何もしない"
    assert 'git diff --name-only "$1" origin/main -- docs/predictions' in mf
    assert "grep -E '^docs/predictions/[0-9]{8}\\.json$'" in mf, "YYYYMMDD.json だけを対象にする"
    assert 'git diff --quiet origin/main HEAD -- "$f"' in mf, "手元が触っていないファイルは飛ばす"
    assert 'git show "origin/main:$f" > "$tmp"' in mf
    assert 'python scripts/merge_dayfile.py "$f" "$tmp" "$f" --latest docs/predictions/latest.json' in mf
    assert mf.index('git show "origin/main:$f"') < mf.index("merge_dayfile.py") < mf.index('rm -f "$tmp"')
    assert "git add docs/predictions" in mf and 'git commit -q -m "merge day file"' in mf
    assert mf.index("merge_dayfile.py") < mf.index('git commit -q -m "merge day file"')
    # 対象は commit_push だけ
    assert "merge_dayfile" not in sh and "merge_day_files" not in sh, "self_heal は対象外のはず"
    assert "merge_dayfile" not in (ROOT / "scripts" / "publish_stamps.sh").read_text(encoding="utf-8")
    assert run.count("python scripts/merge_dayfile.py") == 1, "merge_dayfile.py を呼ぶ場所は1つ(merge_day_files の中)"
    # 関数の定義はループ本体(while true)より前
    assert run.index("merge_day_files() {") < run.index("while true; do")
    # 既存の作りは変えていない
    for keep in ("python scripts/model_store.py fetch || true", "python scripts/build_calib.py || true",
                 "git add data/races data/fan docs", 'commit_push "auto results"', 'commit_push "auto update"',
                 "git pull --rebase --autostash -X theirs || true"):
        assert keep in run, f"既存の手順が消えている: {keep}"
    print("  (8) auto-update.yml の commit_push に merge の手順 OK")


def _onerror(func, path, exc_info):
    try:
        os.chmod(path, 0o666)
        func(path)
    except Exception:
        pass


if __name__ == "__main__":
    tests = [test_incident_shape, test_stamped_keeps_ours_picks, test_live_rules, test_observed_rules,
             test_top_level_and_idempotent, test_cli, test_real_file, test_workflow]
    failed = 0
    try:
        for t in tests:
            try:
                t()
            except AssertionError as e:
                failed += 1
                print(f"FAIL {t.__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    finally:
        if TMP.exists():
            shutil.rmtree(TMP, onerror=_onerror)
    if failed:
        print(f"{failed} 件失敗")
        sys.exit(1)
    print("ALL OK")

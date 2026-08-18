# -*- coding: utf-8 -*-
"""結果更新: 指定日(既定=前日JST)のB+Kを取得してデータセットへ追記し、
その日の予測JSONと突き合わせて的中実績をdocs/accuracy.jsonに記録する。"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import build_day, save_year
from common import (is_sengen, sengen_top3p, SENGEN_MIN_ODDS,
                     is_matsu, matsu_top4p, MATSU_MIN_ODDS, MATSU_MAX_ODDS)

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))


def _atomic_write_text(path: Path, text: str):
    """同一ディレクトリのtmpファイルに書いてからos.replaceで差し替える(破損防止)。"""
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def _picks_ok(r, act=None, pay=None):
    """価値フィルタ: 上位3点にオッズ3.1倍未満が含まれれば厳選対象外(300円ボックスで
    的中しても損になるため)。的中買い目(c==act)は暫定オッズが最終に更新されず残る
    ことがあるため、確定した実配当(pay/100)を優先して判定する(UI pickOdds と同一)。"""
    t3 = (r.get("odds") or {}).get("t3") or {}
    for p in r["picks"][:3]:
        c = p["c"]
        if act is not None and pay and c == act:
            o = pay / 100.0
        else:
            o = t3.get(c)
        if o is not None and o < SENGEN_MIN_ODDS:
            return False
    return True


def _matsu_ok(r, act=None, pay=None):
    """プレミアの価値フィルタ: 上位4点は全点オッズ取得済みが前提(1つでもNoneなら対象外。
    厳選と異なりオッズ必須。オッズ帯=市場の同意が選定シグナルのため)。上限(10.0倍)判定は
    常に取得オッズそのままで行う(実配当で事後に外さない)。下限(4.1倍)判定のみ、的中買い目
    (c==act)は暫定オッズが最終に更新されず残ることがあるため、確定した実配当(pay/100)を
    優先して判定する(_picks_okと同一の流儀)。"""
    t3 = (r.get("odds") or {}).get("t3") or {}
    for p in r["picks"][:4]:
        c = p["c"]
        o = t3.get(c)
        if o is None:
            return False
        if o > MATSU_MAX_ODDS:
            return False
        o_min = pay / 100.0 if (act is not None and pay and c == act) else o
        if o_min < MATSU_MIN_ODDS:
            return False
    return True


def evaluate(ymd: str, day_df: pd.DataFrame):
    pred_path = ROOT / "docs" / "predictions" / f"{ymd}.json"
    if not pred_path.exists():
        print(f"no prediction file for {ymd}, skip evaluation")
        return None
    pred = json.loads(pred_path.read_text())
    actual = {}
    pays = {}
    top2_lanes = {}
    abnormal_lanes = {}
    for (venue, rno), g_all in day_df.groupby(["venue", "race_no"]):
        key = (int(venue), int(rno))
        # F/L/K(スタート事故・欠場)のみ返還対象、S(失格)は舟券没収のため対象外
        # (pos欠損かつabnormalがF/L/Kで始まる艇のlaneを記録し、返還舟券の判定に使う)。
        _abn = g_all["abnormal"].astype(str)
        ab = g_all.loc[g_all["pos"].isna() & g_all["abnormal"].notna()
                       & _abn.str.startswith(("F", "L", "K")), "lane"]
        abnormal_lanes[key] = set(int(x) for x in ab)

        # 公式配当(pay3t_combo)の組番を正として採用する。K行の脱落(例: 2文字名の
        # 選手行が正規表現に不一致)でposが欠けていても、払戻情報自体は別行から
        # パースできているため、pos再構成より信頼できる。
        combo_s = g_all["pay3t_combo"].dropna()
        combo_top3 = None
        if len(combo_s):
            cm = re.match(r"^([1-6])-([1-6])-([1-6])$", str(combo_s.iloc[0]))
            if cm:
                combo_top3 = [int(cm.group(1)), int(cm.group(2)), int(cm.group(3))]

        # posからの再構成はcomboフォールバック時のみ使う。同着(デッドヒート)対応:
        # 同一着順に複数艇、次着順が欠番になり得るため、posの若い順→艇番昇順で
        # 並べ、上位3艇を決定的に確定する(欠番/重複はIndexErrorにしない)。
        g = g_all.dropna(subset=["pos"])
        pos_top3 = []
        for pos in sorted(g["pos"].unique()):
            lanes = sorted(int(x) for x in g.loc[g["pos"] == pos, "lane"])
            pos_top3.extend(lanes)
            if len(pos_top3) >= 3:
                break

        if combo_top3 is not None:
            if len(pos_top3) >= 3 and pos_top3[:3] != combo_top3:
                print(f"WARN actual mismatch {key}: pos={pos_top3[:3]} combo={combo_top3}; using combo")
            top3 = combo_top3
        elif len(pos_top3) >= 3:
            top3 = pos_top3[:3]
        else:
            continue

        a, b, c = top3[0], top3[1], top3[2]
        actual[key] = f"{a}-{b}-{c}"
        top2_lanes[key] = (a, b)
        pay = g_all["pay3t_amount"].dropna()
        pays[key] = float(pay.iloc[0]) if len(pay) else 0.0

    day = {"date": ymd, "races": 0, "win_hit": 0,
           "top1_hit": 0, "top5_hit": 0, "top6_hit": 0, "top10_hit": 0,
           "stake5": 0, "return5": 0, "stake6": 0, "return6": 0,
           "fuku_hit": 0, "sen_n": 0, "sen_hit": 0,
           "sen_pred_sum": 0.0, "top5_pred_sum": 0.0,
           "prm_n": 0, "prm_hit": 0, "prm_pred_sum": 0.0,
           "sen_stake": 0, "sen_ret": 0, "prm_stake": 0, "prm_ret": 0,
           # 的中したのに購入額を下回った回数(recompute_sengen.pyと同一定義)
           "sen_hitloss": 0, "prm_hitloss": 0}
    for v in pred.get("venues", []):
        for r in v["races"]:
            key = (v["code"], r["no"])
            act = actual.get(key)
            if not act:
                continue
            day["races"] += 1
            picks = [p["c"] for p in r["picks"]]
            win_lane = int(act.split("-")[0])
            # 本命は保存済みの非丸めargmax(fuku.lane)を使う。boats.wp(3桁丸め)からの
            # 再計算はしない(丸め同値時に若い枠番優先バイアスが生じるため)。複勝と基準を統一。
            top_boat = r.get("fuku", {}).get("lane")
            if top_boat is None and r.get("boats"):
                top_boat = max(r["boats"], key=lambda b: b["wp"])["lane"]
            fuku_lane = top_boat
            top5p = sum(p.get("p", 0) for p in r["picks"][:5])
            top3p = sum(p.get("p", 0) for p in r["picks"][:3])
            top4p = sum(p.get("p", 0) for p in r["picks"][:4])
            if top_boat is not None and top_boat == win_lane:
                day["win_hit"] += 1
            if fuku_lane in top2_lanes.get(key, ()):
                day["fuku_hit"] += 1
            day["top5_pred_sum"] += top5p
            _pay = pays.get(key)
            # 竹/松の該当可否: 優先順位は
            #   1. r["tk"]/r["mt"](レース直下、締切15分前チェックポイントで確定/
            #      取りこぼし時は結果確定時にフォールバックで焼き込み)
            #   2. res["tk"]/res["mt"](旧仕様互換。導入前に確定済みだった本日分等)
            #   3. 従来通りの動的計算(スタンプの無い過去データ、52日互換)
            # 上位ほど遡及改変防止の確度が高いため優先する。
            # actはK帳票由来の実際の着順で、的中判定(act in picks)は優先順位に関わらず従来通り。
            rres = r.get("result") or {}
            if "tk" in r:
                is_tk = bool(r["tk"])
            elif "tk" in rres:
                is_tk = bool(rres["tk"])
            else:
                is_tk = is_sengen(top3p, v["code"], r["no"]) and _picks_ok(r, act, _pay)
            if is_tk:
                day["sen_n"] += 1
                day["sen_pred_sum"] += top3p
                # 竹プランROI: 1レース300円投資、的中時はpay3t(100円あたり払戻)を回収
                day["sen_stake"] += 300
                if act in picks[:3]:
                    day["sen_hit"] += 1
                    day["sen_ret"] += pays.get(key, 0)
                    if pays.get(key, 0) < 300:
                        day["sen_hitloss"] += 1
            if "mt" in r:
                is_mt = bool(r["mt"])
            elif "mt" in rres:
                is_mt = bool(rres["mt"])
            else:
                is_mt = is_matsu(top4p, v["code"], r["no"]) and _matsu_ok(r, act, _pay)
            if is_mt:
                day["prm_n"] += 1
                day["prm_pred_sum"] += top4p
                # 松プランROI: 1レース400円投資、的中時はpay3tを回収
                day["prm_stake"] += 400
                if act in picks[:4]:
                    day["prm_hit"] += 1
                    day["prm_ret"] += pays.get(key, 0)
                    if pays.get(key, 0) < 400:
                        day["prm_hitloss"] += 1
            if picks and picks[0] == act:
                day["top1_hit"] += 1
            # F/L等の異常艇が絡む組番は全額返還されるため、実投入額(返還されない点数)のみを
            # stakeに計上する(1点あたり100円: stake5=500円/5点, stake6=600円/6点と整合)。
            ab = abnormal_lanes.get(key, set())

            def _combo_ok(c):
                if not ab:
                    return True
                return not any(int(n) in ab for n in c.split("-"))

            day["stake5"] += 100 * sum(1 for c in picks[:5] if _combo_ok(c))
            if act in picks[:5]:
                day["top5_hit"] += 1
                day["return5"] += pays.get(key, 0)
            day["stake6"] += 100 * sum(1 for c in picks[:6] if _combo_ok(c))
            if act in picks[:6]:
                day["top6_hit"] += 1
                day["return6"] += pays.get(key, 0)
            if act in picks[:10]:
                day["top10_hit"] += 1
    return day


def main():
    yesterday = (datetime.now(JST) - timedelta(days=1)).strftime("%Y%m%d")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=yesterday)
    ap.add_argument("--skip-dataset", action="store_true")
    a = ap.parse_args()
    ymd = a.date

    df = build_day(ymd)
    if df is None:
        print(f"{ymd}: no data (no races held)")
        return
    if not a.skip_dataset:
        save_year(df, ymd[:4])

    day = evaluate(ymd, df)
    if day:
        acc_path = ROOT / "docs" / "accuracy.json"
        acc = json.loads(acc_path.read_text()) if acc_path.exists() else {"days": []}
        acc["days"] = [d for d in acc["days"] if d["date"] != ymd] + [day]
        acc["days"].sort(key=lambda d: d["date"])
        acc["days"] = acc["days"][-365:]
        t = {"races": 0, "win_hit": 0, "top1_hit": 0, "top5_hit": 0, "top6_hit": 0,
             "top10_hit": 0, "stake5": 0, "return5": 0, "stake6": 0, "return6": 0,
             "fuku_hit": 0, "sen_n": 0, "sen_hit": 0, "sen_pred_sum": 0.0, "top5_pred_sum": 0.0,
             "prm_n": 0, "prm_hit": 0, "prm_pred_sum": 0.0,
             "sen_stake": 0, "sen_ret": 0, "prm_stake": 0, "prm_ret": 0,
             "sen_hitloss": 0, "prm_hitloss": 0}
        for d in acc["days"]:
            for k in t:
                t[k] += d.get(k, 0)
        acc["total"] = t
        _atomic_write_text(acc_path, json.dumps(acc, ensure_ascii=False))
        print("accuracy:", json.dumps(day, ensure_ascii=False))


if __name__ == "__main__":
    main()

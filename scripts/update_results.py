# -*- coding: utf-8 -*-
"""結果更新: 指定日(既定=前日JST)のB+Kを取得してデータセットへ追記し、
その日の予測JSONと突き合わせて的中実績をdocs/accuracy.jsonに記録する。"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from build_dataset import build_day, save_year
from common import is_sengen, sengen_top3p, SENGEN_MIN_ODDS

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
        # F/L等の異常艇(pos欠損かつabnormalあり)のlaneを記録しておく(返還舟券の判定に使う)。
        ab = g_all.loc[g_all["pos"].isna() & g_all["abnormal"].notna(), "lane"]
        abnormal_lanes[(int(venue), int(rno))] = set(int(x) for x in ab)
        g = g_all.dropna(subset=["pos"])
        # 同着(デッドヒート)対応: 同一着順に複数艇、次着順が欠番になり得るため
        # posの若い順→艇番昇順で並べ、上位3艇を決定的に確定する(欠番/重複はIndexErrorにしない)。
        top3 = []
        for pos in sorted(g["pos"].unique()):
            lanes = sorted(int(x) for x in g.loc[g["pos"] == pos, "lane"])
            top3.extend(lanes)
            if len(top3) >= 3:
                break
        if len(top3) < 3:
            continue
        a, b, c = top3[0], top3[1], top3[2]
        actual[(int(venue), int(rno))] = f"{a}-{b}-{c}"
        top2_lanes[(int(venue), int(rno))] = (a, b)
        pay = g["pay3t_amount"].dropna()
        pays[(int(venue), int(rno))] = float(pay.iloc[0]) if len(pay) else 0.0

    day = {"date": ymd, "races": 0, "win_hit": 0,
           "top1_hit": 0, "top5_hit": 0, "top6_hit": 0, "top10_hit": 0,
           "stake5": 0, "return5": 0, "stake6": 0, "return6": 0,
           "fuku_hit": 0, "sen_n": 0, "sen_hit": 0,
           "sen_pred_sum": 0.0, "top5_pred_sum": 0.0}
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
            if top_boat is not None and top_boat == win_lane:
                day["win_hit"] += 1
            if fuku_lane in top2_lanes.get(key, ()):
                day["fuku_hit"] += 1
            day["top5_pred_sum"] += top5p
            _pay = pays.get(key)
            if is_sengen(top3p, v["code"]) and _picks_ok(r, act, _pay):
                day["sen_n"] += 1
                day["sen_pred_sum"] += top3p
                if act in picks[:3]:
                    day["sen_hit"] += 1
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
             "fuku_hit": 0, "sen_n": 0, "sen_hit": 0, "sen_pred_sum": 0.0, "top5_pred_sum": 0.0}
        for d in acc["days"]:
            for k in t:
                t[k] += d.get(k, 0)
        acc["total"] = t
        _atomic_write_text(acc_path, json.dumps(acc, ensure_ascii=False))
        print("accuracy:", json.dumps(day, ensure_ascii=False))


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""B(番組表)+K(競走成績)をダウンロードし結合、entries_YYYY.csv.gzへ保存。
usage: python scripts/build_dataset.py --start 20210601 --end 20260610 [--sleep 0.4]"""
import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from common import download_day, parse_b, parse_k

OUT_DIR = Path(__file__).parent.parent / "data" / "races"

ENTRY_COLS = [
    "date", "venue", "race_no", "race_type", "distance", "deadline",
    "lane", "toban", "name", "age", "branch", "weight", "class",
    "nat_win", "nat_in2", "loc_win", "loc_in2",
    "motor_no", "motor_in2", "boat_no", "boat_in2",
    "pos", "abnormal", "course", "st", "ex_time",
    "kimarite", "wind", "wave",
    "pay3t_combo", "pay3t_amount", "pay2t_combo", "pay2t_amount",
]


def build_day(ymd: str) -> pd.DataFrame | None:
    """1日分のB+Kを取得し結合したDataFrameを返す(無開催はNone)"""
    btxt = download_day("B", ymd)
    ktxt = download_day("K", ymd)
    if btxt is None and ktxt is None:
        return None
    b_races = parse_b(btxt, ymd) if btxt else []
    k_races = parse_k(ktxt, ymd) if ktxt else []
    kmap = {(r["venue"], r["race_no"]): r for r in k_races}
    rows = []
    for br in b_races:
        kr = kmap.get((br["venue"], br["race_no"]))
        krow_map = {x["lane"]: x for x in kr["rows"]} if kr else {}
        for rc in br["racers"]:
            kx = krow_map.get(rc["lane"], {})
            rows.append({
                "date": ymd, "venue": br["venue"], "race_no": br["race_no"],
                "race_type": br["race_type"], "distance": br["distance"],
                "deadline": br["deadline"],
                "lane": rc["lane"], "toban": rc["toban"], "name": rc["name"],
                "age": rc["age"], "branch": rc["branch"], "weight": rc["weight"],
                "class": rc["class"],
                "nat_win": rc["nat_win"], "nat_in2": rc["nat_in2"],
                "loc_win": rc["loc_win"], "loc_in2": rc["loc_in2"],
                "motor_no": rc["motor_no"], "motor_in2": rc["motor_in2"],
                "boat_no": rc["boat_no"], "boat_in2": rc["boat_in2"],
                "pos": kx.get("pos"), "abnormal": kx.get("abnormal"),
                "course": kx.get("course"), "st": kx.get("st"),
                "ex_time": kx.get("ex_time"),
                "kimarite": kr["kimarite"] if kr else None,
                "wind": kr["wind"] if kr else None,
                "wave": kr["wave"] if kr else None,
                "pay3t_combo": kr["pay_3t"]["combo"] if kr and kr["pay_3t"] else None,
                "pay3t_amount": kr["pay_3t"]["amount"] if kr and kr["pay_3t"] else None,
                "pay2t_combo": kr["pay_2t"]["combo"] if kr and kr["pay_2t"] else None,
                "pay2t_amount": kr["pay_2t"]["amount"] if kr and kr["pay_2t"] else None,
            })
    if not rows:
        return None
    return pd.DataFrame(rows, columns=ENTRY_COLS)


def save_year(df_new: pd.DataFrame, year: str):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"entries_{year}.csv.gz"
    if path.exists():
        old = pd.read_csv(path, dtype={"date": str})
        old = old[~old["date"].isin(set(df_new["date"]))]
        df = pd.concat([old, df_new], ignore_index=True)
    else:
        df = df_new
    df = df.sort_values(["date", "venue", "race_no", "lane"])
    df.to_csv(path, index=False, compression="gzip")
    print(f"saved {path.name}: total {len(df)} rows")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--sleep", type=float, default=0.4)
    a = ap.parse_args()
    d = date(int(a.start[:4]), int(a.start[4:6]), int(a.start[6:]))
    end = date(int(a.end[:4]), int(a.end[4:6]), int(a.end[6:]))
    buf, cur_year = [], a.start[:4]
    n_days, n_races = 0, 0
    while d <= end:
        ymd = d.strftime("%Y%m%d")
        if ymd[:4] != cur_year and buf:
            save_year(pd.concat(buf, ignore_index=True), cur_year)
            buf, cur_year = [], ymd[:4]
        try:
            df = build_day(ymd)
        except Exception as e:
            print(f"{ymd}: ERROR {e}")
            df = None
        if df is not None:
            buf.append(df)
            n_days += 1
            n_races += len(df) // 6
        d += timedelta(days=1)
        time.sleep(a.sleep)
        if n_days % 50 == 0 and df is not None:
            print(f"progress: {ymd} days={n_days} races={n_races}", flush=True)
    if buf:
        save_year(pd.concat(buf, ignore_index=True), cur_year)
    print(f"done. days={n_days} races={n_races}")


if __name__ == "__main__":
    main()

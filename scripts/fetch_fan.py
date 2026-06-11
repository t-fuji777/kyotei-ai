# -*- coding: utf-8 -*-
"""ファン手帳データ(期別成績)取得: 必要な期のfanYYMM.lzhを公式サイトからDLし
parse_fanで解析して data/fan/fan_YYMM.csv.gz に保存する。既存ファイルはスキップ。

期マッピング: fanYY10(5-10月集計) -> 翌年前期(1-6月)適用 / fanYY04(11-4月集計) -> 同年後期(7-12月)適用
"""
import argparse
import csv
import gzip
import io
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import http_get
from parse_fan import parse_file, LAYOUT

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))
URL = "https://www.boatrace.jp/static_extra/pc_static/download/data/kibetsu/{name}.lzh"


def period_to_file(year: int, ki: int) -> str:
    """適用期(year, ki) -> fanファイル名"""
    if ki == 1:
        return f"fan{(year - 1) % 100:02d}10"
    return f"fan{year % 100:02d}04"


def periods_between(start_ymd: str, end_ymd: str):
    ys, ms = int(start_ymd[:4]), int(start_ymd[4:6])
    ye, me = int(end_ymd[:4]), int(end_ymd[4:6])
    cur = (ys, 1 if ms <= 6 else 2)
    end = (ye, 1 if me <= 6 else 2)
    out = []
    while cur <= end:
        out.append(cur)
        cur = (cur[0], 2) if cur[1] == 1 else (cur[0] + 1, 1)
    return out


def fetch_one(name: str, year: int, ki: int, outdir: Path) -> bool:
    dst = outdir / f"fan_{name[3:]}.csv.gz"
    if dst.exists():
        print(f"{name}: exists, skip")
        return True
    try:
        data = http_get(URL.format(name=name))
    except Exception as e:
        print(f"{name}: download failed ({e})")
        return False
    import lhafile
    f = lhafile.Lhafile(io.BytesIO(data))
    raw = f.read(f.infolist()[0].filename)
    rows = parse_file(raw)
    cols = [n for n, _, _ in LAYOUT]
    with gzip.open(dst, "wt", encoding="utf-8", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(cols)
        for r in rows:
            # ファイル内のyear/kiを信頼するが、欠落時は引数で補完
            if r.get("year") is None:
                r["year"], r["ki"] = year, ki
            w.writerow([r[c] for c in cols])
    print(f"{name}: saved {len(rows)} racers -> {dst.name}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="この日付の期から取得(YYYYMMDD)")
    ap.add_argument("--latest", action="store_true", help="現期と次期のみ確認")
    a = ap.parse_args()
    outdir = ROOT / "data" / "fan"
    outdir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(JST).strftime("%Y%m%d")
    if a.latest:
        periods = periods_between(today, today)
        y, k = periods[0]
        nxt = (y, 2) if k == 1 else (y + 1, 1)
        periods.append(nxt)
    else:
        periods = periods_between(a.start or "20210101", today)
    for year, ki in periods:
        fetch_one(period_to_file(year, ki), year, ki, outdir)


if __name__ == "__main__":
    main()

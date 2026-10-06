# -*- coding: utf-8 -*-
"""entries(data/races/entries_*.csv.gz)の読み込みを1か所にまとめる。

学習(train.load_entries)と当日予測(predict_today.load_hist)が別々に読んでいたのを、同じ関数に
する。履歴の特徴量(features_hist.build_hist)は 日付→会場→レース番号→枠 の並びと全レース6行を前提に
するので、ここで並べ替えまで済ませる。
今のファイルは保存時(build_dataset.save_year)に同じ鍵で並んでいて、K0/K1 の行も重複も無いため、
除外と並べ替えは何も動かさない(= 世代1の学習・予測は今と同じ行を同じ順で受け取る)。
"""
import glob
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
SORT_KEYS = ["date", "venue", "race_no", "lane"]
_RE_YEAR = re.compile(r"entries_(\d{4})\.csv\.gz$")


def entries_files():
    return sorted(glob.glob(str(ROOT / "data" / "races" / "entries_*.csv.gz")))


def load_entries(before_ymd=None) -> pd.DataFrame:
    """data/races の全ファイルを読み、K0/K1(欠場)の行を除き、(date, venue, race_no, lane) の重複を除き、
    日付・会場・レース番号・枠で並べ替えて返す。before_ymd('YYYYMMDD')があればその日より前の行だけ。
    date は文字列のまま。index は読み込み時のもの(行を落としても振り直さない。今までの読み込みと同じ)。"""
    files = entries_files()
    if not files:
        raise SystemExit("no entries files. run build_dataset first")
    if before_ymd is not None:
        before_ymd = str(before_ymd)
        # ファイルは日付の年ごとなので、対象より後の年のファイルは読まなくてよい
        year = int(before_ymd[:4])
        keep = []
        for f in files:
            m = _RE_YEAR.search(f.replace("\\", "/"))
            if m and int(m.group(1)) > year:
                continue
            keep.append(f)
        files = keep
    df = pd.concat([pd.read_csv(f, dtype={"date": str}) for f in files], ignore_index=True)
    df = df[~df["abnormal"].isin(["K0", "K1"])]
    df = df.drop_duplicates(subset=SORT_KEYS)
    if before_ymd is not None:
        df = df[df["date"] < before_ymd]
    # 同じ鍵なら元の並びを保つ(stable)。ファイルが既に並んでいれば何も動かない
    df = df.sort_values(SORT_KEYS, kind="stable")
    return df

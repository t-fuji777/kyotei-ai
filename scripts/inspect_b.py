# -*- coding: utf-8 -*-
"""Debug: dump the first ~12 lines of each venue block from today's B file to
see whether a 'day-of-meeting' (Nth day / 第N日) marker is present."""
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import download_day, split_venues, zen2han

JST = timezone(timedelta(hours=9))


def main():
    now = datetime.now(JST)
    ymd = now.strftime("%Y%m%d")
    txt = download_day("B", ymd)
    if txt is None:
        print(f"B file for {ymd} unavailable")
        return
    print(f"B file {ymd} total chars: {len(txt)}")
    # show the very top of the file (global header)
    print("===== FILE TOP (first 6 lines) =====")
    for ln in txt.splitlines()[:6]:
        print(repr(ln))
    # per venue: code + first 12 lines
    count = 0
    for vcode, lines in split_venues(txt, "B"):
        count += 1
        if count > 4:
            break
        print(f"===== VENUE code={vcode} (first 12 lines) =====")
        for ln in lines[:12]:
            print(repr(zen2han(ln)))


if __name__ == "__main__":
    main()

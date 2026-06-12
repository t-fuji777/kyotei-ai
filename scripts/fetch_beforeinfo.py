# -*- coding: utf-8 -*-
"""boatrace.jp 直前情報ページから展示タイム・チルト・スタ展・気象を取得"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import http_get

URL = "https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd:02d}&hd={ymd}"


def parse_beforeinfo(html: str) -> dict:
    out = {"ex": {}, "tilt": {}, "wind": None, "wave": None, "course": {}, "st": {}}
    m = re.search(r"<table[^>]*is-w748[\s\S]*?</table>", html)
    if m:
        for tb in re.findall(r"<tbody[\s\S]*?</tbody>", m.group(0)):
            tds = [re.sub(r"<[^>]+>", "", t).strip()
                   for t in re.findall(r"<td[^>]*>[\s\S]*?</td>", tb)]
            tds = [re.sub(r"\s+", " ", t) for t in tds]
            if len(tds) < 6 or not tds[0].isdigit():
                continue
            lane = int(tds[0])
            if not 1 <= lane <= 6:
                continue
            out["ex"][lane] = float(tds[4]) if re.match(r"^\d\.\d\d$", tds[4]) else None
            out["tilt"][lane] = tds[5] or None
    w = re.search(r'is-wind"[\s\S]*?weather1_bodyUnitLabelData[^>]*>\s*([\d.]+)m', html)
    if w:
        out["wind"] = float(w.group(1))
    v = re.search(r'is-wave"[\s\S]*?weather1_bodyUnitLabelData[^>]*>\s*([\d.]+)cm', html)
    if v:
        out["wave"] = float(v.group(1))
    m2 = re.search(r"<table[^>]*is-w238[\s\S]*?</table>", html)
    if m2:
        c = 0
        for r in re.findall(r"<tr[\s\S]*?</tr>", m2.group(0)):
            tm = re.search(r"is-type(\d)[\s\S]*?boatImage1Time[^>]*>\s*([F.\d]+)", r)
            if tm:
                c += 1
                out["course"][int(tm.group(1))] = c
                out["st"][int(tm.group(1))] = tm.group(2)
    return out


def fetch_beforeinfo(ymd: str, jcd: int, rno: int) -> dict | None:
    try:
        raw = http_get(URL.format(rno=rno, jcd=jcd, ymd=ymd), timeout=20, retries=2)
    except Exception as e:
        print(f"beforeinfo {jcd}-{rno}R: fetch failed ({e})")
        return None
    return parse_beforeinfo(raw.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    import json
    ymd, jcd, rno = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    print(json.dumps(fetch_beforeinfo(ymd, jcd, rno), ensure_ascii=False))

# -*- coding: utf-8 -*-
"""boatrace.jp オッズページから複勝オッズと3連単オッズを取得"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import http_get

URL_TF = "https://www.boatrace.jp/owpc/pc/race/oddstf?rno={rno}&jcd={jcd:02d}&hd={ymd}"
URL_3T = "https://www.boatrace.jp/owpc/pc/race/odds3t?rno={rno}&jcd={jcd:02d}&hd={ymd}"


def _strip(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").strip()


def parse_fuku(html: str) -> dict:
    """複勝オッズ {lane:int -> '1.0-1.2'}"""
    out = {}
    m = re.search(r"複勝オッズ([\s\S]*?)</table>", html)
    if not m:
        return out
    for bm in re.finditer(r'is-boatColor(\d)[^>]*>\s*\d\s*<[\s\S]{0,300}?oddsPoint[^>]*>\s*([\d.\-~〜]+)\s*<', m.group(1)):
        out[int(bm.group(1))] = bm.group(2)
    return out


def parse_3t(html: str) -> dict:
    """3連単オッズ {'1-2-3': 9.2, ...} (120通り) DOM列走査ロジックの正規表現移植"""
    tables = re.findall(r"<table[\s\S]*?</table>", html)
    tbl = None
    for t in tables:
        if t.count("oddsPoint") >= 100:
            tbl = t
            break
    if not tbl:
        return {}
    odds = {}
    col_second = [None] * 6
    for tr in re.findall(r"<tr[\s\S]*?</tr>", tbl):
        cells = re.findall(r"<td([^>]*)>([\s\S]*?)</td>", tr)
        i, col = 0, 0
        while i < len(cells) and col < 6:
            attr, body = cells[i]
            if "rowspan" in attr:
                col_second[col] = _strip(body)
                i += 1
                if i >= len(cells):
                    break
                attr, body = cells[i]
            third = _strip(body)
            o = _strip(cells[i + 1][1]) if i + 1 < len(cells) else None
            sec = col_second[col]
            if sec and third.isdigit() and o:
                try:
                    odds[f"{col + 1}-{sec}-{third}"] = float(o)
                except ValueError:
                    pass
            i += 2
            col += 1
    return odds


def fetch_odds(ymd: str, jcd: int, rno: int) -> dict | None:
    try:
        tf = http_get(URL_TF.format(rno=rno, jcd=jcd, ymd=ymd), timeout=20, retries=2).decode("utf-8", errors="replace")
        t3 = http_get(URL_3T.format(rno=rno, jcd=jcd, ymd=ymd), timeout=20, retries=2).decode("utf-8", errors="replace")
    except Exception as e:
        print(f"odds {jcd}-{rno}R: fetch failed ({e})")
        return None
    return {"fuku": parse_fuku(re.sub(r"\s+", " ", tf)),
            "t3": parse_3t(re.sub(r"\s+", " ", t3))}


if __name__ == "__main__":
    import json
    ymd, jcd, rno = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    o = fetch_odds(ymd, jcd, rno)
    print(json.dumps({"fuku": o["fuku"], "t3_n": len(o["t3"]),
                      "t3_sample": dict(list(o["t3"].items())[:3])}, ensure_ascii=False))

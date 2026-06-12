# -*- coding: utf-8 -*-
"""Fetch odds from boatrace.jp: place(fuku), trifecta(t3), trio(f3),
exacta(t2), quinella(f2), wide(k). Keys: t3 "1-2-3", f3 "1=2=3",
t2 "1-2", f2/k "1=2" (sorted). k values are range strings like "2.2-4.8"."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import http_get

BASE = "https://www.boatrace.jp/owpc/pc/race/{page}?rno={rno}&jcd={jcd:02d}&hd={ymd}"
FUKU_HEAD = "\u8907\u52dd\u30aa\u30c3\u30ba"  # heading text


def _strip(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").strip()


def _tables(html: str):
    return re.findall(r"<table[\s\S]*?</table>", html)


def _walk_rows(tbl: str):
    for tr in re.findall(r"<tr[\s\S]*?</tr>", tbl):
        yield re.findall(r"<td([^>]*)>([\s\S]*?)</td>", tr)


def parse_fuku(html: str) -> dict:
    out = {}
    m = re.search(FUKU_HEAD + r"([\s\S]*?)</table>", html)
    if not m:
        return out
    for bm in re.finditer(r'is-boatColor(\d)[^>]*>\s*\d\s*<[\s\S]{0,300}?oddsPoint[^>]*>\s*([\d.\-~\u301c]+)\s*<', m.group(1)):
        out[int(bm.group(1))] = bm.group(2)
    return out


def _parse_tri(tbl: str, sep: str) -> dict:
    """3-boat tables (trifecta 120 / trio 20). Column-walk with rowgroup
    head cells marked by class is-fs14."""
    odds = {}
    col_second = [None] * 6
    for cells in _walk_rows(tbl):
        i, col = 0, 0
        while i < len(cells):
            attr, body = cells[i]
            if "is-fs14" in attr:
                t = _strip(body)
                if t:
                    col_second[col] = t
                i += 1
                if i >= len(cells):
                    break
                attr, body = cells[i]
            third = _strip(body)
            o = _strip(cells[i + 1][1]) if i + 1 < len(cells) else None
            sec = col_second[col]
            if sec and third.isdigit() and o:
                try:
                    odds[f"{col + 1}{sep}{sec}{sep}{third}"] = float(o)
                except ValueError:
                    pass
            i += 2
            col += 1
    return odds


def _parse_grid2(tbl: str, sep: str, sort2: bool, as_float: bool = True) -> dict:
    """2-boat grid tables (exacta 30 / quinella 15 / wide 15)."""
    odds = {}
    for cells in _walk_rows(tbl):
        i, col = 0, 0
        while i < len(cells):
            a = _strip(cells[i][1])
            o = _strip(cells[i + 1][1]) if i + 1 < len(cells) else None
            if a.isdigit() and o:
                x, y = col + 1, int(a)
                if sort2 and x > y:
                    x, y = y, x
                key = f"{x}{sep}{y}"
                if as_float:
                    try:
                        odds[key] = float(o)
                    except ValueError:
                        pass
                else:
                    odds[key] = o
            i += 2
            col += 1
    return odds


def _find_table(html: str, lo: int, hi: int):
    for t in _tables(html):
        n = t.count("oddsPoint")
        if lo <= n <= hi:
            yield t


def _get(page: str, ymd: str, jcd: int, rno: int) -> str:
    raw = http_get(BASE.format(page=page, rno=rno, jcd=jcd, ymd=ymd), timeout=20, retries=2)
    return re.sub(r"\s+", " ", raw.decode("utf-8", errors="replace"))


def fetch_odds(ymd: str, jcd: int, rno: int) -> dict | None:
    out = {"fuku": {}, "t3": {}, "f3": {}, "t2": {}, "f2": {}, "k": {}}
    try:
        out["fuku"] = parse_fuku(_get("oddstf", ymd, jcd, rno))
        for t in _find_table(_get("odds3t", ymd, jcd, rno), 100, 130):
            out["t3"] = _parse_tri(t, "-")
            break
        for t in _find_table(_get("odds3f", ymd, jcd, rno), 16, 40):
            out["f3"] = _parse_tri(t, "=")
            break
        h = _get("odds2tf", ymd, jcd, rno)
        for t in _find_table(h, 25, 40):
            out["t2"] = _parse_grid2(t, "-", False)
            break
        for t in _find_table(h, 12, 18):
            out["f2"] = _parse_grid2(t, "=", True)
            break
        for t in _find_table(_get("oddsk", ymd, jcd, rno), 12, 18):
            out["k"] = _parse_grid2(t, "=", True, as_float=False)
            break
    except Exception as e:
        print(f"odds {jcd}-{rno}R: fetch failed ({e})")
        return None
    return out


if __name__ == "__main__":
    import json
    ymd, jcd, rno = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    o = fetch_odds(ymd, jcd, rno)
    print(json.dumps({k: len(v) for k, v in o.items()}))

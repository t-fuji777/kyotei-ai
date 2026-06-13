# -*- coding: utf-8 -*-
"""Fetch odds for all bet types from boatrace.jp official odds pages.
Pages: oddstf (win/place), odds3t (trifecta), odds3f (trio),
odds2tf (exacta/quinella), oddsk (wide). Parsers verified against actual payouts."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import http_get

BASE = "https://www.boatrace.jp/owpc/pc/race/{page}?rno={rno}&jcd={jcd:02d}&hd={ymd}"
FUKU_HEAD = "\u8907\u52dd\u30aa\u30c3\u30ba"


def _strip(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").strip()


def _get(page, ymd, jcd, rno):
    raw = http_get(BASE.format(page=page, rno=rno, jcd=jcd, ymd=ymd), timeout=8, retries=1)
    return re.sub(r"\s+", " ", raw.decode("utf-8", errors="replace"))


def _tables(html: str):
    return re.findall(r"<table[\s\S]*?</table>", html)


def _grid(table_html: str):
    """Expand a table into a 2D grid honoring rowspan. Cell -> (text, is_odds)."""
    rows = []
    pending = {}
    for tr in re.findall(r"<tr[\s\S]*?</tr>", table_html):
        cells = re.findall(r"<td([^>]*)>([\s\S]*?)</td>", tr)
        row = []
        ci = 0
        while True:
            while ci in pending:
                rem, val = pending[ci]
                row.append(val)
                if rem <= 1:
                    del pending[ci]
                else:
                    pending[ci] = (rem - 1, val)
                ci += 1
            if not cells:
                break
            attr, body = cells.pop(0)
            val = (_strip(body), "oddsPoint" in attr)
            m = re.search(r'rowspan="?(\d+)', attr)
            rs = int(m.group(1)) if m else 1
            if rs > 1:
                pending[ci] = (rs - 1, val)
            row.append(val)
            ci += 1
        rows.append(row)
    return rows


def _parse_tri(html: str, lo: int, hi: int, sep: str) -> dict:
    """3-boat tables (trifecta '-' / trio '='): column blocks of [second, third, odds]."""
    cand = None
    for t in _tables(html):
        n = t.count("oddsPoint")
        if lo <= n <= hi:
            cand = t
            break
    if not cand:
        return {}
    out = {}
    for row in _grid(cand):
        ncol = (len(row) + 2) // 3
        for col in range(ncol):
            if col * 3 + 2 >= len(row):
                continue
            a, b, o = row[col * 3], row[col * 3 + 1], row[col * 3 + 2]
            if not o[1]:
                continue
            if not (a[0].isdigit() and b[0].isdigit()):
                continue
            key = f"{col + 1}{sep}{a[0]}{sep}{b[0]}"
            try:
                out[key] = float(o[0])
            except ValueError:
                pass
    return out


def _parse_grid2(table_html: str, sep: str) -> dict:
    """2-boat tables: column blocks of [partner, odds]. sep '-' keeps column boat
    first (exacta); sep '=' sorts the pair (quinella/wide). Values kept as strings
    because wide odds can be a range like '1.5-2.1'."""
    out = {}
    for row in _grid(table_html):
        ncol = (len(row) + 1) // 2
        for col in range(ncol):
            if col * 2 + 1 >= len(row):
                continue
            a, o = row[col * 2], row[col * 2 + 1]
            if not o[1] or not a[0].isdigit():
                continue
            if sep == "-":
                key = f"{col + 1}-{a[0]}"
            else:
                x, y = sorted([col + 1, int(a[0])])
                key = f"{x}={y}"
            out[key] = o[0]
    return out


def parse_fuku(html: str) -> dict:
    out = {}
    m = re.search(FUKU_HEAD + r"([\s\S]*?)</table>", html)
    if not m:
        return out
    for bm in re.finditer(r'is-boatColor(\d)[^>]*>\s*\d\s*<[\s\S]{0,300}?oddsPoint[^>]*>\s*([\d.\-~\u301c]+)\s*<', m.group(1)):
        out[int(bm.group(1))] = bm.group(2)
    return out


def _floatify(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        try:
            out[k] = float(v)
        except (ValueError, TypeError):
            pass
    return out


def fetch_odds(ymd: str, jcd: int, rno: int) -> dict | None:
    # fetch the win/place page first; if it fails or odds are not published yet,
    # bail immediately so we do not waste time on the other four pages.
    try:
        tf = _get("oddstf", ymd, jcd, rno)
    except Exception as e:
        print(f"odds {jcd}-{rno}R: not available ({e})")
        return None
    fuku = parse_fuku(tf)
    if not fuku:
        print(f"odds {jcd}-{rno}R: not published yet")
        return None
    try:
        t3 = _get("odds3t", ymd, jcd, rno)
        f3 = _get("odds3f", ymd, jcd, rno)
        tf2 = _get("odds2tf", ymd, jcd, rno)
        kw = _get("oddsk", ymd, jcd, rno)
    except Exception as e:
        print(f"odds {jcd}-{rno}R: partial fetch failed ({e})")
        return None
    res = {"fuku": fuku,
           "t3": _parse_tri(t3, 100, 130, "-"),
           "f3": _parse_tri(f3, 16, 40, "=")}
    two = [t for t in _tables(tf2) if 12 <= t.count("oddsPoint") <= 40]
    t2_tbl = next((t for t in two if t.count("oddsPoint") >= 25), None)
    f2_tbl = next((t for t in two if t.count("oddsPoint") < 25), None)
    res["t2"] = _floatify(_parse_grid2(t2_tbl, "-")) if t2_tbl else {}
    res["f2"] = _floatify(_parse_grid2(f2_tbl, "=")) if f2_tbl else {}
    kt = [t for t in _tables(kw) if 12 <= t.count("oddsPoint") <= 18]
    res["k"] = _parse_grid2(kt[0], "=") if kt else {}
    return res


if __name__ == "__main__":
    import json
    ymd, jcd, rno = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    o = fetch_odds(ymd, jcd, rno)
    print(json.dumps({k: (len(v) if isinstance(v, dict) else v) for k, v in o.items()},
                     ensure_ascii=False))
    print(json.dumps({"fuku": o["fuku"],
                      "t3_123": o["t3"].get("1-2-3"), "f3_123": o["f3"].get("1=2=3"),
                      "t2_12": o["t2"].get("1-2"), "f2_12": o["f2"].get("1=2"),
                      "k_12": o["k"].get("1=2")}, ensure_ascii=False))

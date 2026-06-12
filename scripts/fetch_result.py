# -*- coding: utf-8 -*-
"""boatrace.jp 結果ページから着順・決まり手・払戻を取得(未確定ならNone)"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import http_get, zen2han

URL = "https://www.boatrace.jp/owpc/pc/race/raceresult?rno={rno}&jcd={jcd:02d}&hd={ymd}"


def _strip(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").strip()


def parse_result(html: str) -> dict | None:
    h = re.sub(r"\s+", " ", html)
    m = re.search(r"<table[^>]*is-w495[\s\S]*?</table>", h)
    if not m:
        return None
    pos2lane = {}
    for tb in re.findall(r"<tbody[\s\S]*?</tbody>", m.group(0)):
        tds = [_strip(x) for x in re.findall(r"<td[^>]*>[\s\S]*?</td>", tb)]
        if len(tds) < 2:
            continue
        pos = zen2han(tds[0]).strip()
        if pos.isdigit() and tds[1].isdigit():
            pos2lane[int(pos)] = int(tds[1])
    if not all(k in pos2lane for k in (1, 2, 3)):
        return None  # 未確定 or 返還等
    order = f"{pos2lane[1]}-{pos2lane[2]}-{pos2lane[3]}"

    km = re.search(r"決まり手[\s\S]{0,400}?>(逃げ|差し|まくり差し|まくり|抜き|恵まれ)<", h)
    res = {"order": order, "kimarite": km.group(1) if km else None}

    p3 = re.search(r"3連単([\s\S]{0,1200}?)</tbody>", h)
    if p3:
        nums = re.findall(r"numberSet1_number[^>]*>(\d)<", p3.group(1))
        pay = re.search(r"¥([\d,]+)", p3.group(1))
        if len(nums) >= 3 and pay:
            res["pay3t"] = int(pay.group(1).replace(",", ""))
            res["pay3t_combo"] = "-".join(nums[:3])

    fk = re.search(r"複勝([\s\S]{0,1500}?)</tbody>", h)
    if fk:
        res["fuku_pay"] = {int(a): int(b.replace(",", ""))
                           for a, b in re.findall(r"numberSet1_number[^>]*>(\d)<[\s\S]{0,300}?¥([\d,]+)", fk.group(1))}
    return res


def fetch_result(ymd: str, jcd: int, rno: int) -> dict | None:
    try:
        raw = http_get(URL.format(rno=rno, jcd=jcd, ymd=ymd), timeout=20, retries=2)
    except Exception as e:
        print(f"result {jcd}-{rno}R: fetch failed ({e})")
        return None
    return parse_result(raw.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    import json
    ymd, jcd, rno = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    print(json.dumps(fetch_result(ymd, jcd, rno), ensure_ascii=False))

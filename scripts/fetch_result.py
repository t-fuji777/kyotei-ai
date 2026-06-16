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
    res = {"order": order, "kimarite": km.group(1) if km else None, "ninki": None}

    p3 = re.search(r"3連単([\s\S]{0,1200}?)</tbody>", h)
    if p3:
        nums = re.findall(r"numberSet1_number[^>]*>(\d)<", p3.group(1))
        pay = re.search(r"(?:¥|&yen;|￥)\s*([\d,]+)", p3.group(1))
        if len(nums) >= 3 and pay:
            res["pay3t"] = int(pay.group(1).replace(",", ""))
            res["pay3t_combo"] = "-".join(nums[:3])
            nin = re.search(r"<td[^>]*>(?:\s|<[^>]+>)*(\d+)", p3.group(1)[pay.end():])
            res["ninki"] = int(nin.group(1)) if nin else None

    fk = re.search(r"複勝([\s\S]{0,1500}?)</tbody>", h)
    if fk:
        res["fuku_pay"] = {int(a): int(b.replace(",", ""))
                           for a, b in re.findall(r"numberSet1_number[^>]*>(\d)<[\s\S]{0,300}?(?:¥|&yen;|￥)\s*([\d,]+)", fk.group(1))}
    return res


def fetch_result(ymd: str, jcd: int, rno: int) -> dict | None:
    try:
        raw = http_get(URL.format(rno=rno, jcd=jcd, ymd=ymd), timeout=20, retries=2)
    except Exception as e:
        print(f"result {jcd}-{rno}R: fetch failed ({e})")
        return None
    return parse_result(raw.decode("utf-8", errors="replace"))


BEFORE_URL = "https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd:02d}&hd={ymd}"


def fetch_before_html(ymd, jcd, rno):
    raw = http_get(BEFORE_URL.format(rno=rno, jcd=jcd, ymd=ymd), timeout=20, retries=2)
    return raw.decode("utf-8", errors="replace")


def parse_before(html):
    """beforeinfo HTML -> {"ex": {lane: tenji_time}, "wind": int(m), "wave": int(cm)}.
    展示タイム=各艦tbody内の >X.XX< (体重·チルト·STと区別可)。"""
    h = re.sub(r"\s+", " ", html)

    def wnum(title):
        m = re.search(re.escape(title) + r'</span> <span class="weather1_bodyUnitLabelData">(\d+)', h)
        return int(m.group(1)) if m else None

    ex = {}
    for tb in re.findall(r'<tbody[\s\S]*?</tbody>', h):
        ml = re.search(r'is-boatColor(\d)', tb)
        if not ml:
            continue
        me = re.search(r'>(\d\.\d{2})<', tb)
        if me:
            ex[int(ml.group(1))] = float(me.group(1))
    return {"ex": ex, "wind": wnum("風速"), "wave": wnum("波高")}


def _probe_before(ymd, jcd, rno):
    hh = re.sub(r"\s+", " ", fetch_before_html(ymd, jcd, rno))
    print("LEN", len(hh))
    print("TABLES", re.findall(r'<table[^>]*class="([^"]*)"', hh)[:20])
    mw = re.search(r'weather1[\s\S]{0,1700}', hh)
    print("WEATHER", (mw.group(0)[:1400] if mw else "NONE"))
    for m in re.finditer(r'<table[\s\S]*?</table>', hh):
        t = m.group(0)
        if ("展示" in t) or ("tenji" in t.lower()):
            tbs = re.findall(r'<tbody[\s\S]*?</tbody>', t)
            print("TENJI_NTBODY", len(tbs))
            print("TBODY0", (tbs[0][:1700] if tbs else t[:1700]))
            break
    print("NUMS", re.findall(r'.{12}\d\.\d{2}.{4}', hh)[:30])
    print("PARSED", parse_before(fetch_before_html(ymd, jcd, rno)))


if __name__ == "__main__":
    import json
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        _probe_before(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
    else:
        ymd, jcd, rno = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
        print(json.dumps(fetch_result(ymd, jcd, rno), ensure_ascii=False))

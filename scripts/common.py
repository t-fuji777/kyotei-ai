# -*- coding: utf-8 -*-
"""共通モジュール: 会場定義, ダウンロード, B(番組表)/K(競走成績)パーサ"""
import io
import os
import re
import time
import unicodedata
import urllib.request

VENUES = {
    1: "桐生", 2: "戸田", 3: "江戸川", 4: "平和島", 5: "多摩川",
    6: "浜名湖", 7: "蒲郡", 8: "常滑", 9: "津", 10: "三国",
    11: "びわこ", 12: "住之江", 13: "尼崎", 14: "鳴門", 15: "丸亀",
    16: "児島", 17: "宮島", 18: "徳山", 19: "下関", 20: "若松",
    21: "芦屋", 22: "福岡", 23: "唐津", 24: "大村",
}

BASE = "https://www1.mbrace.or.jp/od2"
UA = "Mozilla/5.0 (compatible; kyotei-ai/1.0; personal research)"


def zen2han(s: str) -> str:
    """全角英数記号を半角化(カナ・漢字は維持)"""
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:  # ！-～
            out.append(chr(code - 0xFEE0))
        elif ch == "\u3000":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def http_get(url: str, timeout=30, retries=3, sleep=2.0) -> bytes:
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:
            last = e
        time.sleep(sleep * (i + 1))
    raise last


def download_day(kind: str, ymd: str) -> str | None:
    """kind: 'B' or 'K'. ymd: YYYYMMDD. 戻り値: cp932デコード済テキスト(無開催等404はNone)"""
    assert kind in ("B", "K")
    yyyymm, yy_mmdd = ymd[:6], ymd[2:]
    url = f"{BASE}/{kind}/{yyyymm}/{kind.lower()}{yy_mmdd}.lzh"
    try:
        raw = http_get(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    import lhafile
    lf = lhafile.Lhafile(io.BytesIO(raw))
    name = lf.infolist()[0].filename
    return lf.read(name).decode("cp932", errors="replace")


def split_venues(text: str, kind: str):
    """ファイル全体を会場ごとに分割。yield (venue_code, lines)"""
    tag_b, tag_e = (kind + "BGN") if kind == "B" else ("KBGN"), ("BEND" if kind == "B" else "KEND")
    tag_b = "BBGN" if kind == "B" else "KBGN"
    tag_e = "BEND" if kind == "B" else "KEND"
    cur_code, cur = None, []
    for line in text.splitlines():
        l = line.rstrip("\r\n")
        if tag_b in l:
            m = re.match(r"\s*(\d{1,2})", l)
            cur_code = int(m.group(1)) if m else None
            cur = []
        elif tag_e in l:
            if cur_code:
                yield cur_code, cur
            cur_code = None
        elif cur_code is not None:
            cur.append(l)


# ---------------- B(番組表) ----------------
RE_B_HEAD = re.compile(r"^\s*(\d{1,2})R\s+(\S+)\s+H(\d+)m\s*電話投票締切予定(\d{1,2}:\d{2})")
RE_B_TAIL = re.compile(
    r"^\s*(\d\.\d\d)\s+(\d{1,3}\.\d\d)"      # 全国勝率 全国2率
    r"\s+(\d\.\d\d)\s+(\d{1,3}\.\d\d)"        # 当地勝率 当地2率
    r"\s*(\d{1,3})\s*(\d{1,3}\.\d\d)"          # モーターNO 2率
    r"\s*(\d{1,3})\s*(\d{1,3}\.\d\d)"          # ボートNO 2率
)


def parse_b_racer_line(raw_line: str):
    """B選手行: 先頭は固定バイト幅(cp932)で切り出し, 残りは正規表現。
    layout(bytes): 0:艇番 1:sp 2-5:登番 6-13:名前(全角4) 14-15:年齢 16-19:支部(全角2) 20-21:体重 22-23:級別 24-:数値"""
    b = raw_line.encode("cp932", errors="replace")
    if len(b) < 30:
        return None
    try:
        lane = int(b[0:1].decode("cp932"))
        toban = int(b[2:6].decode("cp932"))
        name = b[6:14].decode("cp932", errors="replace").replace("\u3000", "").strip()
        age = int(b[14:16].decode("cp932"))
        branch = b[16:20].decode("cp932", errors="replace").replace("\u3000", "").strip()
        weight = int(b[20:22].decode("cp932"))
        klass = b[22:24].decode("cp932")
        rest = b[24:].decode("cp932", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return None
    m = RE_B_TAIL.match(zen2han(rest))
    if not m:
        return None
    return {
        "lane": lane, "toban": toban, "name": name, "age": age,
        "branch": branch, "weight": weight, "class": klass.strip(),
        "nat_win": float(m.group(1)), "nat_in2": float(m.group(2)),
        "loc_win": float(m.group(3)), "loc_in2": float(m.group(4)),
        "motor_no": int(m.group(5)), "motor_in2": float(m.group(6)),
        "boat_no": int(m.group(7)), "boat_in2": float(m.group(8)),
    }


def parse_b(text: str, ymd: str):
    """番組表ファイル全体 -> race dictのリスト"""
    races = []
    for vcode, lines in split_venues(text, "B"):
        norm = [zen2han(l) for l in lines]
        heads = [i for i, l in enumerate(norm) if RE_B_HEAD.match(l)]
        for hi, h in enumerate(heads):
            m = RE_B_HEAD.match(norm[h])
            rno = int(m.group(1))
            end = heads[hi + 1] if hi + 1 < len(heads) else len(lines)
            racers = []
            for j in range(h + 1, end):
                ln = zen2han(lines[j])
                if re.match(r"^[1-6] \d{4}", ln):
                    r = parse_b_racer_line(lines[j])
                    if r:
                        racers.append(r)
            if len(racers) == 6:
                races.append({
                    "date": ymd, "venue": vcode, "race_no": rno,
                    "race_type": m.group(2), "distance": int(m.group(3)),
                    "deadline": m.group(4), "racers": racers,
                })
    return races


# ---------------- K(競走成績) ----------------
RE_K_HEAD = re.compile(r"^\s*(\d{1,2})R\s+(\S+)")
RE_K_ROW = re.compile(
    r"^\s*(0[1-6]|F|L[01]?|K[01]|S[0-2])\s+"   # 着順/異常コード
    r"([1-6])\s+(\d{4})\s+"                      # 艇番 登番
    r"(\S+(?:\s\S+)*?)\s+"                       # 選手名(空白含む可能性, 非貪欲)
    r"(\d{1,3})\s+(\d{1,3})\s+"                  # モーター ボート
    r"(\d\.\d\d|\.)\s+"                          # 展示タイム
    r"([1-6])\s+"                                 # 進入コース
    r"(F?\s*\.?\d{2}|F\.\d{2}|L\.\d{2}|\d\.\d{2}|\.)\s*"  # ST
    r"(\d\.\d\d\.\d|\.)?"                         # レースタイム
)
RE_PAY_3T = re.compile(r"3連単\s+(\d)-(\d)-(\d)\s+([\d,]+)")
RE_PAY_2T = re.compile(r"2連単\s+(\d)-(\d)\s+([\d,]+)")
RE_PAY_3F = re.compile(r"3連複\s+(\d)[-=](\d)[-=](\d)\s+([\d,]+)")
RE_KIMARITE = re.compile(r"(逃げ|差し|まくり差し|まくり|抜き|恵まれ)")


def _st_to_float(s: str):
    s = s.replace(" ", "")
    if s in (".", ""):
        return None
    if s.startswith("F"):
        v = s[1:].lstrip(".")
        try:
            return -float("0." + v)
        except ValueError:
            return None
    if s.startswith("L"):
        return None
    try:
        if s.startswith("."):
            return float("0" + s)
        return float(s)
    except ValueError:
        return None


def parse_k(text: str, ymd: str):
    """競走成績ファイル全体 -> race dictのリスト"""
    races = []
    for vcode, lines in split_venues(text, "K"):
        norm = [zen2han(l) for l in lines]
        heads = []
        for i, l in enumerate(norm):
            m = RE_K_HEAD.match(l)
            # 払戻行(単勝/2連単等)をヘッダ誤認しないよう, 選手行が直後に続くもののみ
            if m and ("H1" in l or "H 1" in l or re.search(r"H\d{3,4}m", l)):
                heads.append(i)
        for hi, h in enumerate(heads):
            m = RE_K_HEAD.match(norm[h])
            rno = int(m.group(1))
            end = heads[hi + 1] if hi + 1 < len(heads) else len(norm)
            block = norm[h:end]
            rows = []
            for ln in block[1:]:
                rm = RE_K_ROW.match(ln)
                if rm:
                    pos_raw = rm.group(1)
                    pos = int(pos_raw) if pos_raw.isdigit() else None
                    rows.append({
                        "pos": pos, "abnormal": None if pos else pos_raw,
                        "lane": int(rm.group(2)), "toban": int(rm.group(3)),
                        "motor_no": int(rm.group(5)), "boat_no": int(rm.group(6)),
                        "ex_time": None if rm.group(7) == "." else float(rm.group(7)),
                        "course": int(rm.group(8)),
                        "st": _st_to_float(rm.group(9)),
                    })
                if len(rows) == 6:
                    break
            blob = "\n".join(block)
            p3t = RE_PAY_3T.search(blob)
            p2t = RE_PAY_2T.search(blob)
            p3f = RE_PAY_3F.search(blob)
            km = RE_KIMARITE.search(blob)
            wind = re.search(r"風\s*\S*\s*(\d+)m", blob)
            wave = re.search(r"波\s*(\d+)cm", blob)
            races.append({
                "date": ymd, "venue": vcode, "race_no": rno,
                "rows": rows,
                "kimarite": km.group(1) if km else None,
                "wind": int(wind.group(1)) if wind else None,
                "wave": int(wave.group(1)) if wave else None,
                "pay_3t": {"combo": f"{p3t.group(1)}-{p3t.group(2)}-{p3t.group(3)}",
                           "amount": int(p3t.group(4).replace(",", ""))} if p3t else None,
                "pay_2t": {"combo": f"{p2t.group(1)}-{p2t.group(2)}",
                           "amount": int(p2t.group(3).replace(",", ""))} if p2t else None,
                "pay_3f": {"combo": f"{p3f.group(1)}={p3f.group(2)}={p3f.group(3)}",
                           "amount": int(p3f.group(4).replace(",", ""))} if p3f else None,
            })
    return races

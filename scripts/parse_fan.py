"""fan手帳データ(選手期別成績, fanYYMM.lzh)パーサ.

公式レイアウト: https://www.boatrace.jp/owpc/pc/extra/data/layout.html
1行416バイト固定長 cp932。2014後期以降のフォーマット(末尾に出身地6桁)。
検証済み(fan2510全1625行): コース進入回数 = 着順1-6合計 + F + L1 + S1 + S2
(L0/K0/K1/S0は進入回数に含まれない)。
"""
import io
import sys
import csv

# (項目名, バイト数, 型) 型: s=文字列, i=整数, f2=小数2桁, f1=小数1桁
LAYOUT = [
    ("toban", 4, "i"), ("name_kanji", 16, "s"), ("name_kana", 15, "s"),
    ("shibu", 4, "s"), ("cls", 2, "s"), ("era", 1, "s"), ("birth", 6, "s"),
    ("sex", 1, "i"), ("age", 2, "i"), ("height", 3, "i"), ("weight", 2, "i"),
    ("blood", 2, "s"), ("win_rate", 4, "f2"), ("in2_rate", 4, "f1"),
    ("n1", 3, "i"), ("n2", 3, "i"), ("starts", 3, "i"),
    ("yushutsu", 2, "i"), ("yusho", 2, "i"), ("avg_st", 3, "f2"),
]
for c in range(1, 7):
    LAYOUT += [
        (f"c{c}_n", 3, "i"), (f"c{c}_in2", 4, "f1"),
        (f"c{c}_st", 3, "f2"), (f"c{c}_st_rank", 3, "f2"),
    ]
LAYOUT += [
    ("cls_prev", 2, "s"), ("cls_prev2", 2, "s"), ("cls_prev3", 2, "s"),
    ("noryoku_prev", 4, "f2"), ("noryoku_now", 4, "f2"),
    ("year", 4, "i"), ("ki", 1, "i"),
    ("calc_from", 8, "s"), ("calc_to", 8, "s"), ("yosei_ki", 3, "i"),
]
for c in range(1, 7):
    for p in range(1, 7):
        LAYOUT.append((f"c{c}_p{p}", 3, "i"))
    for ev in ("f", "l0", "l1", "k0", "k1", "s0", "s1", "s2"):
        LAYOUT.append((f"c{c}_{ev}", 2, "i"))
LAYOUT += [
    ("nc_l0", 2, "i"), ("nc_l1", 2, "i"), ("nc_k0", 2, "i"), ("nc_k1", 2, "i"),
    ("birthplace", 6, "s"),
]

ROW_BYTES = sum(n for _, n, _ in LAYOUT)
assert ROW_BYTES == 416, ROW_BYTES


def _conv(v, t):
    v = v.strip()
    if t == "s":
        return v.replace("\u3000", " ").strip()
    if not v:
        return None
    if t == "i":
        return int(v)
    if t == "f2":
        return int(v) / 100.0
    if t == "f1":
        return int(v) / 10.0
    return v


def parse_line(b: bytes) -> dict:
    d = {}
    pos = 0
    for name, n, t in LAYOUT:
        d[name] = _conv(b[pos:pos + n].decode("cp932"), t)
        pos += n
    return d


def parse_file(data: bytes):
    rows = []
    for line in data.split(b"\r\n"):
        if len(line) == ROW_BYTES:
            rows.append(parse_line(line))
    return rows


def parse_lzh(path: str):
    import lhafile
    f = lhafile.Lhafile(path)
    name = f.infolist()[0].filename
    return parse_file(f.read(name))


if __name__ == "__main__":
    path = sys.argv[1]
    rows = parse_lzh(path) if path.lower().endswith(".lzh") else parse_file(open(path, "rb").read())
    out = sys.argv[2] if len(sys.argv) > 2 else None
    cols = [n for n, _, _ in LAYOUT]
    w = csv.DictWriter(open(out, "w", newline="", encoding="utf-8") if out else sys.stdout,
                       fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow(r)
    print(f"parsed {len(rows)} racers", file=sys.stderr)

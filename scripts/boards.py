# -*- coding: utf-8 -*-
"""3連単の板(全120通りのオッズ)を取得時刻つきで残す(2026-10-07)。

目的: 将来「締切直前のオッズをモデルの確率に合成する」効果を測るには、買い目(上位10点)だけでなく
板の全体が「締切の何分前の値か」と一緒に要る。fetch_odds / fetch_t3 は全120通りを取っているのに、
当日ファイル(docs/predictions)には買い目の分しか残らない(update_all._axis_from_odds)。
ここでは取ったばかりの板をそのまま1行ずつ追記する。通信は増やさない(今取っている板を記録するだけ)。

書く先: data/boards/YYYYMMDD.jsonl(1行1板。追記。フォルダが無ければ作る)。
行: {"t": "HH:MM:SS", "v": 会場, "r": レース, "k": 種別, "m": 締切までの分(整数。締切後は負), "o": [120個]}
  o の並びは scripts/model_v2.PERMS(a, b, c の3重ループ順。COMBO_STR の "1-2-3" の形)。無い買い目は null。
  並びはここで自前に作る(model_v2 は lightgbm を読み込むので、開催中の処理をそれに依存させない)。
  同じ並びであることは tests/test_boards.py で確かめる。
  k: "morning"(朝の暫定。update_all.do_morning_odds)、"pre"(締切前の通常取得。do_odds で final が立たない時)、
     "final"(締切後。do_odds で final が立つ時)、"judge"(打刻の判定直前の取り直し。_make_refresher)。
  m は書いた側(update_all)が取得した時点の時計で測る(締切後は負。floor なので締切の直後は -1)。
重複: 同じ (ymd, v, r, k) で直前に書いた板と o が同じなら書かない(このモジュール内の「最後に書いた板」の覚え。
  プロセスをまたぐ重複は許容する)。"final" は1レース1回まで。
日付: 取得時刻 now の日が ymd と違えば書かない(行の t は時刻だけで日を持たないので、日付をまたいだ実行の板を
  前日のファイルに混ぜない。開催中ループは 23:30 で止まるので本番では起きない)。
失敗しても例外を外に出さない(ログ1行)。開催中の処理(update_all)を板の記録のために止めない。

大きさの目安: 1行約700バイト(120個の倍率を詰めて書く)。1日約160レース × (pre 1〜数回 + final 1 + judge 若干)
≒ 400〜800行 → 1日 300〜600KB。git に入れる(commit は周回末尾の commit_push。auto-update.yml 側で
data/boards を add する)。
"""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))
KINDS = ("morning", "pre", "final", "judge")

# 120通りの並び = a, b, c の3重ループ順(model_v2.PERMS / COMBO_STR と同じ。先頭 "1-2-3"、末尾 "6-5-4")
COMBOS = ["%d-%d-%d" % (a, b, c) for a in range(1, 7) for b in range(1, 7) if b != a
          for c in range(1, 7) if c not in (a, b)]
_POS = {c: i for i, c in enumerate(COMBOS)}

_LAST = {}          # (ymd, v, r, k) -> 直前に書いた o(同じ板を続けて書かないため)
_FINAL_DONE = set()  # (ymd, v, r): "final" を書いたレース(1レース1回まで)


def reset():
    """覚えを消す(テスト用。本番は1プロセス1実行なので呼ぶ必要がない)。"""
    _LAST.clear()
    _FINAL_DONE.clear()


def board_list(t3):
    """{"1-2-3": 9.9, ...}(fetch_t3 / fetch_odds の t3)→ 120個の list(COMBOS の順)。
    無い買い目・数に直せない値・有限でない値は None。dict でなければ全部 None。"""
    o = [None] * len(COMBOS)
    if not isinstance(t3, dict):
        return o
    for c, val in t3.items():
        i = _POS.get(str(c))
        if i is None or val is None or isinstance(val, bool):
            continue
        try:
            x = float(val)
        except (TypeError, ValueError):
            continue
        if math.isfinite(x):
            o[i] = x
    return o


def record(ymd, code, rno, t3, kind, mins, now=None, root=None) -> bool:
    """1板を1行追記する。書いたら True。重複・空の板・日付違い・失敗は False(例外は出さない)。
    ymd: "YYYYMMDD"。code / rno: 会場・レース番号。t3: {"1-2-3": 倍率, ...}。kind: KINDS のどれか。
    mins: 締切までの分(小数可。None 可)。now: 取得した時刻(無ければ今・JST)。
    root: リポジトリの根(update_all はテストで差し替えられる自分の ROOT を渡す。無ければこのファイルから引く)。"""
    try:
        now = now or datetime.now(JST)
        ymd = str(ymd)
        today = now.strftime("%Y%m%d")
        if today != ymd:
            print(f"boards: skip {ymd} {code}-{rno}R {kind} (now is {today})", flush=True)
            return False
        if kind not in KINDS:
            print(f"boards: skip {ymd} {code}-{rno}R: unknown kind {kind!r}", flush=True)
            return False
        v, r = int(code), int(rno)
        o = board_list(t3)
        if all(x is None for x in o):
            return False
        key = (ymd, v, r, kind)
        if kind == "final" and key[:3] in _FINAL_DONE:
            return False
        if _LAST.get(key) == o:
            return False
        m = None if mins is None else int(math.floor(float(mins)))
        row = {"t": now.strftime("%H:%M:%S"), "v": v, "r": r, "k": kind, "m": m, "o": o}
        d = Path(root or ROOT) / "data" / "boards"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{ymd}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        _LAST[key] = o
        if kind == "final":
            _FINAL_DONE.add(key[:3])
        return True
    except Exception as e:
        print(f"boards: record failed {ymd} {code}-{rno}R {kind} ({type(e).__name__}: {e})", flush=True)
        return False

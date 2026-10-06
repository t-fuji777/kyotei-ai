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
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# 厳選(sengen)判定: 買い目は3連単TOP3の3点(300円)に一本化。
# 条件は (a) picks上位3点のp合計(top3p) >= SENGEN_TOP3P_MIN
#        (b) 除外会場でない
#        (c) 上位3点すべてオッズ >= SENGEN_MIN_ODDS(300円買っても損にならない水準)
#        (d) レース番号 >= SENGEN_MIN_RNO(5R以降。1-4Rはモデルの高確率帯が信用できない
#            ことが較正で実証されているため、竹・松ともに対象外)。
# 7339レースのバックテストで決定した固定閾値。かつての2段構成(通常0.48/超厳選0.58、
# 5点買い基準)は検証の結果廃止し、3点買いのtop3p基準に一本化した。
SENGEN_TOP3P_MIN = 0.36
SENGEN_MIN_ODDS = 3.1
SENGEN_EXCLUDE_VENUES = frozenset({3, 4, 14})  # 江戸川/平和島/鳴門(荒れ水面)
SENGEN_MIN_RNO = 5  # 1-4Rはモデル過大評価のため両プラン(竹/松)対象外

# モデルの世代(gen)ごとのしきい値。1 = 現行(二値3本 + 3段の式)、2 = 作り直した候補
# (条件づけ分解ロジット1本。確率の目盛りが上に伸びるので、同じ 0.36 だと厳選が1日約12件に増える)。
# 上の定数はそのまま世代1の値として使う。レースごとに「その買い目を作ったモデルの世代」を
# race["g"] に持ち(無ければ世代1)、しきい値と較正表はレースの g で引く。同じ日に世代の違う
# 買い目が混ざっても(朝の作り直しの前後、予備のモデルで動いた実行機など)、各レースは自分の
# 世代の数字で判定される。画面(docs/index.html)にも同じ表の写しを持つ。
# 世代2の top3p_min / cand_top4p_min は仮の値(件数を今とそろえる方針)。実験の候補モデルでは検証期間で
# 0.457 だったが、本番のコードで学習し直したモデルでは件数がそろう値は 0.451(試験期間の厳選は
# 0.45 で 1.72件/日・的中 54.7%、0.46 で 1.37件/日・54.8%。現行 0.36 は 1.82件/日・54.1%)。
# cand_top4p_min も 0.46 では取り直しの対象が 7.1件/日(現行 12.7件/日)に減る(そろえるなら 0.43)。
# 毎朝の学習し直しで ±0.006 程度動くので、切り替え(段階B)の直前に dry_run の model_report で
# 確かめてから確定する(持ち主の判断。この値を変えれば画面 docs/index.html の写しも同じ値にする)。
# min_odds / min_rno / exclude_venues は世代で変えない。
# JSON に書けるよう値は list / 数値だけにする(frozenset は使わない)。
SENGEN_CFG_BY_GEN = {
    1: {"top3p_min": SENGEN_TOP3P_MIN, "min_odds": SENGEN_MIN_ODDS, "min_rno": SENGEN_MIN_RNO,
        "exclude_venues": sorted(SENGEN_EXCLUDE_VENUES), "cand_top4p_min": 0.36},
    2: {"top3p_min": 0.45, "min_odds": SENGEN_MIN_ODDS, "min_rno": SENGEN_MIN_RNO,
        "exclude_venues": sorted(SENGEN_EXCLUDE_VENUES), "cand_top4p_min": 0.43},
}

_WARNED = set()


def _warn_once(msg):
    """同じ警告を1プロセスで1回だけ出す(レースごとの判定の中から呼ぶので、毎回出すと周回のログが埋まる)。"""
    if msg not in _WARNED:
        _WARNED.add(msg)
        print(msg, flush=True)


def race_gen(race) -> int:
    """レースの買い目を作ったモデルの世代。印(race["g"])が無い・読めないレースは世代1
    (印を付け始める前の日のファイルと同じ扱い)。"""
    try:
        g = (race or {}).get("g")
    except Exception:
        return 1
    if g is None:
        return 1
    try:
        return int(g)
    except Exception:
        return 1


def sengen_cfg_for(gen_or_race):
    """世代の番号、またはレース(dict。race_gen で世代を読む)から、その世代のしきい値を返す。
    知らない世代は世代1の値に落とす(現行の動きを変えない側)。警告は1回だけ出す。
    戻り値は写し(呼び出し側が書き足しても表は汚れない)。"""
    gen = race_gen(gen_or_race) if isinstance(gen_or_race, dict) else gen_or_race
    try:
        gen = int(gen)
    except Exception:
        gen = 1
    cfg = SENGEN_CFG_BY_GEN.get(gen)
    if cfg is None:
        _warn_once(f"sengen_cfg: unknown model gen {gen!r}; using gen 1 thresholds")
        cfg = SENGEN_CFG_BY_GEN[1]
    return dict(cfg)


# 当日予測バッジ(注目/様子見)の実績記録(2026-09-02開始): 較正済みTOP5的中率
# (docs/calib.json、フロントのcalT5と同一式)が BADGE_MIN_CAL 以上なら注目(att=1)、
# 未満なら様子見(att=0)。バッジはライブ再予測で日中に動き得るため、T-15打刻時に
# race["att"]へ焼き込み、実績集計(att_*/yos_*)は打刻値のみを使う(事後集計は後出しになる)。
BADGE_MIN_CAL = 40

# フロント内蔵のフォールバック較正表(index.htmlの_CAL_FALLBACKと同一値)。
# calib.jsonが読めない場合のみ使い、バッジ判定がフロント表示とズレないようにする。
_CAL_T5E = [[7.5, 11.2], [17.5, 20.8], [22.5, 26.1], [27.5, 32.9], [32.5, 39.6],
            [37.5, 43.9], [42.5, 43.9], [47.5, 43.9], [75, 57.7]]
_CAL_T5L = [[7.5, 4.6], [17.5, 17.8], [22.5, 21.3], [27.5, 30.3], [32.5, 37.9],
            [37.5, 43.9], [42.5, 52.2], [47.5, 56.7], [75, 56.7]]
_CALIB_CACHE = None


def _load_calib():
    """docs/calib.json をプロセスごとに1回だけ読む。読めなければ空(内蔵の表に落ちる)。"""
    global _CALIB_CACHE
    if _CALIB_CACHE is None:
        try:
            import json as _json
            from pathlib import Path as _Path
            _CALIB_CACHE = _json.loads(
                (_Path(__file__).resolve().parent.parent / "docs" / "calib.json")
                .read_text(encoding="utf-8"))
        except Exception:
            _CALIB_CACHE = {}
    return _CALIB_CACHE if isinstance(_CALIB_CACHE, dict) else {}


def _calib_tbl(key, gen=1):
    """較正表を世代で引く。順に calib.json の gens[str(gen)][key] → トップレベルの key(現在の
    世代の表) → 内蔵の表。世代の表が要るのに無い時(世代2の表がまだ無い、gens があるのに
    その世代が無い)は黙って落とさず、ログに1行出してからトップレベルへ落ちる。
    世代1で calib.json に gens が無い(今の形)のは正常なので何も出さない。"""
    cal = _load_calib()
    try:
        g = str(int(gen))
    except Exception:
        g = "1"
    gens = cal.get("gens")
    gens = gens if isinstance(gens, dict) else {}
    per_gen = gens.get(g)
    tbl = per_gen.get(key) if isinstance(per_gen, dict) else None
    if tbl:
        return tbl
    if g != "1" or gens:
        _warn_once(f"calib: no table for gen {g} key {key}; falling back to the top-level table")
    tbl = cal.get(key)
    return tbl if tbl else {"t5e": _CAL_T5E, "t5l": _CAL_T5L}[key]


def cal_pct(p, tbl):
    """フロントのcalPct(index.html)と同一の折れ線補間。丸めはJSのMath.round
    (0.5は常に切り上げ)に合わせてfloor(x+0.5)を使う(Pythonのround()は偶数丸めでズレる)。"""
    import math
    x = p * 100.0
    if x <= tbl[0][0]:
        return math.floor(tbl[0][1] * x / tbl[0][0] + 0.5)
    for i in range(1, len(tbl)):
        if x <= tbl[i][0]:
            a, b = tbl[i - 1], tbl[i]
            return math.floor(a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]) + 0.5)
    return math.floor(tbl[-1][1] + 0.5)


def badge_attention(race):
    """注目=1/様子見=0。フロントのactLbl(calT5>=40。1-4Rはt5e表、5R以降はt5l表)と同一判定。
    較正表はレースの世代(race["g"]。無ければ1)の表を使う: 世代が違えば確率の目盛りが違うので、
    同じ生の確率でも実際の的中率が違う。"""
    top5p = sum((p.get("p") or 0) for p in (race.get("picks") or [])[:5])
    tbl = _calib_tbl("t5e" if (race.get("no") or 12) <= 4 else "t5l", race_gen(race))
    return 1 if cal_pct(top5p, tbl) >= BADGE_MIN_CAL else 0


# 締切時の払戻の目安(2026-10-02導入・表示専用で選定には使わない)。
# 判定時(T-15)のオッズは締切までに動く。厳選相当レース(確率条件を満たした上位3点)の
# 2026-08-05〜10-01の実績231件から、判定時オッズ帯ごとに「締切時÷判定時」の比の
# 25%点と75%点を求めたもの。意味は「過去の半分はこの幅に収まり、4回に1回はこれより
# 下がった」。保証ではない。帯は各26〜61件になるよう区切った(細かく切ると件数不足で
# 帯の境目で目安が逆転するため、5倍未満と15倍以上はまとめている)。
# 帯の境目で目安が段差にならないよう、各帯の代表オッズを結ぶ折れ線で補間する。
# (帯の代表オッズ, 25%点, 75%点) 元の帯: 5倍未満/5〜7/7〜10/10〜15/15倍以上
DRIFT_POINTS = [
    (4.0, 0.85, 1.13),
    (6.0, 0.74, 0.98),
    (8.5, 0.62, 0.86),
    (12.5, 0.61, 0.90),
    (20.0, 0.40, 0.83),
]


def drift_range(o):
    """判定時オッズo → [締切時の目安の下限, 上限](小数1桁)。"""
    pts = DRIFT_POINTS
    if o <= pts[0][0]:
        k_lo, k_hi = pts[0][1], pts[0][2]
    elif o >= pts[-1][0]:
        k_lo, k_hi = pts[-1][1], pts[-1][2]
    else:
        for (x0, l0, h0), (x1, l1, h1) in zip(pts, pts[1:]):
            if x0 <= o <= x1:
                t = (o - x0) / (x1 - x0)
                k_lo, k_hi = l0 + (l1 - l0) * t, h0 + (h1 - h0) * t
                break
    return [round(o * k_lo, 1), round(o * k_hi, 1)]


def sengen_top3p(picks):
    return sum((p.get("p") or 0) for p in (picks or [])[:3])


def is_sengen(top3p, venue, rno, cfg=None):
    """厳選の確率条件(top3p のしきい値・除外会場・5R以降)。cfg(sengen_cfg_for の戻り値)が
    あればその値、無ければ世代1の定数(今までと同じ)。cfg に欠けた項目も世代1の値で補う
    (古い形の sengen_cfg を渡された時のため)。"""
    try:
        if cfg is None:
            return (top3p >= SENGEN_TOP3P_MIN and int(venue) not in SENGEN_EXCLUDE_VENUES
                    and int(rno) >= SENGEN_MIN_RNO)
        ex = cfg.get("exclude_venues")
        if ex is None:
            ex = SENGEN_EXCLUDE_VENUES
        return (top3p >= cfg.get("top3p_min", SENGEN_TOP3P_MIN)
                and int(venue) not in ex
                and int(rno) >= cfg.get("min_rno", SENGEN_MIN_RNO))
    except Exception:
        return False


# プレミア(matsu)判定: 厳選のさらに上位ティア。買い目は3連単TOP4の4点(400円)。
# 条件は (a) picks上位4点のp合計(top4p) >= MATSU_TOP4P_MIN
#        (b) 除外会場でない(厳選と共通のSENGEN_EXCLUDE_VENUES)
#        (c) 上位4点すべてオッズ取得済み(Noneが1つでもあれば対象外。厳選と異なりオッズは必須。
#            オッズ帯=市場の同意 が選定シグナルそのものであるため)
#        (d) 全4点のオッズが4.1倍以上10.0倍以下の帯に収まる(モデルの確信と市場の評価が
#            一致する水準=コンセンサス)
#        (e) レース番号 >= SENGEN_MIN_RNO(5R以降。厳選と共通の理由で1-4Rは対象外)。
# 7339レースのバックテスト+時系列分割検証で決定した固定閾値(検証期正解率52〜58%)。
MATSU_TOP4P_MIN = 0.36
MATSU_MIN_ODDS = 4.1
MATSU_MAX_ODDS = 10.0


def matsu_top4p(picks):
    return sum((p.get("p") or 0) for p in (picks or [])[:4])


def is_matsu(top4p, venue, rno):
    try:
        return (top4p >= MATSU_TOP4P_MIN and int(venue) not in SENGEN_EXCLUDE_VENUES
                and int(rno) >= SENGEN_MIN_RNO)
    except Exception:
        return False


# 厳選を締切15分前に確定(打刻)する方式を始めた日。この日以降、打刻の無いレースは厳選の実績に数えない。
# それより前の日は打刻が無いので、集計の時に条件を当てはめて数える(当時の方式)。
# この日以降も「打刻が無ければ後から条件を当てはめる」ままだと、周回が丸1日止まった時に、
# 誰にも知らせていないレースが翌朝の集計で厳選として実績に入ってしまう。
SENGEN_STAMP_FROM = "20260804"


def sengen_counts(r: dict) -> bool:
    """確定済み(tk が焼き込まれた)レースを、厳選の実績に数えるか。
    締切後に判定したレース(ph=1)は数えない: 判定の時点ではもう買えなかったので、実績にすると
    「後から選んだ」のと区別がつかない(2026-08-04 桐生6R はレース後に打刻、08-28 尼崎6R は
    締切の約2時間後に打刻されていた。2026-10-05 に数え方を直した)。"""
    return bool(r.get("tk")) and not r.get("ph")


def sengen_picks(r: dict) -> list:
    """厳選の的中を数える買い目(上位3点)。確定した時点の買い目で数える。
    os(判定時のオッズ)は確定時の買い目の上位4点をその順で持っているので、あればそのキー順を使う。
    確定の後に買い目が差し替わったレースを、差し替え後の目で的中にしないため(2026-08-13 大村8R は
    確定の5分後に買い目が替わり、替わった後の目で的中と数えていた。確定後の差し替えは 9/2 頃から
    起きない作りになっている)。os が無い古い記録は、保存されている買い目の上位3点。"""
    osd = r.get("os")
    if isinstance(osd, dict) and len(osd) >= 3:
        return list(osd.keys())[:3]
    return [p["c"] for p in (r.get("picks") or [])][:3]


def stamp_plans(race, vcode, res=None, now_hhmm=None, late=False):
    """竹/松の該当可否を確定し、レースオブジェクト直下のrace["tk"]/race["mt"]
    (1 or 0)へ焼き込む。目的: 確定後にpicks/oddsがパイプラインの競合(rebase -X theirs等)
    で巻き戻っても、その時点で成立していた判定が遡及改変されないよう固定するため。

    first-wins: race["tk"]が既に存在する場合は何もしない(二度と再計算・上書きしない。
    締切15分前のチェックポイントで確定させ、以後は不変とするため)。
    now_hhmmを渡せばrace["pt"]に確定時刻("HH:MM")も記録する。

    判定は既存ルール(is_sengen, update_results._picks_okと同一)を
    その時点のrace(picks/odds/no)とres(order/pay3t、任意)から再現する。
    しきい値(top3p_min / min_odds / min_rno / exclude_venues)と注目の較正表は、レースの
    世代 race["g"](買い目を作ったモデルの世代。無ければ1)で引く(sengen_cfg_for / badge_attention)。
    - resを渡した場合(結果確定後のフォールバック呼び出し): 的中買い目(c==order)は
      pay3t/100(確定実配当)を優先してオッズ判定する。
    - res無し(締切15分前チェックポイントの呼び出し): 取得済みt3オッズのみで判定する。
    resにorderが無い(中止/不成立等のstatusのみ)場合は何もしない(tk/mtを設定しない)。

    松プランは2026-09-01で終売: 新規レースのmtは常に0を焼き込む(キー自体を省くと
    集計側の動的フォールバックが旧ルールで松を復活させてしまうため、明示的な0で封じる)。
    is_matsu等の定義は過去分(〜2026-08)の再集計用に残している。終売の経緯: T-15判定への
    移行で判定時の板が締切板より系統的に高くなり、締切後オッズで較正された帯4.1〜10.0が
    実質成立不能になった(候補の94%が帯超えでカット、選定0.15件/日)。

    見送り理由(rs)の焼き込み: 確率条件(top3pの閾値・5R以降・除外会場以外。
    is_sengenがTrue)は通るがオッズ条件で不成立となった準候補には、tk確定と
    同時にrace["rs"]へ日本語の短い理由文字列を焼き込む(tkと同じくfirst-wins。
    raceに既に"rs"があれば上書きしない)。tkが成立(1)した場合や、確率条件
    自体を満たさないレースにはrsを書かない。
    - 理由: 上位3点の実効オッズに3.1倍未満があれば「3.1倍未満 {最小値}倍」。
      3.1倍未満は無いがオッズを取得できていない買い目があれば「オッズ未取得」(成立させない)。
    数値は取得オッズ(生のt3値)を小数1桁で表記する。ただし的中買い目のオッズが
    pay3t由来の実効値に置き換わり、その実効値が閾値割れの原因である場合のみ実効値を使う
    (eff_oddsの返り値をそのまま用いる)。"""
    if "tk" in race:
        return
    if res is not None and not res.get("order"):
        return
    rno = race.get("no")
    picks = [p["c"] for p in (race.get("picks") or [])]
    t3 = (race.get("odds") or {}).get("t3") or {}
    order = res.get("order") if res else None
    pay = res.get("pay3t") if res else None

    def eff_odds(c):
        # 実効オッズ: 的中買い目(c==order)はpay3t/100(確定実配当)を優先し、
        # それ以外(pay3t欠落時・res無し時も含む)は取得済みt3オッズをそのまま使う。
        if pay and c == order:
            return pay / 100.0
        return t3.get(c)

    # しきい値はレースの世代(race["g"]。無ければ1)で引く。引数は増やさない: 手動用の古い経路
    # (update_live.py / fetch_results_only.py)や過去3日の取りこぼし回収(update_all._carryover)も、
    # 渡されたレースの印だけで自動的に正しい世代の数字で判定される。
    cfg = sengen_cfg_for(race)
    min_odds = cfg.get("min_odds", SENGEN_MIN_ODDS)
    top3p = sengen_top3p(race.get("picks") or [])
    take_quasi = is_sengen(top3p, vcode, rno, cfg)
    tk = 0
    take_below = []  # 竹: 実効オッズが下限3.1倍未満だったピックのeff_odds値
    take_missing = False  # 上位3点のどれかのオッズが取得できていない
    if take_quasi:
        ok = True
        for c in picks[:3]:
            o = eff_odds(c)
            if o is None:
                # オッズが無い買い目は「3.1倍以上」を確かめられないので成立させない。
                # 以前は無いものを素通りさせており、周回が止まってオッズを1枚も取れなかった
                # レースが、確かめないまま厳選になっていた(2026-08-28 尼崎6R・10R)。
                ok = False
                take_missing = True
            elif o < min_odds:
                ok = False
                take_below.append(o)
        tk = 1 if ok else 0
    race["tk"] = tk
    # 判定時点で取得済みだった生オッズを race["os"] へ残す。
    # odds.t3 は締切後にも再取得され確定オッズで上書きされるため、後から odds.t3 で
    # 選定を再現すると実運用より甘い結果になる(実測で締切前の板は確定板より中央値
    # 1.6倍高い)。判定時の板を残しておけば公開実績を後から正しく再検証できる。
    # 代入は race["tk"] の後に置く: 先に置くと途中で例外が出たとき「osはあるがtkが無い」
    # 状態になり、次回呼び出しがfirst-winsガードを素通りしてosを上書きしてしまう。
    # 既にodds.finalが立っている(=締切後に確定板へ取り直された)場合は判定時点の板
    # ではないので焼かない。この場合は消費側が従来通り odds.t3 にフォールバックする。
    if "os" not in race and not (race.get("odds") or {}).get("final"):
        race["os"] = {c: t3.get(c) for c in picks[:4]}

    # 松プランは終売(2026-09-01)。新規レースは常にmt=0を焼き込む。
    race["mt"] = 0

    # 締切後の判定には印を付ける(ph=1)。締切15分前の確定に間に合わなかったレースで、
    # 締切後の板や実配当を使うなど締切前の確定とは条件が違い、判定時点ではもう買えない
    # ため、記録上で区別できるようにする(2026-10-02。朝の周回の起動遅れで1日4〜11件
    # 発生していた)。res あり=結果確定後のフォールバック、late=締切を過ぎてからの打刻
    # (結果はまだ出ていないが、締切は過ぎている)。
    if res is not None or late:
        race["ph"] = 1
    # 厳選として確定したレースには、締切時の払戻の目安を焼き込む(表示専用)。
    # 事後判定のレースには付けない(既に結果が出ている)。
    elif tk == 1 and not (race.get("odds") or {}).get("jt"):
        # 目安の幅(DRIFT_POINTS)は、締切の約50分前の板から締切までの変動で作ったもの。
        # 判定の直前に取り直した板(odds.jt あり。2026-10-06〜)には当てはまらない(変動はもっと小さい)
        # ので、付けない。取り直した板での実績が溜まったら、幅を作り直して再開する。
        pr = {c: drift_range(t3[c]) for c in picks[:3] if t3.get(c)}
        if pr:
            race["pr"] = pr

    # 当日予測バッジ(注目/様子見)も同時に焼き込む(first-winsはtkと共有)。
    # 実績表のバッジ別集計はこの打刻値だけを使う。
    race["att"] = badge_attention(race)

    if now_hhmm:
        race["pt"] = now_hhmm

    if tk != 1 and "rs" not in race:
        if take_quasi and take_below:
            # 文言の「3.1倍未満」は画面(index.html)が先頭一致で拾う。min_odds はどの世代も 3.1 なので
            # 固定の文字列のまま。世代で min_odds を変える時は、この文言と画面の両方を直すこと。
            race["rs"] = f"3.1倍未満 {min(take_below):.1f}倍"
        elif take_quasi and take_missing:
            race["rs"] = "オッズ未取得"
        elif race.get("qc") and not take_quasi:
            # 確率ドリフトによる脱落。朝に「候補」として画面に出したレースが、
            # 展示反映のライブ再予測でTOP3合計確率が閾値(世代1は0.36)を割り、打刻時には
            # 候補ですらなくなった場合。理由を書かないと候補が痕跡なく消え、
            # 「都合の悪いレースを無かったことにした」のと見分けがつかない
            # (2026-09-12の多摩川8Rで発覚。10日で朝の候補9件中2件が該当していた)。
            # qc/qp は do_stamps が毎周回で焼き込む「候補として公開した証跡」。
            race["rs"] = f"確率低下 {race['qp'] * 100:.0f}%→{top3p * 100:.0f}%"


def zen2han(s: str) -> str:
    """全角英数字を半角化(カナ・漢字は維持)"""
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


try:
    import requests as _rq
    _SESS = _rq.Session()
    _SESS.headers["User-Agent"] = UA
    _SESS.headers["Accept-Language"] = "ja,en;q=0.9"
    _SESS.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
except Exception:
    _SESS = None


def http_get(url: str, timeout=12, retries=2, sleep=1.0) -> bytes:
    last = None
    for i in range(retries):
        try:
            if _SESS is not None:
                r = _SESS.get(url, timeout=timeout)
                if r.status_code == 404:
                    raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
                r.raise_for_status()
                return r.content
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
RE_B_HEAD = re.compile(r"^\s*(\d{1,2})R\s+(.+?)\s+H(\d+)m\s*電話投票締切予定(\d{1,2}:\d{2})")
RE_B_TAIL = re.compile(
    r"^\s*(\d\.\d\d)\s*(\d{1,3}\.\d\d)"      # 全国勝率 全国2率
    r"\s*(\d\.\d\d)\s*(\d{1,3}\.\d\d)"        # 当地勝率 当地2率
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
        print(f"B_PRIMFAIL {raw_line[:40]!r} blen={len(b)} cb={[len(c.encode('cp932','replace')) for c in raw_line[6:18]]}", flush=True)
        mm = re.match(r"^([1-6])\s+(\d{4})(.+?)(\d{2})(\D+?)(\d{2})([AB][12])(.*)$", zen2han(raw_line))
        if not mm:
            return None
        try:
            lane=int(mm.group(1)); toban=int(mm.group(2)); name=mm.group(3).replace("　","").replace(" ","").strip(); age=int(mm.group(4)); branch=mm.group(5).replace("　","").strip(); weight=int(mm.group(6)); klass=mm.group(7); rest=mm.group(8)
        except (ValueError, IndexError):
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


RE_B_DAY = re.compile(r"\u7b2c\s*(\d+)\s*\u65e5")  # 第 N 日 (Nth day of the meeting)


def parse_b(text: str, ymd: str):
    """番組表ファイル全体 -> race dictのリスト"""
    races = []
    for vcode, lines in split_venues(text, "B"):
        norm = [zen2han(l) for l in lines]
        # day-of-meeting marker appears in the venue header (first ~10 lines)
        day_n = None
        for hl in norm[:10]:
            dm = RE_B_DAY.search(hl)
            if dm:
                day_n = int(dm.group(1))
                break
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
                    else:
                        print(f"B_FAIL {vcode}-{rno}R {lines[j][:90]!r} blen={len(lines[j].encode('cp932','replace'))}", flush=True)
            if len(racers) != 6:
                _cand = [zen2han(lines[j])[:16] for j in range(h+1, end) if zen2han(lines[j]).strip()[:1].isdigit()]
                print(f'PARSE_B_DROP {vcode}-{rno}R racers={len(racers)}/6 cand={_cand!r}', flush=True)
            if len(racers) == 6:
                races.append({
                    "date": ymd, "venue": vcode, "race_no": rno,
                    "race_type": m.group(2), "distance": int(m.group(3)),
                    "deadline": m.group(4), "day_n": day_n, "racers": racers,
                })
    return races


# ---------------- K(競走成績) ----------------
RE_K_HEAD = re.compile(r"^\s*(\d{1,2})R\s+(\S+)")
RE_K_ROW = re.compile(
    r"^\s*(0[1-6]|F|L[01]?|K[01]|S[0-2])\s+"   # 着順/異常コード
    r"([1-6])\s+(\d{4})\s+"                      # 艇番 登番
    r"(\S+(?:\s{1,6}\S+)*?)\s+"                  # 選手名(姓/名それぞれ均等割付パディングされており,
                                                   # 姓名とも1文字(例:堤/昇)だと姓名間に最大6文字分の
                                                   # 全角空白が入るため\s{1,4}では取りこぼす。非貪欲)
    r"(\d{1,3})\s+(\d{1,3})\s+"                  # モーター ボート
    r"(\d\.\d\d|\.)\s+"                          # 展示タイム
    r"([1-6])\s+"                                 # 進入コース
    r"(F?\d\.\d{2}|[FL]\s*\.\d{2}|L\d?|K\d?|\d\.\d{2}|\.)\s*"  # ST
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
        m = re.search(r"(\d*)\.(\d+)", s)
        if not m:
            return None
        try:
            return -float("0." + m.group(2))
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
            if len(rows) < 6:
                _cand = [ln[:24] for ln in block[1:] if re.match(r"^\s*(\d{2}|[FLKS]\S?)\s", ln)]
                print(f'PARSE_K_DROP {vcode}-{rno}R rows={len(rows)}/6 cand={_cand!r}', flush=True)
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

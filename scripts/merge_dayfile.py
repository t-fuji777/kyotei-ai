# -*- coding: utf-8 -*-
"""当日の予測ファイル(docs/predictions/YYYYMMDD.json)を JSON 単位で merge する。

使い方: python scripts/merge_dayfile.py <ours> <theirs> <out> [--latest docs/predictions/latest.json]
  ours   = 周回(auto-update.yml)の版。rebase -X theirs で勝った側(作業ツリーのファイル)
  theirs = 上流(origin/main)の版。daily の predict today が push した新しい予測
  out    = 書き先(ours と同じパスでよい)
  --latest = 当日ファイルの写し。存在して date が同じなら merge 結果をそのまま写す
             (predict_today.write / update_all.write が当日ファイルと同じ文字列を書くのと同じ)

なぜ要るか: 当日ファイルは1行 JSON なので git の行単位の merge では「どちらかの版が丸ごと勝つ」しか
できない。周回の commit_push は `git rebase -X theirs origin/main` で押し切る(再適用する側 = 周回の
ローカルコミットが勝つ)ため、daily が 7:00 以降にずれ込んだ日、daily が push した直後の周回が
自分の周回の先頭で同期した古い当日ファイル(前日モデルの買い目)で daily の予測を丸ごと上書きした
(2026-10-06 08:05 の daily update を 08:06 の auto results が巻き戻し、daily_gate が
「本日学習のモデルで作られていない」と判定して daily がもう1回フル実行された)。

規則(predict_today._merge_existing の「観測は引き継ぐ・打刻済み/live の買い目は差し替えない」と同じ向き):
  - 土台は theirs。トップレベルの date / generated_at / model_trained_at / model_gen / sengen_cfg は theirs。
    results_updated_at / odds_updated_at / live_updated_at / live_model_trained_at は新しい方(片方しか
    無ければある方)。live_model_trained_at を theirs 優先にすると、周回が本日学習のモデルで live 再予測した
    買い目を残しても印だけ daily が引き継いだ前日の値に戻り、healthcheck の W5 が誤って立つ。
  - レースは (会場 code, レース no) で対応づけ、theirs のレースに ours の観測項目(_OBSERVED_FIELDS)を
    重ねる。ours にあって theirs に無い項目は ours。両方にある時は項目ごとの規則(_pick_observed)。
  - 買い目一式(_PICK_FIELDS)は、ours が打刻済み(tk あり)なら ours、ours だけ live なら ours、
    両方 live なら live_at の新しい方(同じなら theirs)、それ以外は theirs。
  - ours にしか無いレース・会場は足す。theirs にしか無いものはそのまま。
壊れた入力(JSON でない・dict でない・date が違う)は何も書かず終了コード 2(呼び出し側は -X theirs の
結果のまま進む)。出力は ours/theirs と同じ 1行 JSON(json.dumps(ensure_ascii=False)、改行なし)。"""
import argparse
import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import race_gen

# 観測項目と買い目一式は predict_today と同じ並び(あちらが正)。ここに写すのは、merge_dayfile を
# 周回の bash から単独で呼ぶため(pandas などを読む predict_today を import しない)。
_OBSERVED_FIELDS = ("result", "odds", "st_ex", "ex", "weather", "wind", "wave",
                    "tk", "mt", "pt", "rs", "os", "att", "qc", "qp", "ph", "pr")
_PICK_FIELDS = ("picks", "boats", "conf", "fuku", "sengen", "live", "live_at", "g", "absent")
# 打刻系(first-wins・不変)。両方にあれば先に push された側(theirs)を残す
_STAMP_FIELDS = frozenset({"tk", "mt", "pt", "rs", "os", "att", "qc", "qp", "ph", "pr"})
# 展示系。情報量(艇数)の多い方
_RICH_FIELDS = frozenset({"st_ex", "ex", "weather", "wind", "wave"})
# トップレベルで「新しい方」を採る時刻。live_model_trained_at(直近の live 再予測に使ったモデルの学習時刻)は
# 周回だけが書く(predict_today --live)。daily の版の値は _merge_existing が引き継いだ写しなので新しい方でよい
_NEWER_KEYS = ("results_updated_at", "odds_updated_at", "live_updated_at", "live_model_trained_at")

EXIT_BAD_INPUT = 2


def _newer(ours, theirs):
    """時刻文字列("2026-10-06 08:22:47 JST" など同じ書式)の新しい方。片方しか無ければある方。
    同じ・比べられない時は theirs。"""
    if ours is None:
        return theirs
    if theirs is None:
        return ours
    if isinstance(ours, str) and isinstance(theirs, str) and ours > theirs:
        return ours
    return theirs


def _richness(v):
    """展示系の情報量。dict/list は要素数(= 艇数)、None は 0、それ以外のスカラー(風・波の数値)は 1。"""
    if v is None:
        return 0
    if isinstance(v, (dict, list, tuple)):
        return len(v)
    return 1


def _pick_observed(k, o, t):
    """観測項目 k が両方にある時にどちらを採るか。o = ours の値、t = theirs の値。"""
    if k in _STAMP_FIELDS:
        return t
    if k == "result":
        # order のある方。両方あれば theirs
        if isinstance(t, dict) and t.get("order"):
            return t
        if isinstance(o, dict) and o.get("order"):
            return o
        return t
    if k == "odds":
        # final が立っている方(両方なら theirs)。どちらも final でなければ、判定直前に取り直した板
        # (jt = 取り直した時刻。update_all._make_refresher が t3 を差し替えて付ける)のある方、両方にあれば
        # jt の新しい方(同じなら下へ)。周回が判定直前に取り直した板を daily が引き継いだ古い本オッズで
        # 戻さないため(次の do_odds で上書きされるが、その間は jt が消えて「取り直せなかった」に見える)。
        # それも無ければ、暫定(prov)でない本オッズを暫定より優先する(朝の全売りオッズを本オッズで置き換えた
        # 周回の取得を、daily が引き継いだ古い暫定で戻さないため)。それ以外は theirs
        if isinstance(t, dict) and t.get("final"):
            return t
        if isinstance(o, dict) and o.get("final"):
            return o
        t_jt = str(t.get("jt") or "") if isinstance(t, dict) else ""
        o_jt = str(o.get("jt") or "") if isinstance(o, dict) else ""
        if o_jt != t_jt:
            # 片方にしか無ければある方。両方にあれば新しい方("HH:MM:SS" の文字列比較)
            return o if (not t_jt or o_jt > t_jt) else t
        t_prov = isinstance(t, dict) and bool(t.get("prov"))
        o_prov = isinstance(o, dict) and bool(o.get("prov"))
        if t_prov and not o_prov and isinstance(o, dict) and o.get("t3"):
            return o
        return t
    if k in _RICH_FIELDS:
        return o if _richness(o) > _richness(t) else t
    return t


def _picks_from_ours(o, t):
    """買い目一式を ours から採るか。"""
    # 打刻済み(tk あり)の買い目は変えない(打刻はその時点の picks で判定している。
    # predict_today の「打刻済みは差し替えない」と同じ約束)。結果の付いたレースも同じ
    # (_merge_existing は live / result / tk のどれかで買い目を凍結する。結果は do_results が
    # 打刻と一緒に付けるので実際には tk を伴うが、規則を同じ形にそろえておく)
    if "tk" in o or o.get("result"):
        return True
    o_live, t_live = bool(o.get("live")), bool(t.get("live"))
    if o_live and not t_live:
        return True
    if o_live and t_live:
        # 両方 live なら live_at("HH:MM")の新しい方。同じ・無ければ theirs
        return str(o.get("live_at") or "") > str(t.get("live_at") or "")
    return False


def _merge_race(o, t):
    """theirs のレース t を土台に ours のレース o を重ねた新しい dict を返す。"""
    r = copy.deepcopy(t)
    for k in _OBSERVED_FIELDS:
        if k not in o:
            continue
        if k not in t:
            r[k] = copy.deepcopy(o[k])
        else:
            r[k] = copy.deepcopy(_pick_observed(k, o[k], t[k]))
    # 取得済みのレース名(rn_full)は predict_today._merge_existing と同じく引き継ぐ
    if o.get("rn_full"):
        r["type"] = o.get("type", r.get("type"))
        r["rn_full"] = True
    if _picks_from_ours(o, t):
        for k in _PICK_FIELDS:
            if k in o:
                r[k] = copy.deepcopy(o[k])
            else:
                r.pop(k, None)
        # 買い目を採ったら世代の印もその買い目のもの(印の無い買い目は世代1)。_merge_existing と同じ
        r["g"] = race_gen(o)
    return r


def _code(v):
    return v.get("code")


def merge_dayfile(ours: dict, theirs: dict) -> dict:
    """ours(周回の版)と theirs(上流の版)を merge した新しい dict を返す。入力は変更しない。
    date が違う・dict でない時は ValueError。"""
    if not isinstance(ours, dict) or not isinstance(theirs, dict):
        raise ValueError("当日ファイルが dict でない")
    if not ours.get("date") or ours.get("date") != theirs.get("date"):
        raise ValueError("date が違う(ours=%r theirs=%r)" % (ours.get("date"), theirs.get("date")))
    out = copy.deepcopy(theirs)
    for k in _NEWER_KEYS:
        if k in ours or k in theirs:
            out[k] = _newer(ours.get(k), theirs.get(k))

    t_venues = out.get("venues") or []
    if not isinstance(t_venues, list):
        t_venues = []
    by_code = {_code(v): v for v in t_venues if isinstance(v, dict)}
    for ov in (ours.get("venues") or []):
        if not isinstance(ov, dict):
            continue
        tv = by_code.get(_code(ov))
        if tv is None:
            # ours にしか無い会場はそのまま足す(通常は無い)
            tv = copy.deepcopy(ov)
            t_venues.append(tv)
            by_code[_code(tv)] = tv
            continue
        t_races = tv.get("races") or []
        if not isinstance(t_races, list):
            t_races = []
        by_no = {r.get("no"): i for i, r in enumerate(t_races) if isinstance(r, dict)}
        for orc in (ov.get("races") or []):
            if not isinstance(orc, dict):
                continue
            i = by_no.get(orc.get("no"))
            if i is None:
                t_races.append(copy.deepcopy(orc))
                by_no[orc.get("no")] = len(t_races) - 1
            else:
                t_races[i] = _merge_race(orc, t_races[i])
        # レース番号順(predict_today と同じ並び。ours にしか無いレースを足した時だけ順が変わる)
        try:
            t_races.sort(key=lambda r: r.get("no"))
        except TypeError:
            pass
        tv["races"] = t_races
    try:
        t_venues.sort(key=_code)
    except TypeError:
        pass
    out["venues"] = t_venues
    return out


def dumps(obj) -> str:
    """predict_today.write / update_all.write と同じ書き方(1行・ensure_ascii=False・改行なし)。"""
    return json.dumps(obj, ensure_ascii=False)


def _atomic_write_text(path: Path, txt: str) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(txt, encoding="utf-8")
    os.replace(tmp, path)


def _load(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="当日の予測ファイルを JSON 単位で merge する")
    ap.add_argument("ours", help="周回の版(rebase -X theirs で勝った側)")
    ap.add_argument("theirs", help="上流(origin/main)の版")
    ap.add_argument("out", help="書き先")
    ap.add_argument("--latest", default=None,
                    help="当日ファイルの写し(latest.json)。存在して date が同じなら merge 結果を写す")
    a = ap.parse_args(argv)
    try:
        ours = _load(a.ours)
        theirs = _load(a.theirs)
        merged = merge_dayfile(ours, theirs)
        txt = dumps(merged)
    except Exception as e:  # noqa: BLE001 - 壊れた入力は何も書かずに 2 で戻る(呼び出し側が従来どおり進む)
        print(f"merge_dayfile: merge せず({type(e).__name__}: {e})", file=sys.stderr)
        return EXIT_BAD_INPUT
    _atomic_write_text(Path(a.out), txt)
    n_r = sum(len(v.get("races") or []) for v in merged.get("venues") or [])
    print(f"merge_dayfile: {a.out} date={merged.get('date')} venues={len(merged.get('venues') or [])} "
          f"races={n_r} model_trained_at={merged.get('model_trained_at')}")
    if a.latest:
        lp = Path(a.latest)
        try:
            same_day = lp.exists() and _load(str(lp)).get("date") == merged.get("date")
        except Exception:  # noqa: BLE001 - 読めない latest.json は触らない
            same_day = False
        if same_day:
            _atomic_write_text(lp, txt)
            print(f"merge_dayfile: {a.latest} にも写した")
    return 0


if __name__ == "__main__":
    sys.exit(main())

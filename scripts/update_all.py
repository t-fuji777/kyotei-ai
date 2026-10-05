# -*- coding: utf-8 -*-
"""Unified updater: in ONE pass over today's prediction file, attach race
RESULTS (for finished races) and refresh ODDS (for races on sale), then write
once. Single writer => no git rebase conflicts between separate workflows.

Why unified: odds fetching is slow (~10-15s/race from Actions IPs), and while it
runs a separate results workflow would commit the same latest.json, causing
merge conflicts on push. Doing both here and committing once avoids that."""
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import stamp_plans, badge_attention, sengen_top3p, is_sengen
from fetch_result import fetch_result, fetch_before_html, parse_before
from fetch_odds import fetch_odds, fetch_racename, fetch_t3

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))

# ---- results config ----
GRACE_MIN = 3
RESULT_MAX_PER_RUN = 150
# ---- odds config ----
ODDS_BEFORE_MAX = 60    # only fetch odds for races within 60 min of deadline
ODDS_AFTER = 5
ODDS_MAX_PER_RUN = 8   # keep small so the whole run finishes in time
# ---- morning provisional odds config ----
MORNING_ODDS_MAX_PER_RUN = 12       # lightweight t3-only sweep, far-out races
MORNING_ODDS_FROM_HHMM = (7, 45)    # advance (zen-uri) odds appear ~7:45 JST
SENGEN_REFETCH_MIN = 10             # re-fetch sengen/premier candidates within N min of deadline (final odds check)
CAND_TOP4P_MIN = 0.36               # top-4 cumulative prob threshold for the refetch heuristic; sengen
                                     # (top3p>=0.36) candidates are a subset of top4p>=0.36 candidates, so
                                     # this single top4p check covers both the sengen and premier tiers


def _mins_to_deadline(now, dl):
    if not dl or ":" not in dl:
        return None
    h, m = map(int, dl.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return (t - now).total_seconds() / 60


def _mins_to_deadline_on(now, dl, ymd):
    if not dl or ":" not in dl:
        return None
    h, m = map(int, dl.split(":"))
    base = datetime(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]), h, m, tzinfo=now.tzinfo)
    return (base - now).total_seconds() / 60


def _atomic_write_text(path: Path, txt: str) -> None:
    """Write text to path atomically via a temp file + os.replace, so a mid-write
    crash never leaves a truncated/corrupt file behind."""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(txt)
    os.replace(tmp, path)


def write(obj, ymd):
    d = ROOT / "docs" / "predictions"
    d.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False)
    _atomic_write_text(d / f"{ymd}.json", txt)
    _atomic_write_text(d / "latest.json", txt)


def _axis_from_odds(nr, odds):
    fav_lane = nr.get("fuku", {}).get("lane")
    combos = [p["c"] for p in nr.get("picks", [])]
    axis, axis_combo = {}, {}
    if combos:
        a, b, c = combos[0].split("-")
        s2 = "=".join(sorted([a, b]))
        s3 = "=".join(sorted([a, b, c]))
        axis = {"fuku": odds.get("fuku", {}).get(fav_lane),
                "k": odds.get("k", {}).get(s2),
                "f2": odds.get("f2", {}).get(s2),
                "t2": odds.get("t2", {}).get(f"{a}-{b}"),
                "f3": odds.get("f3", {}).get(s3),
                "t3": odds.get("t3", {}).get(combos[0])}
        axis_combo = {"fuku": str(fav_lane), "k": s2, "f2": s2,
                      "t2": f"{a}-{b}", "f3": s3, "t3": combos[0]}
    return {
        "fuku": odds.get("fuku", {}).get(fav_lane),
        "t3": {c2: odds.get("t3", {}).get(c2) for c2 in combos
               if odds.get("t3", {}).get(c2) is not None},
        "axis": axis, "axis_combo": axis_combo,
    }


STAMP_LEAD_MIN = 15  # 締切何分前からチェックポイント確定を打刻するか
# 締切から何分後まで打刻を許すか。古い作業ツリーのrunnerが何時間も後に
# 「締切15分前の確定」を名乗って打刻するのを防ぐ(2026-08-05に締切39分後の
# 打刻が発生)。この窓を過ぎたレースは do_results のフォールバックが拾う。
STAMP_LATE_MAX = 15


def do_stamps(pred, now, skip=None, only=None, refresh=None) -> int:
    """締切15分前チェックポイント: まだ厳選(竹)が確定していない(r["tk"]無し)かつ
    結果未確定のレースのうち、締切前後STAMP_LEAD_MIN/STAMP_LATE_MAX分以内の
    ものへ、その時点のpicks/oddsで厳選スタンプ(r["tk"]/r["pt"]。mtは終売につき常に0)を
    first-winsで焼き込む。窓を外したものはdo_results側のフォールバックで結果
    確定時に焼く。"""
    n = 0
    healed = 0
    now_hhmm = now.strftime("%H:%M")
    for v in pred["venues"]:
        for r in v["races"]:
            if "tk" in r:
                # 自己修復: tk打刻済みなのにatt(注目/様子見バッジ)が無いレースへ補完する。
                # 導入初日(2026-09-02)の引き継ぎ漏れ由来の欠落救済と、将来のフィールド
                # 消失事故への恒久保険。tk打刻済みは買い目が凍結され較正表も当日固定の
                # ため、凍結picksからのbadge_attentionは打刻時点の値と同一(後出しでない)。
                if r.get("att") is None:
                    r["att"] = badge_attention(r)
                    healed += 1
                continue
            if r.get("result"):
                continue
            # 候補として公開した証跡(qc/qp)をfirst-winsで焼き込む。厳選一覧は打刻前の
            # レースを「候補」として表示するため、一度でも確率条件を満たしたレースは
            # 公開済みとして記録し、後で条件を外れたら見送り理由を書けるようにする
            # (書かないと候補が痕跡なく消える。2026-09-12発覚)。
            if "qc" not in r:
                _p3 = sengen_top3p(r.get("picks") or [])
                if is_sengen(_p3, v["code"], r["no"]):
                    r["qc"] = 1
                    r["qp"] = round(_p3, 4)
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None or mins > STAMP_LEAD_MIN or mins < -STAMP_LATE_MAX:
                continue
            # skip: この実行でオッズを取り直す予定のレース。取り終えるまで打刻を待つ
            # (判定に使うオッズを従来=do_odds完了後の値と同じに保つ)。
            if skip and (v["code"], r["no"]) in skip:
                continue
            # only: 条件を満たすレースだけ打刻する(--results-only 末尾の追い打刻用)。
            if only is not None and not only(r):
                continue
            # refresh: 厳選の候補(確率条件を満たすレース)は、判定の直前に3連単のオッズを取り直す。
            # 取り直すのは締切前の判定だけ(締切後の板は確定オッズで、判定の時点ではもう買えない)。
            # 取り直しの失敗で打刻を止めない(保存済みのオッズで判定する)。
            if refresh is not None and mins >= 0:
                try:
                    if is_sengen(sengen_top3p(r.get("picks") or []), v["code"], r["no"]):
                        refresh(v, r)
                except Exception as e:
                    print(f"  judge-odds {v.get('code')}-{r.get('no')}R: refresh failed ({type(e).__name__})", flush=True)
            stamp_plans(r, v["code"], None, now_hhmm, late=(mins < 0))
            n += 1
    if n:
        print(f"stamps: {n} race(s) checkpointed")
    if healed:
        print(f"stamps: att healed on {healed} race(s)")
    return n + healed


# ---- 厳選の判定に使うオッズの取り直し(2026-10-06 の開催分から) ----
# 厳選の条件は「締切15分前のオッズで上位3点が全て3.1倍以上」。ところが判定に使っていたのは、
# レースが締切60分以内に入った時に1回取得したオッズで、打刻の時点では20〜51分(中央値36分)古かった
# (2026-08-04〜10-01 の厳選候補88件を git 履歴で確認。全件が締切35〜60分前の板)。
# 説明どおりにするため、候補(確率条件を満たすレース)は判定の直前に3連単のオッズ1ページを取り直す。
# 候補は1日数件なので、boatrace.jp への要求は1日数回増えるだけ。
# 日付で切り替えるのは、開催の途中で判定の基準が変わらないようにするため。
ODDS_REFRESH_FROM = "20261006"
ODDS_REFRESH_SKIP_SEC = 180   # この実行でこれ以内に取得したばかりのレースは取り直さない


def _make_refresher(ymd, fresh=None):
    """do_stamps に渡す取り直しの関数を作る。対象日より前なら None(従来どおり保存済みのオッズで判定)。
    fresh は {(会場, レース): (取得した時刻 time.monotonic, 同じ時刻の "HH:MM:SS")}。full の中で
    本オッズを取ったばかりのレースを二重に取りに行かないために使う。
    odds.jt は「判定に使った板を取得した時刻」。付いていれば判定の直前の板で判定した、付いていなければ
    取り直せず保存済みの板(締切の数十分前のもの)で判定した、と後から見分けられる。odds.t3 そのものは
    締切10分前や締切後にも取り直されて上書きされるので、jt が指すのは os(判定時の板)のほうである。"""
    if str(ymd) < ODDS_REFRESH_FROM:
        return None
    fresh = {} if fresh is None else fresh

    def refresh(v, r):
        key = (v["code"], r["no"])
        # 締切を過ぎていたら取り直さない(締切後の板は確定オッズ。呼び出し側も締切前だけ呼ぶが、
        # 合間打刻を止めた経路は開始時刻で残り時間を見ているので、ここで今の時刻でも確かめる)。
        left = _mins_to_deadline(datetime.now(JST), r.get("deadline"))
        if left is None or left < 0:
            return
        got = fresh.get(key)
        if got is not None and time.monotonic() - got[0] < ODDS_REFRESH_SKIP_SEC:
            # この実行で本オッズを取ったばかり。取り直さないが、判定の直前の板であることは残す。
            if isinstance(r.get("odds"), dict):
                r["odds"]["jt"] = got[1]
            return
        t3 = fetch_t3(ymd, v["code"], r["no"])
        if not t3:
            # 取れなかった時は保存済みのオッズで判定する(jt が付かないので、後から見分けられる)。
            print(f"  judge-odds {v['code']}-{r['no']}R: not available, judging with stored odds", flush=True)
            return
        fresh[key] = (time.monotonic(), datetime.now(JST).strftime("%H:%M:%S"))
        combos = [p["c"] for p in (r.get("picks") or [])]
        ex = r.get("odds") or {}
        # 取り直した板だけで置き換える。古い板の値を残すと、今は売られていない買い目(欠場など)を
        # 古いオッズで通してしまう。取り直した板に無い買い目は「オッズ未取得」として成立させない。
        ex["t3"] = {c: t3[c] for c in combos if t3.get(c) is not None}
        if isinstance(ex.get("axis"), dict) and combos:
            ex["axis"]["t3"] = t3.get(combos[0])
        ex["jt"] = fresh[key][1]   # 判定に使う板を取得した時刻
        r["odds"] = ex
        print(f"  judge-odds {v['code']}-{r['no']}R: refreshed t3={len(ex['t3'])}", flush=True)

    return refresh


# ---- full の取得ループの合間に行う打刻(2026-10-02) ----
# 止めたいときはここを False にして main へ入れる。実行中のループは毎周回 scripts/ を
# 取り直すので、1周回(約1〜3分)以内に従来の動き(full の中では開始時刻で1回だけ判定)へ戻る。
MIDRUN_STAMP = True     # full の取得の合間に時刻を取り直して打刻する
MIDRUN_PUBLISH = True   # 合間の打刻をその場で commit/push する(auto-update ループ内のみ)
TAIL_STAMP = True       # --results-only の末尾で、買い目もオッズも動かないレースだけ追い打刻する
EARLY_NOTIFY = True     # --results-only で厳選が確定したら、結果・展示の取得を待たずに公開して通知する
# 合間の公開(git push)を待つ上限。公開→通知の順なので、push が詰まると通知もその分遅れる。
# 平常時は約2秒で終わる。締切まで15分しかないので、詰まった時は30秒で見切って通知へ進む
# (送れなかったコミットは、周回末尾の commit_push が従来どおり送る)。
MIDRUN_PUBLISH_TIMEOUT_SEC = 30
# 合間の通知が失敗した時(購読一覧を取れない等)に、次の合間で試し直す回数の上限。
# これが無いと、次に試すのは次の打刻か full の末尾(最大約11分後)になる。
MIDRUN_NOTIFY_RETRY = 3


def _ts(now) -> str:
    # 合間の書き込みは同じ分に2回起こり得る。画面(docs/index.html の refreshLatest)は
    # *_updated_at の文字列が変わった時だけ再描画するので、秒まで入れて必ず変える。
    return now.strftime("%Y-%m-%d %H:%M:%S JST")


def _publish_midrun() -> None:
    """合間の打刻をその場で公開する。開催中ループ(auto-update)の中でだけ動く。
    scripts/publish_stamps.sh が add / commit / push を1回だけ行い、終わるまで待つ。
    fetch・rebase・reset はしない(作業ツリーを書き換える git 操作を full の途中に挟まない)。
    push が通らなかった分は、周回末尾の commit_push(yml 側)が従来どおり rebase して送る。
    失敗しても取得は続ける。"""
    if not MIDRUN_PUBLISH or os.environ.get("GITHUB_WORKFLOW") != "auto-update":
        return
    sh = Path(__file__).parent / "publish_stamps.sh"
    try:
        sys.stdout.flush()
        rc = subprocess.run(["bash", str(sh), "auto stamps"], cwd=str(ROOT),
                            timeout=MIDRUN_PUBLISH_TIMEOUT_SEC).returncode
        print(f"midrun publish: rc={rc}", flush=True)
    except Exception as e:
        print(f"::warning::midrun publish failed: {e}", flush=True)


def _notify_midrun(pred, ymd) -> bool:
    """合間の打刻で厳選が確定したら、その場で通知する(公開の直後)。締切まで15分しかないので、
    full の終わり(最大約11分後)まで待たせない。通知の設定が無ければ何もしない。失敗しても取得は
    続ける。full の末尾でも同じ関数が呼ばれるが、送信済みの記録があるので二重には送らない。
    戻り値: 送れずに残った通知があるか(True なら _Ticker が次の合間でもう一度試す)。
    通知にかかる時間は notify 側で上限を切ってある(1回の送信全体で約30秒)。"""
    try:
        from notify import notify_events
        return bool(notify_events(pred, ymd))
    except Exception as e:
        print(f"notify skip: {e}", flush=True)
        return False


class _Ticker:
    """full の取得ループの合間に呼ぶ。時刻を取り直し、締切15分前を跨いだレースを
    その場で打刻する。full は開始時刻の now を最後まで使い回すため、従来はオッズ取得中
    (1レース約50秒x最大8件+朝オッズ・レース名で最大約11分)に跨いだレースが full の中では
    打刻されず、直後の --results-only まで待たされていた。
    pending はこの実行でオッズ取得待ちのレース。取り終えるまで打刻しない。
    --results-only の取得ループからは呼ばない: 軽い周回は直後に --live-window が走るので、
    そこで先に打刻すると展示を反映する最後の機会を潰す。full の直後は --results-only が
    続くだけで --live-window を挟まないため、ここで前倒ししても買い目は変わらない。
    打刻の失敗で取得を止めない: 例外は握って以後の合間打刻をやめ、ログに警告を出す。"""

    def __init__(self, pred, ymd):
        self.pred, self.ymd = pred, ymd
        self.stamps = 0
        self.dead = False
        self.fresh = {}         # この実行で本オッズを取得したレースと時刻(判定の直前の取り直しを省くため)
        self.refresh = _make_refresher(ymd, self.fresh)
        self.notify_retry = 0   # 失敗した通知を、あと何回の合間で試し直すか

    def __call__(self, pending=None) -> int:
        if self.dead:
            return 0
        try:
            now = datetime.now(JST)
            if now.strftime("%Y%m%d") != self.ymd:
                return 0
            n = do_stamps(self.pred, now, skip=pending, refresh=self.refresh)
            if n:
                self.stamps += n
                # 以降の取得で落ちても打刻と取得済みオッズが残るよう、先に書く。
                self.pred["results_updated_at"] = _ts(now)
                write(self.pred, self.ymd)
                print(f"stamp-tick {now.strftime('%H:%M:%S')}: {n}", flush=True)
                _publish_midrun()
                self.notify_retry = MIDRUN_NOTIFY_RETRY if _notify_midrun(self.pred, self.ymd) else 0
            elif self.notify_retry > 0:
                # 前の合間の通知が送れずに残っている(購読一覧を取れなかった等の一時的な失敗)。
                # 打刻が無い合間でも試し直す。回数を限るのは、失敗が続く間ずっと取得を遅らせないため。
                self.notify_retry -= 1
                if not _notify_midrun(self.pred, self.ymd):
                    self.notify_retry = 0
            return n
        except Exception:
            self.dead = True
            print("::warning::update_all: stamp tick failed; mid-run stamping disabled for this run",
                  flush=True)
            traceback.print_exc()
            sys.stderr.flush()
            return 0


def _settled(r) -> bool:
    """次の打刻機会までに買い目もオッズも動かないレースか。
    live かつ st_ex あり = --live-window / --live の対象外(買い目が動かない)。
    本オッズ取得済み(prov でない) = 締切前は do_odds が取り直さない(候補の締切10分前
    再取得は打刻より後)。この条件を満たすレースは、いま打刻しても次の周回で打刻しても
    判定内容が同じになる。"""
    o = r.get("odds") or {}
    return bool(r.get("live") and r.get("st_ex") and o.get("t3") and not o.get("prov"))


def do_results(pred, now, ymd, max_fetch=RESULT_MAX_PER_RUN, tick=None) -> int:
    n = 0
    tried = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if r.get("result"):
                continue
            mins = _mins_to_deadline_on(now, r.get("deadline"), ymd)
            if mins is None or mins > -GRACE_MIN:
                continue
            if tried >= max_fetch:
                break
            tried += 1
            if tick:
                tick()
            res = fetch_result(ymd, v["code"], r["no"])
            time.sleep(0.3)
            if not res:
                continue
            if "order" not in res:
                r["result"] = res
                n += 1
                print(f"  result {v['code']}-{r['no']}R: {res.get('status', '?')}")
                continue
            order = res["order"]
            top2 = set(map(int, order.split("-")[:2]))
            fuku_lane = r.get("fuku", {}).get("lane")
            picks = [p["c"] for p in r.get("picks", [])]
            boats = r.get("boats", [])
            # 本命は保存済みの非丸めargmax(fuku.lane)を使う。boats.wp(3桁丸め)からの
            # 再計算はしない(丸め同値時に若い枠番優先バイアスが生じ複勝判定とも食い違うため)。
            top_boat = fuku_lane if fuku_lane is not None else (
                max(boats, key=lambda b: b["wp"])["lane"] if boats else None)
            res["hit_win"] = top_boat is not None and top_boat == int(order.split("-")[0])
            res["hit_fuku"] = fuku_lane in top2
            res["hit_t1"] = bool(picks) and picks[0] == order
            res["hit_t6"] = order in picks[:6]
            res["hit_t10"] = order in picks[:10]
            # 竹/松の該当可否を確定時点のrace(picks/odds)で焼き込む(遡及改変防止)。
            # first-winsのため、チェックポイント(do_stamps)で既に確定済みなら
            # ここでは何もしない(取りこぼした場合のみのフォールバック)。
            stamp_plans(r, v["code"], res, now.strftime("%H:%M"))
            r["result"] = res
            n += 1
            print(f"  result {v['code']}-{r['no']}R: {order}")
    return n


def _has_odds(r):
    """True if this race already has fetched 3t odds stored."""
    o = r.get("odds") or {}
    t3 = o.get("t3") or {}
    return len(t3) > 0


def do_odds(pred, now, ymd, tick=None) -> int:
    # Fetch odds for EVERY race that doesn't yet have them, regardless of whether
    # the race has finished -- boatrace keeps the odds page up for the whole day,
    # so finished races still return their final (confirmed) odds. Skip only races
    # whose deadline is still far in the future (odds not meaningful yet) and races
    # that already have odds stored (provisional ones are re-fetched once after deadline, then marked final).
    targets = []
    for v in pred["venues"]:
        for r in v["races"]:
            o = r.get("odds") or {}
            if o.get("final"):
                continue
            # 結果が付いていても、t3が未取得 or 暫定(prov)のままなら暫定オッズ放置に
            # ならないよう取得対象に残す。確定t3(prov無し)が既にあればここでスキップ
            # (finalは上のo.get("final")で既に判定済み)。
            if r.get("result") and o.get("t3") and not o.get("prov"):
                continue
            _p4 = sum((pk.get("p") or 0) for pk in (r.get("picks") or [])[:4])
            if o.get("t3") and not o.get("prov"):
                m0 = _mins_to_deadline(now, r.get("deadline"))
                if m0 is None:
                    continue
                # real odds stored and deadline still ahead: normally skip, but
                # re-fetch sengen/premier candidates in the final minutes so the
                # final odds-band judgment runs on near-final odds rather than the
                # ~60-min value. top4p>=0.36 covers both tiers (see CAND_TOP4P_MIN).
                # races 1-4 are excluded from both tiers (5R以降のみ対象), so they
                # never qualify for this final-minutes re-fetch either.
                if m0 >= 0 and not (_p4 >= CAND_TOP4P_MIN and r["no"] >= 5 and m0 <= SENGEN_REFETCH_MIN):
                    continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None:
                continue
            # too early: more than ODDS_BEFORE_MAX minutes before deadline
            if mins > ODDS_BEFORE_MAX:
                continue
            targets.append((_p4 < CAND_TOP4P_MIN, mins, v["code"], r["no"]))
    if not targets:
        print("no races need odds")
        return 0
    targets.sort(key=lambda t: (t[0], t[1] < 0, abs(t[1])))
    targets = [(c, n) for _, _, c, n in targets[:ODDS_MAX_PER_RUN]]
    print(f"odds targets ({len(targets)}): {targets}",
          flush=True)
    n = 0
    fetched = 0
    pending = set(targets)  # まだ取得していないレース。取り終えるまで合間の打刻を待たせる
    for v in pred["venues"]:
        for r in v["races"]:
            if (v["code"], r["no"]) not in targets:
                continue
            if fetched >= ODDS_MAX_PER_RUN:
                break
            fetched += 1
            if tick:
                tick(pending)
            odds = fetch_odds(ymd, v["code"], r["no"])
            time.sleep(0.3)
            pending.discard((v["code"], r["no"]))  # 取得に失敗しても外す(従来も既存オッズで打刻していた)
            if not odds or not (odds.get("t3") or odds.get("fuku")):
                print(f"  odds {v['code']}-{r['no']}R: none yet")
                continue
            if odds.get("t3") and getattr(tick, "fresh", None) is not None:
                tick.fresh[(v["code"], r["no"])] = (time.monotonic(), datetime.now(JST).strftime("%H:%M:%S"))
            merged = _axis_from_odds(r, odds)
            ex = r.get("odds", {})
            ex.update(merged)
            ex.pop("prov", None)
            _m = _mins_to_deadline(now, r.get("deadline"))
            if _m is not None and _m < 0:
                ex["final"] = True
            r["odds"] = ex
            n += 1
            print(f"  odds {v['code']}-{r['no']}R: t3={len(odds.get('t3',{}))}")
    return n


def do_morning_odds(pred, now, ymd, tick=None) -> int:
    # Early-morning provisional sweep. Once advance (zen-uri) odds are published
    # (~7:45 JST), fetch lightweight trifecta-only odds for EVERY race that has no
    # odds yet, no matter how far its deadline is, and mark them provisional.
    # do_odds replaces these with the full real odds (and clears the prov flag) as
    # each race enters its 60-min window, then marks them final after deadline.
    # Unpublished races return None here and are simply retried on the next pass.
    if (now.hour, now.minute) < MORNING_ODDS_FROM_HHMM:
        return 0
    targets = []
    for v in pred["venues"]:
        for r in v["races"]:
            if (r.get("odds") or {}).get("t3") or r.get("result"):
                continue
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None or mins <= ODDS_BEFORE_MAX:
                continue  # within-window / finished races are do_odds' job
            targets.append((v["code"], r["no"]))
    if not targets:
        return 0
    targets = targets[:MORNING_ODDS_MAX_PER_RUN]
    print(f"morning odds targets ({len(targets)}): {targets}", flush=True)
    n = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if (v["code"], r["no"]) not in targets:
                continue
            if tick:
                tick()
            t3 = fetch_t3(ymd, v["code"], r["no"])
            time.sleep(0.3)
            if not t3:
                continue
            ex = r.get("odds", {})
            ex.update(_axis_from_odds(r, {"t3": t3}))
            ex["prov"] = True
            r["odds"] = ex
            n += 1
            print(f"  morning odds {v['code']}-{r['no']}R: t3={len(t3)} (prov)")
    return n


RACENAME_MAX_PER_RUN = 12


def do_racenames(pred, ymd, tick=None) -> int:
    # B-program race names are truncated to ~6 chars; fetch the full name from
    # the official racelist page and overwrite. Names are day-invariant, so once
    # stored (rn_full flag) we never re-fetch.
    tried = updated = 0
    for v in pred["venues"]:
        for r in v["races"]:
            if r.get("rn_full"):
                continue
            if tried >= RACENAME_MAX_PER_RUN:
                print(f"racenames: {updated} updated (cap)", flush=True)
                return updated
            if tick:
                tick()
            nm = fetch_racename(ymd, v["code"], r["no"])
            tried += 1
            time.sleep(0.3)
            if nm:
                r["type"] = nm
                r["rn_full"] = True
                updated += 1
    print(f"racenames: {updated} updated, {tried} tried", flush=True)
    return updated


CARRYOVER_DAYS = 3                  # look back this many days (excluding today) for stale results
CARRYOVER_MAX_PER_RUN = RESULT_MAX_PER_RUN  # total fetches across all carryover days this run


def _carryover_one(now, ymd, budget):
    """Backfill missing results for a single past day by reusing do_results
    (which anchors deadlines to `ymd`), bounded by `budget` fetches. Returns
    the number of results fetched."""
    d = ROOT / "docs" / "predictions"
    yp = d / f"{ymd}.json"
    if not yp.exists() or budget <= 0:
        return 0
    try:
        py = json.loads(yp.read_text())
    except Exception:
        return 0
    if not py.get("venues"):
        return 0
    if not any(not r.get("result") for v in py["venues"] for r in v.get("races", [])):
        return 0
    n = do_results(py, now, ymd, max_fetch=budget)
    if not n:
        return 0
    py["results_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    txt = json.dumps(py, ensure_ascii=False)
    _atomic_write_text(yp, txt)
    lp = d / "latest.json"
    if lp.exists():
        try:
            if json.loads(lp.read_text()).get("date") == ymd:
                _atomic_write_text(lp, txt)
        except Exception:
            pass
    print(f"carryover {ymd}: results={n}")
    return n


def _carryover(now):
    """Backfill missing results for the last CARRYOVER_DAYS days (excluding
    today, which the main pass handles). Shares a single fetch budget across
    all days so overall load stays within the existing RESULT_MAX_PER_RUN
    envelope even when several past days still have gaps."""
    budget = CARRYOVER_MAX_PER_RUN
    for back in range(1, CARRYOVER_DAYS + 1):
        if budget <= 0:
            break
        ymd = (now - timedelta(days=back)).strftime("%Y%m%d")
        budget -= _carryover_one(now, ymd, budget)


ST_EX_LEAD = 25
ST_EX_MAX_PER_RUN = 15
# 決着後の取りこぼし回収: 展示は締切25分前〜結果確定までの間しか取りに行かないため、
# その窓でパイプラインが動かないと(実行の重なり/停止など)展示ST・展示タイムが
# 永久に欠落する。beforeinfoはレース後も参照できるので決着後に回収する。
# 1回の実行あたりの件数を絞り、締切から一定時間を過ぎたレースは諦める
# (恒久的に取得できないレースが毎回の枠を食い潰さないようにするため)。
ST_EX_BACKFILL_PER_RUN = 5
ST_EX_BACKFILL_MAX_AGE = 240


def _fill_st_ex(r, ymd, vcode) -> bool:
    """beforeinfoを取得して st_ex / ex / weather を埋める。埋まればTrue。
    取得・解析の例外は呼び出し元へ送出する(リトライ判断は呼び出し元の責務)。"""
    bi = parse_before(fetch_before_html(ymd, vcode, r["no"]))
    stx = bi.get("st", {})
    if not stx:
        return False
    r["st_ex"] = {str(k): val for k, val in stx.items()}
    if not r.get("ex") and len(bi.get("ex", {})) == 6:
        r["ex"] = bi["ex"]
    if bi.get("weather"):
        r["weather"] = bi["weather"]
    return True


def _st_ex_targets(pred, now):
    """展示取得の対象を優先順で返す。
    (1) 未決着かつ締切25分前以内(従来動作。速報性が最優先)
    (2) 決着済みで展示が欠落しているもの(取りこぼし回収。締切から4時間以内)"""
    live, backfill = [], []
    for v in pred["venues"]:
        for r in v["races"]:
            mins = _mins_to_deadline(now, r.get("deadline"))
            if mins is None:
                continue
            if r.get("result"):
                if not r.get("st_ex") and -ST_EX_BACKFILL_MAX_AGE <= mins < 0:
                    backfill.append((v["code"], r))
                continue
            if r.get("st_ex") and r.get("weather"):
                continue
            if mins <= ST_EX_LEAD:
                live.append((v["code"], r))
    return live[:ST_EX_MAX_PER_RUN] + backfill[:ST_EX_BACKFILL_PER_RUN]


def do_st_ex(pred, now, ymd) -> int:
    # Lightweight exhibition (ST / lap-time / weather) backfill so the frequent
    # results-only pass surfaces 展示反映 within ~90s instead of waiting for the
    # slow live re-prediction pass. No ML; only beforeinfo fetches. Bounded by
    # fetch count so it never blocks the results loop for long.
    n = 0
    consec_fail = 0
    for vcode, r in _st_ex_targets(pred, now):
        try:
            filled = _fill_st_ex(r, ymd, vcode)
        except Exception as e:
            print(f"  st_ex {vcode}-{r['no']}R fail: {e}")
            consec_fail += 1
            time.sleep(0.3)
            if consec_fail >= 5:
                print("st_ex: 5 consecutive failures; aborting pass")
                return n
            continue
        consec_fail = 0
        if filled:
            n += 1
        time.sleep(0.3)
    return n


def main():
    now = datetime.now(JST)
    ymd = now.strftime("%Y%m%d")
    results_only = "--results-only" in sys.argv
    _carryover(now)
    path = ROOT / "docs" / "predictions" / f"{ymd}.json"
    if not path.exists():
        print("no prediction file for today")
        return
    pred = json.loads(path.read_text())
    if not pred.get("venues"):
        print("no venues today")
        return

    if results_only:
        # 毎分パスが最も確実に締切T-15のチェックポイントを捉えられるため、
        # 結果確定(do_results)の前にdo_stampsを呼ぶ。
        # 前倒し通知の下準備(打刻の前に、既に確定している厳選を控える)。ここで失敗しても打刻は行う。
        _tk_before = None
        if EARLY_NOTIFY:
            try:
                _tk_before = {(v.get("code"), r.get("no")) for v in pred["venues"] for r in v["races"]
                              if r.get("tk") == 1}
            except Exception:
                _tk_before = None
        _refresh = _make_refresher(ymd)
        n_stp = do_stamps(pred, now, refresh=_refresh)
        if EARLY_NOTIFY and _tk_before is not None:
            # この実行の打刻で厳選が新しく確定した(tk=1 になった)時だけ、結果・展示の取得(20〜170秒)を
            # 待たずに、書く→公開→通知(full の合間打刻と同じ順)。締切まで15分しかないので、通知を
            # 取得の後ろに回さない。公開を先にするのは、通知を開いた時に画面も確定になっているように
            # するため。打刻の大半は tk=0 なので、ここを通るのは1日1〜2回。末尾の notify_events は
            # 残す(ここで送れなかった時のやり直しと、結果の通知のため。送信済みは二重に送らない)。
            # ここでの失敗は握る(結果・展示の取得を止めない)。
            try:
                if n_stp and any(r.get("tk") == 1 and (v.get("code"), r.get("no")) not in _tk_before
                                 for v in pred["venues"] for r in v["races"]):
                    pred["results_updated_at"] = _ts(datetime.now(JST))
                    write(pred, ymd)
                    print(f"stamp-early {datetime.now(JST).strftime('%H:%M:%S')}: sengen confirmed", flush=True)
                    _publish_midrun()
                    _notify_midrun(pred, ymd)
            except Exception:
                print("::warning::update_all: early publish/notify failed", flush=True)
                traceback.print_exc()
        n_res = do_results(pred, now, ymd)
        n_stx = do_st_ex(pred, now, ymd)
        if TAIL_STAMP:
            # 結果・展示の取得(20〜170秒)の間に締切15分前を跨いだレースのうち、買い目も
            # オッズももう動かないものだけ、時刻を取り直して打刻する。それ以外は従来どおり
            # 次の周回(--live-window の後)に回す。
            try:
                now2 = datetime.now(JST)
                if now2.strftime("%Y%m%d") == ymd:
                    n_tail = do_stamps(pred, now2, only=_settled, refresh=_refresh)
                    if n_tail:
                        print(f"stamp-tail {now2.strftime('%H:%M:%S')}: {n_tail}", flush=True)
                    n_stp += n_tail
            except Exception:
                print("::warning::update_all: tail stamp failed", flush=True)
                traceback.print_exc()
        if n_res or n_stx or n_stp:
            pred["results_updated_at"] = _ts(datetime.now(JST))
            write(pred, ymd)
        try:
            from notify import notify_events
            notify_events(pred, ymd)
        except Exception as e:
            print(f"notify skip: {e}")
        print(f"results-only: results={n_res} st_ex={n_stx} stamps={n_stp}")
        return
    # オッズ取得を結果確定より先に行う(確定時スタンプ(stamp_plans)が同一サイクルで
    # 取得した直前オッズを反映して判定できるように)。do_stampsはdo_odds直後・
    # do_resultsの前に置き、その時点の最新オッズでチェックポイント確定させる。
    if MIDRUN_STAMP:
        tick = _Ticker(pred, ymd)
        n_odds = do_odds(pred, now, ymd, tick=tick)
        tick()  # 従来の do_stamps の位置。開始時刻ではなく今の時刻で判定する
        n_res = do_results(pred, now, ymd, tick=tick)
        n_morn = do_morning_odds(pred, now, ymd, tick=tick)
        n_name = do_racenames(pred, ymd, tick=tick)
        n_stp = tick.stamps
    else:
        n_odds = do_odds(pred, now, ymd)
        n_stp = do_stamps(pred, now, refresh=_make_refresher(ymd))
        n_res = do_results(pred, now, ymd)
        n_morn = do_morning_odds(pred, now, ymd)
        n_name = do_racenames(pred, ymd)
    if n_res == 0 and n_odds == 0 and n_morn == 0 and n_name == 0 and n_stp == 0:
        print("nothing to update")
        try:
            from notify import notify_events
            notify_events(pred, ymd)
        except Exception as e:
            print(f"notify skip: {e}")
        return
    if n_res or n_stp:
        # 打刻だけでも画面が再描画されるよう時刻を進める(--results-only 側と同じ扱い)。
        pred["results_updated_at"] = _ts(datetime.now(JST))
    if n_odds or n_morn:
        pred["odds_updated_at"] = now.strftime("%Y-%m-%d %H:%M JST")
    write(pred, ymd)
    try:
        from notify import notify_events
        notify_events(pred, ymd)
    except Exception as e:
        print(f"notify skip: {e}")
    print(f"updated: results={n_res} odds={n_odds} morning={n_morn} names={n_name} stamps={n_stp}")


if __name__ == "__main__":
    main()

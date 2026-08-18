# -*- coding: utf-8 -*-
"""厳選(sengen)ルール改定の再集計: 保存済みdocs/predictions/YYYYMMDD.jsonを新ルール
(common.is_sengen, 3連単TOP3の3点買い)で読み直し、docs/accuracy.jsonの該当日エントリの
sen_n/sen_hit/sen_pred_sumを差し替えてtotalを再集計する。あわせてプレミア(common.is_matsu,
3連単TOP4の4点買い)のprm_n/prm_hit/prm_pred_sumも同時に再集計する。さらに竹プラン
(sen_stake/sen_ret)・松プラン(prm_stake/prm_ret)のROIも同時に再集計する: 竹は対象
レース1件につき300円投資・的中(上位3点内)でpay3t(100円あたり払戻)を回収、松は
1件につき400円投資・的中(上位4点内)でpay3tを回収(update_results.evaluateと同一定義)。
竹・松とも5R以降のみが選定対象(common.SENGEN_MIN_RNO。1-4Rはモデルの高確率帯が
信用できないことが較正で実証されているため除外)。
Kやモデルは不要(予測JSON内のpicksと確定結果のorderだけで完結)。過去日エントリに残る
super_n等のキーはそのまま保持し(履歴残置)、本スクリプトでは触らない。"""
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (is_sengen, sengen_top3p, SENGEN_MIN_ODDS,
                     matsu_top4p, is_matsu, MATSU_MIN_ODDS, MATSU_MAX_ODDS)

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))


def _atomic_write_text(path: Path, text: str):
    """同一ディレクトリのtmpファイルに書いてからos.replaceで差し替える(破損防止)。"""
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def recompute_day(pred: dict):
    """1日分の予測JSON -> {sen_n, sen_hit, sen_pred_sum, prm_n, prm_hit, prm_pred_sum,
    sen_stake, sen_ret, prm_stake, prm_ret}(新ルールで再集計)。確定済み(result.order
    あり)のレースのみ集計対象。sen_stake/sen_ret/prm_stake/prm_retは竹300円・松400円
    投資、的中時はpay3t(100円あたり払戻)回収というROI定義(update_results.evaluateと同一)。"""
    sen_n = 0
    sen_hit = 0
    # 的中したのに購入額(竹300円/松400円)を下回った回数。竹の下限3.1倍は締切15分前の
    # 板で判定するため、締切までにオッズが下がると的中しても損になる。実測で起きている
    # 事象なので「的中すれば必ずプラス」と言わないために数える。
    sen_hitloss = 0
    sen_pred_sum = 0.0
    prm_n = 0
    prm_hit = 0
    prm_hitloss = 0
    prm_pred_sum = 0.0
    sen_stake = 0
    sen_ret = 0
    prm_stake = 0
    prm_ret = 0
    for v in pred.get("venues", []):
        for r in v.get("races", []):
            res = r.get("result")
            if not res or not res.get("order"):
                continue  # 未確定 or 中止/不成立(orderなし)
            top3p = sengen_top3p(r.get("picks"))
            top4p = matsu_top4p(r.get("picks"))
            picks = [p["c"] for p in (r.get("picks") or [])]
            # 価値フィルタ: 上位3点にオッズ3.1倍未満が含まれる場合は厳選から除外
            # (300円ボックスで的中しても損になるため。UI sengenOk / pickOdds と同一条件)。
            # 的中買い目は暫定オッズが最終に更新されず残ることがあるため、確定した
            # 実配当(pay3t/100)を優先して判定する。
            t3 = (r.get("odds") or {}).get("t3") or {}
            _order = res.get("order")
            _pay = res.get("pay3t")
            _status = res.get("status")

            def _eff(c):
                if _order and not _status and _pay and c == _order:
                    return _pay / 100.0
                return t3.get(c)

            picks_ok = all((_eff(c) is None or _eff(c) >= SENGEN_MIN_ODDS) for c in picks[:3])
            rno = r.get("no")
            # 竹/松の該当可否: 優先順位は
            #   1. r["tk"]/r["mt"](レース直下、締切15分前チェックポイントで確定/
            #      取りこぼし時は結果確定時にフォールバックで焼き込み)
            #   2. res["tk"]/res["mt"](旧仕様互換。導入前に確定済みだった本日分等)
            #   3. 従来通りの動的計算(スタンプの無い過去データ、52日互換)
            # 上位ほど遡及改変防止の確度が高いため優先する。
            if "tk" in r:
                is_tk = bool(r["tk"])
            elif "tk" in res:
                is_tk = bool(res["tk"])
            else:
                is_tk = is_sengen(top3p, v.get("code"), rno) and picks_ok
            if is_tk:
                sen_n += 1
                sen_pred_sum += top3p
                # 竹プランROI: 1レース300円投資、的中時はpay3t(100円あたり払戻)を回収
                sen_stake += 300
                if res["order"] in picks[:3]:
                    sen_hit += 1
                    sen_ret += res.get("pay3t") or 0
                    if (res.get("pay3t") or 0) < 300:
                        sen_hitloss += 1

            # プレミア価値フィルタ(update_results._matsu_okと同一ロジックをローカル実装):
            # 上位4点は全点オッズ取得済みが前提(t3.get(c)がNoneなら即対象外)。上限(10.0倍)
            # 判定は常に生の取得オッズt3.get(c)で行う(実配当で事後に外さない)。下限(4.1倍)
            # 判定のみ_effを流用し、的中買い目は実配当優先で見る。
            matsu_ok = True
            for c in picks[:4]:
                o = t3.get(c)
                if o is None or o > MATSU_MAX_ODDS:
                    matsu_ok = False
                    break
                if _eff(c) < MATSU_MIN_ODDS:
                    matsu_ok = False
                    break
            if "mt" in r:
                is_mt = bool(r["mt"])
            elif "mt" in res:
                is_mt = bool(res["mt"])
            else:
                is_mt = is_matsu(top4p, v.get("code"), rno) and matsu_ok
            if is_mt:
                prm_n += 1
                prm_pred_sum += top4p
                # 松プランROI: 1レース400円投資、的中時はpay3tを回収
                prm_stake += 400
                if res["order"] in picks[:4]:
                    prm_hit += 1
                    prm_ret += res.get("pay3t") or 0
                    if (res.get("pay3t") or 0) < 400:
                        prm_hitloss += 1
    return {"sen_n": sen_n, "sen_hit": sen_hit, "sen_pred_sum": sen_pred_sum,
            "prm_n": prm_n, "prm_hit": prm_hit, "prm_pred_sum": prm_pred_sum,
            "sen_stake": sen_stake, "sen_ret": sen_ret,
            "prm_stake": prm_stake, "prm_ret": prm_ret,
            "sen_hitloss": sen_hitloss, "prm_hitloss": prm_hitloss}


def main():
    today_ymd = datetime.now(JST).strftime("%Y%m%d")
    acc_path = ROOT / "docs" / "accuracy.json"
    if not acc_path.exists():
        print("docs/accuracy.json が見つかりません")
        return
    acc = json.loads(acc_path.read_text())
    days_by_date = {d["date"]: d for d in acc.get("days", [])}

    pred_dir = ROOT / "docs" / "predictions"
    files = sorted(glob.glob(str(pred_dir / "*.json")))
    n_updated = 0
    for fp in files:
        ymd = Path(fp).stem
        if not (len(ymd) == 8 and ymd.isdigit()):
            continue  # latest.json等を除外
        if ymd == today_ymd:
            continue  # 当日は稼働ループが書き換えるため対象外
        day = days_by_date.get(ymd)
        if day is None:
            continue  # accuracy.jsonにエントリが無い日はそのまま
        try:
            pred = json.loads(Path(fp).read_text())
        except Exception as e:
            print(f"{ymd}: 予測JSON読み込み失敗 ({e}); skip")
            continue
        agg = recompute_day(pred)
        day["sen_n"] = agg["sen_n"]
        day["sen_hit"] = agg["sen_hit"]
        day["sen_pred_sum"] = agg["sen_pred_sum"]
        day["prm_n"] = agg["prm_n"]
        day["prm_hit"] = agg["prm_hit"]
        day["prm_pred_sum"] = agg["prm_pred_sum"]
        day["sen_stake"] = agg["sen_stake"]
        day["sen_ret"] = agg["sen_ret"]
        day["prm_stake"] = agg["prm_stake"]
        day["prm_ret"] = agg["prm_ret"]
        day["sen_hitloss"] = agg["sen_hitloss"]
        day["prm_hitloss"] = agg["prm_hitloss"]
        n_updated += 1
        rate = (agg["sen_hit"] / agg["sen_n"] * 100) if agg["sen_n"] else 0.0
        prm_rate = (agg["prm_hit"] / agg["prm_n"] * 100) if agg["prm_n"] else 0.0
        print(f"{ymd}: sen_n={agg['sen_n']} sen_hit={agg['sen_hit']} ({rate:.1f}%) "
              f"prm_n={agg['prm_n']} prm_hit={agg['prm_hit']} ({prm_rate:.1f}%)")

    t = {"races": 0, "win_hit": 0, "top1_hit": 0, "top5_hit": 0, "top6_hit": 0,
         "top10_hit": 0, "stake5": 0, "return5": 0, "stake6": 0, "return6": 0,
         "fuku_hit": 0, "sen_n": 0, "sen_hit": 0, "sen_pred_sum": 0.0, "top5_pred_sum": 0.0,
         "prm_n": 0, "prm_hit": 0, "prm_pred_sum": 0.0,
         "sen_stake": 0, "sen_ret": 0, "prm_stake": 0, "prm_ret": 0,
         "sen_hitloss": 0, "prm_hitloss": 0}
    for d in acc.get("days", []):
        for k in t:
            t[k] += d.get(k, 0)
    acc["total"] = t
    _atomic_write_text(acc_path, json.dumps(acc, ensure_ascii=False))

    total_rate = (t["sen_hit"] / t["sen_n"] * 100) if t["sen_n"] else 0.0
    total_prm_rate = (t["prm_hit"] / t["prm_n"] * 100) if t["prm_n"] else 0.0
    print(f"updated {n_updated} day(s). total sen_n={t['sen_n']} sen_hit={t['sen_hit']} "
          f"({total_rate:.1f}%) total prm_n={t['prm_n']} prm_hit={t['prm_hit']} "
          f"({total_prm_rate:.1f}%)")


if __name__ == "__main__":
    main()

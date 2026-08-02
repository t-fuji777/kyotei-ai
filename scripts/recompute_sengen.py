# -*- coding: utf-8 -*-
"""厳選(sengen)ルール改定の再集計: 保存済みdocs/predictions/YYYYMMDD.jsonを新ルール
(common.is_sengen, 3連単TOP3の3点買い)で読み直し、docs/accuracy.jsonの該当日エントリの
sen_n/sen_hit/sen_pred_sumを差し替えてtotalを再集計する。Kやモデルは不要(予測JSON内の
picksと確定結果のorderだけで完結)。過去日エントリに残るsuper_n等のキーはそのまま保持し
(履歴残置)、本スクリプトでは触らない。"""
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import is_sengen, sengen_top3p, SENGEN_MIN_ODDS

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))


def _atomic_write_text(path: Path, text: str):
    """同一ディレクトリのtmpファイルに書いてからos.replaceで差し替える(破損防止)。"""
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def recompute_day(pred: dict):
    """1日分の予測JSON -> {sen_n, sen_hit, sen_pred_sum}(新ルールで再集計)。
    確定済み(result.orderあり)かつ厳選のレースのみ集計対象。"""
    sen_n = 0
    sen_hit = 0
    sen_pred_sum = 0.0
    for v in pred.get("venues", []):
        for r in v.get("races", []):
            res = r.get("result")
            if not res or not res.get("order"):
                continue  # 未確定 or 中止/不成立(orderなし)
            top3p = sengen_top3p(r.get("picks"))
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
            if is_sengen(top3p, v.get("code")) and picks_ok:
                sen_n += 1
                sen_pred_sum += top3p
                if res["order"] in picks[:3]:
                    sen_hit += 1
    return {"sen_n": sen_n, "sen_hit": sen_hit, "sen_pred_sum": sen_pred_sum}


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
        n_updated += 1
        rate = (agg["sen_hit"] / agg["sen_n"] * 100) if agg["sen_n"] else 0.0
        print(f"{ymd}: sen_n={agg['sen_n']} sen_hit={agg['sen_hit']} ({rate:.1f}%)")

    t = {"races": 0, "win_hit": 0, "top1_hit": 0, "top5_hit": 0, "top6_hit": 0,
         "top10_hit": 0, "stake5": 0, "return5": 0, "stake6": 0, "return6": 0,
         "fuku_hit": 0, "sen_n": 0, "sen_hit": 0, "sen_pred_sum": 0.0, "top5_pred_sum": 0.0}
    for d in acc.get("days", []):
        for k in t:
            t[k] += d.get(k, 0)
    acc["total"] = t
    _atomic_write_text(acc_path, json.dumps(acc, ensure_ascii=False))

    total_rate = (t["sen_hit"] / t["sen_n"] * 100) if t["sen_n"] else 0.0
    print(f"updated {n_updated} day(s). total sen_n={t['sen_n']} sen_hit={t['sen_hit']} "
          f"({total_rate:.1f}%)")


if __name__ == "__main__":
    main()

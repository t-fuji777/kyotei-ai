# -*- coding: utf-8 -*-
"""厳選(sengen)ルール改定の再集計: 保存済みdocs/predictions/YYYYMMDD.jsonを新ルール
(common.is_sengen/is_super_sengen)で読み直し、docs/accuracy.jsonの該当日エントリの
sen_n/sen_hit/sen_pred_sum及びsuper_n/super_hit/super_pred_sumを差し替えてtotalを
再集計する。Kやモデルは不要(予測JSON内のpicksと確定結果のorderだけで完結)。"""
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import is_sengen, sengen_top5p, is_super_sengen

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))


def _atomic_write_text(path: Path, text: str):
    """同一ディレクトリのtmpファイルに書いてからos.replaceで差し替える(破損防止)。"""
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)


def recompute_day(pred: dict):
    """1日分の予測JSON -> {sen_n, sen_hit, sen_pred_sum, super_n, super_hit,
    super_pred_sum}(新ルールで再集計)。確定済み(result.orderあり)かつ
    厳選/超厳選のレースのみ、それぞれ集計対象(超厳選は厳選の部分集合)。"""
    sen_n = 0
    sen_hit = 0
    sen_pred_sum = 0.0
    super_n = 0
    super_hit = 0
    super_pred_sum = 0.0
    for v in pred.get("venues", []):
        for r in v.get("races", []):
            res = r.get("result")
            if not res or not res.get("order"):
                continue  # 未確定 or 中止/不成立(orderなし)
            top5p = sengen_top5p(r.get("picks"))
            picks = [p["c"] for p in (r.get("picks") or [])]
            if is_sengen(top5p, v.get("code")):
                sen_n += 1
                sen_pred_sum += top5p
                if res["order"] in picks[:5]:
                    sen_hit += 1
            if is_super_sengen(top5p, v.get("code")):
                super_n += 1
                super_pred_sum += top5p
                if res["order"] in picks[:5]:
                    super_hit += 1
    return {"sen_n": sen_n, "sen_hit": sen_hit, "sen_pred_sum": sen_pred_sum,
            "super_n": super_n, "super_hit": super_hit, "super_pred_sum": super_pred_sum}


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
        day["super_n"] = agg["super_n"]
        day["super_hit"] = agg["super_hit"]
        day["super_pred_sum"] = agg["super_pred_sum"]
        n_updated += 1
        rate = (agg["sen_hit"] / agg["sen_n"] * 100) if agg["sen_n"] else 0.0
        super_rate = (agg["super_hit"] / agg["super_n"] * 100) if agg["super_n"] else 0.0
        print(f"{ymd}: sen_n={agg['sen_n']} sen_hit={agg['sen_hit']} ({rate:.1f}%) "
              f"super_n={agg['super_n']} super_hit={agg['super_hit']} ({super_rate:.1f}%)")

    t = {"races": 0, "win_hit": 0, "top1_hit": 0, "top5_hit": 0, "top6_hit": 0,
         "top10_hit": 0, "stake5": 0, "return5": 0, "stake6": 0, "return6": 0,
         "fuku_hit": 0, "sen_n": 0, "sen_hit": 0, "sen_pred_sum": 0.0, "top5_pred_sum": 0.0,
         "super_n": 0, "super_hit": 0, "super_pred_sum": 0.0}
    for d in acc.get("days", []):
        for k in t:
            t[k] += d.get(k, 0)
    acc["total"] = t
    _atomic_write_text(acc_path, json.dumps(acc, ensure_ascii=False))

    total_rate = (t["sen_hit"] / t["sen_n"] * 100) if t["sen_n"] else 0.0
    total_super_rate = (t["super_hit"] / t["super_n"] * 100) if t["super_n"] else 0.0
    print(f"updated {n_updated} day(s). total sen_n={t['sen_n']} sen_hit={t['sen_hit']} "
          f"({total_rate:.1f}%) total super_n={t['super_n']} super_hit={t['super_hit']} "
          f"({total_super_rate:.1f}%)")


if __name__ == "__main__":
    main()

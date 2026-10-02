#!/usr/bin/env python3
"""朝次処理(daily)の同日再実行を省くかどうかを判定する。標準ライブラリのみ。

daily はschedule遅延対策で1日7回予約されており、最初に成功した回のあとも
残りの回が着弾する。遅い回(実着10〜12時)は開催中ループ(auto-update)と並走し、
12分前に取り出した古い状態から当日ファイルを作り直して上書きするため、その間に
ループが確定した打刻・結果・展示反映を消していた(2026-08-04〜09-30の実測で56回、
打刻110件・結果63件。消えた打刻は数分後に別の時刻・別のオッズで打ち直されていた)。

当日分が朝次処理として完了していれば skip=true を出力し、以降の手順を省く。
判定は「何が出来ているか」だけを見る(実行記録やコミット文言には依存しない):
  1. latest.json が本日の日付で、レースが入っている
  2. 本日学習したモデルで作られている(自己復旧は前日モデルで代行するため、
     その場合は省かず、再学習と作り直しを1回行う)
  3. 本日学習したモデルが配布済み(data/model/live_pointer.json の trained_at が本日)。
     配布に失敗した回のあとは省かず、次に着弾した回が学習と配布をやり直す。
     参照先がまだ無い導入直後も省かない(最初に着弾した回が配布まで行う)
  4. 前日の結果が実績(accuracy.json)に反映済み(前日に予測があった場合)
手動起動(workflow_dispatch)では呼ばれない。必ず最後まで実行する。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))


def _load(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_pointer(path):
    """検証を通った参照先(dict)。無い・不正なら None。model_store を読めない時は中身をそのまま返す。"""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import model_store
        return model_store.load_pointer(path)
    except Exception:
        return _load(path)


def decide(now=None, root=ROOT):
    """(skip, reason) を返す。"""
    now = now or datetime.now(JST)
    today = now.strftime("%Y%m%d")
    yday = (now - timedelta(days=1)).strftime("%Y%m%d")
    pdir = root / "docs" / "predictions"

    latest = _load(pdir / "latest.json")
    if not latest or latest.get("date") != today:
        return False, "当日の予測が未公開"
    if not any(v.get("races") for v in latest.get("venues") or []):
        return False, "当日の予測にレースが無い"
    trained = (latest.get("model_trained_at") or "")[:10].replace("-", "")
    if trained != today:
        return False, "当日の予測が本日学習のモデルで作られていない(model_trained_at=%s)" % (
            latest.get("model_trained_at"),)
    # 参照先は model_store と同じ検証で読む。日付だけ本日でも、形式が不正な参照先は
    # ループが使わない(前回の取得分か予備で動く)ので「未配布」として扱い、省かない。
    ptr = _load_pointer(root / "data" / "model" / "live_pointer.json")
    ptr_at = ptr.get("trained_at") if isinstance(ptr, dict) else None
    if not isinstance(ptr_at, str) or ptr_at[:10].replace("-", "") != today:
        return False, "本日学習のモデルが未配布(参照先 trained_at=%s)" % (ptr_at,)

    yp = _load(pdir / (yday + ".json"))
    if yp and any(v.get("races") for v in yp.get("venues") or []):
        acc = _load(root / "docs" / "accuracy.json") or {}
        if not any(d.get("date") == yday for d in acc.get("days") or []):
            return False, "前日(%s)の結果が実績に未反映" % yday
    return True, "当日分は公開済み(generated_at=%s)" % latest.get("generated_at")


def main():
    skip, reason = decide()
    print("gate: %s -> %s" % (reason, "以降を省く" if skip else "実行する"), file=sys.stderr)
    print("skip=%s" % ("true" if skip else "false"))


if __name__ == "__main__":
    main()

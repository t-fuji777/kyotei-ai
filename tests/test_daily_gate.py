# -*- coding: utf-8 -*-
"""scripts/daily_gate.py の判定(同日の再実行を省くかどうか)を一時ディレクトリで確かめる。

実行: python tests/test_daily_gate.py
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import daily_gate as dg

NOW = datetime(2026, 10, 3, 9, 30, tzinfo=dg.JST)
TODAY, YDAY = "20261003", "20261002"
VENUES = [{"venue": 1, "races": [{"race_no": 1}]}]


def make_root(latest=None, pointer=None, yday_pred=None, accuracy=None):
    root = Path(tempfile.mkdtemp(prefix="gate_test_"))
    pdir = root / "docs" / "predictions"
    pdir.mkdir(parents=True)
    (root / "data" / "model").mkdir(parents=True)
    if latest is not None:
        (pdir / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
    if pointer is not None:
        text = pointer if isinstance(pointer, str) else json.dumps(pointer)
        (root / "data" / "model" / "live_pointer.json").write_text(text, encoding="utf-8")
    if yday_pred is not None:
        (pdir / (YDAY + ".json")).write_text(json.dumps(yday_pred), encoding="utf-8")
    if accuracy is not None:
        (root / "docs" / "accuracy.json").write_text(json.dumps(accuracy), encoding="utf-8")
    return root


def decide(**kw):
    root = make_root(**kw)
    try:
        return dg.decide(NOW, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def latest(date=TODAY, trained="2026-10-03 06:12 JST", venues=VENUES):
    return {"date": date, "generated_at": "2026-10-03 06:20 JST", "model_trained_at": trained, "venues": venues}


def ptr(trained_at):
    return {"v": 1, "asset": "model-20261003-0612-0123456789ab.tar.gz", "trained_at": trained_at}


def test_pointer_missing_does_not_skip():
    """参照先が無い(導入直後): 省かない。最初に着弾した回が配布まで行う。"""
    skip, reason = decide(latest=latest())
    assert skip is False and "未配布" in reason and "None" in reason, reason


def test_pointer_from_yesterday_does_not_skip():
    """参照先が前日のまま(本日の配布に失敗): 省かない。次の回が学習と配布をやり直す。"""
    skip, reason = decide(latest=latest(), pointer=ptr("2026-10-02 06:05 JST"))
    assert skip is False and "未配布" in reason and "2026-10-02 06:05 JST" in reason, reason


def test_pointer_from_today_skips():
    """参照先が本日: 当日分は公開・配布済みなので省く。"""
    skip, reason = decide(latest=latest(), pointer=ptr("2026-10-03 06:12 JST"))
    assert skip is True and "公開済み" in reason, reason


def test_broken_pointer_does_not_skip():
    for bad in ("{not json", "[]", '"text"', "{}", '{"trained_at": null}', '{"trained_at": 20261003}'):
        skip, reason = decide(latest=latest(), pointer=bad)
        assert skip is False and "未配布" in reason, (bad, reason)


def test_earlier_conditions_are_unchanged():
    p = ptr("2026-10-03 06:12 JST")
    assert decide(pointer=p) == (False, "当日の予測が未公開")
    assert decide(latest=latest(date=YDAY), pointer=p) == (False, "当日の予測が未公開")
    assert decide(latest=latest(venues=[]), pointer=p) == (False, "当日の予測にレースが無い")
    skip, reason = decide(latest=latest(trained="2026-10-02 06:05 JST"), pointer=p)     # 自己復旧(前日モデル)
    assert skip is False and "本日学習のモデルで作られていない" in reason, reason
    # 前日の結果が実績に未反映なら省かない / 反映済みなら省く
    yp = {"date": YDAY, "venues": VENUES}
    skip, reason = decide(latest=latest(), pointer=p, yday_pred=yp, accuracy={"days": []})
    assert skip is False and "実績に未反映" in reason, reason
    skip, reason = decide(latest=latest(), pointer=p, yday_pred=yp, accuracy={"days": [{"date": YDAY}]})
    assert skip is True, reason


def test_pointer_check_comes_before_results_check():
    """配布の判定は、予測の判定の後・前日結果の判定の前に行う。"""
    yp = {"date": YDAY, "venues": VENUES}
    skip, reason = decide(latest=latest(), yday_pred=yp, accuracy={"days": []})
    assert skip is False and "未配布" in reason, reason


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok   " + name)
    print("%d tests passed" % n)
    print("ALL OK")

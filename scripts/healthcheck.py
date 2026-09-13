# -*- coding: utf-8 -*-
"""アリテイ朝次処理の健全性判定エンジン(全監視層が共有する唯一の判定器)。

exit code: 0=正常 / 1=警告 / 2=異常(critical)

使い方:
    python scripts/healthcheck.py                  # ローカルの latest.json を判定
    python scripts/healthcheck.py --source url     # 公開URLの latest.json を判定
    python scripts/healthcheck.py --json           # 判定結果をJSONで出力
    python scripts/healthcheck.py --quiet          # 終了コードのみ

判定に使ってはいけないもの(2026-09-12の監査で無効と実測済み):
    HTTP Last-Modified / ETag / content-length
        → GitHub Pages のデプロイ時刻であり、6日間変わっていないファイルでも
          latest.json と1秒違わぬ同一値を返す。鮮度判定には一切使えない。
    results_updated_at / odds_updated_at / live_updated_at
        → 予測が前日のままでも、結果・オッズ更新の副作用で「今日」に化ける。

依存は標準ライブラリのみ(pip install 不要)。
"""
import argparse
import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
JST = timezone(timedelta(hours=9))
PUBLIC_URL = "https://t-fuji777.github.io/kyotei-ai/predictions/latest.json"
DEFAULT_THRESHOLDS = ROOT / "scripts" / "health_thresholds.json"
TIMEOUT_SEC = 20


def jst_now():
    return datetime.now(JST)


def hhmm_to_min(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def load_thresholds(path=None):
    p = Path(path) if path else DEFAULT_THRESHOLDS
    return json.loads(p.read_text(encoding="utf-8"))


def load_local(ymd, file=None):
    if file:
        return json.loads(Path(file).read_text(encoding="utf-8"))
    name = "latest.json" if ymd is None else "%s.json" % ymd
    return json.loads((ROOT / "docs" / "predictions" / name).read_text(encoding="utf-8"))


def load_url(ymd):
    base = PUBLIC_URL if ymd is None else PUBLIC_URL.replace("latest.json", "%s.json" % ymd)
    url = "%s?h=%d" % (base, int(time.time()))
    req = urllib.request.Request(url, headers={
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "User-Agent": "aritei-healthcheck",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
        return json.loads(r.read().decode("utf-8"))


def _median(a):
    return statistics.median(a) if a else None


def collect_metrics(pred, th):
    """退化検知用の指標をレース横断で集計する。"""
    wp_max, p1, t3, confs = [], [], [], []
    struct_bad = 0
    races = 0
    tol = th["wp_sum_tol"]
    want_picks = th["picks_len_expect"]
    for v in pred.get("venues") or []:
        for r in v.get("races") or []:
            races += 1
            boats = r.get("boats") or []
            picks = r.get("picks") or []
            wps = [b.get("wp") for b in boats if isinstance(b.get("wp"), (int, float))]
            if wps:
                wp_max.append(max(wps))
            ps = [x.get("p") for x in picks if isinstance(x.get("p"), (int, float))]
            if ps:
                p1.append(ps[0])
                t3.append(sum(ps[:3]))
            if r.get("conf"):
                confs.append(r["conf"])
            bad = len(picks) != want_picks
            if wps and abs(sum(wps) - 1.0) > tol:
                bad = True
            if bad:
                struct_bad += 1
    return {
        "races": races,
        "wp_med": _median(wp_max),
        "p1_med": _median(p1),
        "top3_med": _median(t3),
        "conf_c_ratio": (confs.count("C") / len(confs)) if confs else None,
        "struct_bad_ratio": (struct_bad / races) if races else None,
    }


def evaluate(pred, th, now=None, gate=True):
    """判定本体。(level, criticals, warnings, metrics) を返す。"""
    now = now or jst_now()
    today = now.strftime("%Y%m%d")
    past_gate = (now.hour * 60 + now.minute) >= hhmm_to_min(th["morning_gate_hhmm"])
    crit, warn = [], []
    no_race = bool(pred.get("no_race"))

    # --- hard gate: 生存判定。これが唯一の確実な指標 ---
    if gate and past_gate:
        if pred.get("date") != today:
            crit.append("C1 date=%s が本日(%s)でない" % (pred.get("date"), today))
        ga = pred.get("generated_at") or ""
        if not ga:
            crit.append("C2 generated_at が無い")
        elif ga[:10].replace("-", "") != today:
            crit.append("C2 generated_at=%s の日付が本日でない" % ga)

    # --- 中身の有無(空JSONがself-healを永久無効化する穴の検知) ---
    m = collect_metrics(pred, th)
    if not no_race and m["races"] == 0:
        crit.append("C3 venues/races が空(番組表の取得またはパースに失敗)")

    # --- 生成時刻の遅さ ---
    ga = pred.get("generated_at") or ""
    if len(ga) >= 16 and ":" in ga[11:16]:
        gmin = hhmm_to_min(ga[11:16])
        if gmin > hhmm_to_min(th["late_crit_hhmm"]):
            crit.append("C4 generated_at=%s が %s より遅い" % (ga[11:16], th["late_crit_hhmm"]))
        elif gmin > hhmm_to_min(th["late_warn_hhmm"]):
            warn.append("W1 generated_at=%s が %s より遅い" % (ga[11:16], th["late_warn_hhmm"]))

    # --- モデル退化(2026-06-12に実発生した無症状障害) ---
    if m["races"] > 0:
        hits = []
        if m["wp_med"] is not None and m["wp_med"] < th["wp_med_min"]:
            hits.append("wp_med=%.4f<%s" % (m["wp_med"], th["wp_med_min"]))
        if m["p1_med"] is not None and m["p1_med"] < th["p1_med_min"]:
            hits.append("p1_med=%.4f<%s" % (m["p1_med"], th["p1_med_min"]))
        if m["top3_med"] is not None and m["top3_med"] < th["top3_med_min"]:
            hits.append("top3_med=%.4f<%s" % (m["top3_med"], th["top3_med_min"]))
        if m["conf_c_ratio"] is not None and m["conf_c_ratio"] > th["conf_c_ratio_max"]:
            hits.append("conf_C=%.4f>%s" % (m["conf_c_ratio"], th["conf_c_ratio_max"]))
        if len(hits) >= th["degrade_crit_hits"]:
            crit.append("C5 モデル退化の疑い: " + " / ".join(hits))
        elif hits:
            warn.append("W3 退化指標が1つヒット: " + " / ".join(hits))

        if m["struct_bad_ratio"] is not None and m["struct_bad_ratio"] > th["struct_bad_ratio_max"]:
            crit.append("C6 構造異常レースが%.1f%%(picks数またはwp合計が不正)" % (m["struct_bad_ratio"] * 100))

        for v in pred.get("venues") or []:
            n = len(v.get("races") or [])
            if n != th["races_per_venue_expect"]:
                warn.append("W2 %s(%s) のレース数が%d" % (v.get("name"), v.get("code"), n))

    level = "critical" if crit else ("warning" if warn else "ok")
    return level, crit, warn, m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["local", "url"], default="local")
    ap.add_argument("--date", default=None, help="YYYYMMDD (省略=latest.json)")
    ap.add_argument("--file", default=None, help="判定対象のJSONを直接指定(--source local のとき)")
    ap.add_argument("--thresholds", default=None)
    ap.add_argument("--no-gate", action="store_true", help="日付ゲートを無効化(過去日の検証用)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    th = load_thresholds(a.thresholds)
    now = jst_now()
    stamp = now.strftime("%Y-%m-%d %H:%M JST")
    try:
        pred = load_url(a.date) if a.source == "url" else load_local(a.date, a.file)
    except Exception as e:
        out = {"level": "critical", "date": None, "generated_at": None,
               "criticals": ["C0 取得/パース不能: %s: %s" % (type(e).__name__, e)],
               "warnings": [], "metrics": {}, "checked_at": stamp, "source": a.source}
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        elif not a.quiet:
            print("[critical] " + out["criticals"][0])
        return 2

    level, crit, warn, m = evaluate(pred, th, now=now, gate=not a.no_gate)
    out = {
        "level": level,
        "date": pred.get("date"),
        "generated_at": pred.get("generated_at"),
        "model_trained_at": pred.get("model_trained_at"),
        "venues": len(pred.get("venues") or []),
        "criticals": crit,
        "warnings": warn,
        "metrics": m,
        "checked_at": stamp,
        "source": a.source,
    }
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif not a.quiet:
        print("[%s] date=%s generated_at=%s venues=%s races=%s"
              % (level, pred.get("date"), pred.get("generated_at"), out["venues"], m["races"]))
        for c in crit:
            print("  critical: " + c)
        for w in warn:
            print("  warning : " + w)
    return {"ok": 0, "warning": 1, "critical": 2}[level]


if __name__ == "__main__":
    sys.exit(main())

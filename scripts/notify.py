# -*- coding: utf-8 -*-
"""厳選プランの確定(conf)と結果(res)を外部Webhook/Web Pushへ通知する。松は2026-09-01終売(mt=1は今後発生しない)。

環境変数 NOTIFY_WEBHOOK が未設定/空ならWebhook送信は行わない。設定時のみ、
送信先URLからTelegram(api.telegram.orgを含む)/ Discord互換 を自動判別してPOSTする。

環境変数 PUSH_SUBS_URL / PUSH_AUTH_KEY / VAPID_PRIVATE が全て設定されている場合のみ、
Cloudflare Worker(push-worker/)経由でホーム画面追加PWAへ本物のWeb Pushを送信する
(pywebpush未インストール環境ではスキップしローカル互換を保つ)。

Webhook/Web Pushはいずれも未設定なら notify_events() は即座に何もしない
(既存パイプラインの挙動に一切影響しない)。どちらか一方でも成功すれば送信成功扱いとする。

重複防止: docs/predictions/notify_state.json に送信済みイベントidを保存する。
イベントidは "conf-{ymd}-{vcode}-{no}" / "res-{ymd}-{vcode}-{no}" の形式で、
日付が変われば当日分以外は間引く(肥大防止)。
"""
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE_PATH = ROOT / "docs" / "predictions" / "notify_state.json"
APP_TAG = "[アリテイ]"
TIMEOUT_SEC = 5


def _atomic_write_text(path: Path, txt: str) -> None:
    """一時ファイル+os.replaceで原子的に書き込む(中断時の破損防止)"""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(txt, encoding="utf-8")
    os.replace(tmp, path)


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"sent": []}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(STATE_PATH, json.dumps(state, ensure_ascii=False))


def _id_ymd(event_id: str):
    parts = event_id.split("-")
    return parts[1] if len(parts) > 1 else None


def _prune_state(state: dict, ymd: str) -> dict:
    """日付が変わったら前日以前のidは間引く(肥大防止)。当日ymdのものだけ残す。"""
    kept = [i for i in state.get("sent", []) if _id_ymd(i) == ymd]
    return {"sent": kept}


def send_webhook(url: str, text: str) -> bool:
    """urllibのみでPOST。Telegram(URLにapi.telegram.orgを含む)は
    {"chat_id":..., "text":...} をsendMessageへ、それ以外はDiscord互換として
    {"content":...} を送る。タイムアウト5秒。失敗はprintして無視しFalseを返す
    (呼び出し元のパイプラインを止めない)。

    テストで差し替えやすいよう、実際のネットワーク送信はこの関数に閉じている。
    """
    try:
        if "api.telegram.org" in url:
            parsed = urllib.parse.urlparse(url)
            qs = urllib.parse.parse_qs(parsed.query)
            chat_id = (qs.get("chat_id") or [None])[0] or os.environ.get("NOTIFY_CHAT_ID")
            if not chat_id:
                print("notify: telegram chat_id not found (set NOTIFY_CHAT_ID or ?chat_id=)")
                return False
            payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        else:
            payload = json.dumps({"content": text}).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            resp.read()
        return True
    except Exception as e:
        print(f"notify: send failed ({e})")
        return False


def _plan_label(r: dict) -> str:
    tk, mt = r.get("tk") == 1, r.get("mt") == 1
    if tk and mt:
        return "厳選・松"
    if tk:
        return "厳選"
    if mt:
        return "松"
    return ""


def _conf_events(pred: dict, ymd: str):
    """確定イベント: tk==1 or mt==1のレースを検出し(id, 文面)を返す。"""
    out = []
    for v in pred.get("venues") or []:
        vname = v.get("name", "")
        vcode = v.get("code")
        for r in v.get("races") or []:
            if r.get("tk") != 1 and r.get("mt") != 1:
                continue
            no = r.get("no")
            eid = f"conf-{ymd}-{vcode}-{no}"
            plan = _plan_label(r)
            pt = r.get("pt")
            time_part = f" {pt}" if pt else ""
            deadline = r.get("deadline", "")
            msg = f"{APP_TAG} {plan}プラン確定{time_part} / {vname}{no}R 締切{deadline}"
            out.append((eid, msg))
    return out


def _res_events(pred: dict, ymd: str):
    """結果イベント: tk==1 or mt==1のレースにresult.orderが付いたら(id, 文面)を返す。
    厳選=picks上位3点内、(終売済みの)松=上位4点内で的中判定。両該当ならそれぞれ記載する。"""
    out = []
    for v in pred.get("venues") or []:
        vname = v.get("name", "")
        vcode = v.get("code")
        for r in v.get("races") or []:
            tk, mt = r.get("tk") == 1, r.get("mt") == 1
            if not tk and not mt:
                continue
            res = r.get("result") or {}
            order = res.get("order")
            if not order:
                continue
            no = r.get("no")
            eid = f"res-{ymd}-{vcode}-{no}"
            picks = [p.get("c") for p in (r.get("picks") or [])]
            pay = res.get("pay3t")
            plans = []
            if tk:
                plans.append(("厳選", order in picks[:3]))
            if mt:
                plans.append(("松", order in picks[:4]))
            multi = len(plans) > 1
            lines = []
            for label, hit in plans:
                prefix = f"{label} " if multi else ""
                if hit:
                    if pay is not None:
                        lines.append(f"{prefix}的中 {vname}{no}R {order} 払戻{pay}円")
                    else:
                        lines.append(f"{prefix}的中 {vname}{no}R {order}")
                else:
                    lines.append(f"{prefix}不的中 {vname}{no}R")
            out.append((eid, "\n".join(lines)))
    return out


def _push_ready() -> bool:
    """Web Push送信に必要な環境変数が全て設定されているか判定する。"""
    return bool(
        (os.environ.get("PUSH_SUBS_URL") or "").strip()
        and (os.environ.get("PUSH_AUTH_KEY") or "").strip()
        and (os.environ.get("VAPID_PRIVATE") or "")
    )


def _fetch_push_subs(subs_url: str, auth_key: str) -> list:
    """GET {subs_url}/subs?key=... で購読一覧(JSON配列)を取得する。
    失敗時は例外を送出せず空リストを返す。"""
    try:
        qs = urllib.parse.urlencode({"key": auth_key})
        req = urllib.request.Request(f"{subs_url.rstrip('/')}/subs?{qs}", method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"notify: push subs fetch failed ({e})")
        return []


def _delete_push_sub(subs_url: str, auth_key: str, sub_id: str) -> None:
    """404/410を返した購読をWorker側KVから削除する(送信側の掃除)。"""
    try:
        qs = urllib.parse.urlencode({"key": auth_key, "id": sub_id})
        req = urllib.request.Request(f"{subs_url.rstrip('/')}/sub?{qs}", method="DELETE")
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            resp.read()
    except Exception as e:
        print(f"notify: push sub delete failed ({e})")


def send_push(text: str) -> bool:
    """購読中の全端末へpywebpushでWeb Push通知を送信する。
    PUSH_SUBS_URL/PUSH_AUTH_KEY/VAPID_PRIVATEのいずれかが未設定なら何もせずFalseを返す。
    pywebpush未インストール環境ではImportErrorをcatchしてスキップする(ローカル互換)。
    1件以上送信成功でTrueを返す(呼び出し元のsent登録判定用)。"""
    subs_url = (os.environ.get("PUSH_SUBS_URL") or "").strip()
    auth_key = (os.environ.get("PUSH_AUTH_KEY") or "").strip()
    vapid_private = os.environ.get("VAPID_PRIVATE") or ""
    if not (subs_url and auth_key and vapid_private):
        return False
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("notify: pywebpush not installed, skip web push")
        return False
    subs = _fetch_push_subs(subs_url, auth_key)
    if not subs:
        # 購読者ゼロ=届け先が無いだけなので配送済み扱い(後から購読した端末に
        # 当日分のバックログが一斉着弾するのを防ぐ。60秒毎の再送スパムも防止)。
        return True
    # Web Push本文は暗号化後4096バイト上限。日本語の長文で超えないよう本文を切り詰める。
    if len(text) > 900:
        text = text[:900] + "\n(続きはアプリで)"
    payload = json.dumps({"title": "アリテイ", "body": text}, ensure_ascii=False)
    ok = False
    for entry in subs:
        sub = entry.get("subscription") if isinstance(entry, dict) else None
        if not sub:
            continue
        sub_id = entry.get("id") or hashlib.sha256(
            sub.get("endpoint", "").encode("utf-8")
        ).hexdigest()
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": "mailto:t.fujino@meihogp.co.jp"},
            )
            ok = True
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                _delete_push_sub(subs_url, auth_key, sub_id)
            else:
                print(f"notify: web push failed ({e})")
        except Exception as e:
            print(f"notify: web push failed ({e})")
    return ok


def notify_events(pred: dict, ymd: str) -> None:
    """当日予測dictを走査し、確定/結果イベントを検出。未送信分のみ1回のPOST/Web Pushに
    まとめて送信し、状態ファイルへ記録する(重複防止)。
    NOTIFY_WEBHOOKもWeb Push設定(PUSH_SUBS_URL等)も未設定/空なら何もしない。"""
    url = (os.environ.get("NOTIFY_WEBHOOK") or "").strip()
    push_ready = _push_ready()
    if not url and not push_ready:
        return
    state = _prune_state(_load_state(), ymd)
    sent = set(state.get("sent", []))
    events = _conf_events(pred, ymd) + _res_events(pred, ymd)
    new_events = [(eid, msg) for eid, msg in events if eid not in sent]
    if not new_events:
        _save_state(state)
        return
    # 1800字を上限にチャンク分割して送信(Discord 2000/Telegram 4096の上限対策)。
    # 送信に成功したチャンクのidだけをsentに記録し、失敗分は次サイクル(60秒後)に再送する。
    # webhook/Web Pushは独立で判定し、どちらか一方でも成功すればsent登録する
    # (両方失敗の場合のみ再送対象)。webhook未設定でWeb Pushのみの構成でも動作する。
    chunks = []
    cur_ids, cur_msgs, cur_len = [], [], 0
    for eid, msg in new_events:
        if cur_msgs and cur_len + len(msg) + 1 > 1800:
            chunks.append((cur_ids, cur_msgs))
            cur_ids, cur_msgs, cur_len = [], [], 0
        cur_ids.append(eid)
        cur_msgs.append(msg)
        cur_len += len(msg) + 1
    if cur_msgs:
        chunks.append((cur_ids, cur_msgs))
    for ids, msgs in chunks:
        text = "\n".join(msgs)
        if not text.startswith(APP_TAG):
            text = APP_TAG + "\n" + text
        webhook_ok = send_webhook(url, text) if url else False
        push_ok = send_push(text) if push_ready else False
        if webhook_ok or push_ok:
            sent.update(ids)
        else:
            print(f"notify send failed: {len(ids)} event(s) will retry next cycle")
    _save_state({"sent": sorted(sent)})

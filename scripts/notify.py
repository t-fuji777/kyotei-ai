# -*- coding: utf-8 -*-
"""厳選プランの確定(conf)と結果(res)を外部Webhook/Web Pushへ通知する。松は2026-09-01終売(mt=1は今後発生しない)。

環境変数 NOTIFY_WEBHOOK が未設定/空ならWebhook送信は行わない。設定時のみ、
送信先URLからTelegram(api.telegram.orgを含む)/ Discord互換 を自動判別してPOSTする。

環境変数 VAPID_PRIVATE(Web Push の秘密鍵)が設定されている場合のみ、購読を預かる
Cloudflare Worker(push-worker/)から購読一覧を取り、ホーム画面に追加したアプリへ Web Push を送る
(pywebpush が無い環境ではスキップする)。Worker への認証は、同じ秘密鍵で署名した短命のトークンで
行う(合言葉は使わない)。Worker のURLは PUSH_SUBS_URL で上書きできる(既定は PUSH_SUBS_URL_DEFAULT)。

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
# 購読を預かる Worker(push-worker/)のURL。秘密ではない(docs/index.html の PUSH_WORKER と同じ値)。
# 空にすると Web Push を送らない(止めたい時はここを空にして main へ入れる)。
PUSH_SUBS_URL_DEFAULT = "https://aritei-push.t-fujino.workers.dev"
PUSH_CONTACT = "mailto:t.fujino@meihogp.co.jp"
PUSH_TIMEOUT_SEC = 10     # 1件の送信にかける上限。応答しない宛先で開催中の処理を止めない
PUSH_TTL_SEC = 900        # 端末が圏外・省電力中でも、この時間は配信サービスが保持して届ける
PUSH_MAX_SUBS = 100       # 1回に送る購読数の上限
PUSH_TOTAL_SEC = 30       # 1回の送信全体にかける上限。応答の遅い宛先が並んでも開催中の処理を止めない


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
            # 通知だけ見て買えるよう、上位3点を添える(打刻後は買い目が凍結されている)。
            picks = [p.get("c") for p in (r.get("picks") or [])[:3] if p.get("c")]
            if picks:
                msg += "\n買い目 " + " / ".join(picks)
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


def _push_cfg():
    """(Worker のURL, 秘密鍵)。どちらかが無ければ Web Push は送らない。"""
    subs_url = (os.environ.get("PUSH_SUBS_URL") or "").strip() or PUSH_SUBS_URL_DEFAULT
    if not subs_url.startswith("https://"):
        subs_url = ""
    return subs_url.rstrip("/"), (os.environ.get("VAPID_PRIVATE") or "").strip()


def _push_ready() -> bool:
    """Web Push送信に必要な設定(Worker のURLと秘密鍵)が揃っているか判定する。"""
    subs_url, private = _push_cfg()
    return bool(subs_url and private)


def _load_vapid(private: str):
    """秘密鍵の文字列から署名用のオブジェクトを作る。PEM でも base64url(DER / 生32バイト)でもよい。"""
    from py_vapid import Vapid
    if "-----BEGIN" in private:
        return Vapid.from_pem(private.encode("utf-8"))
    return Vapid.from_string(private)


def _vapid_public_b64(vapid) -> str:
    """公開鍵(非圧縮65バイト)の base64url。アプリ側の VAPID_PUB と同じ形。"""
    import base64
    from cryptography.hazmat.primitives import serialization
    raw = vapid.public_key.public_bytes(serialization.Encoding.X962,
                                        serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sender_headers(subs_url: str, vapid) -> dict:
    """Worker の送信側専用エンドポイント(/subs, DELETE /sub)に付ける認証ヘッダ。
    宛先(aud)を Worker 自身にした5分有効のトークンを、Web Push の秘密鍵で署名する。"""
    import time
    parsed = urllib.parse.urlparse(subs_url)
    claims = {"aud": f"{parsed.scheme}://{parsed.netloc}", "exp": int(time.time()) + 300,
              "sub": PUSH_CONTACT}
    # 既定の Python-urllib の User-Agent は Cloudflare に弾かれることがあるので明示する。
    return {"Authorization": vapid.sign(claims)["Authorization"], "User-Agent": "aritei-notify"}


def _fetch_push_subs(subs_url: str, headers: dict):
    """GET {subs_url}/subs で購読一覧(JSON配列)を取得する。失敗時は例外を出さず None を返す
    (空の一覧=購読者なし、とは区別する)。"""
    try:
        req = urllib.request.Request(f"{subs_url}/subs", method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else None
    except Exception as e:
        print(f"notify: push subs fetch failed ({e})")
        return None


def _delete_push_sub(subs_url: str, headers: dict, sub_id: str) -> None:
    """404/410を返した購読をWorker側KVから削除する(送信側の掃除)。"""
    try:
        qs = urllib.parse.urlencode({"id": sub_id})
        req = urllib.request.Request(f"{subs_url}/sub?{qs}", method="DELETE", headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            resp.read()
    except Exception as e:
        print(f"notify: push sub delete failed ({e})")


def _push_body(text: str) -> str:
    """通知の本文。題名が「アリテイ」なので、行頭の [アリテイ] は省く。"""
    lines = [ln[len(APP_TAG):].strip() if ln.startswith(APP_TAG) else ln for ln in text.split("\n")]
    body = "\n".join(ln for ln in lines if ln)
    # Web Push本文は暗号化後4096バイト上限。日本語の長文で超えないよう本文を切り詰める。
    if len(body) > 900:
        body = body[:900] + "\n(続きはアプリで)"
    return body


def send_push(text: str) -> bool:
    """購読中の全端末へpywebpushでWeb Push通知を送信する。
    設定(Worker のURL・秘密鍵)が無ければ何もせずFalseを返す。pywebpush が無い環境でも
    スキップする。1件以上送信成功、または購読者ゼロでTrueを返す(呼び出し元のsent登録判定用)。
    購読一覧を取れなかった時は False(次の周回で送り直す)。"""
    subs_url, private = _push_cfg()
    if not (subs_url and private):
        return False
    try:
        from pywebpush import webpush, WebPushException
        vapid = _load_vapid(private)
        headers = _sender_headers(subs_url, vapid)
    except ImportError:
        print("notify: pywebpush not installed, skip web push")
        return False
    except Exception as e:
        print(f"notify: web push setup failed ({type(e).__name__}: {e})")
        return False
    subs = _fetch_push_subs(subs_url, headers)
    if subs is None:
        return False
    if not subs:
        # 購読者ゼロ=届け先が無いだけなので配送済み扱い(後から購読した端末に
        # 当日分のバックログが一斉着弾するのを防ぐ。60秒毎の再送スパムも防止)。
        return True
    if len(subs) > PUSH_MAX_SUBS:
        print(f"notify: {len(subs)} subscriptions, sending to the first {PUSH_MAX_SUBS} only")
        subs = subs[:PUSH_MAX_SUBS]
    payload = json.dumps({"title": "アリテイ", "body": _push_body(text)}, ensure_ascii=False)
    ok = False
    import time
    t0 = time.monotonic()
    for i, entry in enumerate(subs):
        if time.monotonic() - t0 > PUSH_TOTAL_SEC:
            print(f"notify: web push time budget exceeded, {len(subs) - i} subscription(s) not sent")
            break
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
                vapid_private_key=vapid,
                vapid_claims={"sub": PUSH_CONTACT},
                timeout=PUSH_TIMEOUT_SEC,
                # 既定の TTL は0(端末が今つながっていなければ捨てる)。スマホは省電力で
                # 切れていることが多いので保持させる。Urgency: high は省電力中でもすぐ届ける指定。
                ttl=PUSH_TTL_SEC,
                headers={"Urgency": "high"},
            )
            ok = True
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                _delete_push_sub(subs_url, headers, sub_id)
                print(f"notify: expired subscription removed ({status})")
            else:
                print(f"notify: web push failed (status={status})")
        except Exception as e:
            print(f"notify: web push failed ({type(e).__name__})")
    return ok


def selftest(send_text: str = "") -> int:
    """Web Push の設定を確かめる(手動実行のワークフロー用)。秘密鍵そのものは表示しない。
    1) 秘密鍵から導いた公開鍵が、アプリ(docs/index.html)の VAPID_PUB と一致するか
    2) Worker から購読一覧を取れるか 3) send_text があれば実際に送る。0=正常 / 1=異常。
    Worker のURLが未設定でも 1) までは確かめる。"""
    import re
    subs_url, private = _push_cfg()
    print(f"selftest: worker={subs_url or '(未設定)'} / 秘密鍵={'あり' if private else 'なし'}")
    if not private:
        return 1
    try:
        import pywebpush  # noqa: F401
        vapid = _load_vapid(private)
        pub = _vapid_public_b64(vapid)
    except Exception as e:
        print(f"selftest: 秘密鍵を読めない ({type(e).__name__})")
        return 1
    html = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'const VAPID_PUB="([A-Za-z0-9_-]+)"', html)
    app_pub = m.group(1) if m else ""
    same = bool(app_pub) and app_pub == pub
    print(f"selftest: 公開鍵の一致={'はい' if same else 'いいえ'} (秘密鍵から導いた公開鍵 {pub[:12]}… / アプリ {app_pub[:12]}…)")
    if not subs_url:
        print("selftest: Worker のURLが未設定のため、購読一覧の確認は行わない")
        return 0 if same else 1
    subs = _fetch_push_subs(subs_url, _sender_headers(subs_url, vapid))
    print(f"selftest: 購読一覧の取得={'失敗' if subs is None else '成功'} / 購読数={'-' if subs is None else len(subs)}")
    if not same or subs is None:
        return 1
    if send_text:
        sent = send_push(send_text)
        print(f"selftest: テスト通知の送信={'成功' if sent else '失敗(届け先に送れなかった)'}")
        return 0 if sent else 1
    return 0


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


def notify_text(text: str) -> bool:
    """運用アラート(パイプライン障害)の送信。厳選イベント通知とは別系統。

    notify_events() の重複抑止state(notify_state.json)は通さない。連投の抑止は
    呼び出し側(watchdog.yml は1日3回)で行い、ここには状態を持たせない。
    NOTIFY_WEBHOOK / Web Push のいずれも未設定なら何もせず False を返すだけなので、
    未設定環境での挙動は現行と変わらない。
    """
    if not text.startswith(APP_TAG):
        text = APP_TAG + "\n" + text
    url = (os.environ.get("NOTIFY_WEBHOOK") or "").strip()
    ok = send_webhook(url, text) if url else False
    if _push_ready():
        ok = send_push(text) or ok
    return ok


if __name__ == "__main__":
    import sys as _sys
    # 運用アラート用: python scripts/notify.py --text "本文"
    if len(_sys.argv) >= 3 and _sys.argv[1] == "--text":
        sent = notify_text(_sys.argv[2])
        print("notify_text: sent" if sent else "notify_text: 送信先未設定のため送信せず")
        _sys.exit(0)
    # Web Push の設定確認: python scripts/notify.py --selftest ["送るテスト文"]
    if len(_sys.argv) >= 2 and _sys.argv[1] == "--selftest":
        _sys.exit(selftest(_sys.argv[2] if len(_sys.argv) >= 3 else ""))
    print("usage: python scripts/notify.py --text '本文' | --selftest ['テスト文']")
    _sys.exit(2)

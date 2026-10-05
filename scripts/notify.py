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

送らずに「送信済み」として記録だけするもの(_stale_ids): 締切を過ぎた確定、締切後に判定された
レース(ph=1)の確定と結果、締切から RES_MAX_AGE_MIN 分を超えた結果。もう買えない確定や、何時間も
前の結果が後からまとめて届くのを防ぐ。

開催中の処理(update_all.py)の中から呼ばれるので、通知のために処理を待たせない:
Web Push は並列に送り、1回の送信全体を PUSH_TOTAL_SEC 秒で必ず切り上げる。例外は外へ出さない。
"""
import collections
import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
STATE_PATH = ROOT / "docs" / "predictions" / "notify_state.json"
JST = timezone(timedelta(hours=9))
APP_TAG = "[アリテイ]"
TIMEOUT_SEC = 5
# 結果の通知は、締切からこの分数を超えたら送らない(記録だけする)。結果は通常、締切の10〜20分後に出る。
RES_MAX_AGE_MIN = 120
# 購読を預かる Worker(push-worker/)のURL。秘密ではない(docs/index.html の PUSH_WORKER と同じ値)。
# 空にすると Web Push を送らない(止めたい時はここを空にして main へ入れる)。
PUSH_SUBS_URL_DEFAULT = "https://aritei-push.t-fujino.workers.dev"
PUSH_CONTACT = "mailto:t.fujino@meihogp.co.jp"
PUSH_TITLE = "アリテイ"   # 通知の題名の既定値
# 1件の送信の時間切れ(接続, 応答待ち)秒。接続の時間切れは宛先ホストのIPアドレスの数だけ繰り返される
# ので、これだけでは上限にならない。上限を実際に守るのは下の PUSH_TOTAL_SEC(待つ側で切り上げる)。
PUSH_TIMEOUT = (3, 8)
PUSH_TOTAL_SEC = 30       # 1回の送信全体(購読一覧の取得を含む)にかける上限。開催中の処理を止めない
PUSH_WORKERS = 8          # 並列に送る本数。応答しない宛先が混じっていても、他の宛先を待たせない
PUSH_RETRY_WAIT_SEC = 0.5  # 配信サービスの一時的な失敗(5xx・接続の失敗)は、少しおいて1回だけ送り直す
PUSH_FETCH_TRIES = 2      # 購読一覧の取得を試す回数
PUSH_FETCH_WAIT_SEC = 1   # 取得をやり直す前に待つ秒数
# 保持時間(TTL): 端末が圏外・省電力中でも、この時間は配信サービスが保持して届ける。
PUSH_TTL_SEC = 900        # 既定(運用アラートなど)。確定は締切までの残り時間に合わせて短くする
PUSH_TTL_MIN_SEC = 60     # 確定の下限。締切の直前でも、届く機会を1分は残す
PUSH_TTL_RESULT_SEC = 3600  # 結果だけの通知。確定と違って急がないので、長めに保持させる
# 送ってよい宛先のホスト名(push-worker/worker.js と同じ規則。変える時は両方を揃える)。
PUSH_HOSTS = frozenset((
    "fcm.googleapis.com",                  # Chrome(Android / パソコン)
    "jmt17.google.com",                    # Chromium 系の一部
    "updates.push.services.mozilla.com",   # Firefox
    "web.push.apple.com",                  # iPhone / Safari
))
PUSH_HOST_RE = re.compile(r"[a-z0-9-]+\.notify\.windows\.com")   # Windows の Edge(fullmatch で使う)
PUSH_APPLE_HOST = "web.push.apple.com"


def _now_jst() -> datetime:
    """現在時刻(JST)。実行機は UTC で動くので、締切との比較は必ずここを通す。
    テストはこの関数を差し替えて時刻を固定する。"""
    return datetime.now(JST)


def _atomic_write_text(path: Path, txt: str) -> None:
    """一時ファイル+os.replaceで原子的に書き込む(中断時の破損防止)"""
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(txt, encoding="utf-8")
    os.replace(tmp, path)


def _load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"sent": []}
    # 中身が想定の形でなければ空として扱う。そのまま使うと以後ずっと例外になり、通知が出なくなる。
    if not isinstance(state, dict) or not isinstance(state.get("sent"), list):
        return {"sent": []}
    return state


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(STATE_PATH, json.dumps(state, ensure_ascii=False))


def _id_ymd(event_id: str):
    parts = event_id.split("-")
    return parts[1] if len(parts) > 1 else None


def _prune_state(state: dict, ymd: str) -> dict:
    """日付が変わったら前日以前のidは間引く(肥大防止)。当日ymdのものだけ残す。"""
    kept = [i for i in state.get("sent", []) if isinstance(i, str) and _id_ymd(i) == ymd]
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
    厳選=picks上位3点内、(終売済みの)松=上位4点内で的中判定。両該当ならそれぞれ記載する。
    着順が無く status だけの結果(中止・不成立)は、その旨を1行で返す。"""
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
            no = r.get("no")
            eid = f"res-{ymd}-{vcode}-{no}"
            if not order:
                # 中止・不成立: fetch_result は {"status": "中止" または "不成立"} だけを返す(着順なし)。
                # 確定を知らせたレースを音沙汰なしにしない。どちらも舟券は全額返還になる。
                status = res.get("status")
                if status:
                    out.append((eid, f"{status} {vname}{no}R(返還)"))
                continue
            picks = [p.get("c") for p in (r.get("picks") or [])]
            pay = res.get("pay3t")
            plans = []
            if tk:
                # 的中は確定した時点の買い目で数える(scripts/common.py の sengen_picks と同じ規則。
                # os は確定時の買い目の上位4点をその順で持つ)。確定の後に買い目が差し替わっても、
                # 通知で知らせた買い目と違う目で「的中」にしない。
                osd = r.get("os")
                top3 = list(osd.keys())[:3] if isinstance(osd, dict) and len(osd) >= 3 else picks[:3]
                plans.append(("厳選", order in top3))
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


def _mins_past_deadline(deadline, ymd: str):
    """締切を何分過ぎたか(締切前は負の値)。締切や日付を読めなければ None。"""
    try:
        h, m = map(int, str(deadline).split(":"))
        dl = datetime(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]), h, m, tzinfo=JST)
    except Exception:
        return None
    return (_now_jst() - dl).total_seconds() / 60


def _stale_ids(pred: dict, ymd: str) -> set:
    """送らずに「送信済み」として記録するイベントidの集合。
    - 確定: 締切を過ぎたもの、締切後に判定されたもの(ph=1)。もう買えないので知らせない。
      送信済みの記録が無いと何時間後でも「確定」として送られてしまう(記録を失った時、通知を
      有効にした初日、送信の失敗が締切まで続いた時など)。
    - 結果: ph=1 のレース(確定を知らせていないのに的中/不的中だけ届くのを防ぐ)と、締切から
      RES_MAX_AGE_MIN 分を超えたもの。
    締切を読めないレースは、時刻では判断しない(送る)。"""
    out = set()
    for v in pred.get("venues") or []:
        vcode = v.get("code")
        for r in v.get("races") or []:
            if r.get("tk") != 1 and r.get("mt") != 1:
                continue
            no = r.get("no")
            late = r.get("ph") == 1
            age = _mins_past_deadline(r.get("deadline"), ymd)
            if late or (age is not None and age >= 0):
                out.add(f"conf-{ymd}-{vcode}-{no}")
            if late or (age is not None and age > RES_MAX_AGE_MIN):
                out.add(f"res-{ymd}-{vcode}-{no}")
    return out


def _conf_secs_left(pred: dict, ymd: str) -> dict:
    """確定イベントid → 締切までの残り秒数(締切を読めないレースは入れない)。保持時間を決めるのに使う。"""
    out = {}
    for v in pred.get("venues") or []:
        for r in v.get("races") or []:
            if r.get("tk") != 1 and r.get("mt") != 1:
                continue
            age = _mins_past_deadline(r.get("deadline"), ymd)
            if age is not None:
                out[f"conf-{ymd}-{v.get('code')}-{r.get('no')}"] = -age * 60
    return out


def _event_ttl(ids, secs_left: dict) -> int:
    """1回の送信の保持時間(秒)。確定は締切を過ぎたら意味が無いので、締切までの残り時間に合わせる
    (端末が圏外から戻った時に、締切後に届くのを避ける)。結果だけなら急がないので長く保持させる
    (端末が15分以上つながらなくても捨てられない)。確定が複数ある時は、遅い方の締切に合わせる
    (まだ買えるレースの知らせを先に捨てない。文面に締切時刻が入っているので誤解は小さい)。"""
    confs = [i for i in ids if i.startswith("conf-")]
    if not confs:
        return PUSH_TTL_RESULT_SEC
    left = [secs_left[i] for i in confs if i in secs_left]
    if not left:
        return PUSH_TTL_SEC
    return int(max(PUSH_TTL_MIN_SEC, min(PUSH_TTL_SEC, max(left))))


def _event_tag(ids) -> str:
    """通知の tag。同じ出来事を送り直した時に、端末側で前の通知と置き換わる(重ならない)。
    1件ならそのイベントid、複数まとめなら、まとめたidから作る短い値。"""
    if len(ids) == 1:
        return ids[0]
    return "ev-" + hashlib.sha256("|".join(ids).encode("utf-8")).hexdigest()[:16]


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
    parsed = urllib.parse.urlparse(subs_url)
    claims = {"aud": f"{parsed.scheme}://{parsed.netloc}", "exp": int(time.time()) + 300,
              "sub": PUSH_CONTACT}
    # 既定の Python-urllib の User-Agent は Cloudflare に弾かれることがあるので明示する。
    return {"Authorization": vapid.sign(claims)["Authorization"], "User-Agent": "aritei-notify"}


def _call_with_deadline(fn, secs: float):
    """fn() を別スレッドで実行し、secs 秒だけ待つ。戻り値は (間に合ったか, fn の戻り値)。
    urllib の timeout は「接続先のアドレス1個ごと」「受信1回ごと」に効くだけで、名前解決には効かない。
    上限を確実に守るため、待つ側で切り上げる。間に合わなかったスレッドは裏に残り得るが(Python は
    走行中のスレッドを止められない)、daemon なのでプロセスの終了は妨げない。fn の中では print しない。"""
    box = []

    def run():
        try:
            box.append(fn())
        except Exception:
            box.append(None)   # 例外は待つ側へ「結果なし」として返す(スレッドの中で落とさない)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(secs)
    return (True, box[0]) if box else (False, None)


def _fetch_push_subs_once(subs_url: str, headers: dict):
    """GET {subs_url}/subs を1回試す。戻り値は (購読一覧 または None, 失敗の理由)。
    別スレッドから呼ばれるので print しない。"""
    try:
        req = urllib.request.Request(f"{subs_url}/subs", method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return None, str(e) or type(e).__name__
    if not isinstance(data, list):
        return None, "応答が配列でない"
    return data, ""


def _fetch_push_subs(subs_url: str, headers: dict):
    """購読一覧(JSON配列)を取得する。失敗時は例外を出さず None を返す
    (空の一覧=購読者なし、とは区別する)。
    一過性の失敗(Worker の一時的な不調など)で通知を落とさないよう、PUSH_FETCH_WAIT_SEC 秒おいて
    PUSH_FETCH_TRIES 回まで試す。1回の待ちは TIMEOUT_SEC+1 秒で切り上げる。"""
    for attempt in range(PUSH_FETCH_TRIES):
        if attempt:
            time.sleep(PUSH_FETCH_WAIT_SEC)
        done, res = _call_with_deadline(lambda: _fetch_push_subs_once(subs_url, headers), TIMEOUT_SEC + 1)
        subs, why = (res or (None, "想定外の失敗")) if done else (None, "時間切れ")
        if subs is not None:
            return subs
        print(f"notify: 購読一覧を取れなかった ({why}) [{attempt + 1}/{PUSH_FETCH_TRIES}回目]")
    return None


def _delete_push_sub(subs_url: str, headers: dict, sub_id: str) -> bool:
    """届かなくなった購読を Worker 側の保管から消す(送信側の掃除)。成否を返す。
    id は1回の要求に1個だけ付ける(設置済みの Worker が古い版でも通るように)。
    送信の後に本体のスレッドから1件ずつ呼ぶ(並べて出さない)。"""
    try:
        qs = urllib.parse.urlencode({"id": sub_id})
        req = urllib.request.Request(f"{subs_url}/sub?{qs}", method="DELETE", headers=headers)
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            resp.read()
        return True
    except Exception:
        return False


def _endpoint_ok(endpoint) -> bool:
    """宛先URLが取り決めの形か。Worker(push-worker/worker.js)と同じ規則:
    https のみ・ポート指定なし・ユーザー情報なし・空白/バックスラッシュ/非ASCIIなし・
    ホスト名は PUSH_HOSTS の完全一致か PUSH_HOST_RE。
    Worker も登録時に検査するが、送信側でも確かめる: 購読の登録は誰でもできるので、検査が甘かった頃に
    保管された宛先や、応答しない宛先(ポートつき等)へ接続しに行って送信全体を遅らせないため。
    送信に使う requests と解釈が食い違わないよう、形を狭く限る(バックスラッシュは requests では
    ホスト名の区切りになる)。そのうえで、requests が実際に使う解析器(urllib3)の結果とも突き合わせる。"""
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        return False
    if not re.fullmatch(r"https://[!-~]+", endpoint) or "\\" in endpoint:
        return False
    authority = re.split(r"[/?#]", endpoint[len("https://"):], maxsplit=1)[0]
    if "@" in authority or ":" in authority:
        return False
    host = authority.lower()
    if not (host in PUSH_HOSTS or PUSH_HOST_RE.fullmatch(host)):
        return False
    try:
        from urllib3.util import parse_url
    except ImportError:
        return True   # urllib3 が無い環境では requests も無く、送信自体が行われない
    try:
        u = parse_url(endpoint)
    except Exception:
        return False
    return u.scheme == "https" and u.auth is None and u.port is None and (u.host or "").lower() == host


def _host_label(endpoint) -> str:
    """ログに出す宛先の名前(スキームとホスト名、付いていればポートまで)。パスには端末ごとの秘密の
    文字列が入るので出さない。ユーザー情報も出さない。登録は誰でもできる(中身を信用できない)ので、
    英数字と . : - 以外は ? に置き換える。"""
    if not isinstance(endpoint, str):
        return "(文字列でない)"
    m = re.match(r"\s*([A-Za-z][A-Za-z0-9+.-]{0,15})://([^/?#\\]*)", endpoint)
    if not m:
        return "(URLでない)"
    hostport = m.group(2).rsplit("@", 1)[-1]
    return (m.group(1).lower() + "://" + re.sub(r"[^A-Za-z0-9.:\[\]-]", "?", hostport))[:100]


def _sub_id(entry: dict, endpoint) -> str:
    """掃除に使う購読のid(64桁の16進)。Worker が返した id を使い、無ければ宛先から計算する。"""
    sid = entry.get("id")
    if isinstance(sid, str) and re.fullmatch(r"[0-9a-f]{64}", sid):
        return sid
    return hashlib.sha256(str(endpoint or "").encode("utf-8")).hexdigest()


def _resp_reason(resp) -> str:
    """配信サービスが返した失敗の理由(Apple は {"reason": "BadDeviceToken"} の形で返す)。
    ログ用に英数字だけを残す。読めなければ空。"""
    try:
        reason = resp.json().get("reason")
    except Exception:
        return ""
    return re.sub(r"[^A-Za-z0-9_]", "", str(reason))[:40] if reason else ""


def _send_one(webpush, WebPushException, vapid, sub: dict, payload: str, ttl: int, is_apple: bool):
    """1件送る。戻り値は (結果, 補足, 送り直す価値があるか)。結果は "ok" / "gone"(掃除する)/ "fail"。
    別スレッドから呼ばれるので print しない。宛先・鍵・トークンは補足に入れない(公開ログに出るため)。"""
    try:
        webpush(
            subscription_info=sub,
            data=payload,
            vapid_private_key=vapid,
            # pywebpush は渡した辞書に aud / exp を書き足す。宛先ごとに新しい辞書を渡し、使い回さない。
            vapid_claims={"sub": PUSH_CONTACT},
            timeout=PUSH_TIMEOUT,
            # 既定の TTL は0(端末が今つながっていなければ捨てる)。スマホは省電力で
            # 切れていることが多いので保持させる。Urgency: high は省電力中でもすぐ届ける指定。
            ttl=ttl,
            headers={"Urgency": "high"},
        )
        return "ok", "", False
    except WebPushException as e:
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)
        reason = _resp_reason(resp)
        note = f"{status} {reason}".strip()
        # 404 / 410 は「その購読はもう無い」。Apple は、存在しない宛先に 400(理由 BadDeviceToken)を
        # 返す(404 / 410 にならない)ので、放っておくと届かない登録が残り続けて枠を埋める。これも掃除する。
        # 理由が違う 400(こちらの要求の形の問題など)では消さない。正しい iPhone の購読まで消えるため。
        if status in (404, 410) or (status == 400 and is_apple and reason == "BadDeviceToken"):
            return "gone", note, False
        return "fail", f"status={note}", isinstance(status, int) and status >= 500
    except Exception as e:
        name = type(e).__name__
        # 接続の失敗は送り直す価値がある。時間切れは、送り直すと応答しない宛先に倍の時間を使うのでしない。
        return "fail", name, isinstance(e, OSError) and "timeout" not in name.lower()


def _run_jobs(jobs, deadline: float) -> list:
    """jobs(引数なしの関数の並び)を PUSH_WORKERS 本のスレッドで実行し、deadline(time.monotonic の値)
    まで待つ。戻り値は、それまでに終わった分の結果。
    締切を過ぎた分は待たずに戻る: 実行中のスレッドは止められないので裏に残り得るが、新しい仕事は
    始めず、今の1件が終わった時点で終わる。daemon なのでプロセスの終了は妨げない。
    スレッドの中では print しない(残ったスレッドの出力が、後の処理の出力に割り込まないように)。"""
    lock = threading.Lock()
    queue = iter(jobs)
    results = []
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            with lock:
                job = next(queue, None)
            if job is None:
                return
            try:
                res = job()
            except Exception as e:
                res = ("fail", type(e).__name__, None)
            with lock:
                results.append(res)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(PUSH_WORKERS, len(jobs)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    stop.set()
    with lock:
        return list(results)


def _counts(notes) -> str:
    """["410", "410", "status=500"] → "410 x2, status=500 x1"(ログ用)"""
    return ", ".join(f"{k} x{n}" for k, n in collections.Counter(notes).most_common(6))


def _push_report(text: str, tag: str = "", title: str = "", ttl=None) -> dict:
    """購読中の全端末へ Web Push を送り、結果を辞書で返す(例外は出さない)。
    state: "unset"(設定なし)/ "nolib"(pywebpush なし)/ "setup"(鍵を読めない等)/
           "nofetch"(購読一覧を取れない)/ "nosubs"(購読ゼロ)/ "sent"(1件以上届いた)/ "failed"
    ok / gone / fail / left: 届いた・掃除した・失敗した・時間切れで結果を待たなかった件数。"""
    rep = {"state": "unset", "subs": 0, "ok": 0, "gone": 0, "fail": 0, "left": 0, "bad": 0}
    try:
        return _push_run(rep, text, tag, title, ttl)
    except Exception as e:
        # 想定外の失敗でも、呼び出し元(開催中の処理)へ例外を出さない。
        print(f"notify: プッシュ通知の送信で想定外の失敗 ({type(e).__name__})")
        rep["state"] = "failed"
        return rep


def _push_run(rep: dict, text: str, tag: str, title: str, ttl) -> dict:
    subs_url, private = _push_cfg()
    if not (subs_url and private):
        return rep
    deadline = time.monotonic() + PUSH_TOTAL_SEC
    try:
        from pywebpush import webpush, WebPushException
        vapid = _load_vapid(private)
        headers = _sender_headers(subs_url, vapid)
    except ImportError:
        print("notify: pywebpush が入っていないので、プッシュ通知は送らない")
        rep["state"] = "nolib"
        return rep
    except Exception as e:
        print(f"notify: プッシュ通知の準備に失敗(秘密鍵を読めない等) ({type(e).__name__}: {e})")
        rep["state"] = "setup"
        return rep
    subs = _fetch_push_subs(subs_url, headers)
    if subs is None:
        rep["state"] = "nofetch"
        return rep
    rep["subs"] = len(subs)
    if not subs:
        # 購読者ゼロ=届け先が無いだけなので配送済み扱い(後から購読した端末に
        # 当日分のバックログが一斉着弾するのを防ぐ。60秒毎の再送スパムも防止)。
        # 黙って送信済みにすると「誰にも届いていない」ことに気づけないので、1行出す。
        print("notify: 購読している端末が無い(送る相手がいないので、送信済みとして扱う)")
        rep["state"] = "nosubs"
        return rep
    msg = {"title": title or PUSH_TITLE, "body": _push_body(text)}
    if tag:
        msg["tag"] = tag
    payload = json.dumps(msg, ensure_ascii=False)
    ttl = PUSH_TTL_SEC if ttl is None else int(ttl)

    def deliver(sub, sub_id, is_apple):
        kind, note, again = _send_one(webpush, WebPushException, vapid, sub, payload, ttl, is_apple)
        if kind == "fail" and again and time.monotonic() + PUSH_RETRY_WAIT_SEC < deadline:
            # 1台でも届けば送信済みになるので、一時的な失敗で1台だけ取りこぼさないよう、1回だけ送り直す。
            time.sleep(PUSH_RETRY_WAIT_SEC)
            kind, note, _ = _send_one(webpush, WebPushException, vapid, sub, payload, ttl, is_apple)
        return kind, note, (sub_id if kind == "gone" else None)

    good, bad_ids, bad_labels = [], [], []
    for entry in subs:
        if not isinstance(entry, dict):
            continue   # Worker は必ず {id, subscription} の形で返す。違うものは宛先も id も分からないので飛ばす
        sub = entry.get("subscription")
        endpoint = sub.get("endpoint") if isinstance(sub, dict) else None
        if _endpoint_ok(endpoint):
            host = urllib.parse.urlsplit(endpoint).hostname or ""
            good.append((sub, _sub_id(entry, endpoint), host.lower() == PUSH_APPLE_HOST))
        else:
            bad_ids.append(_sub_id(entry, endpoint))
            bad_labels.append(_host_label(endpoint))
    if bad_ids:
        # 取り決めから外れた宛先へは接続しない。ホスト名を出しておく(正規の配信サービスが増えた時に、
        # ここを見て PUSH_HOSTS と Worker の両方へ足せるように)。
        rep["bad"] = len(bad_ids)
        print(f"notify: 規則に合わない宛先 {len(bad_ids)}件は送らずに掃除する ({_counts(bad_labels)})")
    # 並びを毎回変える。Worker が返す順は固定なので、全体の締切で切り上げた時に、いつも同じ端末が
    # 後回しになって届かない、ということを避ける。Worker が返した全件へ送る(件数で切り捨てない)。
    random.shuffle(good)
    sends = _run_jobs([lambda a=a: deliver(*a) for a in good], deadline)
    # 掃除は、送信が全部終わった後に1件ずつ順番に出す。Worker は全購読を1つの値にまとめて
    # 「読んで→書く」ので、同時に出すと後から書いた方だけが残り、先の削除が取り消される。
    # 全体の締切までに終わらなかった分は、次の送信でまた掃除の対象になる。
    undeleted = 0
    for sub_id in bad_ids + [r[2] for r in sends if r[0] == "gone" and r[2]]:
        if time.monotonic() >= deadline or not _delete_push_sub(subs_url, headers, sub_id):
            undeleted += 1
    rep["ok"] = sum(1 for r in sends if r[0] == "ok")
    rep["gone"] = sum(1 for r in sends if r[0] == "gone")
    rep["fail"] = sum(1 for r in sends if r[0] == "fail")
    rep["left"] = len(good) - len(sends)
    rep["state"] = "sent" if rep["ok"] else "failed"
    detail = []
    if rep["gone"]:
        detail.append("掃除: " + _counts(r[1] for r in sends if r[0] == "gone"))
    if rep["fail"]:
        detail.append("失敗: " + _counts(r[1] for r in sends if r[0] == "fail"))
    if undeleted:
        detail.append(f"Worker からの削除は次回へ {undeleted}件")
    print(f"notify: プッシュ通知 宛先{len(good)}件 → 届いた {rep['ok']} / 掃除 {rep['gone']} / "
          f"失敗 {rep['fail']} / 時間切れ {rep['left']}" + (f" ({' / '.join(detail)})" if detail else ""))
    return rep


def _push_body(text: str) -> str:
    """通知の本文。題名が「アリテイ」なので、行頭の [アリテイ] は省く。"""
    lines = [ln[len(APP_TAG):].strip() if ln.startswith(APP_TAG) else ln for ln in text.split("\n")]
    body = "\n".join(ln for ln in lines if ln)
    # Web Push本文は暗号化後4096バイト上限。日本語の長文で超えないよう本文を切り詰める。
    if len(body) > 900:
        body = body[:900] + "\n(続きはアプリで)"
    return body


def send_push(text: str, tag: str = "", title: str = "", ttl=None) -> bool:
    """購読中の全端末へpywebpushでWeb Push通知を送信する。
    設定(Worker のURL・秘密鍵)が無ければ何もせずFalseを返す。pywebpush が無い環境でも
    スキップする。1件以上送信成功、または購読者ゼロでTrueを返す(呼び出し元のsent登録判定用)。
    購読一覧を取れなかった時は False(次の周回で送り直す)。
    tag: 同じ tag の通知は端末側で置き換わる(省略可)。title: 題名(省略時は「アリテイ」)。
    ttl: 配信サービスに保持させる秒数(省略時は PUSH_TTL_SEC)。"""
    return _push_report(text, tag, title, ttl)["state"] in ("sent", "nosubs")


def selftest(send_text: str = "") -> int:
    """Web Push の設定を確かめる(手動実行のワークフロー用)。秘密鍵そのものは表示しない。
    1) 秘密鍵から導いた公開鍵が、アプリ(docs/index.html)の VAPID_PUB と一致するか
    2) Worker から購読一覧を取れるか 3) send_text があれば実際に送る。0=正常 / 1=異常。
    Worker のURLが未設定でも 1) までは確かめる。"""
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
        if not subs:
            # 購読ゼロを「成功」と出すと、誰にも届いていないことに気づけない。
            print("selftest: 購読がまだ無いので送っていない"
                  "(アプリの設定タブで「通知を受け取る」を押してから、もう一度実行する)")
            return 1
        rep = _push_report(send_text)
        ok = rep["state"] == "sent"
        print(f"selftest: テスト通知の送信={'成功' if ok else '失敗(届け先に送れなかった)'}"
              f" (届いた {rep['ok']} / 掃除 {rep['gone']} / 失敗 {rep['fail']} / 時間切れ {rep['left']})")
        return 0 if ok else 1
    return 0


def notify_events(pred: dict, ymd: str) -> bool:
    """当日予測dictを走査し、確定/結果イベントを検出。未送信分のみ1回のPOST/Web Pushに
    まとめて送信し、状態ファイルへ記録する(重複防止)。
    NOTIFY_WEBHOOKもWeb Push設定(PUSH_SUBS_URL等)も未設定/空なら何もしない。
    戻り値: 送れずに残ったイベントがあるか(True なら、呼び出し側が少し後でもう一度呼ぶ価値がある)。"""
    url = (os.environ.get("NOTIFY_WEBHOOK") or "").strip()
    push_ready = _push_ready()
    if not url and not push_ready:
        return False
    state = _prune_state(_load_state(), ymd)
    sent = set(state.get("sent", []))
    events = _conf_events(pred, ymd) + _res_events(pred, ymd)
    # 締切を過ぎた確定と古い結果は、送らずに送信済みとして記録する(理由は _stale_ids)。
    stale = _stale_ids(pred, ymd)
    skipped = sorted({eid for eid, _ in events if eid in stale and eid not in sent})
    if skipped:
        print(f"notify: 締切を過ぎた確定・古い結果は送らずに記録する ({', '.join(skipped)})")
        sent.update(skipped)
    new_events = [(eid, msg) for eid, msg in events if eid not in sent]
    if not new_events:
        _save_state({"sent": sorted(sent)})
        return False
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
    secs_left = _conf_secs_left(pred, ymd)
    unsent = False
    for ids, msgs in chunks:
        text = "\n".join(msgs)
        if not text.startswith(APP_TAG):
            text = APP_TAG + "\n" + text
        webhook_ok = send_webhook(url, text) if url else False
        push_ok = False
        if push_ready:
            title, body = "", text
            if len(ids) == 1 and ids[0].startswith("conf-") and "\n" in msgs[0]:
                # 確定が1件だけの時は、1行目(何が確定したか)を題名に、残り(買い目)を本文にする。
                # 畳まれた通知は本文が1行しか見えないので、開かなくても買い目まで読めるようにする。
                head, body = msgs[0].split("\n", 1)
                title = _push_body(head)
            push_ok = send_push(body, tag=_event_tag(ids), title=title, ttl=_event_ttl(ids, secs_left))
        if webhook_ok or push_ok:
            sent.update(ids)
        else:
            unsent = True
            print(f"notify: 送れなかった通知 {len(ids)}件は、次の周回でやり直す")
    _save_state({"sent": sorted(sent)})
    return unsent


def notify_text_status(text: str) -> str:
    """運用アラート(パイプライン障害)の送信。厳選イベント通知とは別系統。
    戻り値: "sent"(どこかへ届いた)/ "nosubs"(Web Push の購読がゼロで、他の送信先も無い)/
            "failed"(送信先は設定済みだが届けられなかった)/ "unset"(送信先が未設定)

    notify_events() の重複抑止state(notify_state.json)は通さない。連投の抑止は
    呼び出し側(watchdog.yml は1日3回)で行い、ここには状態を持たせない。
    通知に tag は付けない(別々の異常の知らせが、端末側で1つに置き換わって消えないように)。
    NOTIFY_WEBHOOK / Web Push のいずれも未設定なら何もしないので、
    未設定環境での挙動は現行と変わらない。
    """
    if not text.startswith(APP_TAG):
        text = APP_TAG + "\n" + text
    url = (os.environ.get("NOTIFY_WEBHOOK") or "").strip()
    push_ready = _push_ready()
    if not url and not push_ready:
        return "unset"
    ok = send_webhook(url, text) if url else False
    push_state = _push_report(text)["state"] if push_ready else ""
    if ok or push_state == "sent":
        return "sent"
    return "nosubs" if push_state == "nosubs" else "failed"


def notify_text(text: str) -> bool:
    """運用アラートを送る(notify_text_status の真偽版)。どこかへ届いたら True。"""
    return notify_text_status(text) == "sent"


if __name__ == "__main__":
    import sys as _sys
    # 運用アラート用: python scripts/notify.py --text "本文"
    if len(_sys.argv) >= 3 and _sys.argv[1] == "--text":
        # 「送信先が未設定」と「設定済みなのに届けられなかった」を分けて出す(後者は調べる必要がある)。
        print({
            "sent": "notify_text: 送信した",
            "nosubs": "notify_text: 購読している端末が無いため、送っていない",
            "failed": "notify_text: 送信失敗(送信先は設定済みだが、届けられなかった)",
            "unset": "notify_text: 送信先未設定のため送信せず",
        }[notify_text_status(_sys.argv[2])])
        _sys.exit(0)
    # Web Push の設定確認: python scripts/notify.py --selftest ["送るテスト文"]
    if len(_sys.argv) >= 2 and _sys.argv[1] == "--selftest":
        _sys.exit(selftest(_sys.argv[2] if len(_sys.argv) >= 3 else ""))
    print("usage: python scripts/notify.py --text '本文' | --selftest ['テスト文']")
    _sys.exit(2)

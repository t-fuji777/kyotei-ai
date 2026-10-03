# -*- coding: utf-8 -*-
"""scripts/notify.py を、実物の pywebpush / py_vapid / HTTP を通して確かめる(通信は 127.0.0.1 の中だけ)。

tests/test_notify_push.py は偽物に渡した引数を見るだけなので、引数名の間違いや pywebpush の版が
変わった時の食い違いは見つけられない。ここでは 127.0.0.1 に偽の Worker(購読一覧・掃除)と偽の
配信サーバーを立て、実際に署名・暗号化された要求を受け取り、受信側の鍵で復号して中身を確かめる。
外部へは一切送らない(127.0.0.1 以外の名前解決は、このテストの中では失敗させる)。
配信サーバーを 127.0.0.1 に向けるため、宛先の検査(_endpoint_ok)はテストの中だけ差し替える。

pywebpush が入っていない環境では「飛ばした」と表示して正常終了する。
実行: python tests/test_notify_integration.py   (Windows では PYTHONUTF8=1 を付ける)"""
import base64
import contextlib
import hashlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import pywebpush  # noqa: F401
    import py_vapid  # noqa: F401
    import http_ece
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils as ecutils
except ImportError as e:
    print("SKIPPED: pywebpush が入っていないので、通信込みのテストは飛ばした (%s)" % e.name)
    sys.exit(0)

os.environ["NO_PROXY"] = "127.0.0.1,localhost"     # 手元にプロキシの設定があっても、偽サーバーへは直接つなぐ
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import notify as N

JST = timezone(timedelta(hours=9))
YMD = "20261003"


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64ud(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_key():
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    raw = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return k, pem, raw


# 送信側の鍵(本番では secrets.VAPID_PRIVATE)。Worker と配信サーバーは公開鍵で署名を確かめる。
_, SENDER_PEM, SENDER_PUB = new_key()
_, OTHER_PEM, _ = new_key()                          # Worker が知らない鍵(署名が合わない場合の確認用)
# 受信側(端末のブラウザ)の鍵。届いた本文をこれで復号する。
RECV_PRIV, _, RECV_PUB = new_key()
AUTH = os.urandom(16)


def verify_jwt(token, pub_bytes):
    """ES256 のトークンを公開鍵で検証し、(ヘッダ, クレーム) を返す。署名が合わなければ例外。"""
    h, p, s = token.split(".")
    sig = b64ud(s)
    assert len(sig) == 64, len(sig)
    der = ecutils.encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pub_bytes).verify(
        der, (h + "." + p).encode(), ec.ECDSA(hashes.SHA256()))
    return json.loads(b64ud(h)), json.loads(b64ud(p))


def token_of(authorization):
    assert authorization.startswith("vapid t="), authorization[:12]
    return authorization.split("t=", 1)[1].split(",", 1)[0]


LOG = {"subs_get": [], "push": [], "delete": []}
SUBS = {"value": [], "fail_first": 0}     # fail_first: 最初の何回かの GET /subs を 500 にする
BEHAVIOUR = {}                            # 配信サーバー: 宛先の名前 → (状態コード, 待たせる秒数, 本文)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body=b""):
        try:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass                           # 送信側が待つのをやめて切った後(時間切れの確認)

    def _is_sender(self):
        """本物の Worker と同じ確認: 設定された公開鍵で署名を検証し、aud が自分自身で、期限が短いこと。"""
        try:
            _, claims = verify_jwt(token_of(self.headers.get("Authorization", "")), SENDER_PUB)
        except Exception:
            return False
        return claims.get("aud") == BASE and 0 < claims.get("exp", 0) - time.time() <= 900

    def do_GET(self):
        if urlparse(self.path).path != "/subs":
            return self._send(404)
        LOG["subs_get"].append(dict(self.headers))
        if not self._is_sender():
            return self._send(401, b'{"error":"unauthorized"}')
        if SUBS["fail_first"] > 0:
            SUBS["fail_first"] -= 1
            return self._send(500, b"temporarily broken")
        self._send(200, json.dumps(SUBS["value"]).encode())

    def do_DELETE(self):
        u = urlparse(self.path)
        if u.path != "/sub":
            return self._send(404)
        if not self._is_sender():
            return self._send(401)
        LOG["delete"].append(parse_qs(u.query).get("id", []))
        self._send(200, b'{"ok":true}')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        name = self.path.rsplit("/", 1)[-1]
        status, delay, text = BEHAVIOUR.get(name, (201, 0, b""))
        LOG["push"].append({"name": name, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if delay:
            time.sleep(delay)
        self._send(status, text)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass                               # 送信側が時間切れで接続を切った時の例外表示を出さない


SERVER = QuietServer(("127.0.0.1", 0), Handler)
threading.Thread(target=SERVER.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % SERVER.server_address[1]

# ---- 外へ出さないための備え: 127.0.0.1 以外の名前解決は失敗させ、試みがあれば記録する ----
LEAKS = []
_real_getaddrinfo = socket.getaddrinfo


def _local_only(host, *a, **k):
    if host not in ("127.0.0.1", "localhost"):
        LEAKS.append(host)
        raise socket.gaierror("blocked by the test: %r" % (host,))
    return _real_getaddrinfo(host, *a, **k)


socket.getaddrinfo = _local_only

# ---- notify を偽サーバーへ向ける ----
# notify は Worker のURLに https しか認めないので、このテストの中だけ設定を差し替える。
os.environ.pop("NOTIFY_WEBHOOK", None)
CFG = {"url": BASE, "key": SENDER_PEM}
N._push_cfg = lambda: (CFG["url"], CFG["key"])
# 宛先の検査: 偽の配信サーバー(BASE/push/…)だけを追加で通す。それ以外は本物の検査のまま。
_real_endpoint_ok = N._endpoint_ok
N._endpoint_ok = lambda e: (isinstance(e, str) and e.startswith(BASE + "/push/")) or _real_endpoint_ok(e)
N.PUSH_FETCH_WAIT_SEC = 0
N.PUSH_RETRY_WAIT_SEC = 0
N.STATE_PATH = Path(tempfile.mkdtemp()) / "notify_state.json"


def set_now(hhmm):
    h, m = map(int, hhmm.split(":"))
    t = datetime(int(YMD[:4]), int(YMD[4:6]), int(YMD[6:8]), h, m, 0, tzinfo=JST)
    N._now_jst = lambda: t


def sub(name):
    endpoint = "%s/push/%s" % (BASE, name)
    return {"id": hashlib.sha256(endpoint.encode()).hexdigest(),
            "subscription": {"endpoint": endpoint, "keys": {"p256dh": b64u(RECV_PUB), "auth": b64u(AUTH)}}}


def reset(subs, **behaviour):
    for v in LOG.values():
        del v[:]
    SUBS["value"], SUBS["fail_first"] = list(subs), 0
    BEHAVIOUR.clear()
    BEHAVIOUR.update(behaviour)
    CFG["url"], CFG["key"] = BASE, SENDER_PEM
    if N.STATE_PATH.exists():
        N.STATE_PATH.unlink()


def decrypt(push):
    """配信サーバーが受け取った本文を、受信側の鍵で復号する(端末のブラウザがやること)。"""
    plain = http_ece.decrypt(push["body"], private_key=RECV_PRIV, auth_secret=AUTH, version="aes128gcm")
    return json.loads(plain.decode("utf-8"))


def pushes(name):
    return [p for p in LOG["push"] if p["name"] == name]


@contextlib.contextmanager
def quiet():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def race5(**extra):
    r = {"no": 5, "tk": 1, "mt": 0, "pt": "17:04", "deadline": "17:19",
         "picks": [{"c": "1-3-4"}, {"c": "1-4-3"}, {"c": "1-3-5"}, {"c": "1-2-3"}]}
    r.update(extra)
    return {"venues": [{"code": 19, "name": "下関", "races": [r]}]}


# ---------------------------------------------------------------- テスト

def test_confirmation_on_the_wire():
    """確定1件: Worker への署名、配信サーバーへの署名・ヘッダ、暗号化された本文を実物で確かめる。"""
    reset([sub("owner")])
    set_now("17:05")
    with quiet():
        assert N.notify_events(race5(), YMD) is False
    # Worker への要求: 送信側の鍵で署名した短命のトークン(Worker 自身が宛先)と User-Agent
    g = LOG["subs_get"][-1]
    assert g["User-Agent"] == "aritei-notify"
    head, claims = verify_jwt(token_of(g["Authorization"]), SENDER_PUB)
    assert head["alg"] == "ES256" and claims["aud"] == BASE and claims["sub"].startswith("mailto:")
    assert 0 < claims["exp"] - time.time() <= 300
    # 配信サーバーへの要求
    assert len(LOG["push"]) == 1
    p = LOG["push"][0]
    h = p["headers"]
    assert h["ttl"] == str(14 * 60), "確定の保持時間は締切までの残り(17:05 → 締切17:19)"
    assert h["urgency"] == "high" and h["content-encoding"] == "aes128gcm"
    assert ",k=" + b64u(SENDER_PUB) in h["authorization"].replace(" ", "")
    head, claims = verify_jwt(token_of(h["authorization"]), SENDER_PUB)
    assert head["alg"] == "ES256" and claims["aud"] == BASE and claims["sub"] == N.PUSH_CONTACT
    assert 0 < claims["exp"] - time.time() <= 24 * 3600
    assert len(p["body"]) < 4096
    assert decrypt(p) == {"title": "厳選プラン確定 17:04 / 下関5R 締切17:19",
                          "body": "買い目 1-3-4 / 1-4-3 / 1-3-5",
                          "tag": "conf-%s-19-5" % YMD}, decrypt(p)
    assert json.loads(N.STATE_PATH.read_text(encoding="utf-8")) == {"sent": ["conf-%s-19-5" % YMD]}
    with quiet():
        N.notify_events(race5(), YMD)                     # 2度目は送らない(Worker へも行かない)
    assert len(LOG["push"]) == 1 and len(LOG["subs_get"]) == 1


def test_result_and_cancellation():
    reset([sub("owner")])
    set_now("17:40")
    with quiet():
        N.notify_events(race5(result={"order": "1-3-4", "pay3t": 1230}), YMD)
    p = LOG["push"][-1]
    assert p["headers"]["ttl"] == "3600", "結果だけの通知は長く保持させる"
    assert decrypt(p) == {"title": "アリテイ", "body": "的中 下関5R 1-3-4 払戻1230円", "tag": "res-%s-19-5" % YMD}
    assert len(LOG["push"]) == 1, "締切を過ぎた確定は送らない(結果だけ)"
    reset([sub("owner")])
    with quiet():
        N.notify_events(race5(result={"status": "中止", "ninki": None}), YMD)
    assert decrypt(LOG["push"][-1])["body"] == "中止 下関5R(返還)"


def test_longest_body_fits():
    reset([sub("owner")])
    with quiet():
        assert N.send_push("[アリテイ]\n" + "あ" * 3000) is True
    assert len(LOG["push"][0]["body"]) < 4096, "暗号化後の上限(4096バイト)に収まる"


def test_many_devices_in_parallel():
    """並列に送っても、1件ずつ正しく署名・暗号化される(同じ鍵のオブジェクトを複数のスレッドで使う)。"""
    names = ["dev%02d" % i for i in range(40)]
    reset([sub(n) for n in names])
    with quiet() as out:
        rep = N._push_report("[アリテイ] 厳選プラン確定 17:04 / 下関5R 締切17:19", tag="conf-x", ttl=600)
    assert rep["state"] == "sent" and rep["ok"] == 40 and rep["fail"] == 0 and rep["left"] == 0, rep
    assert sorted(p["name"] for p in LOG["push"]) == names
    for p in LOG["push"]:
        _, claims = verify_jwt(token_of(p["headers"]["authorization"]), SENDER_PUB)
        assert claims["aud"] == BASE and p["headers"]["ttl"] == "600"
        assert decrypt(p) == {"title": "アリテイ", "body": "厳選プラン確定 17:04 / 下関5R 締切17:19", "tag": "conf-x"}
    assert out.getvalue().count("\n") == 1, "ログは結果をまとめた1行だけ"


def test_mixed_results_cleanup_and_retry():
    """届く宛先・失効した宛先(410)・一時的に失敗する宛先(500)・断られる宛先(403)が混じった送信。"""
    reset([sub("gone"), sub("err"), sub("forbidden"), sub("ok1"), sub("ok2")],
          gone=(410, 0, b""), err=(500, 0, b""), forbidden=(403, 0, b'{"reason":"BadJwtToken"}'))
    with quiet() as out:
        rep = N._push_report("x")
    assert rep["state"] == "sent" and (rep["ok"], rep["gone"], rep["fail"], rep["left"]) == (2, 1, 2, 0), rep
    assert len(pushes("ok1")) == 1 and len(pushes("ok2")) == 1 and len(pushes("gone")) == 1
    assert len(pushes("err")) == 2, "500 は1回だけ送り直す"
    assert len(pushes("forbidden")) == 1, "403 は送り直さない"
    assert LOG["delete"] == [[sub("gone")["id"]]], "掃除は署名つきの DELETE で、id は1回に1個"
    log = out.getvalue()
    assert "届いた 2 / 掃除 1 / 失敗 2 / 時間切れ 0" in log and "status=403 BadJwtToken x1" in log, log
    assert BASE + "/push/" not in log, "宛先はログに出さない"


def test_slow_destination_does_not_hold_the_caller():
    """応答しない宛先があっても、他の端末へはすぐ届き、全体の上限で呼び出し元へ戻る。"""
    saved = (N.PUSH_TOTAL_SEC, N.PUSH_TIMEOUT)
    try:
        # 1) 全体の上限(本番は30秒)で、応答を待たずに戻る
        N.PUSH_TOTAL_SEC = 2.0
        reset([sub("slow1"), sub("slow2"), sub("owner")], slow1=(201, 7, b""), slow2=(201, 7, b""))
        t0 = time.monotonic()
        with quiet():
            rep = N._push_report("x")
        el = time.monotonic() - t0
        assert rep["state"] == "sent" and rep["ok"] == 1 and rep["left"] == 2, rep
        assert len(pushes("owner")) == 1 and 1.8 <= el < 4.0, el
        # 2) 1件ごとの時間切れ(接続, 応答待ち)の組が、実物の pywebpush / requests にそのまま通る
        N.PUSH_TOTAL_SEC = 20.0
        N.PUSH_TIMEOUT = (1, 1)
        reset([sub("slow3"), sub("owner")], slow3=(201, 5, b""))
        t0 = time.monotonic()
        with quiet() as out:
            rep = N._push_report("x")
        el = time.monotonic() - t0
        assert rep["ok"] == 1 and rep["fail"] == 1 and rep["left"] == 0, rep
        assert "ReadTimeout x1" in out.getvalue() and el < 4.0, (out.getvalue(), el)
        assert len(pushes("slow3")) == 1, "時間切れは送り直さない"
    finally:
        N.PUSH_TOTAL_SEC, N.PUSH_TIMEOUT = saved


def test_worker_hiccup_is_retried():
    reset([sub("owner")])
    SUBS["fail_first"] = 1                                # 1回目の GET /subs は 500
    with quiet() as out:
        assert N.send_push("x") is True
    assert len(LOG["subs_get"]) == 2 and len(pushes("owner")) == 1
    assert "購読一覧を取れなかった" in out.getvalue()
    reset([sub("owner")])
    SUBS["fail_first"] = 2                                # 2回とも失敗 → 送らずに戻る(次の周回でやり直す)
    set_now("17:05")
    with quiet():
        assert N.notify_events(race5(), YMD) is True
    assert len(LOG["subs_get"]) == 2 and not LOG["push"]
    assert json.loads(N.STATE_PATH.read_text(encoding="utf-8")) == {"sent": []}


def test_worker_rejects_a_wrong_key_and_unreachable_worker_does_not_raise():
    reset([sub("owner")])
    CFG["key"] = OTHER_PEM                                # Worker が知らない鍵で署名 → 401 → 送らない
    with quiet():
        rep = N._push_report("x")
    assert rep["state"] == "nofetch" and not LOG["push"], rep
    reset([sub("owner")])
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = "http://127.0.0.1:%d" % s.getsockname()[1]
    s.close()
    CFG["url"] = closed                                   # 誰も待ち受けていないポート
    t0 = time.monotonic()
    with quiet():
        assert N.send_push("x") is False
        set_now("17:05")
        assert N.notify_events(race5(), YMD) is True
    assert time.monotonic() - t0 < 30 and not LOG["push"]


def test_endpoint_outside_the_rule_is_never_contacted():
    """ポートつき等、取り決めから外れた宛先(以前の Worker が受け付けていた形)へは接続しない。"""
    bad = {"id": "ab" * 32, "subscription": {"endpoint": "https://fcm.googleapis.com:81/fcm/send/x",
                                             "keys": {"p256dh": b64u(RECV_PUB), "auth": b64u(AUTH)}}}
    evil = {"id": "cd" * 32, "subscription": {"endpoint": "https://evil.example\\.fcm.googleapis.com/x",
                                              "keys": {"p256dh": b64u(RECV_PUB), "auth": b64u(AUTH)}}}
    reset([bad, evil, sub("owner")])
    t0 = time.monotonic()
    with quiet() as out:
        rep = N._push_report("x")
    assert rep["state"] == "sent" and rep["ok"] == 1 and rep["bad"] == 2, rep
    assert time.monotonic() - t0 < 5 and len(pushes("owner")) == 1
    assert sorted(LOG["delete"]) == [[bad["id"]], [evil["id"]]], LOG["delete"]
    assert "https://fcm.googleapis.com:81" in out.getvalue()
    assert not LEAKS, LEAKS


def test_selftest_end_to_end():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "docs").mkdir()
    (tmp / "docs" / "index.html").write_text('const VAPID_PUB="%s";' % b64u(SENDER_PUB), encoding="utf-8")
    saved = N.ROOT
    N.ROOT = tmp
    try:
        reset([])
        with quiet() as out:
            assert N.selftest("") == 0
            assert N.selftest("テスト") == 1
        assert "公開鍵の一致=はい" in out.getvalue() and "購読がまだ無いので送っていない" in out.getvalue()
        reset([sub("owner")])
        with quiet() as out:
            assert N.selftest("テスト通知です") == 0
        assert decrypt(pushes("owner")[0]) == {"title": "アリテイ", "body": "テスト通知です"}
        assert "テスト通知の送信=成功" in out.getvalue()
        CFG["key"] = OTHER_PEM                            # アプリの公開鍵と対にならない秘密鍵
        with quiet() as out:
            assert N.selftest("") == 1
        assert "公開鍵の一致=いいえ" in out.getvalue()
    finally:
        N.ROOT = saved


def test_update_all_results_only_end_to_end():
    """開催中の処理(update_all.py --results-only)から実物の notify を通す。
    厳選が確定したら「書く → 公開 → 通知」が結果の取得より前に起き、配信サーバーが応答しなくても、
    Worker が落ちていても、打刻と結果の取得は続く。時計と取得(boatrace.jp)だけ差し替える。"""
    import types
    import update_all as U

    combos = ["1-2-3", "1-3-2", "2-1-3", "1-2-4", "1-4-2", "2-1-4"]

    def race(no, deadline, **extra):
        r = {"no": no, "deadline": deadline, "type": "x", "rn_full": True, "boats": [], "fuku": {"lane": 1},
             "picks": [{"c": c, "p": (0.15 if i < 3 else 0.02)} for i, c in enumerate(combos)],
             "odds": {"t3": {c: 9.9 for c in combos}}, "live": True, "st_ex": {"1": ".10"}, "weather": {"sky": "x"}}
        r.update(extra)
        return r

    now = datetime(2026, 10, 3, 17, 4, 5, tzinfo=JST)   # 5R(締切17:19)の締切15分前を過ぎた直後
    order = []                                           # 公開・配信サーバーへの到着・結果の取得が起きた順

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    def fake_result(ymd, jcd, rno):
        order.append("result:%d" % rno)
        return None

    def fake_publish(cmd, **kw):
        order.append("publish")
        return types.SimpleNamespace(returncode=0)

    def go(subs, worker_url=None, **behaviour):
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "docs" / "predictions"
        d.mkdir(parents=True)
        pred = {"date": YMD, "generated_at": "x", "venues": [{"code": 19, "name": "下関", "races": [
            race(1, "16:40", tk=0, att=0), race(5, "17:19")]}]}
        (d / (YMD + ".json")).write_text(json.dumps(pred, ensure_ascii=False), encoding="utf-8")
        reset(subs, **behaviour)
        if worker_url:
            CFG["url"] = worker_url
        del order[:]
        U.ROOT = tmp
        N.STATE_PATH = d / "notify_state.json"
        t0 = time.monotonic()
        with quiet():
            U.main()
        out = json.loads((d / (YMD + ".json")).read_text(encoding="utf-8"))
        state = json.loads(N.STATE_PATH.read_text(encoding="utf-8"))
        return {r["no"]: r for r in out["venues"][0]["races"]}, state, time.monotonic() - t0

    saved = {k: getattr(U, k) for k in ("ROOT", "datetime", "fetch_result", "fetch_before_html", "time", "subprocess")}
    saved_n = (N.STATE_PATH, N.PUSH_TOTAL_SEC)
    saved_env, saved_argv = os.environ.get("GITHUB_WORKFLOW"), sys.argv
    real_do_post = Handler.do_POST

    def do_post_logged(self):
        order.append("push:" + self.path.rsplit("/", 1)[-1])
        return real_do_post(self)

    try:
        Handler.do_POST = do_post_logged
        U.datetime = FakeDT
        U.fetch_result = fake_result
        U.fetch_before_html = lambda *a: ""
        U.time = types.SimpleNamespace(sleep=lambda s: None)
        U.subprocess = types.SimpleNamespace(run=fake_publish)
        N._now_jst = lambda: now
        os.environ["GITHUB_WORKFLOW"] = "auto-update"
        sys.argv = ["update_all.py", "--results-only"]

        # 1) 平常時: 公開 → 通知(配信サーバーに到着)→ 結果の取得、の順
        races, state, el = go([sub("owner")])
        assert races[5]["tk"] == 1 and races[5]["pt"] == "17:04"
        assert order == ["publish", "push:owner", "result:1"], order
        assert decrypt(pushes("owner")[0]) == {"title": "厳選プラン確定 17:04 / 下関5R 締切17:19",
                                               "body": "買い目 1-2-3 / 1-3-2 / 2-1-3",
                                               "tag": "conf-%s-19-5" % YMD}
        assert state == {"sent": ["conf-%s-19-5" % YMD]}

        # 2) 配信サーバーが応答しない: 上限(ここでは2秒。本番は30秒)で切り上げて、結果の取得へ進む
        N.PUSH_TOTAL_SEC = 2.0
        races, state, el = go([sub("stuck")], stuck=(201, 9, b""))
        assert races[5]["tk"] == 1 and "result:1" in order, order
        assert state == {"sent": []}, "届いていないので未送信のまま(次の周回でやり直す)"
        assert el < 7.0, "通知の待ちは、前倒しの1回と末尾の1回を合わせても上限の2回分まで: %.1f秒" % el

        # 3) Worker が落ちている: 通知は諦めて、打刻と結果の取得は続く
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = "http://127.0.0.1:%d" % s.getsockname()[1]
        s.close()
        races, state, el = go([sub("owner")], worker_url=closed)
        assert races[5]["tk"] == 1 and "result:1" in order and not LOG["push"], order
        assert state == {"sent": []}
    finally:
        Handler.do_POST = real_do_post
        for k, v in saved.items():
            setattr(U, k, v)
        N.STATE_PATH, N.PUSH_TOTAL_SEC = saved_n
        sys.argv = saved_argv
        if saved_env is None:
            os.environ.pop("GITHUB_WORKFLOW", None)
        else:
            os.environ["GITHUB_WORKFLOW"] = saved_env


def test_text_alert_end_to_end():
    reset([sub("owner")])
    with quiet():
        assert N.notify_text_status("朝次処理の異常を検知しました。\nIssueを確認してください。") == "sent"
    p = pushes("owner")[0]
    assert decrypt(p) == {"title": "アリテイ", "body": "朝次処理の異常を検知しました。\nIssueを確認してください。"}
    assert p["headers"]["ttl"] == str(N.PUSH_TTL_SEC)


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok   " + name)
    assert not LEAKS, "127.0.0.1 以外へつなごうとした: %r" % (LEAKS,)
    SERVER.shutdown()
    from importlib.metadata import version
    print("%d tests passed (pywebpush %s / py-vapid %s)" % (n, version("pywebpush"), version("py-vapid")))
    print("ALL OK")

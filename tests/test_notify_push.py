# -*- coding: utf-8 -*-
"""scripts/notify.py の Web Push 送信を、通信なしで確かめる。
pywebpush / py_vapid は偽物に差し替えるので、入っていない環境でも動く。時刻も固定する
(締切を過ぎた確定は送らない、という判定が実行した時刻に左右されないように)。
実物の pywebpush と HTTP を通す確認は tests/test_notify_integration.py で行う。

実行: python tests/test_notify_push.py   (Windows では PYTHONUTF8=1 を付ける)"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import notify as N

JST = timezone(timedelta(hours=9))
YMD = "20261003"
WORKER = "https://aritei-push.example.workers.dev"
SENT = []          # webpush に渡された引数
DELETED = []       # 掃除で消した購読id
DELETE_THREADS = []  # 掃除を本体のスレッドから呼んだか
SIGNED = []        # 署名したクレーム
REAL_FETCH = N._fetch_push_subs
REAL_FETCH_ONCE = N._fetch_push_subs_once
REAL_NOW = N._now_jst
N.PUSH_RETRY_WAIT_SEC = 0     # テストでは待たない
N.PUSH_FETCH_WAIT_SEC = 0


class FakeResp:
    def __init__(self, status, reason=None):
        self.status_code = status
        self._reason = reason

    def json(self):
        if self._reason is None:
            raise ValueError("no json")
        return {"reason": self._reason}


class FakeWebPushException(Exception):
    def __init__(self, status, reason=None):
        super().__init__("push failed")
        self.response = FakeResp(status, reason) if status else None


class FakeVapid:
    def sign(self, claims):
        SIGNED.append(dict(claims))
        return {"Authorization": "vapid t=h.p.s,k=pub"}


def install_fakes(behaviour=None):
    """behaviour: endpoint → 例外 / 引数なしの関数(待たせる等)/ それらのリスト(1回ごとに順に使う)/
    None=成功。"""
    behaviour = behaviour or {}

    def webpush(**kw):
        SENT.append(kw)
        b = behaviour.get(kw["subscription_info"]["endpoint"])
        if isinstance(b, list):
            b = b.pop(0) if b else None
        if b is not None and not isinstance(b, BaseException):
            b = b()
        if b is not None:
            raise b

    sys.modules["pywebpush"] = types.SimpleNamespace(webpush=webpush, WebPushException=FakeWebPushException)
    N._load_vapid = lambda private: FakeVapid()
    def fake_delete(url, headers, sub_id):
        DELETED.append(sub_id)
        DELETE_THREADS.append(threading.current_thread() is threading.main_thread())
        return True

    N._delete_push_sub = fake_delete


def reset(subs, behaviour=None, url=WORKER, private="dummy-private"):
    del SENT[:], DELETED[:], SIGNED[:], DELETE_THREADS[:]
    install_fakes(behaviour)
    N._fetch_push_subs = lambda u, headers: subs
    if url is None:
        os.environ.pop("PUSH_SUBS_URL", None)
    else:
        os.environ["PUSH_SUBS_URL"] = url
    if private is None:
        os.environ.pop("VAPID_PRIVATE", None)
    else:
        os.environ["VAPID_PRIVATE"] = private
    os.environ.pop("NOTIFY_WEBHOOK", None)


def set_now(hhmm, ymd=YMD):
    """notify が見る現在時刻(JST)を固定する。"""
    h, m = map(int, hhmm.split(":"))
    t = datetime(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]), h, m, 0, tzinfo=JST)
    N._now_jst = lambda: t


def ep(i, host="fcm.googleapis.com"):
    return "https://%s/fcm/send/t%d" % (host, i)


def sub(i, host="fcm.googleapis.com"):
    return {"id": "%064x" % i, "subscription": {"endpoint": ep(i, host), "keys": {"p256dh": "k", "auth": "a"}}}


def sent_to(i, host="fcm.googleapis.com"):
    return [kw for kw in SENT if kw["subscription_info"]["endpoint"] == ep(i, host)]


def payloads():
    return [json.loads(kw["data"]) for kw in SENT]


@contextlib.contextmanager
def state_file():
    """送信済みの記録を一時ファイルに向ける。"""
    saved = N.STATE_PATH
    N.STATE_PATH = Path(tempfile.mkdtemp()) / "notify_state.json"
    try:
        yield N.STATE_PATH
    finally:
        N.STATE_PATH = saved


@contextlib.contextmanager
def captured():
    """標準出力を集める。どのスレッドから書かれたかも記録する。"""
    class Spy(io.StringIO):
        from_main = []

        def write(self, s):
            Spy.from_main.append(threading.current_thread() is threading.main_thread())
            return super().write(s)

    Spy.from_main = []
    buf = Spy()
    with contextlib.redirect_stdout(buf):
        yield buf


def race5(**extra):
    r = {"no": 5, "tk": 1, "mt": 0, "pt": "17:04", "deadline": "17:19",
         "picks": [{"c": "1-3-4"}, {"c": "1-4-3"}, {"c": "1-3-5"}, {"c": "1-2-3"}]}
    r.update(extra)
    return r


def pred_of(*races):
    return {"venues": [{"code": 19, "name": "下関", "races": list(races)}]}


def saved_ids(path):
    return json.loads(path.read_text(encoding="utf-8"))["sent"]


# ---------------------------------------------------------------- 送信(send_push)

def test_not_ready_without_key_or_url():
    reset([sub(1)], private=None)
    assert N._push_ready() is False and N.send_push("x") is False and not SENT
    saved = N.PUSH_SUBS_URL_DEFAULT
    try:
        N.PUSH_SUBS_URL_DEFAULT = "__PUSH_WORKER_URL__"          # URL が未設定(置き換え前)の間は送らない
        reset([sub(1)], url=None)
        assert N._push_ready() is False and N.send_push("x") is False and not SENT
        N.PUSH_SUBS_URL_DEFAULT = WORKER                         # 既定のURLがあれば、環境変数なしで送れる
        reset([sub(1)], url=None)
        assert N._push_ready() is True and N.send_push("x") is True and len(SENT) == 1
    finally:
        N.PUSH_SUBS_URL_DEFAULT = saved


def test_send_uses_ttl_urgency_timeout_and_strips_tag():
    reset([sub(1), sub(2)])
    ok = N.send_push("[アリテイ] 厳選プラン確定 17:04 / 下関5R 締切17:19\n買い目 1-3-4 / 1-4-3 / 1-3-5")
    assert ok is True and len(SENT) == 2
    kw = SENT[0]
    assert kw["ttl"] == N.PUSH_TTL_SEC > 0, "TTL 0 だと、端末が今つながっていない時に捨てられる"
    assert kw["timeout"] == N.PUSH_TIMEOUT == (3, 8), "接続3秒・応答8秒"
    assert kw["headers"] == {"Urgency": "high"}
    assert isinstance(kw["vapid_private_key"], FakeVapid) and kw["vapid_claims"] == {"sub": N.PUSH_CONTACT}
    assert SENT[0]["vapid_claims"] is not SENT[1]["vapid_claims"], "クレームの辞書を宛先の間で使い回さない"
    payload = json.loads(kw["data"])
    assert payload == {"title": "アリテイ",
                       "body": "厳選プラン確定 17:04 / 下関5R 締切17:19\n買い目 1-3-4 / 1-4-3 / 1-3-5"}, payload
    # 警告文(先頭行がタグだけ)でも空行を残さない
    assert N._push_body("[アリテイ]\n朝次処理の異常を検知しました。") == "朝次処理の異常を検知しました。"
    assert len(N._push_body("あ" * 2000)) < 950


def test_tag_title_and_ttl_can_be_given():
    reset([sub(1)])
    assert N.send_push("本文", tag="conf-20261003-19-5", title="題名", ttl=123) is True
    assert payloads() == [{"title": "題名", "body": "本文", "tag": "conf-20261003-19-5"}]
    assert SENT[0]["ttl"] == 123


def test_expired_subscription_is_removed_and_others_still_get_it():
    s1, s2, s3 = sub(1), sub(2), sub(3)
    reset([s1, s2, s3], behaviour={ep(1): FakeWebPushException(410), ep(2): FakeWebPushException(403)})
    assert N.send_push("x") is True                      # 3件目に届いたので成功
    assert DELETED == [s1["id"]], DELETED                # 410 は掃除、403 は残す
    assert len(sent_to(2)) == 1, "403 は送り直さない"
    reset([s1], behaviour={ep(1): FakeWebPushException(404)})
    assert N.send_push("x") is False and DELETED == [s1["id"]]
    reset([s1], behaviour={ep(1): RuntimeError("boom")})
    assert N.send_push("x") is False and DELETED == [] and len(SENT) == 1   # 想定外の例外でも止まらない


def test_apple_400_is_removed_but_400_elsewhere_is_kept():
    a, g = sub(1, "web.push.apple.com"), sub(2)
    reset([a, g], behaviour={ep(1, "web.push.apple.com"): FakeWebPushException(400, "BadDeviceToken"),
                             ep(2): FakeWebPushException(400, "BadDeviceToken")})
    assert N.send_push("x") is False
    assert DELETED == [a["id"]], "Apple の 400(存在しない宛先)は掃除する。他の配信サービスの 400 は残す"
    # Apple の 400 でも、理由が「宛先が無い」以外(こちらの要求の形の問題など)なら消さない。
    # 消すと、正しい iPhone の購読まで失われる。
    for reason in ("BadWebPushTopic", "TooManyProviderTokenUpdates", None):
        reset([a], behaviour={ep(1, "web.push.apple.com"): FakeWebPushException(400, reason)})
        assert N.send_push("x") is False and DELETED == [], reason


def test_cleanup_deletes_one_id_per_request():
    """掃除は id 1個ずつ(設置済みの古い Worker は、1回の DELETE で1個しか受けない)。"""
    reset([sub(i) for i in range(1, 4)], behaviour={ep(i): FakeWebPushException(410) for i in range(1, 4)})
    assert N.send_push("x") is False
    assert sorted(DELETED) == sorted(sub(i)["id"] for i in range(1, 4))
    assert all(isinstance(d, str) and len(d) == 64 for d in DELETED)
    # 掃除は送信の後に本体のスレッドから1件ずつ出す(並べて出すと、1つの値を読んで書く Worker では
    # 後から書いた方だけが残り、先の削除が取り消される)。
    assert DELETE_THREADS == [True, True, True], DELETE_THREADS


def test_temporary_failure_is_retried_once_per_device():
    """1台でも届けば送信済みになる。一時的な失敗(5xx・接続の失敗)で1台だけ取りこぼさないよう1回送り直す。"""
    reset([sub(1), sub(2)], behaviour={ep(1): [FakeWebPushException(500), None]})
    rep = N._push_report("x")
    assert rep["state"] == "sent" and rep["ok"] == 2 and rep["fail"] == 0 and len(sent_to(1)) == 2
    reset([sub(1)], behaviour={ep(1): [ConnectionError("reset"), None]})
    assert N.send_push("x") is True and len(SENT) == 2
    reset([sub(1)], behaviour={ep(1): FakeWebPushException(503)})
    assert N.send_push("x") is False and len(SENT) == 2, "送り直しは1回だけ"
    reset([sub(1)], behaviour={ep(1): TimeoutError("slow")})
    assert N.send_push("x") is False and len(SENT) == 1, "時間切れは送り直さない(応答しない宛先に倍の時間を使わない)"


def test_no_subscribers_counts_as_delivered_but_fetch_failure_retries():
    reset([])
    with captured() as out:
        assert N.send_push("x") is True and not SENT     # 購読者ゼロ=配送済み扱い(後で一斉に届くのを防ぐ)
    assert "購読している端末が無い" in out.getvalue(), "誰にも届いていないことが、ログで分かる"
    reset(None)
    assert N.send_push("x") is False and not SENT        # 一覧を取れなかった=次の周回で送り直す


def test_sends_to_every_subscription_the_worker_returns():
    """先頭100件で切り捨てない(偽の購読を並べて、持ち主を送信対象から押し出せないように)。"""
    reset([sub(i) for i in range(230)])
    rep = N._push_report("x")
    assert rep["state"] == "sent" and rep["ok"] == 230 and len(SENT) == 230
    assert {kw["subscription_info"]["endpoint"] for kw in SENT} == {ep(i) for i in range(230)}


def test_total_time_budget_returns_without_waiting_for_slow_destinations():
    """応答しない宛先が混じっていても、他の宛先へはすぐ届き、全体の上限で呼び出し元へ戻る
    (開催中の処理を止めない)。"""
    saved = N.PUSH_TOTAL_SEC
    N.PUSH_TOTAL_SEC = 1.0
    try:
        hang = lambda: time.sleep(4)
        reset([sub(i) for i in range(13)], behaviour={ep(i): hang for i in range(3)})
        t0 = time.monotonic()
        rep = N._push_report("x")
        el = time.monotonic() - t0
        assert rep["state"] == "sent" and rep["ok"] == 10 and rep["left"] == 3, rep
        assert 0.8 <= el < 2.5, el
        assert all(len(sent_to(i)) == 1 for i in range(3, 13))
        # 全部の宛先が応答しない時も、上限で戻る(未送信のまま=次の周回でやり直す)
        reset([sub(1), sub(2)], behaviour={ep(1): hang, ep(2): hang})
        t0 = time.monotonic()
        assert N.send_push("x") is False
        assert time.monotonic() - t0 < 2.5
    finally:
        N.PUSH_TOTAL_SEC = saved


def test_endpoint_rule_matches_the_worker():
    ok = [
        "https://fcm.googleapis.com/fcm/send/abc:DEF_123-x",
        "https://fcm.googleapis.com/wp/abc",
        "https://jmt17.google.com/fcm/send/abc",
        "https://updates.push.services.mozilla.com/wpush/v2/gAAAA",
        "https://web.push.apple.com/QOabc",
        "https://wns2-by3p.notify.windows.com/w/?token=AwYAAAC%2bxyz",
        "https://FCM.googleapis.com/fcm/send/abc",                # ホスト名の大文字は同じ宛先
    ]
    ng = [
        "http://fcm.googleapis.com/fcm/send/abc",                  # https のみ
        "https://fcm.googleapis.com:81/fcm/send/abc",              # ポート指定
        "https://fcm.googleapis.com:443/fcm/send/abc",
        "https://user:pass@fcm.googleapis.com/x",                  # ユーザー情報
        "https://evil.example@fcm.googleapis.com/x",
        "https://evil.example\\.fcm.googleapis.com/x",             # バックスラッシュ(requests は evil.example へ行く)
        "https://fcm.googleapis.com\\@evil.example/x",
        " https://fcm.googleapis.com/x", "https://fcm.googleapis.com/x\n", "https://fcm.googleapis.com/a b",
        "https://fcm.googleapis.com/あ",                       # 非ASCII
        "https://fcm.googleapis.com.evil.example/x", "https://evilfcm.googleapis.com/x",
        "https://anything.fcm.googleapis.com/x",                   # サブドメインは不可(完全一致)
        "https://fcm.googleapis.com./x",
        "https://api.push.apple.com/x", "https://push.apple.com/x", "https://a.web.push.apple.com/x",
        "https://notify.windows.com/x", "https://a.b.notify.windows.com/x", "https://a_b.notify.windows.com/x",
        "https://www.google.com/x", "https://127.0.0.1/x", "https://[::1]/x",
        "HTTPS://fcm.googleapis.com/x", "ftp://fcm.googleapis.com/x", "fcm.googleapis.com/x", "", None, 5,
        "https://fcm.googleapis.com/" + "a" * 2100,
    ]
    for u in ok:
        assert N._endpoint_ok(u) is True, u
    for u in ng:
        assert N._endpoint_ok(u) is False, u


def test_bad_endpoint_is_not_contacted_and_is_removed():
    bad = {"id": "%064x" % 9, "subscription": {"endpoint": "https://fcm.googleapis.com:81/fcm/send/SECRETTOKEN",
                                               "keys": {"p256dh": "k", "auth": "a"}}}
    reset([bad, sub(1)])
    with captured() as out:
        assert N.send_push("x") is True
    assert [kw["subscription_info"]["endpoint"] for kw in SENT] == [ep(1)], "規則外の宛先へは接続しない"
    assert DELETED == [bad["id"]], "規則外の宛先は掃除する"
    log = out.getvalue()
    assert "https://fcm.googleapis.com:81" in log, "外した宛先のホスト名をログに出す"
    assert "SECRETTOKEN" not in log and ep(1) not in log, "宛先のパス(端末ごとの秘密)はログに出さない"
    # ログに出す名前: ユーザー情報は出さず、記号は置き換える
    assert N._host_label("https://user:pw@evil.example:8443/p?q") == "https://evil.example:8443"
    assert N._host_label("https://a\nb::warning::x/p") == "https://a?b::warning::x"
    assert N._host_label(None) == "(文字列でない)" and N._host_label("abc") == "(URLでない)"


def test_result_is_one_summary_line_printed_by_the_main_thread():
    reset([sub(1), sub(2), sub(3)], behaviour={ep(1): FakeWebPushException(410), ep(2): FakeWebPushException(403)})
    with captured() as out:
        N.send_push("x")
    lines = [ln for ln in out.getvalue().split("\n") if ln]
    assert len(lines) == 1 and "届いた 1 / 掃除 1 / 失敗 1 / 時間切れ 0" in lines[0], lines
    assert "410 x1" in lines[0] and "status=403 x1" in lines[0], lines
    assert all(type(out).from_main), "送信スレッドの中からは出力しない"


def test_fetch_is_tried_twice_and_never_hangs():
    reset([])
    N._fetch_push_subs = REAL_FETCH
    calls = []
    try:
        def flaky(url, headers):
            calls.append(url)
            return (None, "HTTP Error 500") if len(calls) == 1 else ([sub(1)], "")
        N._fetch_push_subs_once = flaky
        with captured() as out:
            assert N.send_push("x") is True and len(SENT) == 1 and len(calls) == 2
        assert "購読一覧を取れなかった" in out.getvalue()
        del calls[:]
        N._fetch_push_subs_once = lambda url, headers: (calls.append(url), (None, "down"))[1]
        assert N.send_push("x") is False and len(calls) == N.PUSH_FETCH_TRIES == 2
        # 応答が返らない(名前解決が固まる等)時も、待つ側で切り上げる
        saved = N.TIMEOUT_SEC
        N.TIMEOUT_SEC = -0.8                              # 1回の待ち = TIMEOUT_SEC + 1 = 0.2秒
        try:
            N._fetch_push_subs_once = lambda url, headers: (time.sleep(3), ([sub(1)], ""))[1]
            t0 = time.monotonic()
            assert N._fetch_push_subs(WORKER, {}) is None
            assert time.monotonic() - t0 < 1.5
        finally:
            N.TIMEOUT_SEC = saved
    finally:
        N._fetch_push_subs_once = REAL_FETCH_ONCE


def test_sender_token_is_short_lived_and_bound_to_the_worker():
    reset([])
    h = N._sender_headers(WORKER + "/", FakeVapid())
    c = SIGNED[-1]
    assert h["Authorization"].startswith("vapid t=") and h["User-Agent"]
    assert c["aud"] == WORKER and 0 < c["exp"] - time.time() <= 300 and c["sub"].startswith("mailto:")


def test_setup_failure_does_not_raise():
    reset([sub(1)])

    def boom(private):
        raise ValueError("bad key")

    N._load_vapid = boom
    assert N.send_push("x") is False and not SENT


def test_unexpected_error_does_not_escape():
    """通知の中で何が起きても、呼び出し元(開催中の処理)へ例外を出さない。"""
    reset([sub(1)])

    def boom(url, headers):
        raise RuntimeError("unexpected")

    N._fetch_push_subs = boom
    with state_file():
        set_now("17:05")
        assert N.send_push("x") is False
        assert N.notify_events(pred_of(race5()), YMD) is True      # 未送信のまま残る


# ---------------------------------------------------------------- 文面と、送る・送らないの判定

def test_conf_message_carries_the_picks():
    pred = {"venues": [{"code": 19, "name": "下関", "races": [
        {"no": 5, "tk": 1, "mt": 0, "pt": "17:04", "deadline": "17:19",
         "picks": [{"c": "1-3-4", "p": 0.2}, {"c": "1-4-3", "p": 0.1}, {"c": "1-3-5", "p": 0.09}, {"c": "1-2-3", "p": 0.05}]},
        {"no": 6, "tk": 0, "mt": 0, "pt": "17:30", "deadline": "17:45", "picks": [{"c": "1-2-3", "p": 0.2}]},
    ]}]}
    ev = N._conf_events(pred, "20261003")
    assert ev == [("conf-20261003-19-5", "[アリテイ] 厳選プラン確定 17:04 / 下関5R 締切17:19\n買い目 1-3-4 / 1-4-3 / 1-3-5")], ev


def test_stale_ids():
    conf, res = "conf-%s-19-5" % YMD, "res-%s-19-5" % YMD
    p = pred_of(race5())
    set_now("17:05")
    assert N._stale_ids(p, YMD) == set()
    set_now("17:18")
    assert N._stale_ids(p, YMD) == set()
    set_now("17:19")
    assert N._stale_ids(p, YMD) == {conf}, "締切を過ぎた確定は送らない"
    set_now("19:19")
    assert N._stale_ids(p, YMD) == {conf}, "結果は締切からちょうど120分までは送る"
    set_now("19:20")
    assert N._stale_ids(p, YMD) == {conf, res}, "締切から120分を超えた結果は送らない"
    set_now("17:05")
    assert N._stale_ids(pred_of(race5(ph=1)), YMD) == {conf, res}, "締切後の判定(ph=1)は確定も結果も送らない"
    assert N._stale_ids(pred_of(race5(tk=0)), YMD) == set(), "厳選でないレースは対象外"
    assert N._stale_ids(pred_of(race5(deadline="")), YMD) == set(), "締切を読めない時は、時刻では判断しない"
    # 実行機は UTC で動く。JST に直して比べる(UTC 08:20 = JST 17:20 は締切17:19の後)
    N._now_jst = lambda: datetime(2026, 10, 3, 8, 20, tzinfo=timezone.utc)
    assert N._stale_ids(p, YMD) == {conf}
    N._now_jst = REAL_NOW
    assert N._now_jst().utcoffset() == timedelta(hours=9)


def test_notify_events_sends_once():
    with state_file() as path:
        reset([sub(1)])
        set_now("17:05")
        pred = pred_of(race5())
        assert N.notify_events(pred, YMD) is False       # 送れた=未送信は残っていない
        assert len(SENT) == 1
        assert N.notify_events(pred, YMD) is False       # 同じ確定は2度送らない
        assert len(SENT) == 1
        set_now("17:40")
        pred["venues"][0]["races"][0]["result"] = {"order": "1-3-4", "pay3t": 1230}
        N.notify_events(pred, YMD)                       # 結果が付いたら結果を送る
        assert len(SENT) == 2 and "的中 下関5R 1-3-4 払戻1230円" in payloads()[1]["body"]
        assert saved_ids(path) == ["conf-%s-19-5" % YMD, "res-%s-19-5" % YMD]
        # 一覧を取れなかった回は「未送信」のまま残り、次の周回で送り直す
        path.unlink()
        reset(None)
        set_now("17:05")
        pred = pred_of(race5())
        assert N.notify_events(pred, YMD) is True, "未送信が残ったことを呼び出し元へ返す"
        assert not SENT and saved_ids(path) == []
        reset([sub(1)])
        assert N.notify_events(pred, YMD) is False and len(SENT) == 1


def test_single_confirmation_uses_first_line_as_title_and_carries_a_tag():
    with state_file():
        reset([sub(1)])
        set_now("17:05")
        N.notify_events(pred_of(race5()), YMD)
        assert payloads() == [{"title": "厳選プラン確定 17:04 / 下関5R 締切17:19",
                               "body": "買い目 1-3-4 / 1-4-3 / 1-3-5",
                               "tag": "conf-%s-19-5" % YMD}], payloads()


def test_result_and_bundles_keep_the_default_title():
    with state_file():
        reset([sub(1)])
        set_now("17:40")                                  # 5R は締切後(確定は送らない)。結果だけが出る
        N.notify_events(pred_of(race5(result={"order": "1-5-4", "pay3t": 1430})), YMD)
        assert payloads() == [{"title": "アリテイ", "body": "不的中 下関5R", "tag": "res-%s-19-5" % YMD}], payloads()
    with state_file():
        reset([sub(1)])
        set_now("17:05")                                  # 確定が2件同時 → まとめて1通
        N.notify_events(pred_of(race5(), race5(no=6, deadline="17:25", pt="17:05")), YMD)
        p = payloads()
        assert len(p) == 1 and p[0]["title"] == "アリテイ"
        assert p[0]["body"].startswith("厳選プラン確定 17:04 / 下関5R 締切17:19\n買い目")
        assert "下関6R 締切17:25" in p[0]["body"]
        assert p[0]["tag"].startswith("ev-") and len(p[0]["tag"]) == 19
        ids = ["conf-%s-19-5" % YMD, "conf-%s-19-6" % YMD]
        assert p[0]["tag"] == N._event_tag(ids) and N._event_tag(ids) != N._event_tag(ids[::-1])
        assert N._event_tag(ids[:1]) == ids[0]


def test_resend_after_failure_carries_the_same_tag():
    """送り直しの通知は同じ tag になる(端末側で前の通知と置き換わり、重ならない)。"""
    with state_file():
        reset([sub(1)], behaviour={ep(1): [TimeoutError("slow"), None]})
        set_now("17:05")
        pred = pred_of(race5())
        assert N.notify_events(pred, YMD) is True
        assert N.notify_events(pred, YMD) is False
        tags = [p["tag"] for p in payloads()]
        assert tags == ["conf-%s-19-5" % YMD] * 2, tags


def test_late_confirmation_and_old_result_are_recorded_without_sending():
    conf, res = "conf-%s-19-5" % YMD, "res-%s-19-5" % YMD
    done = race5(result={"order": "1-3-4", "pay3t": 1230})
    # 記録が無いまま夜になった(通知を有効にした初日・記録を失った時): 何も送らず、記録だけする
    with state_file() as path:
        reset([sub(1)])
        set_now("22:29")
        with captured() as out:
            assert N.notify_events(pred_of(done), YMD) is False
        assert not SENT and saved_ids(path) == [conf, res]
        assert "送らずに記録" in out.getvalue()
    # 締切の11分後に結果が出た: 確定は送らず(もう買えない)、結果だけを送る
    with state_file() as path:
        reset([sub(1)])
        set_now("17:30")
        N.notify_events(pred_of(done), YMD)
        assert [p["body"] for p in payloads()] == ["的中 下関5R 1-3-4 払戻1230円"], payloads()
        assert saved_ids(path) == [conf, res]
    # 締切を過ぎるまで送信が失敗し続けた確定は、締切後は送らない(記録して終わる)
    with state_file() as path:
        reset(None)
        set_now("17:10")
        assert N.notify_events(pred_of(race5()), YMD) is True and saved_ids(path) == []
        set_now("17:19")
        reset([sub(1)])
        assert N.notify_events(pred_of(race5()), YMD) is False
        assert not SENT and saved_ids(path) == [conf]


def test_post_deadline_stamp_sends_neither_confirmation_nor_result():
    """締切後に判定されたレース(ph=1)は、tk=1 でも確定を知らせない。結果だけが届くこともない。"""
    with state_file() as path:
        reset([sub(1)])
        set_now("18:26")
        r = {"no": 7, "tk": 1, "mt": 0, "ph": 1, "pt": "18:25", "deadline": "18:18",
             "picks": [{"c": "1-2-3"}, {"c": "1-3-2"}, {"c": "2-1-3"}], "result": {"order": "1-2-3", "pay3t": 980}}
        assert N.notify_events(pred_of(r), YMD) is False
        assert not SENT and saved_ids(path) == ["conf-%s-19-7" % YMD, "res-%s-19-7" % YMD]


def test_ttl_follows_the_deadline():
    conf, res = "conf-%s-19-5" % YMD, "res-%s-19-5" % YMD
    with state_file():
        reset([sub(1)])
        set_now("17:05")                                  # 締切17:19 の14分前
        N.notify_events(pred_of(race5()), YMD)
        assert SENT[0]["ttl"] == 14 * 60, SENT[0]["ttl"]
        set_now("17:40")
        N.notify_events(pred_of(race5(result={"order": "1-3-4", "pay3t": 1230})), YMD)
        assert SENT[1]["ttl"] == N.PUSH_TTL_RESULT_SEC == 3600, "結果だけの通知は長く保持させる"
    assert N._event_ttl([conf], {conf: 20 * 60.0}) == 900, "上限は900秒"
    assert N._event_ttl([conf], {conf: 30.0}) == 60, "締切直前でも60秒は保持させる"
    assert N._event_ttl([conf, res], {conf: 300.0}) == 300, "確定を含む時は確定の規則"
    assert N._event_ttl([conf, "conf-x"], {conf: 100.0, "conf-x": 400.0}) == 400, "複数の確定は遅い方の締切"
    assert N._event_ttl([conf], {}) == N.PUSH_TTL_SEC, "締切を読めない時は既定値"
    assert N._event_ttl([res], {conf: 100.0}) == 3600
    set_now("17:05")
    assert N._conf_secs_left(pred_of(race5()), YMD) == {conf: 14 * 60.0}


def test_cancelled_race_is_reported():
    """確定を知らせたレースが中止・不成立になったら、その旨を結果として送る(音沙汰なしにしない)。"""
    res = "res-%s-19-5" % YMD
    for status in ("中止", "不成立"):          # scripts/fetch_result.py が返す status はこの2つ
        p = pred_of(race5(result={"status": status, "ninki": None}))
        assert N._res_events(p, YMD) == [(res, "%s 下関5R(返還)" % status)]
    assert N._res_events(pred_of(race5(result={"ninki": None})), YMD) == []
    assert N._res_events(pred_of(race5(tk=0, result={"status": "中止"})), YMD) == [], "厳選でないレースは対象外"
    with state_file() as path:
        reset([sub(1)])
        set_now("17:05")
        pred = pred_of(race5())
        N.notify_events(pred, YMD)
        set_now("17:30")
        pred["venues"][0]["races"][0]["result"] = {"status": "中止", "ninki": None}
        assert N.notify_events(pred, YMD) is False
        assert payloads()[1] == {"title": "アリテイ", "body": "中止 下関5R(返還)", "tag": res}, payloads()
        assert saved_ids(path) == ["conf-%s-19-5" % YMD, res]


def test_state_file_with_unexpected_content_is_treated_as_empty():
    for garbage in ("[]", '{"sent": "conf"}', '"x"', "null", "{broken", '{"sent": [1, null, "res-x-1-1"]}'):
        with state_file() as path:
            path.write_text(garbage, encoding="utf-8")
            reset([sub(1)])
            set_now("17:05")
            assert N.notify_events(pred_of(race5()), YMD) is False, garbage
            assert len(SENT) == 1 and saved_ids(path) == ["conf-%s-19-5" % YMD], garbage


# ---------------------------------------------------------------- 見える化(selftest / --text)

def test_selftest_does_not_report_success_when_nobody_is_subscribed():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "docs").mkdir()
    (tmp / "docs" / "index.html").write_text('<script>const VAPID_PUB="PUBKEY_ABC-123";</script>', encoding="utf-8")
    saved = (N.ROOT, N._vapid_public_b64)
    N.ROOT = tmp
    N._vapid_public_b64 = lambda vapid: "PUBKEY_ABC-123"
    try:
        reset([])
        with captured() as out:
            assert N.selftest("テスト") == 1
        assert "購読がまだ無いので送っていない" in out.getvalue() and "成功" not in out.getvalue().split("購読数")[1]
        assert N.selftest("") == 0                        # 文面なし(設定の確認だけ)は、購読ゼロでも正常
        reset([sub(1)])
        with captured() as out:
            assert N.selftest("テスト") == 0 and len(SENT) == 1
        assert "テスト通知の送信=成功" in out.getvalue() and "届いた 1" in out.getvalue()
        reset([sub(1)], behaviour={ep(1): FakeWebPushException(403)})
        assert N.selftest("テスト") == 1
        reset(None)
        assert N.selftest("テスト") == 1
    finally:
        N.ROOT, N._vapid_public_b64 = saved


def test_text_alert_tells_failure_from_not_configured():
    reset([sub(1)], private=None)
    assert N.notify_text_status("x") == "unset" and N.notify_text("x") is False
    reset(None)
    assert N.notify_text_status("x") == "failed", "設定済みなのに届けられなかった時は「未設定」と出さない"
    reset([])
    assert N.notify_text_status("x") == "nosubs" and N.notify_text("x") is False
    reset([sub(1)])
    assert N.notify_text_status("朝次処理の異常を検知しました。") == "sent" and N.notify_text("x") is True
    assert payloads()[0] == {"title": "アリテイ", "body": "朝次処理の異常を検知しました。"}, "運用アラートに tag は付けない"
    assert SENT[0]["ttl"] == N.PUSH_TTL_SEC


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            log = io.StringIO()          # notify のログは、失敗した時だけ見せる
            try:
                with contextlib.redirect_stdout(log):
                    fn()
            except BaseException:
                print(log.getvalue())
                print("FAIL " + name)
                raise
            N._now_jst = REAL_NOW
            n += 1
            print("ok   " + name)
    print("%d tests passed" % n)
    print("ALL OK")

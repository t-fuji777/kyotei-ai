# -*- coding: utf-8 -*-
"""scripts/notify.py の Web Push 送信を、通信なしで確かめる。
pywebpush / py_vapid は偽物に差し替えるので、入っていない環境でも動く。

実行: python tests/test_notify_push.py   (Windows では PYTHONUTF8=1 を付ける)"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import notify as N

WORKER = "https://aritei-push.example.workers.dev"
SENT = []          # webpush に渡された引数
DELETED = []       # 掃除で消した購読id
SIGNED = []        # 署名したクレーム


class FakeResp:
    def __init__(self, status):
        self.status_code = status


class FakeWebPushException(Exception):
    def __init__(self, status):
        super().__init__("push failed")
        self.response = FakeResp(status) if status else None


class FakeVapid:
    def sign(self, claims):
        SIGNED.append(dict(claims))
        return {"Authorization": "vapid t=h.p.s,k=pub"}


def install_fakes(behaviour=None):
    """behaviour: endpoint → 例外(または None=成功)。"""
    behaviour = behaviour or {}

    def webpush(**kw):
        SENT.append(kw)
        b = behaviour.get(kw["subscription_info"]["endpoint"])
        if b is not None:
            raise b

    sys.modules["pywebpush"] = types.SimpleNamespace(webpush=webpush, WebPushException=FakeWebPushException)
    N._load_vapid = lambda private: FakeVapid()
    N._delete_push_sub = lambda url, headers, sub_id: DELETED.append(sub_id)


def reset(subs, behaviour=None, url=WORKER, private="dummy-private"):
    del SENT[:], DELETED[:], SIGNED[:]
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


def sub(i):
    return {"id": "%064x" % i, "subscription": {"endpoint": "https://fcm.googleapis.com/fcm/send/t%d" % i,
                                                "keys": {"p256dh": "k", "auth": "a"}}}


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
    assert kw["timeout"] == N.PUSH_TIMEOUT_SEC and kw["headers"] == {"Urgency": "high"}
    assert isinstance(kw["vapid_private_key"], FakeVapid) and kw["vapid_claims"] == {"sub": N.PUSH_CONTACT}
    payload = json.loads(kw["data"])
    assert payload["title"] == "アリテイ"
    assert payload["body"] == "厳選プラン確定 17:04 / 下関5R 締切17:19\n買い目 1-3-4 / 1-4-3 / 1-3-5", payload["body"]
    # 警告文(先頭行がタグだけ)でも空行を残さない
    assert N._push_body("[アリテイ]\n朝次処理の異常を検知しました。") == "朝次処理の異常を検知しました。"
    assert len(N._push_body("あ" * 2000)) < 950


def test_expired_subscription_is_removed_and_others_still_get_it():
    s1, s2, s3 = sub(1), sub(2), sub(3)
    reset([s1, s2, s3], behaviour={s1["subscription"]["endpoint"]: FakeWebPushException(410),
                                   s2["subscription"]["endpoint"]: FakeWebPushException(500)})
    assert N.send_push("x") is True                      # 3件目に届いたので成功
    assert DELETED == [s1["id"]], DELETED                # 410 は掃除、500 は残す
    reset([s1], behaviour={s1["subscription"]["endpoint"]: FakeWebPushException(404)})
    assert N.send_push("x") is False and DELETED == [s1["id"]]
    reset([s1], behaviour={s1["subscription"]["endpoint"]: RuntimeError("timeout")})
    assert N.send_push("x") is False and DELETED == []   # 想定外の例外でも止まらない


def test_no_subscribers_counts_as_delivered_but_fetch_failure_retries():
    reset([])
    assert N.send_push("x") is True and not SENT         # 購読者ゼロ=配送済み扱い(後で一斉に届くのを防ぐ)
    reset(None)
    assert N.send_push("x") is False and not SENT        # 一覧を取れなかった=次の周回で送り直す


def test_cap_on_number_of_subscriptions():
    reset([sub(i) for i in range(N.PUSH_MAX_SUBS + 30)])
    assert N.send_push("x") is True and len(SENT) == N.PUSH_MAX_SUBS


def test_total_time_budget_stops_a_slow_run():
    """応答の遅い宛先が並んでも、全体の上限で打ち切る(開催中の処理を止めない)。"""
    reset([sub(i) for i in range(10)])
    clock = {"t": 0.0}
    real_webpush = sys.modules["pywebpush"].webpush

    def slow(**kw):
        clock["t"] += 9.0                                  # 1件9秒かかる宛先
        return real_webpush(**kw)

    sys.modules["pywebpush"].webpush = slow
    import time as _time
    real_monotonic = _time.monotonic
    _time.monotonic = lambda: clock["t"]
    try:
        assert N.send_push("x") is True
    finally:
        _time.monotonic = real_monotonic
    assert 1 <= len(SENT) < 10 and len(SENT) == int(N.PUSH_TOTAL_SEC // 9) + 1, len(SENT)


def test_sender_token_is_short_lived_and_bound_to_the_worker():
    import time
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


def test_conf_message_carries_the_picks():
    pred = {"venues": [{"code": 19, "name": "下関", "races": [
        {"no": 5, "tk": 1, "mt": 0, "pt": "17:04", "deadline": "17:19",
         "picks": [{"c": "1-3-4", "p": 0.2}, {"c": "1-4-3", "p": 0.1}, {"c": "1-3-5", "p": 0.09}, {"c": "1-2-3", "p": 0.05}]},
        {"no": 6, "tk": 0, "mt": 0, "pt": "17:30", "deadline": "17:45", "picks": [{"c": "1-2-3", "p": 0.2}]},
    ]}]}
    ev = N._conf_events(pred, "20261003")
    assert ev == [("conf-20261003-19-5", "[アリテイ] 厳選プラン確定 17:04 / 下関5R 締切17:19\n買い目 1-3-4 / 1-4-3 / 1-3-5")], ev


def test_notify_events_sends_once():
    tmp = Path(tempfile.mkdtemp())
    saved = N.STATE_PATH
    N.STATE_PATH = tmp / "notify_state.json"
    os.environ.pop("NOTIFY_WEBHOOK", None)
    try:
        reset([sub(1)])
        pred = {"venues": [{"code": 19, "name": "下関", "races": [
            {"no": 5, "tk": 1, "pt": "17:04", "deadline": "17:19", "picks": [{"c": "1-3-4"}, {"c": "1-4-3"}, {"c": "1-3-5"}]}]}]}
        N.notify_events(pred, "20261003")
        assert len(SENT) == 1
        N.notify_events(pred, "20261003")                # 同じ確定は2度送らない
        assert len(SENT) == 1
        pred["venues"][0]["races"][0]["result"] = {"order": "1-3-4", "pay3t": 1230}
        N.notify_events(pred, "20261003")                # 結果が付いたら結果を送る
        assert len(SENT) == 2 and "的中 下関5R 1-3-4 払戻1230円" in json.loads(SENT[1]["data"])["body"]
        # 一覧を取れなかった回は「未送信」のまま残り、次の周回で送り直す
        N.STATE_PATH.unlink()
        reset(None)
        N.notify_events(pred, "20261003")
        assert not SENT and json.loads(N.STATE_PATH.read_text(encoding="utf-8")) == {"sent": []}
    finally:
        N.STATE_PATH = saved


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok   " + name)
    print("%d tests passed" % n)
    print("ALL OK")

# -*- coding: utf-8 -*-
"""scripts/model_store.py(モデルの取得・検証・配布・予備更新)を、通信なしで確かめる。

Release の代わりに一時ディレクトリを置き、MODEL_BASE_URL(file://)で取得先を差し替える。
gh コマンドは偽物(FakeGh)に差し替える。モデルは検査を通る形の合成ファイルを使う。
実物のモデルが要る項目だけは、作業ツリーの data/model(Windows では改行が CRLF になり、
LightGBM に渡すとプロセスごと落ちる)ではなく、git show HEAD:data/model/<名前> で取り出した
LF 版を使う(git や lightgbm が無い環境では飛ばす)。

実行: python tests/test_model_store.py
"""
import atexit
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import types
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import model_store as ms

REAL_HTTP_GET = ms._http_get
REAL_GH = ms._gh
REAL_GH_READY = ms._gh_ready
REAL_VALID_DIR = ms.valid_dir
REAL_READ_POINTER = ms.read_pointer
NET = {"n": 0}
TMP_ROOTS = []
SKIPPED = []           # 環境の都合で飛ばした確認(末尾の集計に出す。REQUIRE_REAL_MODEL=1 なら飛ばさず失敗にする)

HEAD = b"tree\nversion=v4\nnum_class=1\n"
TAIL = b"\nend of parameters\n\npandas_categorical:[]\n"
FILLER = b"feature_infos=none\n" * 7000            # 約133KB(検査の下限100KBを超える)


def body(tag=""):
    return HEAD + ("tag=%s\n" % tag).encode() + FILLER + TAIL


def make_model(d, trained_at, tag=""):
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    for n in ms.MODEL_FILES:
        (d / n).write_bytes(body(tag + n))
    (d / "meta.json").write_text(json.dumps(
        {"trained_at": trained_at, "period": ["20210611", "20261001"]}), encoding="utf-8")
    return d


def sandbox():
    """一時ディレクトリをリポジトリの根に見立てる。予備(data/model)は 2026-09-01 学習。"""
    root = Path(tempfile.mkdtemp(prefix="ms_test_"))
    TMP_ROOTS.append(root)
    rel = root / "_release"
    rel.mkdir()
    ms.ROOT = root
    ms._sleep = lambda sec: None
    ms._gh = REAL_GH
    ms._gh_ready = REAL_GH_READY
    ms.valid_dir = REAL_VALID_DIR
    ms.read_pointer = REAL_READ_POINTER
    NET["n"] = 0

    def counted(url, dest):
        NET["n"] += 1
        return REAL_HTTP_GET(url, dest)

    ms._http_get = counted
    os.environ["MODEL_BASE_URL"] = rel.as_uri()
    for k in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY"):
        os.environ.pop(k, None)
    make_model(ms._p("frozen"), "2026-09-01 06:00 JST", "frozen")
    return root, rel


def fake_publish(rel, trained_at, tag=""):
    """gh を使わない配布: build → tar.gz → 偽Release → 参照先(daily がコミットするもの)。"""
    make_model(ms._p("build"), trained_at, tag)
    stage = ms.ROOT / "_stage"
    tar, sha, size = ms.build_tarball(ms._p("build"), stage)
    ptr = ms.make_pointer(ms._p("build"), tar.name, sha, size)
    shutil.copyfile(tar, rel / tar.name)
    ms._atomic_write(ms._p("pointer"), json.dumps(ptr, indent=1) + "\n")
    shutil.rmtree(stage)
    return ptr


def drop_build():
    """学習していない実行機(開催中ループなど)の状態にする。"""
    shutil.rmtree(ms._p("build"), ignore_errors=True)


def quiet(fn, *a, **kw):
    """表示を捨てて実行し、(戻り値, 表示) を返す。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **kw)
    return r, buf.getvalue()


def kinds():
    return [(k, m["trained_at"]) for _d, m, k in ms.candidates()]


def tree_hash(d):
    return {p.name: ms._sha256(p) for p in sorted(Path(d).iterdir()) if p.is_file()}


# ------------------------------------------------------------------ 取得と候補

def test_no_pointer_uses_frozen_only():
    root, rel = sandbox()
    (ok, msg), out = quiet(ms.fetch)
    assert ok and "参照先が無い" in msg and out == "", (ok, msg, out)
    assert NET["n"] == 0
    assert kinds() == [("frozen", "2026-09-01 06:00 JST")]
    assert not ms._p("live").exists()                 # 導入前は何も作らない(従来と同じ動き)
    assert quiet(ms.main, ["fetch"]) == (0, "")


def test_fetch_installs_then_stays_silent_without_network():
    root, rel = sandbox()
    p1 = fake_publish(rel, "2026-10-03 06:12 JST", "d1")
    drop_build()
    (ok, msg), out = quiet(ms.fetch)
    assert ok and "取得して切り替えた" in out and p1["asset"] in out, out
    assert NET["n"] == 1
    assert kinds()[0] == ("live", "2026-10-03 06:12 JST")
    assert ms.live_current().name == p1["sha256"][:12]
    assert tree_hash(ms.live_current()) == p1["files"]
    os.environ["MODEL_BASE_URL"] = (root / "nowhere").as_uri()     # 通信したら失敗する状態
    (ok, msg), out = quiet(ms.fetch)
    assert ok and msg == "" and out == "" and NET["n"] == 1         # 同期済み: 無言・通信なし
    leftovers = [p.name for p in ms._p("live").iterdir() if p.name.startswith(".")]
    assert leftovers == [], leftovers


def test_tarball_is_deterministic():
    root, rel = sandbox()
    make_model(ms._p("build"), "2026-10-03 06:12 JST", "d1")
    a = ms.build_tarball(ms._p("build"), root / "s1")
    time.sleep(1.1)                                                 # 時刻が変わっても同じバイト列
    os.utime(ms._p("build") / "meta.json")
    b = ms.build_tarball(ms._p("build"), root / "s2")
    assert a[1] == b[1] and a[0].name == b[0].name and a[2] == b[2]
    assert a[0].name == "model-20261003-0612-%s.tar.gz" % a[1][:12]
    assert ms.ASSET_RE.fullmatch(a[0].name)
    with tarfile.open(a[0], "r:gz") as tf:
        assert tf.getnames() == list(ms.FILES)
    ptr = ms.make_pointer(ms._p("build"), a[0].name, a[1], a[2])
    assert ms._pointer_problem(ptr) is None and ptr["period_end"] == "20261001"
    assert len(json.dumps(ptr, indent=1)) < 1000                    # 参照先は小さい


def test_network_down_keeps_previous_then_backs_off_then_recovers():
    root, rel = sandbox()
    p1 = fake_publish(rel, "2026-10-03 06:12 JST", "d1")
    drop_build()
    assert quiet(ms.fetch)[0][0]
    p2 = fake_publish(rel, "2026-10-04 06:05 JST", "d2")
    drop_build()
    os.environ["MODEL_BASE_URL"] = (root / "nowhere").as_uri()
    n0 = NET["n"]
    (ok, msg), out = quiet(ms.fetch)
    assert not ok and "取得できない" in out and "警告" in out, out
    assert NET["n"] == n0 + 1
    assert kinds()[0] == ("live", "2026-10-03 06:12 JST")           # 前日の取得分で続行(予備まで落ちない)
    fail = json.loads((ms._p("live") / ".fetch_fail.json").read_text(encoding="utf-8"))
    assert fail["sha256"] == p2["sha256"] and fail["count"] == 1
    (ok, msg), out = quiet(ms.fetch)
    assert not ok and "再試行" in out and NET["n"] == n0 + 1         # 直後は通信しない(1行だけ表示)
    assert out.count("\n") == 1 and "::warning" not in out
    st = ms.status()
    assert st["in_sync"] is False and st["active"] == "live" and st["fetch_fail"]["count"] == 1
    assert quiet(ms.main, ["fetch"])[0] == 1
    # 待ち時間が過ぎれば自分で再試行する
    fail["at"] = time.time() - ms.RETRY_SEC - 1
    (ms._p("live") / ".fetch_fail.json").write_text(json.dumps(fail), encoding="utf-8")
    (ok, msg), out = quiet(ms.fetch)
    assert not ok and NET["n"] == n0 + 2
    assert json.loads((ms._p("live") / ".fetch_fail.json").read_text(encoding="utf-8"))["count"] == 2
    # 復旧後は --force で待たずに取得できる
    os.environ["MODEL_BASE_URL"] = rel.as_uri()
    (ok, msg), out = quiet(ms.fetch, force=True)
    assert ok and "取得して切り替えた" in out
    assert kinds()[0] == ("live", "2026-10-04 06:05 JST") and ms.status()["in_sync"]
    assert not (ms._p("live") / ".fetch_fail.json").exists()
    # 版のディレクトリは現行と1つ前だけ残す
    p3 = fake_publish(rel, "2026-10-05 06:01 JST", "d3")
    drop_build()
    assert quiet(ms.fetch)[0][0]
    dirs = sorted(d.name for d in ms._p("live").iterdir() if d.is_dir())
    assert dirs == sorted([p2["sha256"][:12], p3["sha256"][:12]]), dirs


def test_warning_annotation_on_actions():
    root, rel = sandbox()
    fake_publish(rel, "2026-10-03 06:12 JST", "d1")
    drop_build()
    os.environ["MODEL_BASE_URL"] = (root / "nowhere").as_uri()
    os.environ["GITHUB_ACTIONS"] = "true"
    try:
        (ok, msg), out = quiet(ms.fetch)
    finally:
        os.environ.pop("GITHUB_ACTIONS", None)
    assert not ok and out.startswith("::warning title=model_store::"), out
    assert kinds() == [("frozen", "2026-09-01 06:00 JST")]          # 取得できなければ予備で動く


def test_corrupted_asset_is_rejected_and_current_untouched():
    root, rel = sandbox()
    fake_publish(rel, "2026-10-03 06:12 JST", "d1")
    drop_build()
    assert quiet(ms.fetch)[0][0]
    p2 = fake_publish(rel, "2026-10-04 06:05 JST", "d2")
    drop_build()
    bad = rel / p2["asset"]
    raw = bytearray(bad.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    bad.write_bytes(bytes(raw))
    (ok, msg), out = quiet(ms.fetch)
    assert not ok and "sha256 不一致" in msg, msg
    assert kinds()[0] == ("live", "2026-10-03 06:12 JST")
    res = ms.verify(now=datetime(2026, 10, 4, 9, 0, tzinfo=ms.JST), thresholds={})
    assert res["level"] == "critical" and res["criticals"][0].startswith("M1"), res
    # 途中で切れた資産(size 不一致)
    bad.write_bytes(bytes(raw[: len(raw) // 2]))
    (ok, msg), out = quiet(ms.fetch, force=True)
    assert not ok and "size 不一致" in msg, msg
    assert kinds()[0] == ("live", "2026-10-03 06:12 JST")


def _evil_pointer(rel, build_tar):
    """中身が想定外の tar.gz を偽Releaseに置き、それを指す(形式は正しい)参照先を返す。"""
    tmp = ms.ROOT / "_evil.tar.gz"
    build_tar(tmp)
    sha = ms._sha256(tmp)
    name = "model-20261003-0612-%s.tar.gz" % sha[:12]
    shutil.move(str(tmp), str(rel / name))
    ptr = {"v": 1, "tag": ms.TAG, "asset": name, "sha256": sha, "size": (rel / name).stat().st_size,
           "trained_at": "2026-10-03 06:12 JST", "period_end": None,
           "files": {n: "0" * 64 for n in ms.FILES}}
    assert ms._pointer_problem(ptr) is None
    ms._atomic_write(ms._p("pointer"), json.dumps(ptr))
    return ptr


def test_unexpected_tar_members_are_rejected():
    root, rel = sandbox()
    src = make_model(root / "_src", "2026-10-03 06:12 JST", "x")

    def traversal(path):
        with tarfile.open(path, "w:gz") as tf:
            for n in ms.FILES:
                tf.add(src / n, arcname=n)
            tf.add(src / "meta.json", arcname="../evil.json")

    def symlink(path):
        with tarfile.open(path, "w:gz") as tf:
            for n in ms.FILES[1:]:
                tf.add(src / n, arcname=n)
            ti = tarfile.TarInfo("meta.json")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tf.addfile(ti)

    def missing(path):
        with tarfile.open(path, "w:gz") as tf:
            for n in ms.FILES[1:]:
                tf.add(src / n, arcname=n)

    def wrong_content(path):                       # 4ファイル揃っているが、参照先の files と中身が違う
        with tarfile.open(path, "w:gz") as tf:
            for n in ms.FILES:
                tf.add(src / n, arcname=n)

    for build_tar, word in ((traversal, "想定外"), (symlink, "想定外"), (missing, "足りない"),
                            (wrong_content, "中身の sha256 不一致")):
        ptr = _evil_pointer(rel, build_tar)
        (ok, msg), out = quiet(ms.fetch, force=True)
        assert not ok and word in msg, (build_tar.__name__, msg)
        assert ms.live_current() is None
        assert not (root / "evil.json").exists() and not (root / "data" / "evil.json").exists()
        assert not (ms._p("live") / "evil.json").exists()
        try:
            ms.install(rel / ptr["asset"], ptr)
            raise AssertionError("install は例外を出すはず")
        except RuntimeError:
            pass
    assert kinds() == [("frozen", "2026-09-01 06:00 JST")]


def test_valid_dir_checks():
    root, rel = sandbox()
    d = make_model(root / "m", "2026-10-03 06:12 JST")
    assert ms.valid_dir(d)["trained_at"] == "2026-10-03 06:12 JST"
    full = body("x")
    cases = {
        "途中で切れた": full[: len(full) // 2 + 60000],
        "小さすぎる": HEAD + TAIL,
        "先頭が違う": b"<html>" + full[6:],
        "空": b"",
    }
    for label, data in cases.items():
        make_model(d, "2026-10-03 06:12 JST")
        (d / "model_top2.txt").write_bytes(data)
        assert ms.valid_dir(d) is None, label
    make_model(d, "2026-10-03 06:12 JST")
    (d / "model_win.txt").unlink()
    assert ms.valid_dir(d) is None
    for meta in ("{", "[]", "{}", '{"trained_at": 20261003}', '{"trained_at": "yesterday"}', '{"trained_at": ""}'):
        make_model(d, "2026-10-03 06:12 JST")
        (d / "meta.json").write_text(meta, encoding="utf-8")
        assert ms.valid_dir(d) is None, meta
    make_model(d, "2026-10-03 06:12 JST")
    (d / "meta.json").unlink()                                      # train.py が途中で落ちた状態
    assert ms.valid_dir(d) is None
    assert ms.valid_dir(root / "does-not-exist") is None
    # Windows の作業ツリー(改行が CRLF)でも検査は通る
    make_model(d, "2026-10-03 06:12 JST")
    for n in ms.MODEL_FILES:
        (d / n).write_bytes((d / n).read_bytes().replace(b"\n", b"\r\n"))
    assert ms.valid_dir(d) is not None


def test_candidates_order():
    root, rel = sandbox()
    assert kinds() == [("frozen", "2026-09-01 06:00 JST")]                         # (a) 予備だけ
    p1 = fake_publish(rel, "2026-10-03 06:12 JST", "d1")
    drop_build()
    assert quiet(ms.fetch)[0][0]
    assert [k for k, _ in kinds()] == ["live", "frozen"]                           # (b) 取得済みあり
    make_model(ms._p("build"), "2026-10-04 06:05 JST", "d2")
    assert [k for k, _ in kinds()] == ["build", "live", "frozen"]                  # (c) build あり
    # 同時刻なら build > live > frozen
    make_model(ms._p("build"), "2026-10-03 06:12 JST", "d1")
    make_model(ms._p("frozen"), "2026-10-03 06:12 JST", "d1")
    assert [k for k, _ in kinds()] == ["build", "live", "frozen"]
    # 古い build は後ろへ回る(新しい取得分を優先)
    make_model(ms._p("build"), "2026-09-20 06:00 JST", "old")
    make_model(ms._p("frozen"), "2026-09-01 06:00 JST", "frozen")
    assert [k for k, _ in kinds()] == ["live", "build", "frozen"]
    drop_build()
    # (d) 取得済みのモデルが途中で切れている → 検査で外れて予備
    f = ms.live_current() / "model_win.txt"
    f.write_bytes(f.read_bytes()[:110000])
    assert ms.live_current() is None
    assert kinds() == [("frozen", "2026-09-01 06:00 JST")]
    # CURRENT の中身が不正でも落ちない
    (ms._p("live") / "CURRENT").write_text("../../x\n", encoding="utf-8")
    assert ms.live_current() is None and [k for k, _ in kinds()] == ["frozen"]
    # 壊れた取得分は、次の fetch で取り直される
    (ms._p("live") / "CURRENT").write_text(p1["sha256"][:12] + "\n", encoding="utf-8")
    assert quiet(ms.fetch)[0][0] and kinds()[0] == ("live", "2026-10-03 06:12 JST")
    # 何も無ければ空
    shutil.rmtree(ms._p("live"))
    shutil.rmtree(ms._p("frozen"))
    assert ms.candidates() == []


def test_build_not_older_than_pointer_skips_network():
    root, rel = sandbox()
    fake_publish(rel, "2026-10-03 06:12 JST", "d1")
    # 配布した直後の daily: build と参照先が同時刻 → 通信しない
    (ok, msg), out = quiet(ms.fetch)
    assert ok and out == "" and NET["n"] == 0 and not ms._p("live").exists()
    assert kinds()[0] == ("build", "2026-10-03 06:12 JST")
    # 配布に失敗した日の daily: build が参照先より新しい → 通信しない。朝の予測は build で作る
    make_model(ms._p("build"), "2026-10-04 06:05 JST", "d2")
    os.environ["MODEL_BASE_URL"] = (root / "nowhere").as_uri()
    (ok, msg), out = quiet(ms.fetch)
    assert ok and NET["n"] == 0
    assert kinds()[0] == ("build", "2026-10-04 06:05 JST")
    # build が参照先より古ければ取りに行く
    os.environ["MODEL_BASE_URL"] = rel.as_uri()
    make_model(ms._p("build"), "2026-09-20 06:00 JST", "old")
    (ok, msg), out = quiet(ms.fetch)
    assert ok and NET["n"] == 1 and kinds()[0] == ("live", "2026-10-03 06:12 JST")


def test_invalid_pointer_is_rejected():
    root, rel = sandbox()
    good = fake_publish(rel, "2026-10-03 06:12 JST", "d1")
    drop_build()
    assert ms._pointer_problem(good) is None
    bad_cases = [
        dict(good, asset="../../etc/passwd"),
        dict(good, asset="../" + good["asset"]),
        dict(good, asset=good["asset"] + "\n"),
        dict(good, asset=good["asset"].replace("model-", "MODEL-")),
        dict(good, asset="model-20261003-0612-%s.tar.gz?x=1" % good["sha256"][:12]),
        dict(good, asset="model-20261003-0612-%s.tar.gz" % ("0" * 12)),      # sha256 と食い違う
        dict(good, asset=None),
        dict(good, sha256=good["sha256"][:63]),
        dict(good, sha256=good["sha256"].upper()),
        dict(good, size=str(good["size"])),
        dict(good, size=True),
        dict(good, size=0),
        dict(good, size=ms.MAX_BYTES + 1),
        dict(good, trained_at=None),
        dict(good, trained_at="soon"),
        dict(good, files=None),
        dict(good, files={"meta.json": good["files"]["meta.json"]}),
        dict(good, files=dict(good["files"], **{"../x": "0" * 64})),
        dict(good, files=dict(good["files"], **{"meta.json": "zz"})),
        [good],
        "text",
    ]
    for bad in bad_cases:
        assert ms._pointer_problem(bad), bad
        ms._p("pointer").write_text(json.dumps(bad), encoding="utf-8")
        ptr, problem = ms.read_pointer()
        assert ptr is None and problem, bad
        assert ms.load_pointer() is None
    ms._p("pointer").write_text("{not json", encoding="utf-8")
    assert ms.read_pointer() == (None, "JSON として読めない")
    (ok, msg), out = quiet(ms.fetch)
    assert not ok and "不正" in out and NET["n"] == 0               # 不正な参照先では通信もしない
    assert kinds() == [("frozen", "2026-09-01 06:00 JST")]
    assert ms.status()["in_sync"] is False
    res = ms.verify(now=datetime(2026, 9, 1, 9, 0, tzinfo=ms.JST), thresholds={})
    assert res["level"] == "critical" and "不正" in res["criticals"][0], res
    ms._p("pointer").unlink()
    assert ms.read_pointer() == (None, None)


def test_fetch_never_raises():
    root, rel = sandbox()
    fake_publish(rel, "2026-10-03 06:12 JST", "d1")
    drop_build()

    def boom(*a, **kw):
        raise ValueError("想定外")

    ms.read_pointer = boom
    (ok, msg), out = quiet(ms.fetch)
    assert not ok and "想定外の例外" in msg
    ms.read_pointer = REAL_READ_POINTER
    ms._http_get = boom
    (ok, msg), out = quiet(ms.fetch)
    assert not ok and kinds() == [("frozen", "2026-09-01 06:00 JST")]


def test_download_limits():
    root, rel = sandbox()
    p1 = fake_publish(rel, "2026-10-03 06:12 JST", "d1")
    drop_build()
    ms._http_get = REAL_HTTP_GET
    old = (ms.DL_DEADLINE, ms.MAX_BYTES)
    try:
        ms.DL_DEADLINE = -1                                        # 全体の打ち切り
        (ok, msg), out = quiet(ms.fetch, force=True)
        assert not ok and "TimeoutError" in msg, msg
        ms.DL_DEADLINE = old[0]
        ms.MAX_BYTES = 1000                                        # 大きさの上限
        try:
            ms.download(p1["asset"], root / "_dl")
            raise AssertionError("上限を超えたら例外のはず")
        except RuntimeError as e:
            assert "大きすぎる" in str(e)
    finally:
        ms.DL_DEADLINE, ms.MAX_BYTES = old
    assert ms.live_current() is None
    assert quiet(ms.fetch, force=True)[0][0] and ms.live_current() is not None
    assert ms.asset_url("a.tar.gz") == rel.as_uri() + "/a.tar.gz"
    os.environ.pop("MODEL_BASE_URL")
    assert ms.asset_url("a.tar.gz") == "https://github.com/t-fuji777/kyotei-ai/releases/download/model-live/a.tar.gz"
    os.environ["GITHUB_REPOSITORY"] = "someone/fork"
    assert ms.asset_url("a.tar.gz") == "https://github.com/someone/fork/releases/download/model-live/a.tar.gz"
    os.environ.pop("GITHUB_REPOSITORY")


# ------------------------------------------------------------------ 予備の更新

def test_freeze_rules():
    root, rel = sandbox()
    frozen = ms._p("frozen")
    try:
        ms.freeze()
        raise AssertionError("元が無ければ例外のはず")
    except RuntimeError:
        pass
    code, out = quiet(ms.main, ["freeze"])
    assert code == 1 and "freeze に失敗" in out
    make_model(ms._p("build"), "2026-10-03 06:10 JST", "new")
    before = tree_hash(frozen)
    changed, msg = ms.freeze(now=datetime(2026, 9, 20, 7, 0, tzinfo=ms.JST))        # 予備は19日前 → 更新しない
    assert changed is False and tree_hash(frozen) == before, msg
    changed, msg = ms.freeze(now=datetime(2026, 9, 30, 7, 0, tzinfo=ms.JST))        # 29日前 → 更新しない
    assert changed is False
    changed, msg = ms.freeze(now=datetime(2026, 10, 1, 7, 0, tzinfo=ms.JST))        # 30日前 → 更新する
    assert changed is True
    assert ms.valid_dir(frozen)["trained_at"] == "2026-10-03 06:10 JST"
    assert tree_hash(frozen) == tree_hash(ms._p("build"))
    assert sorted(p.name for p in frozen.iterdir()) == sorted(ms.FILES)             # 一時ファイルを残さない
    # 参照先(同じディレクトリにある)は予備の更新で消えない
    ptr = fake_publish(rel, "2026-11-10 06:00 JST", "nov")
    changed, msg = ms.freeze(now=datetime(2026, 11, 10, 7, 0, tzinfo=ms.JST))
    assert changed is True and ms.load_pointer() == ptr
    # --force は日数に関わらず更新する(train.yml 用)
    make_model(ms._p("build"), "2026-11-11 06:00 JST", "nov11")
    assert ms.freeze(now=datetime(2026, 11, 11, 7, 0, tzinfo=ms.JST))[0] is False
    assert ms.freeze(force=True, now=datetime(2026, 11, 11, 7, 0, tzinfo=ms.JST))[0] is True
    assert ms.valid_dir(frozen)["trained_at"] == "2026-11-11 06:00 JST"
    # 予備が壊れていれば日数に関わらず作り直す
    (frozen / "model_win.txt").write_bytes(b"broken")
    assert ms.freeze(now=datetime(2026, 11, 11, 8, 0, tzinfo=ms.JST))[0] is True
    assert ms.valid_dir(frozen) is not None
    # build が無ければ取得済みの現行版が元になる
    drop_build()
    p = fake_publish(rel, "2026-12-25 06:00 JST", "dec")
    drop_build()
    assert quiet(ms.fetch)[0][0]
    assert ms.freeze(now=datetime(2026, 12, 25, 7, 0, tzinfo=ms.JST))[0] is True
    assert ms.valid_dir(frozen)["trained_at"] == "2026-12-25 06:00 JST" and ms.load_pointer() == p
    code, out = quiet(ms.main, ["freeze"])
    assert code == 0 and "更新しない" in out


def test_freeze_replaces_meta_last_and_keeps_tmp_out_of_git_dir():
    """置き換えは meta.json が最後(途中で止まっても古い meta のままなので次回やり直す)。
    一時ファイルは git 管理下の data/model に作らない。"""
    root, rel = sandbox()
    frozen = ms._p("frozen")
    make_model(ms._p("build"), "2026-10-03 06:10 JST", "new")
    order, seen_in_frozen = [], []
    real_replace = os.replace

    def spy(src, dst):
        order.append(Path(dst).name)
        seen_in_frozen.append(sorted(p.name for p in frozen.iterdir()))
        return real_replace(src, dst)

    ms.os.replace = spy
    try:
        assert ms.freeze(now=datetime(2026, 10, 3, 7, 0, tzinfo=ms.JST))[0] is True
    finally:
        ms.os.replace = real_replace
    assert order[-1] == "meta.json" and sorted(order) == sorted(ms.FILES), order
    assert all(names == sorted(ms.FILES) for names in seen_in_frozen), seen_in_frozen
    assert not [p for p in ms._p("live").iterdir() if p.name.startswith(".freeze-")]


def test_freeze_does_not_downgrade():
    """予備が30日以上前でも、元のモデルが予備より新しくなければ更新しない。"""
    root, rel = sandbox()
    frozen = ms._p("frozen")                                  # 2026-09-01 学習
    make_model(ms._p("build"), "2026-08-15 06:00 JST", "older")
    before = tree_hash(frozen)
    changed, msg = ms.freeze(now=datetime(2026, 10, 20, 7, 0, tzinfo=ms.JST))
    assert changed is False and "新しくない" in msg and tree_hash(frozen) == before, msg


def test_pointer_trained_at_mismatch_is_rejected():
    """参照先の trained_at だけが資産の中身と違う場合も取得を拒否し、CURRENT は変えない。"""
    root, rel = sandbox()
    ptr = fake_publish(rel, "2026-10-03 06:12 JST", "a")
    drop_build()
    assert quiet(ms.fetch)[0][0]
    cur = ms.live_current()
    ptr2 = fake_publish(rel, "2026-10-04 06:12 JST", "b")
    drop_build()
    bad = dict(ptr2, trained_at="2026-10-04 06:13 JST")
    ms._atomic_write(ms._p("pointer"), json.dumps(bad, indent=1) + "\n")
    (ok, msg), out = quiet(ms.fetch, force=True)
    assert not ok and "trained_at が参照先と違う" in msg, msg
    assert ms.live_current() == cur


def test_install_keeps_existing_same_version():
    """同じ版が既に置いてある時は消さずに使う(同時に取得した別のプロセスが読んでいる版を消さない)。"""
    root, rel = sandbox()
    ptr = fake_publish(rel, "2026-10-03 06:12 JST", "a")
    drop_build()
    assert quiet(ms.fetch)[0][0]
    cur = ms.live_current()
    marker = cur / "model_win.txt"
    ino_before = (marker.stat().st_mtime_ns, marker.stat().st_size)
    removed = []
    real_rmtree = shutil.rmtree

    def spy(path, *a, **kw):
        removed.append(Path(path).name)
        return real_rmtree(path, *a, **kw)

    ms.shutil.rmtree = spy
    try:
        ms.install(rel / ptr["asset"], ptr)
    finally:
        ms.shutil.rmtree = real_rmtree
    assert cur.name not in removed, removed
    assert (marker.stat().st_mtime_ns, marker.stat().st_size) == ino_before
    assert ms.live_current() == cur
    # 置いてある版が壊れていれば置き直す
    marker.write_bytes(b"broken")
    ms.install(rel / ptr["asset"], ptr)
    assert ms.live_current() == cur and tree_hash(cur) == ptr["files"]


def test_verify_needs_republish_only_for_asset_or_backup_problems():
    root, rel = sandbox()
    ptr = fake_publish(rel, "2026-10-03 06:12 JST", "a")
    drop_build()
    make_model(ms._p("frozen"), "2026-10-01 06:00 JST", "frozen")
    th = {"model_stale_crit_days": 2, "model_frozen_warn_days": 45}
    ok = ms.verify(now=datetime(2026, 10, 3, 9, 0, tzinfo=ms.JST), thresholds=th)
    assert ok["level"] == "ok" and ok["needs_republish"] is False, ok
    stale = ms.verify(now=datetime(2026, 10, 5, 9, 0, tzinfo=ms.JST), thresholds=th)      # 学習が止まっているだけ
    assert stale["level"] == "critical" and stale["needs_republish"] is False, stale
    assert any(c.startswith("M2") for c in stale["criticals"])
    (rel / ptr["asset"]).unlink()                                                          # 資産が消えた
    gone = ms.verify(now=datetime(2026, 10, 3, 9, 0, tzinfo=ms.JST), thresholds=th)
    assert gone["level"] == "critical" and gone["needs_republish"] is True, gone
    ms._p("pointer").write_text("{broken", encoding="utf-8")                              # 参照先が不正
    badp = ms.verify(now=datetime(2026, 10, 3, 9, 0, tzinfo=ms.JST), thresholds=th)
    assert badp["level"] == "critical" and badp["needs_republish"] is True, badp


def test_age_days_counts_in_jst():
    from datetime import timedelta, timezone
    utc = timezone(timedelta(0))
    now_utc = datetime(2026, 10, 2, 15, 1, tzinfo=utc)                 # JST では 10/03 00:01
    assert ms._age_days("2026-10-01 23:58 JST", now_utc) == 2
    assert ms._age_days("2026-10-01 23:58 JST", datetime(2026, 10, 3, 0, 1, tzinfo=ms.JST)) == 2
    assert ms._age_days("壊れた値", now_utc) is None


def test_valid_dir_accepts_long_tail_after_end_of_parameters():
    """'end of parameters' の後ろに長い pandas_categorical の行が続いても、正常なモデルとして通す。"""
    root, rel = sandbox()
    d = make_model(root / "_long", "2026-10-03 06:12 JST", "x")
    long_tail = b"\nend of parameters\n\npandas_categorical:[" + b"1234," * 4000 + b"0]\n"
    for n in ms.MODEL_FILES:
        (d / n).write_bytes(HEAD + FILLER + long_tail)
    assert len(long_tail) > 4096 * 4 and ms.valid_dir(d) is not None
    (d / "model_win.txt").write_bytes(HEAD + FILLER)                   # 末尾の印が無い(書きかけ)
    assert ms.valid_dir(d) is None


# ------------------------------------------------------------------ 健全性の判定

def test_verify_levels():
    root, rel = sandbox()
    th = {}
    res = ms.verify(now=datetime(2026, 9, 2, 9, 0, tzinfo=ms.JST), thresholds=th)   # 参照先なし・予備は前日
    assert res["level"] == "warning" and res["warnings"][0].startswith("M0"), res
    assert res["effective_trained_at"] == "2026-09-01 06:00 JST" and NET["n"] == 0
    ptr = fake_publish(rel, "2026-10-03 06:10 JST", "d1")
    drop_build()
    make_model(ms._p("frozen"), "2026-10-01 06:00 JST", "frozen")
    res = ms.verify(now=datetime(2026, 10, 3, 9, 0, tzinfo=ms.JST), thresholds=th)
    assert res["level"] == "ok" and res["asset_ok"] is True, res
    assert res["effective_trained_at"] == "2026-10-03 06:10 JST" and NET["n"] == 1
    assert not ms._p("live").exists() or not any(ms._p("live").iterdir())           # 検査は取得物を残さない
    res = ms.verify(now=datetime(2026, 10, 4, 11, 0, tzinfo=ms.JST), thresholds=th)  # 翌日の午前 → まだ正常
    assert res["level"] == "ok", res
    res = ms.verify(now=datetime(2026, 10, 4, 13, 0, tzinfo=ms.JST), thresholds=th)  # 翌日の午後 → 警告
    assert res["level"] == "warning" and res["warnings"][0].startswith("M4"), res
    res = ms.verify(now=datetime(2026, 10, 5, 9, 0, tzinfo=ms.JST), thresholds=th)   # 2日前 → 異常
    assert res["level"] == "critical" and res["criticals"][0].startswith("M2"), res
    res = ms.verify(now=datetime(2026, 10, 5, 9, 0, tzinfo=ms.JST), thresholds={"model_stale_crit_days": 5})
    assert res["level"] == "ok", res                                                 # 閾値は設定で変えられる
    # 予備が古い
    res = ms.verify(now=datetime(2026, 11, 15, 9, 0, tzinfo=ms.JST), thresholds={"model_stale_crit_days": 99})
    assert res["level"] == "warning" and any(w.startswith("M5") for w in res["warnings"]), res
    # 資産が消えた → 3回試してから critical。実効は予備(2日前)なので M2 も出る
    (rel / ptr["asset"]).unlink()
    n0 = NET["n"]
    res = ms.verify(now=datetime(2026, 10, 3, 9, 0, tzinfo=ms.JST), thresholds=th)
    assert res["level"] == "critical" and res["criticals"][0].startswith("M1"), res
    assert any(c.startswith("M2") for c in res["criticals"]) and res["asset_ok"] is False
    assert res["effective_trained_at"] == "2026-10-01 06:00 JST"
    assert NET["n"] == n0 + ms.VERIFY_TRIES
    # 予備が壊れている
    (ms._p("frozen") / "model_top3.txt").write_bytes(b"x")
    res = ms.verify(now=datetime(2026, 10, 3, 9, 0, tzinfo=ms.JST), thresholds=th)
    assert any(c.startswith("M3") for c in res["criticals"]), res
    # 既定の閾値は scripts/health_thresholds.json から読む
    th_file = json.loads((REPO / "scripts" / "health_thresholds.json").read_text(encoding="utf-8"))
    assert th_file["model_stale_crit_days"] == 2 and th_file["model_frozen_warn_days"] == 45


def test_verify_always_outputs_json_even_on_exception():
    root, rel = sandbox()

    def boom(d):
        raise OSError("disk exploded")

    ms.valid_dir = boom
    res = ms.verify()
    assert res["level"] == "critical" and res["criticals"][0].startswith("M9"), res
    assert set(res) >= {"level", "criticals", "warnings", "effective_trained_at"}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ms.main(["verify", "--json"])
    out = json.loads(buf.getvalue())
    assert code == 2 and out["level"] == "critical" and "disk exploded" in out["criticals"][0]
    ms.valid_dir = REAL_VALID_DIR
    # 例外が無い時の終了コード(参照先なし・予備は本日学習 → 警告=1)
    make_model(ms._p("frozen"), datetime.now(ms.JST).strftime("%Y-%m-%d 06:00 JST"), "today")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ms.main(["verify", "--json"])
    assert code == 1 and json.loads(buf.getvalue())["level"] == "warning"


def test_cli_verify_subprocess_outputs_utf8_json():
    """watchdog と同じ呼び方(別プロセス・標準出力をそのまま受ける)で、UTF-8 の JSON が出ること。
    リポジトリの状態に左右されないよう、別プロセスの側でも根を一時ディレクトリへ差し替える。"""
    root, rel = sandbox()
    ptr = fake_publish(rel, "2026-10-03 06:12 JST", "d1")
    drop_build()
    (rel / ptr["asset"]).unlink()                                   # 資産が消えた → critical(理由は日本語)
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import model_store as ms; "
            "from pathlib import Path; ms.ROOT = Path(sys.argv[2]); ms._sleep = lambda sec: None; "
            "sys.exit(ms.main(['verify', '--json']))")
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    r = subprocess.run([sys.executable, "-c", code, str(REPO / "scripts"), str(root)],
                       capture_output=True, env=env, timeout=120)
    out = json.loads(r.stdout.decode("utf-8"))
    assert r.returncode == 2 and out["level"] == "critical", (r.returncode, out)
    assert any(c.startswith("M1 配布物") for c in out["criticals"]), out
    # スクリプトとして直接実行できること(status は通信しない)
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "model_store.py"), "status"],
                       capture_output=True, env=dict(env, PYTHONUTF8="1"), timeout=120)
    assert r.returncode == 0 and "candidates" in json.loads(r.stdout.decode("utf-8"))


# ------------------------------------------------------------------ 配布(gh は偽物)

class FakeGh:
    """gh release view / create / upload / delete-asset の代役。資産は rel ディレクトリに置く。"""

    def __init__(self, rel, exists=True):
        self.rel = rel
        self.exists = exists
        self.assets = {}
        self.calls = []
        self.fail = set()
        self.create_race = False          # create は失敗するが、Release は(他の誰かが)作っている
        self.drop_upload = False          # upload は成功を返すが、公開URLからは取れない
        self.corrupt_upload = False       # upload した中身が壊れる

    def add_existing(self, name, size=1000, state="uploaded"):
        (self.rel / name).write_bytes(b"old")
        self.assets[name] = {"size": size, "state": state}

    def names(self, sub):
        return [c[0] for c in self.calls if c[0][:2] == ("release", sub)]

    def __call__(self, *args, timeout=ms.GH_TIMEOUT, check=True):
        self.calls.append((args, timeout))
        rc, out, err = self._run(args)
        if check and rc != 0:
            raise RuntimeError("gh %s failed: %s" % (" ".join(args[:2]), err))
        return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def _run(self, args):
        sub = args[1]
        assert args[0] == "release" and args[2] == ms.TAG and "--repo" in args, args
        if sub == "view":
            if "view" in self.fail or not self.exists:
                return 1, "", "release not found"
            return 0, json.dumps({"assets": [dict(a, name=n) for n, a in self.assets.items()]}), ""
        if sub == "create":
            assert "--prerelease" in args and "--latest=false" in args and "--notes" in args, args
            if "create" in self.fail:
                if self.create_race:
                    self.exists = True
                return 1, "", "HTTP 422: already_exists"
            self.exists = True
            return 0, "", ""
        if sub == "upload":
            if "upload" in self.fail:
                return 1, "", "upload failed"
            path = Path(args[3])
            if path.name in self.assets and "--clobber" not in args:
                return 1, "", "asset under the same name already exists"
            if not self.drop_upload:
                data = path.read_bytes()
                (self.rel / path.name).write_bytes(data[:-1] + b"X" if self.corrupt_upload else data)
            self.assets[path.name] = {"size": path.stat().st_size, "state": "uploaded"}
            return 0, "", ""
        if sub == "delete-asset":
            assert "--yes" in args, args
            if "delete" in self.fail:
                return 1, "", "delete failed"
            self.assets.pop(args[3])
            (self.rel / args[3]).unlink()
            return 0, "", ""
        raise AssertionError("想定外の gh 呼び出し: %r" % (args,))


def use_fake_gh(rel, **kw):
    gh = FakeGh(rel, **kw)
    ms._gh = gh
    ms._gh_ready = lambda: True
    return gh


def test_publish_happy_path_creates_release_and_writes_pointer_last():
    root, rel = sandbox()
    gh = use_fake_gh(rel, exists=False)
    make_model(ms._p("build"), "2026-10-03 06:12 JST", "d1")
    frozen_before = tree_hash(ms._p("frozen"))
    ptr, out = quiet(ms.publish)
    assert len(gh.names("create")) == 1 and len(gh.names("upload")) == 1
    assert "--clobber" not in gh.names("upload")[0]
    assert ms.load_pointer() == ptr and ptr["trained_at"] == "2026-10-03 06:12 JST"
    assert (rel / ptr["asset"]).stat().st_size == ptr["size"] and ms._sha256(rel / ptr["asset"]) == ptr["sha256"]
    assert ptr["files"] == tree_hash(ms._p("build"))
    assert ms._p("pointer").read_bytes().endswith(b"\n") and b"\r" not in ms._p("pointer").read_bytes()
    # publish は取得物の切替も予備の更新もしない
    assert ms.live_current() is None and tree_hash(ms._p("frozen")).items() >= frozen_before.items()
    assert not any(p.name.startswith(".stage") for p in ms._p("live").iterdir())
    # gh の呼び出しは全て時間切れ付き(upload 180秒、他60秒)
    for args, timeout in gh.calls:
        assert timeout == (ms.GH_UPLOAD_TIMEOUT if args[1] == "upload" else ms.GH_TIMEOUT), (args, timeout)
    assert ms.GH_UPLOAD_TIMEOUT == 180 and ms.GH_TIMEOUT == 60
    assert REAL_GH.__kwdefaults__["timeout"] == 60                  # 本物の _gh も既定で時間切れ付き
    # 配布した直後の同じ実行機では build を使い、通信しない
    n0 = NET["n"]
    assert quiet(ms.fetch)[0][0] and NET["n"] == n0 and kinds()[0][0] == "build"
    # 別の実行機(ループ)は公開URLから取得して同じ中身になる
    drop_build()
    assert quiet(ms.fetch)[0][0] and tree_hash(ms.live_current()) == ptr["files"]
    # 同じモデルをもう一度配布しても、同名の資産があるのでアップロードしない
    make_model(ms._p("build"), "2026-10-03 06:12 JST", "d1")
    ptr2, out = quiet(ms.publish)
    assert ptr2 == ptr and len(gh.names("upload")) == 1 and "アップロードを省く" in out
    assert quiet(ms.main, ["publish"])[0] == 0


def test_publish_failures_never_touch_pointer():
    root, rel = sandbox()
    old = fake_publish(rel, "2026-10-02 06:00 JST", "d0")
    old_bytes = ms._p("pointer").read_bytes()
    frozen_before = tree_hash(ms._p("frozen"))

    def expect_fail(word):
        make_model(ms._p("build"), "2026-10-03 06:12 JST", "d1")
        try:
            quiet(ms.publish)
            raise AssertionError("publish は失敗するはず: " + word)
        except RuntimeError as e:
            assert word in str(e), (word, str(e))
        assert ms._p("pointer").read_bytes() == old_bytes, word
        assert tree_hash(ms._p("frozen")) == frozen_before, word
        code, out = quiet(ms.main, ["publish"])
        assert code == 1 and "モデルの配布に失敗" in out and ms._p("pointer").read_bytes() == old_bytes

    # Release を用意できない
    gh = use_fake_gh(rel, exists=False)
    gh.fail = {"create"}
    expect_fail("Release model-live を用意できない")
    assert gh.names("upload") == []
    # アップロードに失敗
    gh = use_fake_gh(rel)
    gh.fail = {"upload"}
    expect_fail("upload")
    # アップロードは成功と言うが、公開URLから読み戻せない → 参照先を書かない
    gh = use_fake_gh(rel)
    gh.drop_upload = True
    n0 = NET["n"]
    expect_fail("読み戻せない")
    assert NET["n"] == n0 + 2 * ms.READBACK_TRIES                   # 6回試す(publish と main の2回分)
    # 読み戻した中身が違う → 参照先を書かない
    gh = use_fake_gh(rel)
    gh.corrupt_upload = True
    expect_fail("読み戻せない")
    # 学習が途中で落ちている(meta.json が無い)
    gh = use_fake_gh(rel)
    make_model(ms._p("build"), "2026-10-03 06:12 JST", "d1")
    (ms._p("build") / "meta.json").unlink()
    code, out = quiet(ms.main, ["publish"])
    assert code == 1 and gh.calls == [] and ms._p("pointer").read_bytes() == old_bytes
    # gh が時間切れ
    gh = use_fake_gh(rel)

    def slow(*args, timeout=None, check=True):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=timeout)

    ms._gh = slow
    make_model(ms._p("build"), "2026-10-03 06:12 JST", "d1")
    code, out = quiet(ms.main, ["publish"])
    assert code == 1 and "TimeoutExpired" in out and ms._p("pointer").read_bytes() == old_bytes
    assert ms.load_pointer() == old


def test_publish_without_gh_exits_1_and_changes_nothing():
    """gh コマンドが無い環境: exit 1・参照先が作られない・data/model が変わらない。"""
    root, rel = sandbox()
    make_model(ms._p("build"), "2026-10-03 06:12 JST", "d1")
    frozen_before = tree_hash(ms._p("frozen"))
    saved = {k: os.environ.get(k) for k in ("PATH", "GH_TOKEN", "GITHUB_TOKEN")}
    try:
        os.environ["PATH"] = str(root / "empty-bin")
        os.environ["GH_TOKEN"] = "dummy-for-test"
        assert ms._gh_ready() is False                              # gh が無い
        code, out = quiet(ms.main, ["publish"])
        assert code == 1 and "gh コマンドと GH_TOKEN が必要" in out, out
        os.environ["GITHUB_ACTIONS"] = "true"
        code, out = quiet(ms.main, ["publish"])
        assert code == 1 and out.startswith("::error title=")
    finally:
        os.environ.pop("GITHUB_ACTIONS", None)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    assert not ms._p("pointer").exists()
    assert tree_hash(ms._p("frozen")) == frozen_before
    assert not ms._p("live").exists() or not any(ms._p("live").iterdir())


def test_publish_release_create_race_and_clobber_only_for_leftover():
    root, rel = sandbox()
    # create は失敗したが、直後の view が成功する(他の実行が先に作った) → 続行
    gh = use_fake_gh(rel, exists=False)
    gh.fail = {"create"}
    gh.create_race = True
    make_model(ms._p("build"), "2026-10-03 06:12 JST", "d1")
    ptr, out = quiet(ms.publish)
    assert ms.load_pointer() == ptr and len(gh.names("upload")) == 1
    # 同名の残骸(途中で切れたアップロード)がある時だけ --clobber
    make_model(ms._p("build"), "2026-10-04 06:05 JST", "d2")
    tar, sha, size = ms.build_tarball(ms._p("build"), root / "_peek")
    gh = use_fake_gh(rel)
    gh.add_existing(tar.name, size=123, state="uploaded")           # サイズが違う残骸
    ptr2, out = quiet(ms.publish)
    assert "--clobber" in gh.names("upload")[0] and ms.load_pointer() == ptr2
    assert ms._sha256(rel / ptr2["asset"]) == ptr2["sha256"]
    gh = use_fake_gh(rel)
    gh.add_existing(tar.name, size=size, state="new")               # 状態が uploaded でない残骸
    quiet(ms.publish)
    assert "--clobber" in gh.names("upload")[0]


def test_select_prune_keeps_current_and_old_pointer_asset():
    names = ["model-202610%02d-0600-%012x.tar.gz" % (d, d) for d in range(1, 11)]   # 10日分
    junk = ["notes.txt", "model-20260101-0600-zzzzzzzzzzzz.tar.gz", "model-20200101-0600-abc.tar.gz", None]
    cur, old = names[9], names[0]
    # 新しい順に7個残し、8個目以降(01〜03日)が対象。ただし旧参照先(01日)は残す
    assert ms.select_prune(names + junk, {cur, old}) == [names[2], names[1]]
    assert ms.select_prune(names + junk, {cur}) == [names[2], names[1], names[0]]
    assert ms.select_prune(names + junk, {cur, None}) == [names[2], names[1], names[0]]
    # 今回の資産が(学習時刻の巻き戻りで)古い名前でも消さない
    assert names[1] not in ms.select_prune(names, {names[1], old})
    assert ms.select_prune(names[:7], {cur}) == []
    assert ms.select_prune([], set()) == []
    assert ms.select_prune(names, {cur, old}, keep=2) == names[7:0:-1]
    assert not any(j in ms.select_prune(names + junk, set(), keep=0) for j in junk)  # 形の違う名前は触らない
    assert ms.KEEP_ASSETS == 7


def test_publish_prunes_old_assets_but_failure_is_only_a_warning():
    root, rel = sandbox()
    old = fake_publish(rel, "2026-09-20 06:00 JST", "old")           # main の参照先が指している資産(最古)
    gh = use_fake_gh(rel)
    gh.assets[old["asset"]] = {"size": old["size"], "state": "uploaded"}
    others = ["model-202609%02d-0600-%012x.tar.gz" % (d, d) for d in range(21, 30)]   # 9個
    for n in others:
        gh.add_existing(n)
    gh.add_existing("readme.txt")
    make_model(ms._p("build"), "2026-10-03 06:12 JST", "d1")
    ptr, out = quiet(ms.publish)
    left = sorted(gh.assets)
    # 11個(旧参照先 + 9個 + 今回)のうち新しい7個を残す → 対象は古い4個。旧参照先は残すので削除は3個
    assert sorted(a[3] for a in gh.names("delete-asset")) == sorted(others[:3])
    assert old["asset"] in left and ptr["asset"] in left and "readme.txt" in left
    assert len([n for n in left if ms.ASSET_RE.fullmatch(n)]) == 8
    # 整理に失敗しても配布は成功(参照先は新しい資産を指す)
    make_model(ms._p("build"), "2026-10-04 06:05 JST", "d2")
    gh.fail = {"delete"}
    for n in others[:3]:
        gh.add_existing(n)
    ptr2, out = quiet(ms.publish)
    assert ms.load_pointer() == ptr2 and "整理に失敗" in out
    assert quiet(ms.main, ["publish"])[0] == 0


# ------------------------------------------------------------------ 実物のモデル(LF 版)

def real_model_dir():
    """git show HEAD:data/model/<名前> で LF 版を取り出す。取り出せなければ None。"""
    d = Path(tempfile.mkdtemp(prefix="ms_real_"))
    TMP_ROOTS.append(d)
    try:
        for n in ms.FILES:
            r = subprocess.run(["git", "-C", str(REPO), "show", "HEAD:data/model/" + n],
                               capture_output=True, timeout=120)
            if r.returncode != 0 or not r.stdout:
                return None
            (d / n).write_bytes(r.stdout)
    except Exception:
        return None
    if b"\r\n" in (d / "model_win.txt").read_bytes()[:4096] or ms.valid_dir(d) is None:
        return None
    return d


def test_real_model_roundtrip_is_identical():
    real = real_model_dir()
    if real is None:
        if os.environ.get("REQUIRE_REAL_MODEL"):
            raise AssertionError("実物のモデルを取り出せない(REQUIRE_REAL_MODEL 指定時は飛ばさない)")
        print("     (飛ばす: git から実物のモデルを取り出せない)")
        SKIPPED.append("test_real_model_roundtrip_is_identical")
        return
    root, rel = sandbox()
    shutil.copytree(real, ms._p("build"))
    stage = root / "_stage"
    tar, sha, size = ms.build_tarball(ms._p("build"), stage)
    ptr = ms.make_pointer(ms._p("build"), tar.name, sha, size)
    shutil.copyfile(tar, rel / tar.name)
    ms._atomic_write(ms._p("pointer"), json.dumps(ptr, indent=1) + "\n")
    drop_build()
    make_model(ms._p("frozen"), "2020-01-01 06:00 JST", "ancient")
    assert quiet(ms.fetch)[0][0]
    cur = ms.live_current()
    assert tree_hash(cur) == tree_hash(real) == ptr["files"]
    print("     資産 %s %d バイト" % (tar.name, size))
    try:
        import lightgbm as lgb
        import numpy as np
    except Exception:
        if os.environ.get("REQUIRE_REAL_MODEL"):
            raise
        print("     (予測の一致は飛ばす: lightgbm / numpy が無い)")
        SKIPPED.append("test_real_model_roundtrip_is_identical(予測の一致・load_models)")
        return
    rng = np.random.default_rng(0)
    for n in ms.MODEL_FILES:
        a = lgb.Booster(model_file=str(real / n))
        b = lgb.Booster(model_file=str(cur / n))
        x = rng.normal(size=(200, a.num_feature()))
        assert a.num_trees() == b.num_trees() > 0
        assert (a.predict(x) == b.predict(x)).all(), n
    # predict_today.load_models が候補の先頭から読み、読めない候補は飛ばす
    try:
        import predict_today as pt
    except Exception as e:
        if os.environ.get("REQUIRE_REAL_MODEL"):
            raise
        print("     (load_models は飛ばす: predict_today を読み込めない: %s)" % type(e).__name__)
        SKIPPED.append("test_real_model_roundtrip_is_identical(load_models)")
        return
    old_root = pt.ROOT
    pt.ROOT = root
    try:
        (meta, models, sengen), out = quiet(pt.load_models)
        assert "model: live trained_at=%s" % ptr["trained_at"] in out, out
        assert meta["trained_at"] == ptr["trained_at"] and set(models) == {"win", "top2", "top3"}
        assert "top5_min" in sengen and "venues" in sengen
        # この場で学習した新しいモデルがあれば、それを使う(朝の予測は Release に依存しない)
        shutil.copytree(real, ms._p("build"))
        m = json.loads((ms._p("build") / "meta.json").read_text(encoding="utf-8"))
        m["trained_at"] = "2099-01-01 06:00 JST"
        (ms._p("build") / "meta.json").write_text(json.dumps(m), encoding="utf-8")
        (meta, models, sengen), out = quiet(pt.load_models)
        assert "model: build trained_at=2099-01-01 06:00 JST" in out, out
        # 先頭の候補が読めなければ次へ落ちる(LightGBM の例外を模擬)
        real_booster = pt.lgb.Booster

        def picky(model_file=None, **kw):
            if "model_build" in str(model_file):
                raise RuntimeError("模擬: 読み込み失敗")
            return real_booster(model_file=model_file, **kw)

        pt.lgb.Booster = picky
        try:
            (meta, models, sengen), out = quiet(pt.load_models)
        finally:
            pt.lgb.Booster = real_booster
        assert "build を読めない" in out and "model: live trained_at=%s" % ptr["trained_at"] in out, out
        # 改行が CRLF のモデルは LightGBM に渡さず、次の候補へ落ちる(渡すとプロセスごと落ちる)
        bw = ms._p("build") / "model_win.txt"
        lf = bw.read_bytes()
        bw.write_bytes(lf.replace(b"\n", b"\r\n"))
        try:
            (meta, models, sengen), out = quiet(pt.load_models)
        finally:
            bw.write_bytes(lf)
        assert "build は改行が CRLF なので使えない" in out, out
        assert "model: live trained_at=%s" % ptr["trained_at"] in out, out
        # 取得処理が例外を出しても予測は止まらない
        real_fetch = ms.fetch

        def bad_fetch(*a, **kw):
            raise RuntimeError("模擬: 取得の不具合")

        ms.fetch = bad_fetch
        try:
            (meta, models, sengen), out = quiet(pt.load_models)
        finally:
            ms.fetch = real_fetch
        assert "取得の不具合" in out and "model: build" in out, out
        # 参照先が無い導入前: 予備(git の data/model)だけで動く。取得物も作らない
        drop_build()
        shutil.rmtree(ms._p("live"))
        shutil.rmtree(ms._p("frozen"))
        shutil.copytree(real, ms._p("frozen"))
        real_at = json.loads((real / "meta.json").read_text(encoding="utf-8"))["trained_at"]
        (meta, models, sengen), out = quiet(pt.load_models)
        assert out.strip() == "model: frozen trained_at=%s" % real_at, out
        assert not ms._p("live").exists() and NET["n"] == 1
        # どの候補も無い時だけ止まる
        shutil.rmtree(ms._p("frozen"))
        try:
            quiet(pt.load_models)
            raise AssertionError("候補が無ければ止まるはず")
        except SystemExit as e:
            assert "使えるモデルが無い" in str(e)
    finally:
        pt.ROOT = old_root


def cleanup():
    ms._http_get = REAL_HTTP_GET
    ms._gh = REAL_GH
    ms._gh_ready = REAL_GH_READY
    os.environ.pop("MODEL_BASE_URL", None)
    for d in TMP_ROOTS:
        shutil.rmtree(d, ignore_errors=True)


atexit.register(cleanup)

if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok   " + name)
    if SKIPPED:
        print("%d tests run, 一部を飛ばした: %s" % (n, " / ".join(SKIPPED)))
        print("OK (一部未確認)")
    else:
        print("%d tests passed" % n)
        print("ALL OK")

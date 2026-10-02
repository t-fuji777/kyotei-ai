#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""学習済みモデルの配布と取得。標準ライブラリのみ(pip install 不要)。

学習済みモデル(3本で約10MB)は毎朝の再学習でほぼ全体が変わるため、git に積むと履歴が
1日あたり約4MB増える。そこで毎日のモデルは GitHub Release(タグ model-live)の資産に置き、
git には「どの資産が現行か」を示す小さな参照先だけを積む。

  data/model_build/              train.py の出力(gitignore)。学習したジョブの中だけにある
  data/model_live/<sha12>/       Release から取得した版(gitignore)。CURRENT が現行の版を指す
  data/model/live_pointer.json   参照先(git)。現行の資産名・sha256・学習時刻
  data/model/                    凍結した予備(git。30日ごとに更新)。取得できない時はこれで動く

モデルの選び方(candidates): build / live / 予備 のうち検査を通るものを、学習時刻の新しい順
(同時刻なら build > live > 予備)に並べる。predict_today.load_models が先頭から使う。
参照先が無い間(導入前)は予備だけが候補になり、従来と同じ動きになる。

資産は上書きしない。毎回別名(model-YYYYMMDD-HHMM-<sha256先頭12桁>.tar.gz)で上げ、公開URLから
読み戻して sha256 が合うことを確かめてから参照先を書き換える(gh release upload --clobber は
既存の資産を先に削除するため、上書き方式だと資産が無い時間ができる)。

壊れたモデルファイルを LightGBM に渡すと、Python の例外にならずプロセスごと落ちることがある。
そのため取得時の sha256 照合と valid_dir の検査を必ず先に通す。

使い方:
  python scripts/model_store.py fetch [--force]   参照先の資産を取得(済みなら何もしない)。終了コード 0=成功 / 1=失敗
  python scripts/model_store.py publish           data/model_build を Release へ上げ、参照先を書く(gh と GH_TOKEN が必要)。0 / 1
  python scripts/model_store.py freeze [--force]  予備(data/model)が30日以上前の学習なら更新する
  python scripts/model_store.py verify [--json]   配布の健全性を判定する(watchdog 用)。0=正常 / 1=警告 / 2=異常
  python scripts/model_store.py status            現在の状態を表示
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JST = timezone(timedelta(hours=9))
TAG = "model-live"
DEFAULT_REPO = "t-fuji777/kyotei-ai"
MODEL_FILES = ("model_win.txt", "model_top2.txt", "model_top3.txt")
FILES = ("meta.json", "model_top2.txt", "model_top3.txt", "model_win.txt")   # tar に入れる順(固定)
ASSET_RE = re.compile(r"model-\d{8}-\d{4}-([0-9a-f]{12})\.tar\.gz")           # fullmatch で使う
SHA_RE = re.compile(r"[0-9a-f]{64}")
VID_RE = re.compile(r"[0-9a-f]{12}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
KEEP_ASSETS = 7            # Release に残す世代数
FREEZE_DAYS = 30           # 予備を更新する間隔(日)
RETRY_SEC = 600            # 取得に失敗した資産を再試行するまでの間隔(秒)
MIN_MODEL_BYTES = 100_000  # これより小さいモデルファイルは壊れているとみなす
MAX_BYTES = 64 * 1024 * 1024
HTTP_TIMEOUT = 10          # 1回の通信待ち(秒)
DL_DEADLINE = 30           # ダウンロード全体の上限(秒)。開催中ループを止める時間の上限になる
GH_TIMEOUT = 60
GH_UPLOAD_TIMEOUT = 180
READBACK_TRIES = 6         # publish: 公開URLからの読み戻し回数
READBACK_WAIT = 5
VERIFY_TRIES = 3           # verify: 資産の取得を試す回数
VERIFY_WAIT = 10
STALE_TMP_SEC = 3600       # data/model_live に残った一時ディレクトリを消すまでの時間

_sleep = time.sleep        # テストで差し替える


# ---------------------------------------------------------------- 小道具

def _p(name):
    return {"build": ROOT / "data" / "model_build",
            "live": ROOT / "data" / "model_live",
            "frozen": ROOT / "data" / "model",
            "pointer": ROOT / "data" / "model" / "live_pointer.json"}[name]


def _repo():
    return os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO


def _out(line):
    """表示の失敗(文字コード・閉じた出力先)で処理を止めない。"""
    line = str(line)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(line.encode(enc, "backslashreplace").decode(enc, "replace"), flush=True)
        except Exception:
            pass
    except Exception:
        pass


def _one_line(msg):
    return " ".join(str(msg).split())


def _say(msg):
    _out("model_store: %s" % _one_line(msg))


def _warn(msg):
    """Actions 上では実行結果の画面に警告として残す。"""
    if os.environ.get("GITHUB_ACTIONS"):
        _out("::warning title=model_store::%s" % _one_line(msg))
    else:
        _out("model_store: 警告: %s" % _one_line(msg))


def _err(e):
    return "%s: %s" % (type(e).__name__, e)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _atomic_write(path, text):
    """同じディレクトリの一時ファイルへ書いてから置き換える(途中の状態を見せない)。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
    try:
        tmp.write_bytes(text.encode("utf-8"))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _age_days(trained_at, now=None):
    """trained_at(先頭が YYYY-MM-DD)から now(JST)までの日数。読めなければ None。"""
    now = now or datetime.now(JST)
    try:
        d = datetime.strptime(str(trained_at)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    return (now.date() - d).days


# ---------------------------------------------------------------- 検査と候補

def valid_dir(d):
    """モデル一式が揃っていて壊れていなければ meta(dict) を返す。駄目なら None。例外は出さない。

    meta.json に trained_at(YYYY-MM-DD で始まる文字列)があり、モデル3本がそれぞれ100KB以上、
    先頭が 'tree'、末尾4KBに 'end of parameters' があること。Windows の作業ツリーでは改行が
    CRLF になるので 'tree' + 改行 では判定しない。末尾の検査は書きかけ(途中で切れたファイル)を弾く。
    """
    try:
        d = Path(d)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return None
        ta = meta.get("trained_at")
        if not isinstance(ta, str) or not DATE_RE.match(ta):
            return None
        for n in MODEL_FILES:
            p = d / n
            size = p.stat().st_size
            if size < MIN_MODEL_BYTES:
                return None
            with open(p, "rb") as f:
                if f.read(4) != b"tree":
                    return None
                f.seek(max(0, size - 4096))
                if b"end of parameters" not in f.read():
                    return None
        return meta
    except Exception:
        return None


def _pointer_problem(ptr):
    """参照先の形式を検証する。asset 名はパスとURLに使うので、決めた形以外は受け付けない。"""
    if not isinstance(ptr, dict):
        return "形式が違う"
    asset, sha, size, ta, files = (ptr.get(k) for k in ("asset", "sha256", "size", "trained_at", "files"))
    m = ASSET_RE.fullmatch(asset) if isinstance(asset, str) else None
    if not m:
        return "asset 名が不正"
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        return "sha256 が不正"
    if m.group(1) != sha[:12]:
        return "asset 名と sha256 が食い違う"
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_BYTES:
        return "size が不正"
    if not isinstance(ta, str) or not DATE_RE.match(ta):
        return "trained_at が不正"
    if (not isinstance(files, dict) or set(files) != set(FILES)
            or any(not isinstance(h, str) or not SHA_RE.fullmatch(h) for h in files.values())):
        return "files が不正"
    return None


def read_pointer(path=None):
    """(参照先, 問題) を返す。参照先が無ければ (None, None)、読めない・不正なら (None, 理由)。"""
    p = Path(path) if path else _p("pointer")
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except Exception as e:
        return None, "読めない(%s)" % type(e).__name__
    try:
        ptr = json.loads(raw)
    except Exception:
        return None, "JSON として読めない"
    problem = _pointer_problem(ptr)
    if problem:
        return None, problem
    return ptr, None


def load_pointer(path=None):
    """検証を通った参照先。無い・不正なら None。"""
    return read_pointer(path)[0]


def live_current():
    """data/model_live の現行版のディレクトリ。無い・壊れているなら None。"""
    live = _p("live")
    try:
        name = (live / "CURRENT").read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not VID_RE.fullmatch(name):
        return None
    d = live / name
    return d if valid_dir(d) else None


_KIND_ORDER = {"build": 0, "live": 1, "frozen": 2}


def candidates():
    """[(dir, meta, kind)]。検査を通るものだけを、学習時刻の新しい順(同時刻なら build > live > frozen)に。"""
    out = []
    for kind, d in (("build", _p("build")), ("live", live_current()), ("frozen", _p("frozen"))):
        meta = valid_dir(d) if d is not None else None
        if meta:
            out.append((d, meta, kind))
    out.sort(key=lambda x: _KIND_ORDER[x[2]])
    out.sort(key=lambda x: x[1]["trained_at"], reverse=True)     # 安定ソート(同時刻は上の順のまま)
    return out


# ---------------------------------------------------------------- 資産(tar.gz)の作成と展開

def build_tarball(src, out_dir):
    """4ファイルの決定的な tar.gz を作る(同じ入力なら同じバイト列)。(path, sha256, size) を返す。"""
    src, out_dir = Path(src), Path(out_dir)
    meta = valid_dir(src)
    if not meta:
        raise RuntimeError("モデルが揃っていないか壊れている: %s" % src)
    stamp = "".join(c for c in meta["trained_at"][:16] if c.isdigit())       # YYYYMMDDHHMM
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / ("build.tmp%d.tar.gz" % os.getpid())
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tf:
                for n in FILES:
                    ti = tarfile.TarInfo(n)
                    ti.size = (src / n).stat().st_size
                    ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0o644, 0, 0, "", ""
                    with open(src / n, "rb") as f:
                        tf.addfile(ti, f)
    sha = _sha256(tmp)
    name = "model-%s-%s-%s.tar.gz" % (stamp[:8], stamp[8:12], sha[:12])
    if not ASSET_RE.fullmatch(name):
        tmp.unlink()
        raise RuntimeError("trained_at から資産名を作れない: %r" % meta["trained_at"])
    dst = out_dir / name
    os.replace(tmp, dst)
    return dst, sha, dst.stat().st_size


def make_pointer(src, tar_name, sha, size):
    meta = valid_dir(src)
    if not meta:
        raise RuntimeError("モデルが揃っていないか壊れている: %s" % src)
    period = meta.get("period")
    return {"v": 1, "tag": TAG, "asset": tar_name, "sha256": sha, "size": size,
            "trained_at": meta["trained_at"],
            "period_end": period[1] if isinstance(period, list) and len(period) == 2 else None,
            "files": {n: _sha256(Path(src) / n) for n in FILES}}


def _extract_verified(tar_path, ptr, out_dir):
    """size と sha256 を照合してから、決めた4ファイルだけを out_dir へ取り出し、中身も照合する。
    どこかが合わなければ例外。extractall は使わない(想定外のパスへ書かせない)。"""
    tar_path, out_dir = Path(tar_path), Path(out_dir)
    size = tar_path.stat().st_size
    if size != ptr["size"]:
        raise RuntimeError("size 不一致(実際 %d / 参照先 %d)" % (size, ptr["size"]))
    sha = _sha256(tar_path)
    if sha != ptr["sha256"]:
        raise RuntimeError("sha256 不一致(実際 %s.. / 参照先 %s..)" % (sha[:12], ptr["sha256"][:12]))
    out_dir.mkdir(parents=True)
    seen = set()
    with tarfile.open(tar_path, "r:gz") as tf:
        for m in tf:
            if not m.isreg() or m.name not in FILES or m.name in seen or m.size > MAX_BYTES:
                raise RuntimeError("想定外の中身が入っている: %r" % m.name)
            seen.add(m.name)
            src = tf.extractfile(m)
            with open(out_dir / m.name, "wb") as dst:
                shutil.copyfileobj(src, dst)
    if seen != set(FILES):
        raise RuntimeError("ファイルが足りない: %s" % sorted(set(FILES) - seen))
    meta = valid_dir(out_dir)
    if not meta:
        raise RuntimeError("取り出したモデルが検査を通らない")
    if meta["trained_at"] != ptr["trained_at"]:
        raise RuntimeError("trained_at が参照先と違う(%s / %s)" % (meta["trained_at"], ptr["trained_at"]))
    for n, h in ptr["files"].items():
        if _sha256(out_dir / n) != h:
            raise RuntimeError("中身の sha256 不一致: %s" % n)
    return meta


def _cleanup_live(keep):
    """現行と1つ前以外の版と、古い一時ディレクトリを消す。失敗しても構わない。"""
    try:
        keep = {Path(k).name for k in keep if k is not None}
        for d in _p("live").iterdir():
            if not d.is_dir() or d.name in keep:
                continue
            if d.name.startswith(".") and time.time() - d.stat().st_mtime < STALE_TMP_SEC:
                continue                                 # 他のプロセスが使っているかもしれない
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def install(tar_path, ptr):
    """検証 → 展開 → CURRENT の切替。問題があれば例外を出し、CURRENT には触れない。"""
    live = _p("live")
    live.mkdir(parents=True, exist_ok=True)
    vid = ptr["sha256"][:12]
    tmpd = live / (".new-%s-%d" % (vid, os.getpid()))
    shutil.rmtree(tmpd, ignore_errors=True)
    try:
        _extract_verified(tar_path, ptr, tmpd)
        final = live / vid
        prev = live_current()
        if final.exists():
            shutil.rmtree(final)
        os.replace(tmpd, final)
        _atomic_write(live / "CURRENT", vid + "\n")              # ここが切替(置き換え1回)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    _cleanup_live({final, prev})
    try:
        (live / ".fetch_fail.json").unlink()
    except OSError:
        pass
    return final


# ---------------------------------------------------------------- 取得

def asset_url(asset):
    base = os.environ.get("MODEL_BASE_URL") or (
        "https://github.com/%s/releases/download/%s" % (_repo(), TAG))
    return base.rstrip("/") + "/" + asset


def _http_get(url, dest):
    """url を dest へ保存する。1回の通信待ち HTTP_TIMEOUT 秒、全体 DL_DEADLINE 秒、MAX_BYTES で打ち切る。"""
    req = urllib.request.Request(url, headers={"User-Agent": "aritei-model-store"})
    t0 = time.monotonic()
    n = 0
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r, open(dest, "wb") as f:
        read = getattr(r, "read1", None) or r.read      # read1 は届いた分だけ返す(全体の打ち切りを効かせる)
        while True:
            b = read(1 << 16)
            if not b:
                break
            n += len(b)
            if n > MAX_BYTES:
                raise RuntimeError("資産が大きすぎる(%d バイト超)" % MAX_BYTES)
            f.write(b)
            if time.monotonic() - t0 > DL_DEADLINE:
                raise TimeoutError("ダウンロードが %d 秒を超えた" % DL_DEADLINE)


def download(asset, dest_dir):
    """公開URLから資産を取得する(トークン不要)。gh は使わない。"""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / asset
    _http_get(asset_url(asset), dest)
    return dest


def _fetch(force):
    ptr, problem = read_pointer()
    if ptr is None:
        if problem:
            msg = "参照先(live_pointer.json)が不正(%s)。手元にあるモデル(前回の取得分か予備)で続行する" % problem
            _say(msg)
            return False, msg
        return True, "参照先が無い(導入前)。git の data/model を使う"
    bm = valid_dir(_p("build"))
    if bm and bm["trained_at"] >= ptr["trained_at"]:
        return True, "この場で学習したモデル(trained_at=%s)を使う" % bm["trained_at"]
    live = _p("live")
    cur = live_current()
    if cur is not None and cur.name == ptr["sha256"][:12]:
        return True, ""                                  # 同期済み(毎周回ここで終わる。通信しない)
    fail = _load_json(live / ".fetch_fail.json")
    fail = fail if isinstance(fail, dict) else {}
    same = fail.get("sha256") == ptr["sha256"]
    try:
        waited = time.time() - float(fail.get("at") or 0)
    except Exception:
        waited = RETRY_SEC
    if not force and same and 0 <= waited < RETRY_SEC:
        msg = "モデル %s は直前に取得できなかった。%d秒おきに再試行する(%s)" % (
            ptr["asset"], RETRY_SEC, fail.get("error"))
        _say(msg)
        return False, msg
    tmp = live / (".dl-%d" % os.getpid())
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        tar = download(ptr["asset"], tmp)
        install(tar, ptr)
        msg = "取得して切り替えた: %s (trained_at=%s)" % (ptr["asset"], ptr["trained_at"])
        _say(msg)
        return True, msg
    except Exception as e:
        err = _err(e)
        count = 1
        try:
            count = int(fail.get("count") or 0) + 1 if same else 1
            _atomic_write(live / ".fetch_fail.json", json.dumps(
                {"sha256": ptr["sha256"], "asset": ptr["asset"], "at": time.time(),
                 "error": err, "count": count}, ensure_ascii=False))
        except Exception:
            pass
        msg = "モデル %s を取得できない(%d回目): %s。手元にあるモデル(前回の取得分か予備)で続行する" % (
            ptr["asset"], count, err)
        _warn(msg)
        return False, msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def fetch(force=False):
    """data/model_live を参照先に合わせる。(成功したか, 説明) を返す。例外は外へ出さない。

    順に: 参照先なし→成功(何もしない) / この場で学習したモデルが参照先以上→成功(通信しない) /
    取得済み→成功(無言) / 同じ資産で直前に失敗→失敗(通信しない) / 公開URLから取得して検証・切替。
    失敗しても CURRENT には触れないので、前回の取得分か予備で動き続ける。
    force は「直前に失敗した資産の再試行待ち」だけを飛ばす。
    """
    try:
        return _fetch(force)
    except Exception as e:
        msg = "取得処理が想定外の例外で中断: %s。手元にあるモデルで続行する" % _err(e)
        _warn(msg)
        return False, msg


# ---------------------------------------------------------------- 配布(gh が必要)

def _gh_ready():
    return bool(shutil.which("gh")) and bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))


def _gh(*args, timeout=GH_TIMEOUT, check=True):
    """gh を呼ぶ。必ず時間切れ付き(既定60秒)。時間切れは例外(TimeoutExpired)になる。"""
    r = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError("gh %s が失敗(exit %d): %s" % (
            " ".join(args[:2]), r.returncode, (r.stderr or r.stdout or "").strip()[:300]))
    return r


def _release_assets():
    """Release(TAG)の資産の一覧。Release を参照できなければ None。"""
    r = _gh("release", "view", TAG, "--repo", _repo(), "--json", "assets", check=False)
    if r.returncode != 0:
        return None
    try:
        assets = (json.loads(r.stdout) or {}).get("assets") or []
    except Exception:
        return None
    return [a for a in assets if isinstance(a, dict)]


def _ensure_release():
    """Release が無ければ作る(prerelease、latest にしない)。資産の一覧を返す。"""
    assets = _release_assets()
    if assets is not None:
        return assets
    err = None
    try:
        _gh("release", "create", TAG, "--repo", _repo(), "--prerelease", "--latest=false",
            "--title", "学習済みモデル(自動管理)",
            "--notes", "daily が毎朝アップロードする学習済みモデル。現行は data/model/live_pointer.json "
                       "が指す資産。手で編集・削除しないこと(消えても次の daily が作り直す)。")
        _say("Release %s を作成した" % TAG)
    except Exception as e:
        err = e                                           # 作成に失敗しても、直後に参照できれば続行する
    assets = _release_assets()
    if assets is None:
        raise RuntimeError("Release %s を用意できない: %s" % (TAG, err or "作成後も参照できない"))
    return assets


def select_prune(names, keep_names, keep=KEEP_ASSETS):
    """削除する資産名を選ぶ(純粋関数)。決めた形の名前だけを対象に、名前の降順(=学習時刻の新しい順)で
    keep 個を残し、それより古いものを返す。keep_names(今回の資産と旧参照先の資産)は必ず残す。"""
    keep_names = {n for n in (keep_names or ()) if n}
    ours = sorted({n for n in names if isinstance(n, str) and ASSET_RE.fullmatch(n)}, reverse=True)
    return [n for n in ours[keep:] if n not in keep_names]


def publish():
    """data/model_build を Release へ上げ、公開URLから読み戻せた後にだけ参照先を書く。
    失敗したら例外(参照先には触れない。ループは前回の資産で動き続ける)。freeze は呼ばない。"""
    repo = _repo()
    if not _gh_ready():
        raise RuntimeError("配布には gh コマンドと GH_TOKEN が必要")
    build = _p("build")
    if not valid_dir(build):
        raise RuntimeError("data/model_build に完成したモデルが無い(学習が失敗している)")
    old_ptr = load_pointer()                              # 整理で残すため、開始時点の参照先を覚えておく
    stage = _p("live") / (".stage-%d" % os.getpid())
    shutil.rmtree(stage, ignore_errors=True)
    try:
        tar, sha, size = build_tarball(build, stage)
        ptr = make_pointer(build, tar.name, sha, size)
        problem = _pointer_problem(ptr)
        if problem:
            raise RuntimeError("作った参照先が不正: %s" % problem)
        assets = _ensure_release()
        same = [a for a in assets if a.get("name") == tar.name]
        if same and same[0].get("state") == "uploaded" and same[0].get("size") == size:
            _say("同じ資産が既にある。アップロードを省く: %s" % tar.name)
        else:
            # 同名の残骸(途中で切れたアップロード)がある時だけ --clobber で置き換える
            _gh("release", "upload", TAG, str(tar), "--repo", repo, *(["--clobber"] if same else []),
                timeout=GH_UPLOAD_TIMEOUT)
            _say("アップロードした: %s (%d バイト)" % (tar.name, size))
        last = None
        for i in range(READBACK_TRIES):                   # ループと同じ経路(公開URL)で取り直して確かめる
            try:
                got = download(tar.name, stage / "back")
                if got.stat().st_size == size and _sha256(got) == sha:
                    last = None
                    break
                last = "読み戻した内容が違う(size/sha256 不一致)"
            except Exception as e:
                last = _err(e)
            if i < READBACK_TRIES - 1:
                _sleep(READBACK_WAIT)
        if last is not None:
            raise RuntimeError("上げた資産を公開URLから読み戻せない: %s" % last)
        _atomic_write(_p("pointer"), json.dumps(ptr, ensure_ascii=False, indent=1) + "\n")
        _say("配布した: %s sha256=%s.. trained_at=%s" % (tar.name, sha[:12], ptr["trained_at"]))
        try:                                              # 古い資産の整理。失敗は警告のみ
            names = [a.get("name") for a in (_release_assets() or [])]
            for name in select_prune(names, {tar.name, (old_ptr or {}).get("asset")}):
                _gh("release", "delete-asset", TAG, name, "--repo", repo, "--yes")
                _say("古い資産を削除した: %s" % name)
        except Exception as e:
            _warn("古い資産の整理に失敗(配布自体は成功): %s" % _err(e))
        return ptr
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# ---------------------------------------------------------------- 予備の更新

def freeze(force=False, now=None):
    """予備(data/model)を更新する。(更新したか, 説明) を返す。元が無い時は例外。

    元は data/model_build(無ければ data/model_live の現行)。予備が壊れている、予備の学習が
    FREEZE_DAYS 日以上前、または force の時だけ複写する。
    """
    src = _p("build") if valid_dir(_p("build")) else live_current()
    sm = valid_dir(src) if src is not None else None
    if not sm:
        raise RuntimeError("予備の元になるモデルが無い(data/model_build も data/model_live も使えない)")
    frozen = _p("frozen")
    fm = valid_dir(frozen)
    if fm and not force:
        age = _age_days(fm["trained_at"], now)
        if age is not None and age < FREEZE_DAYS:
            return False, "予備は%d日前の学習(%d日未満)。更新しない" % (age, FREEZE_DAYS)
        if sm["trained_at"] <= fm["trained_at"]:
            return False, "元のモデルが予備より新しくない。更新しない"
    frozen.mkdir(parents=True, exist_ok=True)
    order = MODEL_FILES + ("meta.json",)                  # meta.json を最後に置き換える
    tmps = [frozen / (n + ".tmp%d" % os.getpid()) for n in order]
    try:
        for n, tmp in zip(order, tmps):
            shutil.copyfile(Path(src) / n, tmp)
        for n, tmp in zip(order, tmps):
            os.replace(tmp, frozen / n)
    finally:
        for tmp in tmps:
            try:
                tmp.unlink()
            except OSError:
                pass
    return True, "予備を更新した(trained_at=%s)" % sm["trained_at"]


# ---------------------------------------------------------------- 状態と健全性

def status():
    ptr, problem = read_pointer()
    c = candidates()
    act = c[0] if c else None
    return {"active": act[2] if act else None,
            "active_trained_at": act[1]["trained_at"] if act else None,
            "candidates": [{"kind": k, "trained_at": m["trained_at"]} for _d, m, k in c],
            "pointer_asset": (ptr or {}).get("asset"),
            "pointer_trained_at": (ptr or {}).get("trained_at"),
            "pointer_problem": problem,
            "in_sync": bool(act) and problem is None and (
                ptr is None or act[1]["trained_at"] >= ptr["trained_at"]),
            "fetch_fail": _load_json(_p("live") / ".fetch_fail.json")}


def _check_asset(ptr):
    """参照先の資産を取得・照合・展開・検査する。問題が無ければ None、あれば最後の理由。"""
    last = None
    for i in range(VERIFY_TRIES):
        tmp = _p("live") / (".verify-%d" % os.getpid())
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            tar = download(ptr["asset"], tmp)
            _extract_verified(tar, ptr, tmp / "x")
            return None
        except Exception as e:
            last = _err(e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if i < VERIFY_TRIES - 1:
            _sleep(VERIFY_WAIT)
    return last or "不明な理由"


def _verify(now, thresholds):
    now = now or datetime.now(JST)
    th = thresholds if thresholds is not None else (
        _load_json(ROOT / "scripts" / "health_thresholds.json") or {})
    crit_days = int(th.get("model_stale_crit_days", 2))
    frozen_warn = int(th.get("model_frozen_warn_days", 45))
    crit, warn = [], []
    fm = valid_dir(_p("frozen"))
    if fm is None:
        crit.append("M3 予備モデル(data/model)が無いか壊れている")
    effective = fm["trained_at"] if fm else None
    ptr, problem = read_pointer()
    asset_ok = None
    if ptr is None and problem is None:
        warn.append("M0 参照先(live_pointer.json)が無い。git の予備モデルだけで動いている")
    elif ptr is None:
        crit.append("M1 参照先(live_pointer.json)が不正: %s" % problem)
    else:
        bad = _check_asset(ptr)
        asset_ok = bad is None
        if asset_ok:
            effective = ptr["trained_at"]
        else:
            crit.append("M1 配布物 %s を取得・検証できない: %s" % (ptr["asset"], bad))
    age = _age_days(effective, now) if effective else None
    if age is not None and age >= crit_days:
        crit.append("M2 使えるモデルが%d日前の学習のまま(trained_at=%s)" % (age, effective))
    elif age is not None and age >= 1 and now.hour >= 12:
        warn.append("M4 本日学習のモデルが未配布(trained_at=%s)" % effective)
    fage = _age_days(fm["trained_at"], now) if fm else None
    if fage is not None and fage >= frozen_warn:
        warn.append("M5 予備モデルが%d日前の学習のまま(定期更新が止まっている)" % fage)
    return {"level": "critical" if crit else ("warning" if warn else "ok"),
            "criticals": crit, "warnings": warn,
            "effective_trained_at": effective,
            "pointer_asset": (ptr or {}).get("asset"),
            "pointer_trained_at": (ptr or {}).get("trained_at"),
            "asset_ok": asset_ok,
            "frozen_trained_at": fm["trained_at"] if fm else None,
            "checked_at": now.strftime("%Y-%m-%d %H:%M JST")}


def verify(now=None, thresholds=None):
    """配布の健全性を dict で返す。verify 自体が例外で止まっても critical として必ず返す
    (監視が無音にならないように)。"""
    try:
        return _verify(now, thresholds)
    except Exception as e:
        return {"level": "critical",
                "criticals": ["M9 モデル配布の検査が例外で中断: %s" % _err(e)],
                "warnings": [], "effective_trained_at": None}


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="学習済みモデルの配布と取得")
    ap.add_argument("cmd", choices=["fetch", "publish", "freeze", "verify", "status"])
    ap.add_argument("--force", action="store_true",
                    help="fetch: 直前の失敗による再試行待ちを飛ばす / freeze: 日数に関わらず更新する")
    ap.add_argument("--json", action="store_true", help="verify: JSON で出力する")
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        ok, _msg = fetch(force=a.force)                   # 表示は fetch の中で行う
        return 0 if ok else 1
    if a.cmd == "publish":
        try:
            publish()
            return 0
        except Exception as e:
            msg = "モデルの配布に失敗: %s。参照先は書き換えていない(ループは前回の資産で続行)" % _err(e)
            if os.environ.get("GITHUB_ACTIONS"):
                _out("::error title=モデルの配布に失敗::%s" % _one_line(msg))
            else:
                _out("model_store: 失敗: %s" % _one_line(msg))
            return 1
    if a.cmd == "freeze":
        try:
            _changed, msg = freeze(force=a.force)
            _say("freeze: %s" % msg)
            return 0
        except Exception as e:
            _warn("freeze に失敗: %s" % _err(e))
            return 1
    if a.cmd == "verify":
        res = verify()
        if a.json:
            try:
                text = json.dumps(res, ensure_ascii=False, indent=2)
            except Exception as e:
                text = json.dumps({"level": "critical", "criticals": ["M9 結果を JSON にできない: %s" % _err(e)],
                                   "warnings": [], "effective_trained_at": None}, ensure_ascii=False)
            try:
                sys.stdout.buffer.write(text.encode("utf-8") + b"\n")     # 端末の文字コードに左右されない
                sys.stdout.flush()
            except Exception:
                print(json.dumps(json.loads(text)), flush=True)           # ASCII のみ
        else:
            _out("[%s] effective_trained_at=%s" % (res["level"], res.get("effective_trained_at")))
            for c in res["criticals"]:
                _out("  critical: " + c)
            for w in res["warnings"]:
                _out("  warning : " + w)
        return {"ok": 0, "warning": 1}.get(res["level"], 2)
    _out(json.dumps(status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

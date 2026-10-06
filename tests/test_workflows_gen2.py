# -*- coding: utf-8 -*-
"""ワークフロー(.github/workflows)の世代2向けの変更が、設計書 4.8 / 4.9 どおりの形になっているかを
YAML を読んで確かめる(yml は手元で実行できないので、構造と順番と if 条件を機械的に見る)。

確かめること:
  daily.yml    ジョブ上限 90分 / 学習の手順に timeout-minutes 45 と continue-on-error /
               手順の順番(gate → ループ起動 → pip → … → 学習 → 配布 → 較正表 → commit → 予測 → ループ起動)/
               較正表の手順が学習・配布の後、commit の前にある / commit が較正の種を add する /
               lightgbm の版の固定 / 末尾のループ起動は always() のまま
  watchdog.yml ジョブ上限 75分 / 復旧待ち 45分(seq 1 45)と文言
  train.yml    入力 dry_run・model_gen・allow_format_change / dry_run の時に配布と commit を省く if /
               freeze --force を残し --allow-format-change は入力で付ける / lightgbm の版の固定
  auto-update.yml lightgbm の版の固定だけ(他の手順は変えていない)
  .gitignore   data/feat_cache/ と .tmp_*/(テストの一時フォルダ)

使い方: リポジトリの根で  PYTHONUTF8=1 python -X utf8 tests/test_workflows_gen2.py
PyYAML が無い環境では、YAML の読み込みを省いて文字列の確認だけを行う。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

LGB_PIN = '"lightgbm>=4.6,<5"'


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load(path):
    """YAML として読む。`on:` は PyYAML では True キーになるので文字列に直す。"""
    doc = yaml.safe_load(read(path))
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def steps_of(doc, job):
    return doc["jobs"][job]["steps"]


def step_index(steps, pred, what):
    hits = [i for i, s in enumerate(steps) if pred(s)]
    assert len(hits) == 1, f"{what}: 該当する手順が {len(hits)} 個(1個のはず)"
    return hits[0]


def name_is(name):
    return lambda s: s.get("name") == name


def run_has(text):
    return lambda s: text in str(s.get("run", ""))


# ---------------------------------------------------------------- daily.yml
def test_daily():
    path = os.path.join(WF, "daily.yml")
    text = read(path)
    assert LGB_PIN in text, "daily.yml: lightgbm の版が固定されていない"
    assert "pip install lhafile pandas numpy lightgbm pillow" not in text, "daily.yml: 固定前の pip install が残っている"
    assert "data/calib_seed_gen2.json" in text, "daily.yml: 較正の種を add していない"
    if yaml is None:
        print("  (PyYAML 無し: daily.yml は文字列の確認だけ)")
        return
    doc = load(path)
    job = doc["jobs"]["daily"]
    assert job["timeout-minutes"] == 90, f"daily.yml: ジョブ上限が {job['timeout-minutes']}(90 のはず)"
    steps = steps_of(doc, "daily")

    i_gate = step_index(steps, lambda s: s.get("id") == "gate", "gate")
    i_loop1 = step_index(steps, name_is("開催中ループ(auto-update)を学習の前に起動しておく"), "学習前のループ起動")
    i_pip = step_index(steps, run_has("pip install"), "pip install")
    i_results = step_index(steps, name_is("update yesterday results"), "前日結果")
    i_train = step_index(steps, lambda s: s.get("id") == "train", "学習")
    i_pub = step_index(steps, lambda s: s.get("id") == "publish", "配布")
    i_calib = step_index(steps, name_is("build calibration"), "較正表")
    i_commit = step_index(steps, lambda s: str(s.get("name", "")).startswith("commit"), "commit")
    i_pred = step_index(steps, name_is("predict today"), "予測")
    i_loop2 = step_index(steps, name_is("開催中ループ(auto-update)が動いていなければ起動"), "末尾のループ起動")
    i_fail = step_index(steps, name_is("学習または配布の失敗を知らせる"), "失敗の通知")

    order = [i_gate, i_loop1, i_pip, i_results, i_train, i_pub, i_calib, i_commit, i_pred, i_loop2, i_fail]
    assert order == sorted(order), f"daily.yml: 手順の順番が設計と違う {order}"
    assert i_fail == len(steps) - 1, "daily.yml: 失敗の通知が最後の手順でない"

    # 学習前のループ起動: 省いた回では走らせない(末尾の always() が担う)・失敗しても続行
    s = steps[i_loop1]
    assert s.get("if") == "steps.gate.outputs.skip != 'true'", "学習前のループ起動: if 条件"
    assert s.get("continue-on-error") is True, "学習前のループ起動: continue-on-error が無い"
    assert s["run"].strip() == "bash scripts/ensure_loop.sh", "学習前のループ起動: run"

    # 学習: 時間切れ 45 分、continue-on-error は維持
    s = steps[i_train]
    assert s.get("timeout-minutes") == 45, f"学習: timeout-minutes が {s.get('timeout-minutes')}(45 のはず)"
    assert s.get("continue-on-error") is True, "学習: continue-on-error が外れている"
    assert s.get("if") == "steps.gate.outputs.skip != 'true'", "学習: if 条件"
    assert s["run"].strip() == "python scripts/train.py", "学習: run が変わっている"

    # 配布: 学習が成功した時だけ(変えていない)
    s = steps[i_pub]
    assert "steps.train.outcome == 'success'" in s.get("if", ""), "配布: 学習成功の条件が外れている"
    assert s.get("timeout-minutes") == 8 and s.get("continue-on-error") is True, "配布: 既存の設定が変わっている"

    # 較正表: 学習・配布の後、commit の前。学習の成否に関わらず走る(if は gate だけ)
    s = steps[i_calib]
    assert i_pub < i_calib < i_commit, "較正表: 位置が 配布の後・commit の前 でない"
    assert s.get("if") == "steps.gate.outputs.skip != 'true'", "較正表: if 条件(学習の成否に依らないはず)"
    assert s.get("continue-on-error") is True, "較正表: continue-on-error が外れている"
    assert s["run"].strip() == "python scripts/build_calib.py", "較正表: run"
    # 学習より前に較正表の手順が残っていないこと
    assert not any("build_calib" in str(x.get("run", "")) for x in steps[:i_train]), "較正表: 学習より前にも残っている"

    # commit: docs(calib.json を含む)と、有る時だけ較正の種を add する
    run = steps[i_commit]["run"]
    assert "git add data/races data/fan data/model docs" in run, "commit: git add の対象が変わっている"
    assert 'if [ -f data/calib_seed_gen2.json ]; then git add data/calib_seed_gen2.json; fi' in run, "commit: 較正の種の add(有る時だけ)が無い"
    assert run.index("git add data/races") < run.index("calib_seed_gen2") < run.index("git commit"), "commit: add と commit の順番"

    # 末尾のループ起動は always() のまま、失敗の通知の条件も変えていない
    assert steps[i_loop2].get("if") == "always()", "末尾のループ起動: always() でない"
    assert steps[i_loop2]["run"].strip() == "bash scripts/ensure_loop.sh"
    cond = re.sub(r"\s+", " ", steps[i_fail].get("if", ""))
    assert "steps.train.outcome == 'failure'" in cond and "steps.publish.outcome == 'failure'" in cond, "失敗の通知: 条件が変わっている"

    # 予測の手順は触っていない(docs/predictions だけ add する作りのまま)
    run = steps[i_pred]["run"]
    assert "git add docs/predictions" in run and "git reset --hard origin/main" in run, "予測: 既存の作りが変わっている"
    assert "build_calib" not in run, "予測: 較正表をここで作っても main に入らない(commit の前に作る)"
    print("  daily.yml OK")


# ------------------------------------------------------------- watchdog.yml
def test_watchdog():
    path = os.path.join(WF, "watchdog.yml")
    text = read(path)
    assert "seq 1 45" in text and "seq 1 20" not in text, "watchdog.yml: 復旧待ちが 45 回(分)になっていない"
    assert "45分待っても復旧せず" in text and "20分待っても復旧せず" not in text, "watchdog.yml: 復旧せずの文言"
    assert "45分以内の復旧:" in text and "20分以内の復旧:" not in text, "watchdog.yml: Issue の文言"
    if yaml is None:
        print("  (PyYAML 無し: watchdog.yml は文字列の確認だけ)")
        return
    doc = load(path)
    job = doc["jobs"]["watch"]
    assert job["timeout-minutes"] == 75, f"watchdog.yml: ジョブ上限が {job['timeout-minutes']}(75 のはず)"
    steps = steps_of(doc, "watch")
    i_re = step_index(steps, lambda s: s.get("id") == "recheck", "復旧を確認")
    s = steps[i_re]
    assert s.get("if") == "steps.check.outputs.level == 'critical'", "復旧を確認: if 条件が変わっている"
    assert "for i in $(seq 1 45); do" in s["run"] and "sleep 60" in s["run"], "復旧を確認: 60秒 × 45回でない"
    # 復旧待ち(45) + verify の最大(300×2 + 120 秒 = 12分) + 余裕 が上限に収まる
    assert 45 + 12 + 3 <= job["timeout-minutes"], "watchdog.yml: 上限が内訳の合計より短い"
    # ほかの手順の if 条件は変えていない
    i_heal = step_index(steps, lambda s: s.get("id") == "heal", "heal")
    assert "steps.check.outputs.level == 'critical'" in steps[i_heal]["if"], "heal: if 条件"
    assert "contents: read" in text, "watchdog.yml: 書き込み権限が増えている"
    print("  watchdog.yml OK")


# ---------------------------------------------------------------- train.yml
def test_train():
    path = os.path.join(WF, "train.yml")
    text = read(path)
    assert LGB_PIN in text, "train.yml: lightgbm の版が固定されていない"
    assert "freeze --force" in text, "train.yml: freeze --force が消えている(--force は『30日を無視』の意味で残す)"
    assert "--allow-format-change" in text, "train.yml: --allow-format-change を付ける経路が無い"
    assert "/usr/bin/time -v" in text, "train.yml: /usr/bin/time -v で測っていない"
    if yaml is None:
        print("  (PyYAML 無し: train.yml は文字列の確認だけ)")
        return
    doc = load(path)
    inputs = doc["on"]["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["type"] == "boolean" and inputs["dry_run"]["default"] is False, "train.yml: dry_run の型と既定値"
    assert inputs["allow_format_change"]["type"] == "boolean" and inputs["allow_format_change"]["default"] is False
    assert inputs["model_gen"]["default"] == "", "train.yml: model_gen の既定は空(定数のまま)"
    job = doc["jobs"]["train"]
    assert job["env"]["DRY_RUN"] == "${{ github.event.inputs.dry_run }}"
    assert job["env"]["ALLOW_FORMAT_CHANGE"] == "${{ github.event.inputs.allow_format_change }}"
    assert job["env"]["IN_MODEL_GEN"] == "${{ github.event.inputs.model_gen }}"
    steps = steps_of(doc, "train")
    i_pip = step_index(steps, run_has("pip install"), "pip install")
    i_train = step_index(steps, lambda s: s.get("id") == "train", "学習")
    i_pub = step_index(steps, name_is("モデルを配布 (Release資産) と予備の更新"), "配布")
    i_commit = step_index(steps, name_is("commit"), "commit")
    assert [i_pip, i_train, i_pub, i_commit] == sorted([i_pip, i_train, i_pub, i_commit])
    assert i_commit == len(steps) - 1
    # 学習の手順: dry_run でも通常でも train.py を呼ぶ。時間切れは無い(train.yml の上限 300 分のまま)
    run = steps[i_train]["run"]
    assert "timeout-minutes" not in steps[i_train]
    assert run.count("python scripts/train.py") == 3, "学習: 通常 / time あり / time なし の3経路で train.py を呼ぶ"
    assert 'if [ "$DRY_RUN" != "true" ]; then' in run, "学習: dry_run の分岐"
    assert "/usr/bin/time -v -o time.txt python scripts/train.py" in run
    assert "set +e" in run and "set -e" in run and "exit $RC" in run, "学習: 失敗の終了コードを計測の後に返す"
    assert 'export MODEL_GEN="$IN_MODEL_GEN"' in run, "学習: model_gen を環境変数 MODEL_GEN で渡す"
    assert "GITHUB_STEP_SUMMARY" in run, "学習: 要約に出していない"
    assert "Maximum resident set size" in run, "学習: メモリの最大を読んでいない"
    # 配布と commit は dry_run の時に省く
    for i in (i_pub, i_commit):
        assert steps[i].get("if") == "github.event.inputs.dry_run != 'true'", f"{steps[i]['name']}: dry_run で省く if が無い"
    run = steps[i_pub]["run"]
    assert "python scripts/model_store.py publish" in run
    assert "python scripts/model_store.py freeze --force $FREEZE_ARGS" in run, "配布: freeze の呼び方"
    assert 'if [ "$ALLOW_FORMAT_CHANGE" = "true" ]; then' in run and 'FREEZE_ARGS="--allow-format-change"' in run
    assert steps[i_pub].get("timeout-minutes") == 10
    run = steps[i_commit]["run"]
    assert "git add data/model docs/model_report.json" in run, "commit: 既存の add が変わっている"
    assert 'if [ -f data/calib_seed_gen2.json ]; then git add data/calib_seed_gen2.json; fi' in run, "commit: 較正の種の add"
    assert job["timeout-minutes"] == 300 and doc["concurrency"]["group"] == "data"
    print("  train.yml OK")


# ---------------------------------------------------------- auto-update.yml
def test_auto_update():
    path = os.path.join(WF, "auto-update.yml")
    text = read(path)
    assert LGB_PIN in text, "auto-update.yml: lightgbm の版が固定されていない"
    assert "pip install pandas numpy lightgbm lhafile" not in text, "auto-update.yml: 固定前の pip install が残っている"
    if yaml is None:
        print("  (PyYAML 無し: auto-update.yml は文字列の確認だけ)")
        return
    doc = load(path)
    steps = steps_of(doc, "loop")
    i_pip = step_index(steps, run_has('pip install pandas numpy "lightgbm>=4.6,<5" lhafile requests'), "pip install")
    assert i_pip == 2, "auto-update.yml: pip install の位置が変わっている(checkout, setup-python の次)"
    # ループ本体は触っていない: 代行経路(self_heal)は build_calib を呼び、docs(calib.json)を add する
    run = steps_of(doc, "loop")[-1]["run"]
    assert "python scripts/build_calib.py || true" in run and "git add data/races data/fan docs" in run, "self_heal: 較正表の作成と add"
    assert "python scripts/model_store.py fetch || true" in run
    assert doc["jobs"]["loop"]["timeout-minutes"] == 320
    print("  auto-update.yml OK")


# ------------------------------------------------------------ .gitignore
def test_gitignore():
    lines = [ln.strip() for ln in read(os.path.join(ROOT, ".gitignore")).splitlines()]
    assert "data/feat_cache/" in lines, ".gitignore: data/feat_cache/ が無い"
    assert ".tmp_*/" in lines, ".gitignore: テストの一時フォルダ .tmp_*/ が無い(git add -A で枝に入ってしまう)"
    for keep in ("data/model_build/", "data/model_live/", "data/model/*.tmp*"):
        assert keep in lines, f".gitignore: 既存の {keep} が消えている"
    print("  .gitignore OK")


# ------------------------------------------------------------ 全 yml が読める
def test_all_yaml_parse():
    if yaml is None:
        print("  (PyYAML 無し: 構文の確認は省いた)")
        return
    for fn in sorted(os.listdir(WF)):
        if fn.endswith(".yml"):
            load(os.path.join(WF, fn))
    print("  全 yml が YAML として読める")


if __name__ == "__main__":
    tests = [test_all_yaml_parse, test_daily, test_watchdog, test_train, test_auto_update, test_gitignore]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"{failed} 件失敗")
        sys.exit(1)
    print("ALL OK")

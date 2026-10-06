# kyotei-ai

競艇(ボートレース)3連単AI予測システム。

## 構成

- データ源: 公式 mbrace 番組表(B)・競走成績(K) LZHファイル 直近5年分 + ファン手帳(期別成績)
- 学習(世代1 = 切り戻し用。`scripts/train.py` の `MODEL_GEN = 1` に戻すと翌朝から世代1): LightGBM 3段二値モデル(1着/2着内/3着内、45特徴量) + 3段Plackett-Luce型で3連単120通りを確率化
- 学習(世代2 = 本番。`MODEL_GEN = 2`): 世代1の45 + 履歴108 = 153特徴量、条件づけ分解ロジットの LightGBM 1本で3連単120通りを直接確率化。詳しくは下の「モデルの世代」
- 厳選モード: 本命の複勝(2着以内)的中率90%超を目標に、閾値と対象場をvalidで自動決定
- 配信: GitHub Actions 自動実行 + GitHub Pages PWA (`docs/`)
- 的中実績: 前日結果と予測を突合して自動記録(全体/厳選)

## ワークフロー

| ファイル | 役割 | 起動 |
|---|---|---|
| sample.yml | 指定日の生B/Kテキストを `sample_raw/` に保存(パーサ検証用) | 手動 |
| backfill.yml | 期間指定でB/K取得・解析 → `data/races/entries_YYYY.csv.gz` | 手動 |
| train.yml | 学習 → Release(`model-live`)へ配布 + `data/model/`(予備)更新 + `docs/model_report.json`。入力 `dry_run` で配布・commit を省いて所要時間とメモリだけ測れる | 手動 |
| daily.yml | 前日結果反映 + 学習・配布 + 較正表 + 当日予測 → `docs/predictions/` `docs/accuracy.json` `docs/calib.json` | 毎日 6:30 / 8:55 JST |

## モデルの世代

| 世代 | 作り | 配布物の形 | 状態 |
|---|---|---|---|
| 1(現行) | 45特徴量。二値モデル3本(1着 / 2着以内 / 3着以内)を3段の式で3連単120通りにする | `model_win.txt` `model_top2.txt` `model_top3.txt` + `meta.json`(約10MB) | 本番。`scripts/train.py` の `MODEL_GEN = 1` |
| 2(本番) | 現行45 + 履歴108(選手の実力値・モーター・節間・展示の癖など。`scripts/features_hist.py`)= 153特徴量。1着→2着→3着の条件づけ分解ロジットを LightGBM 1本で学習し、120通りを直接出す | `model.txt` + `meta.json`(約4.5MB) | `MODEL_GEN = 2` にした翌朝(2026-10-08 予定)から学習・予測が世代2 |

- 予測側は読み込んだモデルの形で経路を選ぶので、どちらの形の資産・予備でも動く(切り替えと巻き戻しのどちらでも、両方の形が読める状態を保つ)。
- 各レースには買い目を作ったモデルの世代 `g` を書き、厳選のしきい値と較正表はレースの世代で引く(`scripts/common.py` の `SENGEN_CFG_BY_GEN`)。同じ日に世代の違う買い目が混ざっても、各レースは自分の世代の数字で判定される。
- 世代2の較正表の種(`data/calib_seed_gen2.json`。学習に使っていない試験期間の予測から作る)は train.py が書き、daily が commit する。当日分の特徴量の保存 `data/feat_cache/` は gitignore。
- 世代2の厳選のしきい値(`top3p_min` 0.45、`cand_top4p_min` 0.43)は、取り直し後のデータで「学習に使っていない検証期間で世代1と同じ本数になる値」として 2026-10-06 に決めた(検証 0.4534・試験 0.4493)。`scripts/health_thresholds.json` は世代2の確率の出方でも誤報が出ないことを確かめた上で据え置き。

## モデルの置き場

学習済みモデル(世代1は約10MB、世代2は約4.5MB)は毎朝ほぼ全体が変わるため、git には積まず GitHub Release に置く(`scripts/model_store.py`)。

- Release `model-live`(prerelease)の資産 `model-YYYYMMDD-HHMM-<sha256先頭12桁>.tar.gz`: 毎日のモデル。daily が配布し、直近7個を残す。中身のファイル名は世代(形)で違う(上の表)
- `data/model/live_pointer.json`: 現行の資産名と sha256(参照先)。開催中ループなどはこれを見て資産を `data/model_live/` へ取得する。項目 `format`(無ければ 1)で形が分かる
- `data/model/`: 凍結した予備(30日ごとに更新)。資産を取得できない時はこれで動く。予備の形は勝手には替えない(`model_store.py freeze` は形が違うモデルでは `--allow-format-change` が無い限り何もしない。train.yml の入力 `allow_format_change`)。古いコードへ巻き戻した時の保険になるので、世代2で運用を続けると決めるまで世代1の形のまま置く
- `data/model_build/` `data/model_live/` `data/feat_cache/`: 学習の出力・取得物・当日分の特徴量の保存(gitignore)

運用上の注意:

- 特徴量(`scripts/features.py` の `FEATURES` / `FEATURES_V2`、`scripts/features_hist.py`)を変えたら train.yml を手動実行する(取得済みの版や予備が新しいコードと合わなくなるため)
- 世代2へ切り替える前に、train.yml を `dry_run: true`(必要なら `model_gen: 2`)で1回動かし、Actions 上の学習時間・メモリ・自己検査を確かめる(要約に出る)
- リポジトリ設定で immutable releases を有効にしない(資産を追加・整理できなくなる)
- 履歴を書き換える時は、先に `model-live` のリリースとタグを消す(タグが古い履歴を保持する。次の daily が作り直す)

## 初期セットアップ手順

1. backfill.yml を実行(例: start=20210601, end=今日)
2. train.yml を実行
3. daily.yml を手動実行して当日予測を確認
4. Settings → Pages → Branch: main / Folder: /docs を有効化

## スクリプト

- `scripts/common.py` 取得・LZH解凍・B/Kパーサ
- `scripts/parse_fan.py` / `scripts/fetch_fan.py` ファン手帳(期別成績)の解析・取得
- `scripts/build_dataset.py` 日次データセット構築
- `scripts/features.py` 特徴量(選手成績365/180/90日窓、会場×枠など45項目。世代2はこれに `features_hist.py` の履歴108項目を足す)
- `scripts/features_hist.py` 世代2の履歴特徴量(実力値・モーター・節間・展示の癖など108項目)
- `scripts/model_v2.py` 世代2のモデル(条件づけ分解ロジット。1レース156行 → 120通り)
- `scripts/train.py` 学習・バックテスト(`MODEL_GEN` で世代1 / 世代2を切り替え)
- `scripts/predict_today.py` 当日予測JSON生成
- `scripts/model_store.py` 学習済みモデルの配布(Release)・取得・検証・予備更新
- `scripts/update_results.py` 結果反映・的中実績更新

## 免責

予測は参考情報です。的中・回収を保証するものではありません。

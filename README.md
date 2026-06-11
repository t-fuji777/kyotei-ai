# kyotei-ai

競艇(ボートレース)3連単AI予測システム。

## 構成

- データ源: 公式 mbrace 番組表(B)・競走成績(K) LZHファイル 直近5年分 + ファン手帳(期別成績)
- 学習: LightGBM 3段二値モデル(1着/2着内/3着内) + 3段Plackett-Luce型で3連単120通りを確率化
- 厳選モード: 本命の複勝(2着以内)的中率90%超を目標に、閾値と対象場をvalidで自動決定
- 配信: GitHub Actions 自動実行 + GitHub Pages PWA (`docs/`)
- 的中実績: 前日結果と予測を突合して自動記録(全体/厳選)

## ワークフロー

| ファイル | 役割 | 起動 |
|---|---|---|
| sample.yml | 指定日の生B/Kテキストを `sample_raw/` に保存(パーサ検証用) | 手動 |
| backfill.yml | 期間指定でB/K取得・解析 → `data/races/entries_YYYY.csv.gz` | 手動 |
| train.yml | 学習 → `data/model/` + `docs/model_report.json` | 手動 |
| daily.yml | 前日結果反映 + 当日予測 → `docs/predictions/` `docs/accuracy.json` | 毎日 6:30 / 8:55 JST |

## 初期セットアップ手順

1. backfill.yml を実行(例: start=20210601, end=今日)
2. train.yml を実行
3. daily.yml を手動実行して当日予測を確認
4. Settings → Pages → Branch: main / Folder: /docs を有効化

## スクリプト

- `scripts/common.py` 取得・LZH解凍・B/Kパーサ
- `scripts/parse_fan.py` / `scripts/fetch_fan.py` ファン手帳(期別成績)の解析・取得
- `scripts/build_dataset.py` 日次データセット構築
- `scripts/features.py` 特徴量(選手成績365/180/90日窓、会場×枠など27項目)
- `scripts/train.py` 学習・λ最適化・バックテスト
- `scripts/predict_today.py` 当日予測JSON生成
- `scripts/update_results.py` 結果反映・的中実績更新

## 免責

予測は参考情報です。的中・回収を保証するものではありません。

#!/usr/bin/env bash
# 開催中ループ(auto-update)を workflow_dispatch で起動する。
#
# このリポジトリのschedule配信は数時間遅れ、しかも1日に数回しか着弾しない
# (auto-updateは30分おきの予約に対し実着は1日4回前後)。ループの起動と5時間ごとの
# 引き継ぎをscheduleに任せると無人の時間帯ができ、その間のレースは締切15分前に
# 確定できず事後判定になる(2026-09-17〜10-01実測: 朝8〜10時台に毎日4〜15件、
# 引き継ぎの隙間が空いた日は19〜20時台にも1〜9件)。workflow_dispatchは遅延しないので、
# 「動いていなければ起動する」をdaily・watchdog・ループ自身の3か所から行う。
#
# 使い方(GH_TOKEN と GITHUB_REPOSITORY が必要。権限は actions: write):
#   bash scripts/ensure_loop.sh            動いている(待機中を含む)回が無ければ起動
#   bash scripts/ensure_loop.sh --handoff  ループ自身が5時間キャップで終わる直前に呼ぶ。
#                                          自分以外に待機中の回が無ければ後継を起動
# 同時実行はワークフロー側の concurrency(group: auto-update)が防ぐ。起動した回は
# 実行中の回が終わるまで待機し、終わり次第走り出す。失敗しても呼び出し元は止めない。
set -u
MODE="${1:-}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY が未設定}"
HM=$(( 10#$(TZ=Asia/Tokyo date +%H) * 60 + 10#$(TZ=Asia/Tokyo date +%M) ))
# ループは5:00より前と23:30以降は即終了する。終了間際(23:00以降)の起動も意味が無い。
if [ "$HM" -lt 300 ] || [ "$HM" -ge 1380 ]; then
  echo "ensure_loop: 時間外(JST $((HM / 60)):$(printf '%02d' $((HM % 60))))のため起動しない"
  exit 0
fi
OTHERS=$(gh api "repos/$REPO/actions/workflows/auto-update.yml/runs?per_page=20" \
           --jq "[.workflow_runs[] | select(.status != \"completed\") | select((.id|tostring) != \"${GITHUB_RUN_ID:-0}\" or \"$MODE\" != \"--handoff\")] | length" 2>/dev/null)
if [ -z "$OTHERS" ]; then
  echo "ensure_loop: 実行状況を取得できなかった(起動しない)"
  exit 0
fi
if [ "$OTHERS" -gt 0 ]; then
  echo "ensure_loop: 稼働中または待機中の回が ${OTHERS} 件あるため起動しない"
  exit 0
fi
if gh workflow run auto-update.yml --repo "$REPO" --ref main; then
  echo "ensure_loop: auto-update を起動した(${MODE:-通常})"
  echo "started=true" >> "${GITHUB_OUTPUT:-/dev/null}"
else
  echo "ensure_loop: 起動に失敗した"
fi
exit 0

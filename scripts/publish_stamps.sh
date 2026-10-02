#!/usr/bin/env bash
# update_all.py(full)が取得の合間に打刻したとき、その場で公開するために呼ぶ。
# やるのは add / commit / push を1回だけ。fetch・rebase・reset はしない:
# full は当日ファイルの内容をメモリに持ったまま動いているので、その途中で作業ツリーを
# 書き換える git 操作を挟まない。push が通らなかった(他の書き手が先に push した)場合は
# ローカルにコミットを残して戻る。周回末尾の commit_push(auto-update.yml)が従来どおり
# rebase して送る。失敗しても呼び出し元を止めない(常に exit 0)。
set -u
MSG="${1:-auto stamps}"
git add docs/predictions || { echo "publish_stamps: add failed"; exit 0; }
if git diff --cached --quiet; then echo "publish_stamps: nothing to commit"; exit 0; fi
git commit -q -m "$MSG" || { echo "publish_stamps: commit failed"; exit 0; }
if git push -q origin HEAD:main; then
  echo "publish_stamps: pushed"
else
  echo "publish_stamps: push not accepted (left for the loop's commit_push)"
fi
exit 0

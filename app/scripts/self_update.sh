#!/bin/bash
# 软件内更新：拉取 GitHub 最新代码 → 重建 .app → 重启托盘。
# 由 GUI 的"检查更新"按钮触发（detached 运行，服务重启不影响本脚本）。
set -e
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
PROJ="$(cd "$(dirname "$0")/../.." && pwd)"
LOG=/tmp/ytrec-update.log
{
  echo "== self-update $(date) =="
  cd "$PROJ"
  sleep 1
  TAG="$1"
  [ -z "$TAG" ] && { echo "no tag given"; exit 1; }
  git fetch -q --tags origin
  echo "checking out release $TAG"
  git checkout -q "tags/$TAG"
  chmod +x app/scripts/build_app.sh
  app/scripts/build_app.sh
  pkill -f 'youtube_recorder.cli tray' 2>/dev/null || true
  pkill -f 'youtube_recorder.cli app' 2>/dev/null || true
  sleep 1
  rm -rf '/Applications/YouTube Recorder.app'
  cp -R "$PROJ/YouTube Recorder.app" /Applications/
  open -n '/Applications/YouTube Recorder.app'
  echo "updated ok"
} >> "$LOG" 2>&1

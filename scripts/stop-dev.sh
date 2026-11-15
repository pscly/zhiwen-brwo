#!/usr/bin/env bash
# 停止开发服务（按 PID 文件，不用 pkill 以免误杀）
set -u
cd "$(dirname "$0")/.."
if [ -f .testdata/serve.pid ]; then
  PID=$(cat .testdata/serve.pid)
  if kill -0 "$PID" 2>/dev/null; then kill "$PID" && echo "已停止 PID $PID"; else echo "PID $PID 已不存在"; fi
  rm -f .testdata/serve.pid
else
  echo "没有 pid 文件"
fi

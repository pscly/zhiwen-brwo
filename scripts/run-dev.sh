#!/usr/bin/env bash
# 开发/测试用启动脚本：脱离终端启动，PID 写入 .testdata/serve.pid
set -u
cd "$(dirname "$0")/.."
ROOT=${ZW_ROOT:-$PWD/.testdata}
mkdir -p "$ROOT"
if [ -f .testdata/serve.pid ] && kill -0 "$(cat .testdata/serve.pid)" 2>/dev/null; then
  echo "已在运行，PID $(cat .testdata/serve.pid)"; exit 0
fi
set -a
# shellcheck disable=SC1091
[ -f .env.test ] && . ./.env.test
set +a
setsid .venv/bin/python -m zw_core.main >> "$ROOT/serve.out" 2>&1 < /dev/null &
echo $! > .testdata/serve.pid
sleep 5
if curl -sf -m 3 http://127.0.0.1:${ZW_PORT:-8730}/healthz > /dev/null; then
  echo "已启动，PID $(cat .testdata/serve.pid)，端口 ${ZW_PORT:-8730}"
else
  echo "启动失败，看 $ROOT/serve.out"; tail -20 "$ROOT/serve.out"; exit 1
fi

#!/usr/bin/env bash
# 把同步中心部署到 192.168.3.5 的 Docker 里。
# 幂等：可反复执行，会同步代码并重建容器。
set -euo pipefail
HOST=${HOST:-192.168.3.5}
REMOTE_DIR=${REMOTE_DIR:-/opt/zhiwen-hub}
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)/hub"

echo "==> 1/5 准备远端目录 $HOST:$REMOTE_DIR"
ssh "$HOST" "sudo mkdir -p $REMOTE_DIR /srv/zw-hub/data && sudo chown -R \$(id -u):\$(id -g) $REMOTE_DIR /srv/zw-hub/data"

echo "==> 2/5 同步代码"
rsync -az --delete --exclude='__pycache__' --exclude='.venv' --exclude='.env' \
  "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"

echo "==> 3/5 生成/保留令牌"
if ssh "$HOST" "test -f $REMOTE_DIR/.env"; then
  echo "    .env 已存在，沿用原令牌"
else
  TOKEN=$(python3 -c "import secrets;print('zw_' + secrets.token_urlsafe(36))")
  ssh "$HOST" "cat > $REMOTE_DIR/.env <<ENVFILE
ZW_HUB_TOKEN=$TOKEN
ENVFILE
chmod 600 $REMOTE_DIR/.env"
  echo "    已生成新令牌（存在 $REMOTE_DIR/.env）"
fi

echo "==> 4/5 构建并启动"
ssh "$HOST" "cd $REMOTE_DIR && sudo docker compose up -d --build"

echo "==> 5/5 验证"
sleep 4
ssh "$HOST" "curl -fsS http://127.0.0.1:8700/healthz" && echo
echo "完成。令牌查看：ssh $HOST 'cat $REMOTE_DIR/.env'"

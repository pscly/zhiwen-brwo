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
  # 老 .env 没有面板相关配置时补上（不改已有值）
  ssh "$HOST" "grep -q '^ZW_PANEL_PASSWORD=' $REMOTE_DIR/.env || echo 'ZW_PANEL_PASSWORD=REDACTED_PANEL_PASSWORD' >> $REMOTE_DIR/.env"
  ssh "$HOST" "grep -q '^ZW_DEVICE_AUTO_APPROVE=' $REMOTE_DIR/.env || echo 'ZW_DEVICE_AUTO_APPROVE=true' >> $REMOTE_DIR/.env"
  ssh "$HOST" "grep -q '^ZW_PANEL_USER=' $REMOTE_DIR/.env || echo 'ZW_PANEL_USER=pscly' >> $REMOTE_DIR/.env"
else
  TOKEN=$(python3 -c "import secrets;print('zw_' + secrets.token_urlsafe(36))")
  PANEL_PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(12))")
  ssh "$HOST" "cat > $REMOTE_DIR/.env <<ENVFILE
ZW_HUB_TOKEN=$TOKEN
# 管理面板（浏览器登录）。默认给的是仓库里的 pscly/REDACTED_PANEL_PASSWORD，
# 这里生成一个随机密码更安全；想用固定密码就把这行改成 ZW_PANEL_PASSWORD=你的密码
ZW_PANEL_USER=pscly
ZW_PANEL_PASSWORD=$PANEL_PW
# 新设备是否自动放行：个人自用=true（客户端只填 IP 就能用）
ZW_DEVICE_AUTO_APPROVE=true
ENVFILE
chmod 600 $REMOTE_DIR/.env"
  echo "    已生成新令牌（存在 $REMOTE_DIR/.env）"
fi

echo "==> 4/5 构建并启动"
ssh "$HOST" "cd $REMOTE_DIR && sudo docker compose up -d --build"

echo "==> 5/5 验证"
# 容器刚 recreate 时还在启动，固定 sleep 容易在慢机器上误报失败。
# 改成轮询到就绪（最多 60 秒），失败时把容器日志尾巴打出来便于定位。
for i in $(seq 1 30); do
  if ssh "$HOST" "curl -fsS -m 3 http://127.0.0.1:8700/healthz" 2>/dev/null; then
    echo
    ssh "$HOST" 'sudo docker ps --filter name=zw-hub --format "    {{.Names}} {{.Status}} {{.Image}}"' || true
    echo "完成。令牌查看：ssh $HOST 'cat $REMOTE_DIR/.env'"
    exit 0
  fi
  sleep 2
done
echo "    [失败] hub 60 秒内没就绪，最近日志："
ssh "$HOST" 'sudo docker logs --tail 30 zw-hub 2>&1' || true
exit 1


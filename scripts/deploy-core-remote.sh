#!/usr/bin/env bash
# 把 core 部署到另一台 Linux 机器（裸装，systemd 常驻）。
#
# 幂等：可反复执行，会同步代码、复用 .env、重启服务。
# 用法：
#   bash scripts/deploy-core-remote.sh                 # 默认部署到 192.168.3.5
#   HOST=192.168.3.5 PORT=8731 REMOTE_DIR=/opt/zhiwen-core-b \
#     NODE_ID=core-b bash scripts/deploy-core-remote.sh
#
# 与 deploy-hub-35.sh 的区别：hub 走 Docker，core 必须能开浏览器，
# 放容器里要 --no-sandbox 且设备/字体容易缺，裸装更省事。
set -euo pipefail

HOST=${HOST:-192.168.3.5}
PORT=${PORT:-8731}
REMOTE_DIR=${REMOTE_DIR:-/opt/zhiwen-core}
NODE_ID=${NODE_ID:-$(hostname -s)-b}
CHROME_BIN=${CHROME_BIN:-}
POOL_START=${POOL_START:-22001}
POOL_END=${POOL_END:-22100}
HUB_URL=${HUB_URL:-http://192.168.3.5:8700}
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 1/6 准备远端目录 $HOST:$REMOTE_DIR"
ssh "$HOST" "mkdir -p $REMOTE_DIR && echo ok" > /dev/null

echo "==> 2/6 同步代码"
rsync -az --delete --exclude='__pycache__' --exclude='.venv' --exclude='*.env' \
  "$LOCAL_DIR/core/" "$HOST:$REMOTE_DIR/"

echo "==> 3/6 确保 python venv 可用"
# Ubuntu 最小安装常常没有 ensurepip，导致 python3 -m venv 建出来的环境没有 pip
ssh "$HOST" 'python3 -c "import ensurepip" 2>/dev/null || sudo apt-get install -y -qq python3-venv python3-pip > /dev/null' || \
  echo "    （安装 python3-venv 失败，如果后面建 venv 报错，手动执行：sudo apt install python3-venv）"

echo "==> 4/6 建/复用虚拟环境"
ssh "$HOST" "cd $REMOTE_DIR && if [ ! -x .venv/bin/python ]; then rm -rf .venv && python3 -m venv .venv; fi && .venv/bin/python -m pip install -q -e . && .venv/bin/python -c 'import zw_core;print(\"    core\", zw_core.__version__)'"

echo "==> 5/6 生成/保留配置"
if ssh "$HOST" "test -f $REMOTE_DIR/core.env"; then
  echo "    core.env 已存在，沿用原配置"
  NEW_TOKEN=""
else
  NEW_TOKEN=$(python3 -c "import secrets;print('zw_' + secrets.token_urlsafe(30))")
  HUB_TOKEN=${HUB_TOKEN:-$(ssh "$HOST" 'sed -n "s/^ZW_HUB_TOKEN=//p" /opt/zhiwen-hub/.env 2>/dev/null' || true)}
  SYNC_PW=${SYNC_PW:-$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")}
  ssh "$HOST" "cat > $REMOTE_DIR/core.env <<ENVFILE
# 由 deploy-core-remote.sh 生成，含密钥，权限 600
ZW_ROOT=$REMOTE_DIR/data
ZW_NODE_ID=$NODE_ID
ZW_HOST=0.0.0.0
ZW_PORT=$PORT
ZW_GATEWAY_PUBLIC_BASE=http://$HOST:$PORT
ZW_ADMIN_TOKEN=$NEW_TOKEN
ZW_PORT_POOL_START=$POOL_START
ZW_PORT_POOL_END=$POOL_END
ZW_MAX_RUNNING=8
ZW_HUB_URL=$HUB_URL
ZW_HUB_TOKEN=$HUB_TOKEN
ZW_SYNC_PASSWORD=$SYNC_PW
ZW_LOG_LEVEL=INFO
ENVFILE
chmod 600 $REMOTE_DIR/core.env"
  [ -n "$CHROME_BIN" ] && ssh "$HOST" "echo 'ZW_CHROME_BIN=$CHROME_BIN' >> $REMOTE_DIR/core.env"
  echo "    已生成 core.env"
  echo "    访问令牌：$NEW_TOKEN"
  echo "    同步主密码：$SYNC_PW"
fi

echo "==> 5.5/6 检查端口占用"
# 端口被旧的 nohup 进程占住时，systemd 起不来，日志里只有一句
# "address already in use"，很难看出是被谁占了。这里先查一次并给明确提示。
BUSY=$(ssh "$HOST" "(ss -ltnp 2>/dev/null || true) | grep ':$PORT ' || true")
if [ -n "$BUSY" ]; then
  echo "    [警告] 端口 $PORT 已被占用："
  echo "$BUSY" | sed 's/^/      /'
  echo "    先停掉它（例如 kill <pid> 或 systemctl stop 旧单元），否则新服务起不来。"
  echo "    继续执行会在下面验证步骤里暴露这个错误。"
fi

echo "==> 6/6 用 systemd 常驻（无 systemd 则退回 nohup）"
if ssh "$HOST" 'command -v systemctl >/dev/null'; then
  ssh "$HOST" "sudo tee /etc/systemd/system/zhiwen-core-$(echo $PORT).service > /dev/null <<UNIT
[Unit]
Description=知纹核心（zhiwen core，端口 $PORT）
After=network-online.target

[Service]
Type=simple
EnvironmentFile=$REMOTE_DIR/core.env
ExecStart=$REMOTE_DIR/.venv/bin/python -m zw_core.main
WorkingDirectory=$REMOTE_DIR
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now zhiwen-core-$PORT.service && sudo systemctl restart zhiwen-core-$PORT.service"
else
  echo "    没有 systemd，用 nohup 起"
  ssh "$HOST" "cd $REMOTE_DIR && set -a && . ./core.env && set +a && (setsid nohup .venv/bin/python -m zw_core.main > $REMOTE_DIR/serve.out 2>&1 < /dev/null &)"
fi

echo "==> 验证"
sleep 5
ssh "$HOST" "curl -fsS http://127.0.0.1:$PORT/readyz" && echo
echo
echo "完成。"
echo "  控制台：http://$HOST:$PORT"
echo "  令牌：ssh $HOST 'cat $REMOTE_DIR/core.env | grep ADMIN_TOKEN'"

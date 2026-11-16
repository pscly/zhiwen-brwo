#!/usr/bin/env bash
# 把同步中心部署到目标机器（默认 192.168.3.5）的 Docker 里。
#
# 幂等：可反复执行，会同步代码、复用 .env、重建容器并轮询到就绪。
#
# ## 为什么镜像走"本地构建 + save/load"而不是在目标机上 build
#
# 目标机（内网小主机）连 PyPI 很慢，`pip install` 常常跑到一半断流。
# 而 hub 的 Dockerfile 里**故意不加 `|| true`**（加了会把 pip 失败吞掉，
# 产出一个没有 fastapi 的镜像，容器起不来但 compose 显示成功 —— 真踩过）。
#
# 所以流程是：
#   1. 在本机（开发机，网络好）`docker build`
#   2. `docker save | gzip` → `scp` → 目标机 `docker load`
#   3. 目标机 `docker compose up -d`（pull_policy: never，绝不联网）
#
# 如果本机没有 docker，退回到"在目标机上 build"（并明确告警）。
set -euo pipefail

HOST=${HOST:-192.168.3.5}
REMOTE_DIR=${REMOTE_DIR:-/opt/zhiwen-hub}
DATA_DIR=${DATA_DIR:-/srv/zw-hub/data}
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)/hub"
VERSION=$(python3 -c "import re,sys;print(re.search(r'version = \"([^\"]+)\"', open('$LOCAL_DIR/pyproject.toml').read()).group(1))")
IMAGE="zw-hub:$VERSION"

echo "==> 0/6 目标 $HOST:$REMOTE_DIR    版本 $VERSION"

echo "==> 1/6 准备远端目录"
ssh "$HOST" "sudo mkdir -p $REMOTE_DIR $DATA_DIR && sudo chown -R \$(id -u):\$(id -g) $REMOTE_DIR $DATA_DIR"

echo "==> 2/6 同步代码（含 docker-compose.yml 与 Dockerfile）"
# 注意：必须把 compose 文件一起同步。只同步 src/ 会导致
# "改了 .env 但容器行为没变"—— 因为 compose 里的环境变量默认值还是旧的。
rsync -az --delete --exclude='__pycache__' --exclude='.venv' --exclude='.env' \
  "$LOCAL_DIR/" "$HOST:$REMOTE_DIR/"

echo "==> 3/6 生成/保留配置"
if ssh "$HOST" "test -f $REMOTE_DIR/.env"; then
  echo "    .env 已存在，沿用原配置"
  # 老 .env 缺的配置项补上（不改已有值）
  # 只补"缺的键"，不覆盖已有值（已有的凭据是用户自己定的，不能动）
  ssh "$HOST" "grep -q '^ZW_PANEL_USER=' $REMOTE_DIR/.env || echo 'ZW_PANEL_USER=admin' >> $REMOTE_DIR/.env"
  ssh "$HOST" "grep -q '^ZW_PANEL_PASSWORD=' $REMOTE_DIR/.env || echo 'ZW_PANEL_PASSWORD=1234' >> $REMOTE_DIR/.env"
  ssh "$HOST" "grep -q '^ZW_DEVICE_AUTO_APPROVE=' $REMOTE_DIR/.env || echo 'ZW_DEVICE_AUTO_APPROVE=true' >> $REMOTE_DIR/.env"
else
  PANEL_PW=$(python3 -c "import secrets;print(secrets.token_urlsafe(12))")
  ssh "$HOST" "cat > $REMOTE_DIR/.env <<ENVFILE
# 可选：留空 = 客户端不要对接码，准入靠设备注册表（每台电脑都要注册、可踢）
# 想更严就取消注释并填一串随机值：
#ZW_HUB_TOKEN=换成足够长的随机串
ZW_PANEL_USER=admin
ZW_PANEL_PASSWORD=$PANEL_PW
ZW_DEVICE_AUTO_APPROVE=true
ENVFILE
chmod 600 $REMOTE_DIR/.env"
  echo "    已生成 .env（面板密码 $PANEL_PW）"
fi

echo "==> 4/6 准备镜像 $IMAGE"
if command -v docker > /dev/null 2>&1; then
  echo "    在本机构建（网络好，避免目标机 pip 断流）"
  ( cd "$LOCAL_DIR" && docker build -t "$IMAGE" . > /tmp/zw-hub-build.log 2>&1 ) || {
    echo "    [失败] 本机构建失败，日志尾部：" >&2
    tail -20 /tmp/zw-hub-build.log >&2
    exit 1
  }
  # 构建出来的镜像必须真的能 import 依赖，否则等于产出一个坏镜像
  docker run --rm --entrypoint python "$IMAGE" -c "import fastapi, uvicorn, zw_hub; print('    镜像自检 OK:', zw_hub.__version__)"
  echo "    传输到 $HOST …"
  docker save "$IMAGE" | gzip -1 > /tmp/zw-hub-image.tar.gz
  scp -q /tmp/zw-hub-image.tar.gz "$HOST:/tmp/zw-hub-image.tar.gz"
  ssh "$HOST" "sudo docker load -i /tmp/zw-hub-image.tar.gz > /dev/null && rm -f /tmp/zw-hub-image.tar.gz"
  rm -f /tmp/zw-hub-image.tar.gz
else
  echo "    [警告] 本机没有 docker，改在目标机上构建（pip 可能很慢或不稳）" >&2
  ssh "$HOST" "cd $REMOTE_DIR && sudo docker compose build"
fi

echo "==> 5/6 启动容器"
# 用 compose 但不让它联网拉镜像（compose 文件里有 pull_policy: never）
ssh "$HOST" "cd $REMOTE_DIR && sudo docker compose up -d --force-recreate"

echo "==> 6/6 验证（轮询到就绪，最多 60 秒）"
# 用**局域网地址**验证，而不是 127.0.0.1：
# 1) 它才证明"别的机器能连上"，这才是用户关心的；
# 2) 只绑内网 IP 的 compose 配置下，回环地址本来就该连不上。
for _ in $(seq 1 30); do
  if ssh "$HOST" "curl -fsS -m 3 http://$HOST:8700/healthz" 2> /dev/null; then
    echo
    ssh "$HOST" 'sudo docker ps --filter name=zw-hub --format "    {{.Names}} {{.Status}} {{.Image}}"'
    echo
    echo "完成。"
    echo "  本机回环自检：$(ssh "$HOST" 'curl -fsS -m 3 http://127.0.0.1:8700/healthz 2>/dev/null || echo "(未绑回环，正常)"')"
    echo "  主控台：http://$HOST:8700/"
    echo "  面板账号：ssh $HOST 'grep ZW_PANEL_ $REMOTE_DIR/.env'"
    exit 0
  fi
  sleep 2
done

echo "    [失败] hub 60 秒内没就绪。容器日志尾部：" >&2
ssh "$HOST" 'sudo docker logs --tail 30 zw-hub 2>&1' >&2
exit 1

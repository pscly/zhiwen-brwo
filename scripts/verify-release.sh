#!/usr/bin/env bash
# 验证**已发布的产物**能不能跑（验的是用户会下载到的那个文件，不是源码）。
#
# 为什么需要它：`check-all.sh` 验的是工作区里的代码。而用户拿到的是
# GitHub Releases 上的二进制 —— 打包环节出问题（缺库、glibc 版本、路径写错）
# 在源码测试里完全看不出来。历史上就踩过：ubuntu-latest 打出来的包
# 要求 GLIBC_2.42，在 Debian/Ubuntu 22 上直接跑不起来。
#
# 用法：
#   bash scripts/verify-release.sh            # 验最新 release
#   bash scripts/verify-release.sh v0.7.49    # 验指定 tag
#   SKIP_DOWNLOAD=1 bash scripts/verify-release.sh v0.7.49   # 复用 /tmp 里已下好的
#
# 只验当前平台能跑的那个资产（Linux 上验 Linux 包）。Windows/macOS 的包
# 需要在对应系统上跑，脚本会明确说明「本平台不适用」，不会假装验过。
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT" || exit 1
TAG="${1:-}"
if [ -z "$TAG" ]; then
  TAG=$(gh release list --limit 1 --json tagName -q '.[0].tagName' 2>/dev/null || true)
fi
[ -z "$TAG" ] && { echo "拿不到 release tag，请显式给一个（如 v0.7.49）"; exit 2; }

OS=$(uname -s); ARCH=$(uname -m)
case "$OS" in
  Linux)  ASSET="zhiwen-client-Linux-x86_64" ;;
  Darwin) ASSET="zhiwen-client-macOS.zip" ;;
  *)      echo "本平台（$OS）暂不支持自动验证产物；请到对应系统上跑 --selftest。"; exit 3 ;;
esac

DIR="/tmp/zw-verify-$TAG"
mkdir -p "$DIR"
if [ "${SKIP_DOWNLOAD:-0}" != "1" ] || [ ! -s "$DIR/$ASSET" ]; then
  echo "下载 $TAG / $ASSET …"
  rm -f "$DIR/$ASSET"
  gh release download "$TAG" -R "$(git remote get-url origin | sed 's|.*[:/]\([^/]*/[^/]*\)\.git|\1|; s|.*[:/]\([^/]*/[^/]*\)$|\1|')" \
     -p "$ASSET" -D "$DIR" || { echo "下载失败"; exit 1; }
fi

WANT=$(gh release view "$TAG" --json assets -q ".assets[]|select(.name==\"$ASSET\")|.size")
GOT=$(stat -c%s "$DIR/$ASSET" 2>/dev/null || stat -f%z "$DIR/$ASSET")
[ "$WANT" = "$GOT" ] || { echo "❌ 大小不符：期望 $WANT，实际 $GOT（下载被截断）"; exit 1; }
echo "✅ 下载完整：$GOT 字节"

if [ "$OS" = "Darwin" ]; then
  ( cd "$DIR" && unzip -oq "$ASSET" )
  BIN=$(find "$DIR" -maxdepth 2 -name "zhiwen-client" -o -maxdepth 2 -name "ZhiwenClient.app" | head -1)
  [ -n "$BIN" ] || { echo "❌ 解压后找不到可执行文件"; exit 1; }
else
  BIN="$DIR/$ASSET"
  chmod +x "$BIN"
fi

echo "跑产物自检 …"
OUT=$(ZW_ROOT="$DIR/root" "$BIN" --selftest 2>&1)
echo "$OUT" | tail -20
echo "$OUT"
if ! echo "$OUT" | grep -qE "全部 [0-9]+ 项通过"; then
  echo "❌ 发布产物自检未通过"
  exit 1
fi
echo "✅ 发布产物自检通过"

# ── 第二步：用**产物本身**跑完整 e2e（能配到的都配上）────────────
#
# 自检只证明"能起来、依赖齐全"。真正要看的是"这个文件能不能干活"——
# 起 core、建环境、开浏览器、读 cookie、导出导入、同步。这些都只有
# 跑 e2e 才知道。历史上 `serve` 子命令在产物里根本不生效（入口是
# client.py 而非 cli.py），自检能过、e2e 直接 Connection refused。
if [ "${SKIP_E2E:-0}" = "1" ] || [ "$OS" != "Linux" ]; then
  echo "（跳过产物 e2e；设 SKIP_E2E=0 可在 Linux 上跑）"
  exit 0
fi

PORT="${E2E_PORT:-8853}"
E2E_ROOT="$DIR/root-e2e"
rm -rf "$E2E_ROOT"
TOK="zw_artifactcheck_$$"
export ZW_ROOT="$E2E_ROOT" ZW_HOST=0.0.0.0 ZW_PORT="$PORT" ZW_ADMIN_TOKEN="$TOK"
export ZW_SHELL_ENABLED=false ZW_SYNC_PASSWORD="${E2E_SYNC_PASSWORD:-ArtifactE2E-Pass}"
export ZW_PORT_POOL_START="${E2E_POOL_START:-22401}" ZW_PORT_POOL_END="${E2E_POOL_END:-22440}"
[ -n "${ZW_CHROME_BIN:-}" ] || export ZW_CHROME_BIN=""
if [ -n "${ZW_HUB_URL:-}" ]; then
  DEV="zwd_artifact$PPID${RANDOM}abc"
  export ZW_DEVICE_ID="$DEV" ZW_HUB_TOKEN="${ZW_HUB_TOKEN:-}"
fi

echo
echo "用产物起 core 跑 e2e …"
nohup "$BIN" serve > "$DIR/e2e-core.log" 2>&1 &
CORE_PID=$!
for _ in $(seq 1 40); do curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/healthz" && break; sleep 0.5; done
CODE=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz")
echo "  产物版 core → $CODE"
if [ "$CODE" != "200" ]; then
  echo "❌ 产物起不来（看 $DIR/e2e-core.log）"
  tail -20 "$DIR/e2e-core.log"
  kill $CORE_PID 2>/dev/null
  exit 1
fi

# 配了 hub 就顺手批准这台自检设备，否则同步那三项会红（那不是 bug）
if [ -n "${ZW_HUB_URL:-}" ] && [ -n "${ZW_PANEL_USER:-}" ]; then
  LAN=$(python3 -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('$ZW_HUB_URL'.split('//')[-1].split(':')[0],80));print(s.getsockname()[0])" 2>/dev/null || echo 127.0.0.1)
  curl -s -c "$DIR/c.txt" -X POST "$ZW_HUB_URL/panel/api/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$ZW_PANEL_USER\",\"password\":\"${ZW_PANEL_PASSWORD:-}\"}" >/dev/null 2>&1
  sleep 2
  curl -s -b "$DIR/c.txt" -X POST "$ZW_HUB_URL/panel/api/devices/$DEV/approve" -o /dev/null 2>&1
else
  LAN=127.0.0.1
fi

E2E_OUT=$(ZW_CORE="http://$LAN:$PORT" ZW_ADMIN_TOKEN="$TOK" \
  ZW_SYNC_PASSWORD="${E2E_SYNC_PASSWORD:-ArtifactE2E-Pass}" \
  "$ROOT/.venv/bin/python" "$ROOT/scripts/e2e-check.py" 2>&1)
echo "$E2E_OUT" | tail -8
if [ -n "${ZW_HUB_URL:-}" ] && [ -n "${ZW_PANEL_USER:-}" ]; then
  curl -s -b "$DIR/c.txt" -X DELETE "$ZW_HUB_URL/panel/api/devices/$DEV" -o /dev/null 2>&1
fi
kill $CORE_PID 2>/dev/null
if echo "$E2E_OUT" | grep -qE "失败 0 项"; then
  echo "✅ 发布产物跑 e2e 通过"
  exit 0
fi
echo "❌ 发布产物跑 e2e 失败"
exit 1

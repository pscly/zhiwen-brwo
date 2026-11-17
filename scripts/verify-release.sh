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
cd "$(dirname "$0")/.." || exit 1
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
if echo "$OUT" | grep -qE "全部 [0-9]+ 项通过"; then
  echo "✅ 发布产物自检通过"
  exit 0
fi
echo "❌ 发布产物自检未通过"
exit 1

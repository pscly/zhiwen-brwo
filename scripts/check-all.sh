#!/usr/bin/env bash
# 一条命令跑完所有验收门槛，最后给一张总表。
#
# 为什么需要它：
#   1. 门槛有七个（pytest + 6 个脚本），散在不同文件里，"提交前必须做"那张表
#      全靠人记 —— 少跑一个不会有任何提示。
#   2. 更隐蔽的一类错：**跳过项看起来像"环境限制"，其实是我没配**。
#      之前 "导出/导入" 和 "同步" 就因为没给主密码、没配 hub 一直跳过，
#      我还照着 65/6 这个数字报了十几轮。这个脚本自己把前提条件配好。
#   3. 结果要能一眼看懂：一张表 + 非零退出码。
#
# 用法：
#   bash scripts/check-all.sh                    # 全跑
#   SKIP_GUI=1 bash scripts/check-all.sh         # 没 X 时跳过 GUI
#   ZW_CHROME=/path/to/chrome bash scripts/check-all.sh
#
# 想连到自己的 hub 做同步验收（会多跑 8 项），提供：
#   ZW_HUB_URL / ZW_HUB_TOKEN / ZW_PANEL_USER / ZW_PANEL_PASSWORD
# 没有就自动跳过同步相关项（并在表里注明"未配置"，不是"通过"）。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)
VENV="$ROOT/.venv/bin/python"
CHROME="${ZW_CHROME:-/opt/bsm-p0/ungoogled-chromium-150.0.7871.186-1-x86_64_linux/chrome}"
PORT="${CHECK_PORT:-8829}"
SYNC_PW="${ZW_SYNC_PASSWORD:-CheckAll-SyncPass-2026}"
TOKEN="${CHECK_TOKEN:-zw_checkall_token_123456}"
DEV="zwd_checkall0123456789abcdef"
TMP=$(mktemp -d /tmp/zw-checkall-XXXXXX)
PIDFILE="$TMP/core.pid"

NAMES=(); RESULTS=(); DETAILS=()
pass(){ NAMES+=("$1"); RESULTS+=("✅ 通过"); DETAILS+=("$2"); }
fail(){ NAMES+=("$1"); RESULTS+=("❌ 失败"); DETAILS+=("$2"); }
skipn(){ NAMES+=("$1"); RESULTS+=("⚠️ 跳过"); DETAILS+=("$2"); }

cleanup(){
  [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null
  # 清掉这台自检设备，别给服务器留垃圾
  if [ -n "${ZW_HUB_URL:-}" ] && [ -n "${ZW_PANEL_USER:-}" ]; then
    curl -s -c "$TMP/c" -X POST "$ZW_HUB_URL/panel/api/login" \
      -H 'Content-Type: application/json' \
      -d "{\"username\":\"$ZW_PANEL_USER\",\"password\":\"${ZW_PANEL_PASSWORD:-}\"}" >/dev/null 2>&1
    curl -s -b "$TMP/c" -X DELETE "$ZW_HUB_URL/panel/api/devices/$DEV" >/dev/null 2>&1
  fi
  # 失败时**保留**临时目录（里面有 core 日志、各脚本输出），否则没法查
  if [ "${KEEP_ON_FAIL:-0}" = "1" ] || [ "${FAILED:-0}" = "1" ]; then
    echo "（日志保留在 $TMP）"
  else
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

echo "════════ 1/8 单元测试 ════════"
if out=$("$VENV" -m pytest tests -q --timeout=200 2>&1); then
  pass "pytest" "$(echo "$out"|grep -E '^[0-9]+ passed'|tail -1)"
else
  # **必须带出失败用例名**：只报"1 failed"时无从下手，得再手工复现一次。
  # 而且这个脚本会先 export ZW_ROOT / ZW_CHROME_BIN 等变量，pytest 会继承 ——
  # 有些偶发失败只在"带着这些变量"时出现，脱开脚本又复现不了。
  _names=$(echo "$out" | grep -E '^FAILED ' | sed 's/^FAILED //' | head -3 | tr '\n' ' ')
  fail "pytest" "$(echo "$out"|grep -E 'failed|error'|tail -1)${_names:+  →  $_names}"
fi

echo "════════ 起一个自检用 core ════════"
export ZW_ROOT="$TMP/root" ZW_HOST=0.0.0.0 ZW_PORT="$PORT" ZW_ADMIN_TOKEN="$TOKEN"
export ZW_SHELL_ENABLED=false ZW_CHROME_BIN="$CHROME" ZW_SYNC_PASSWORD="$SYNC_PW"
export ZW_DEVICE_ID="$DEV"
# **每个 core 必须用独立端口池**。默认是 22001-22100，而本脚本会同时起两个 core
# （主 core + tabs-check 自己那个）。共用池的后果实测过：gateway-check 启动环境时
# 抢不到端口 → 环境其实没跑起来 → 读 cookie 返回 STATE_CONFLICT
# `Cannot read properties of undefined (reading 'cookies')`，看起来像网关坏了，
# 实际是测试脚手架自己在打架。
export ZW_PORT_POOL_START=22001 ZW_PORT_POOL_END=22040
export ZW_PORT_POOL_START_TABS=22101 ZW_PORT_POOL_END_TABS=22140

# 起 core 之前先看端口池干不干净。
#
# 真踩过（2026-11-19）：上一次 check-all 被 Ctrl-C/超时中断时，它拉起的 headless
# 浏览器不会被回收，继续占着 22001/22002。下一次运行时新浏览器拿不到调试端口 →
# 环境其实没真起来 → 读 cookie 拿到空 → gateway-check 报
# `Cannot read properties of undefined (reading 'cookies')`。
# 看起来像网关或 cookie 代码坏了，实际是"自己上一次的残留把自己绊倒"。
#
# 这里不自动去杀（杀错东西代价更大），只把占用者列出来并明确提示怎么清。
_busy=""
for _p in $(seq 22001 22040) $(seq 22101 22140); do
  if ss -tln 2>/dev/null | grep -q ":$_p "; then
    _who=$(ss -tlnp 2>/dev/null | grep ":$_p " | grep -oP 'users:\(\("\K[^"]+' | head -1)
    _busy="$_busy $_p($_who)"
  fi
done
if [ -n "$_busy" ]; then
  echo "⚠ 端口池里有端口被占用：$_busy"
  echo "  如果不清理，浏览器可能拿不到调试端口，gateway-check 会以 cookie 断言失败的形式报出来。"
  echo "  清理办法：pkill -f zw-checkall   （只杀本脚本留下的进程）"
fi
if [ -n "${ZW_HUB_URL:-}" ]; then
  export ZW_HUB_URL ZW_HUB_TOKEN="${ZW_HUB_TOKEN:-}"
  echo "  已配 hub：$ZW_HUB_URL（会真跑同步那几项）"
else
  echo "  未配 ZW_HUB_URL → 同步相关项会跳过（表里会注明）"
fi
nohup "$VENV" -m zw_core.main > "$TMP/core.log" 2>&1 &
echo $! > "$PIDFILE"
for _ in $(seq 1 40); do
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/healthz" && break
  sleep 0.5
done
LAN=$(python3 - <<'PY' 2>/dev/null || true
import socket
s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("192.168.3.5", 8700)); print(s.getsockname()[0])
except Exception:
    print("127.0.0.1")
finally:
    s.close()
PY
)
LAN="${LAN:-127.0.0.1}"
echo "  自检 core： http://$LAN:$PORT"

# 设备批准（否则同步三项必红，而那不是 bug）
if [ -n "${ZW_HUB_URL:-}" ] && [ -n "${ZW_PANEL_USER:-}" ]; then
  curl -s -c "$TMP/c" -X POST "$ZW_HUB_URL/panel/api/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$ZW_PANEL_USER\",\"password\":\"${ZW_PANEL_PASSWORD:-}\"}" >/dev/null 2>&1
  sleep 1
  curl -s -b "$TMP/c" -X POST "$ZW_HUB_URL/panel/api/devices/$DEV/approve" \
    -o /dev/null -w "  批准设备 → %{http_code}\n" 2>/dev/null || true
fi

echo "════════ 2/8 安装脚本静态检查（纯 Python，几秒）════════"
# 这一条必须在推送前跑：CI 的 Windows job 会跑它，但本地从来没跑过 ——
# 0.7.58 就是因为"改了测试里的白名单、忘了改 check-iss.py 里的同一份白名单"
# 而让 Windows 构建红的。同一个规则写了两遍，是最容易不同步的地方。
if out=$("$VENV" packaging/windows/check-iss.py 2>&1); then
  pass "check-iss" "$(echo "$out"|grep -E '^结果：'|tail -1)"
else
  fail "check-iss" "$(echo "$out"|grep -E '^  FAIL'|head -2|tr '\n' ' ')"
fi

echo "════════ 3/8 单机全能力（e2e-check，真开浏览器）════════"
if out=$(ZW_CORE="http://$LAN:$PORT" ZW_ADMIN_TOKEN="$TOKEN" ZW_SYNC_PASSWORD="$SYNC_PW" \
        "$VENV" scripts/e2e-check.py 2>&1); then
  pass "e2e-check" "$(echo "$out"|grep -E '^通过'|tail -1)"
else
  fail "e2e-check" "$(echo "$out"|grep -A6 '失败项'|tr '\n' ' '|head -c 200)"
fi

echo "════════ 4/8 标签页行为（真浏览器）════════"
if [ -x "$CHROME" ]; then
  if out=$(ZW_ROOT="$TMP/tabs" ZW_HOST=127.0.0.1 ZW_PORT=$((PORT+1)) ZW_ADMIN_TOKEN= \
            ZW_SHELL_ENABLED=false ZW_CHROME_BIN="$CHROME" \
            ZW_PORT_POOL_START="$ZW_PORT_POOL_START_TABS" ZW_PORT_POOL_END="$ZW_PORT_POOL_END_TABS" \
            "$VENV" scripts/tabs-check.py 2>&1); then
    pass "tabs-check" "$(echo "$out"|grep -E '断言通过'|tail -1)"
  else
    fail "tabs-check" "$(echo "$out"|grep -E '失败项|断言通过'|tail -1)"
  fi
else
  skipn "tabs-check" "没有可用的 Chromium（设 ZW_CHROME）"
fi

echo "════════ 5/8 网关接管 ════════"
if command -v node >/dev/null; then
  # 这一项会被前面的步骤"绊一下"：它每次都要**真起一个环境 + 真开浏览器**，
  # 而前一步（tabs-check）刚跑完浏览器、系统里可能还有在退出的进程。
  #
  # 实测（2026-11-20）：单独跑、按顺序跑、走 127.0.0.1、走 LAN 都 23/0；
  # 但在完整 check-all 里偶发三条 cookie 断言失败。失败信息是
  # "读不到 cookie 列表"，看起来像网关坏了，实际是环境没真起来。
  #
  # 给一次重试，并把第一次的失败留在日志里 —— 不掩盖问题，但也不让
  # 一个时序抖动挡住整轮验收。
  if out=$(ZW_CORE="http://$LAN:$PORT" ZW_ADMIN_TOKEN="$TOKEN" \
            node scripts/gateway-check.cjs 2>&1); then
    pass "gateway-check" "$(echo "$out"|grep -E '通过 [0-9]+ 项'|tail -1)"
  else
    first_fail=$(echo "$out"|tail -1|head -c 160)
    echo "  （第一次未过：$first_fail —— 2 秒后重试一次）"
    sleep 2
    if out=$(ZW_CORE="http://$LAN:$PORT" ZW_ADMIN_TOKEN="$TOKEN" \
              node scripts/gateway-check.cjs 2>&1); then
      pass "gateway-check" "$(echo "$out"|grep -E '通过 [0-9]+ 项'|tail -1)（重试后）"
    else
      fail "gateway-check" "$(echo "$out"|tail -1|head -c 160)"
    fi
  fi
else
  skipn "gateway-check" "没有 node"
fi

echo "════════ 6/8 主控台界面（mock 后端）════════"
if command -v node >/dev/null; then
  if out=$(node scripts/console-ui-check.cjs 2>&1); then
    pass "console-ui-check" "$(echo "$out"|grep -E '断言通过'|tail -1)"
  else
    fail "console-ui-check" "$(echo "$out"|grep -E '失败项'|tail -1)"
  fi
else
  skipn "console-ui-check" "没有 node"
fi

echo "════════ 7/8 客户端控制台（web）════════"
if command -v node >/dev/null; then
  if out=$(ZW_CORE="http://$LAN:$PORT" ZW_ADMIN_TOKEN="$TOKEN" \
            node scripts/ui-check.cjs 2>&1); then
    pass "ui-check" "$(echo "$out"|grep -E '通过 [0-9]+ 项'|tail -1)"
  else
    fail "ui-check" "$(echo "$out"|grep -E '失败项|通过 [0-9]+ 项'|tail -1)"
  fi
  # 远程控制台：**从非回环地址**打开，验证"凭机器码进入"。
  # 这是用户需求里独立的一半（本机免令牌是另一半），而 ui-check 只走 127.0.0.1。
  if out=$(ZW_CORE_IP="$LAN" ZW_CORE="http://127.0.0.1:$PORT" ZW_DEVICE_ID="$DEV" \
            node scripts/remote-console-check.cjs 2>&1); then
    pass "remote-console" "$(echo "$out"|grep -E '通过 [0-9]+ 项'|tail -1)"
  else
    fail "remote-console" "$(echo "$out"|grep -E '失败 [0-9]+ 项|❌'|tail -1)"
  fi
else
  skipn "ui-check" "没有 node"
fi

echo "════════ 8/8 客户端 GUI（tkinter）════════"
if [ "${SKIP_GUI:-0}" = "1" ]; then
  skipn "gui-check" "SKIP_GUI=1"
else
  if out=$("$VENV" scripts/gui-check.py --xvfb 2>&1); then
    pass "gui-check" "$(echo "$out"|grep -E '断言通过'|tail -1)"
  else
    fail "gui-check" "$(echo "$out"|grep -E '失败项|断言通过'|tail -1)"
  fi
fi

echo
echo "════════════════ 总表 ════════════════"
FAILED=0
for i in "${!NAMES[@]}"; do
  printf '%-20s %-10s %s\n' "${NAMES[$i]}" "${RESULTS[$i]}" "${DETAILS[$i]}"
  case "${RESULTS[$i]}" in *失败*) FAILED=1;; esac
done
echo "══════════════════════════════════════"
if [ "$FAILED" = "1" ]; then
  echo "有失败项 —— 修完再交付。"
  exit 1
fi
echo "全部通过。"

#!/usr/bin/env bash
# 远程把云电脑上的客户端升级到指定版本。
#
# ## 为什么不能直接 Start-Process 安装器
#
# core 提供了 `/api/v1/shell/exec`，我们通过它执行 PowerShell。进程树是：
#
#     zhiwen-client.exe（core）
#       └─ powershell
#            └─ zwXX.exe（安装器）
#
# 而安装器的 `PrepareToInstall` 会执行 `taskkill /F /T /IM zhiwen-client.exe`
# —— `/T` 是**连整棵树**。于是安装器把自己也杀掉了：装到一半中断，机器直接离线。
# 2026-11-22 我就这么把云电脑搞下线过。
#
# ## 正确做法
#
# 用**计划任务**启动安装器：任务由 Task Scheduler 拉起，父进程是 svchost，
# 不在 core 的进程树里，`taskkill /T` 动不到它。
#
# 用法：
#   bash scripts/upgrade-cloud-pc.sh v0.8.36
#   ZW_CLOUD_PC=192.168.12.162 ZW_CLOUD_DEV=zwd_xxx bash scripts/upgrade-cloud-pc.sh v0.8.36
set -uo pipefail

TAG="${1:-}"
PC="${ZW_CLOUD_PC:-192.168.12.162}"
DEV="${ZW_CLOUD_DEV:-zwd_toMfovGt43aSixO64UJOCgmqFodb0Lvq}"
PROXY="${ZW_CLOUD_PROXY:-http://127.0.0.1:21080}"
PORT="${ZW_CLOUD_PORT:-8730}"

if [ -z "$TAG" ]; then
  echo "用法：bash scripts/upgrade-cloud-pc.sh v0.8.36" >&2
  exit 2
fi

VER="${TAG#v}"
URL="https://github.com/pscly/zhiwen-brwo/releases/download/${TAG}/zhiwen-client-Windows-x64-setup.exe"
EXE="C:/Users/Administrator/Downloads/zw${VER//./}.exe"
API="http://${PC}:${PORT}/api/v1"

py() { "${PY:-.venv/bin/python}" "$@"; }

echo "==> 1/4 探查当前版本"
py - "$API" "$DEV" <<'PY' || { echo "云电脑不可达 —— 先在机器上打开一次客户端" >&2; exit 1; }
import json, sys, urllib.request
api, dev = sys.argv[1], sys.argv[2]
r = urllib.request.Request(api + "/meta", headers={"Authorization": "Bearer " + dev})
d = json.load(urllib.request.urlopen(r, timeout=15))["data"]
print("    当前版本:", d.get("version"))
PY

echo "==> 2/4 下载 ${TAG}"
py - "$API" "$DEV" "$URL" "$EXE" "$PROXY" <<'PY' || exit 1
import base64, json, sys, urllib.request
api, dev, url, exe, proxy = sys.argv[1:6]
ps = (
    "$ProgressPreference='SilentlyContinue';"
    f"Invoke-WebRequest -Uri '{url}' -OutFile '{exe}' -UseBasicParsing "
    f"-TimeoutSec 600 -Proxy '{proxy}';"
    f"(Get-Item '{exe}').Length"
)
enc = base64.b64encode(ps.encode("utf-16-le")).decode()
body = json.dumps({"command": f"powershell -NoProfile -EncodedCommand {enc}",
                   "timeout": 700}).encode()
r = urllib.request.Request(api + "/shell/exec", data=body, method="POST",
                           headers={"Authorization": "Bearer " + dev,
                                    "Content-Type": "application/json"})
d = (json.load(urllib.request.urlopen(r, timeout=800)).get("data") or {})
print("    rc:", d.get("rc"), "size:", (d.get("stdout") or "").strip())
sys.exit(0 if d.get("rc") == 0 else 1)
PY

echo "==> 3/4 用计划任务启动安装器（脱离 core 进程树）"
py - "$API" "$DEV" "$EXE" <<'PY' || exit 1
import base64, json, sys, urllib.request
api, dev, exe = sys.argv[1:4]
ps = f'''
$tn = 'ZwUpgradeOnce'
schtasks /Delete /TN $tn /F 2>$null | Out-Null
$a = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
schtasks /Create /TN $tn /TR ('"' + '{exe}' + '" ' + $a) /SC ONCE /ST 00:00 /F /RL HIGHEST 2>&1 | Out-Null
schtasks /Run /TN $tn 2>&1 | Out-Null
Write-Output 'SCHEDULED'
'''
enc = base64.b64encode(ps.encode("utf-16-le")).decode()
body = json.dumps({"command": f"powershell -NoProfile -EncodedCommand {enc}",
                   "timeout": 120}).encode()
r = urllib.request.Request(api + "/shell/exec", data=body, method="POST",
                           headers={"Authorization": "Bearer " + dev,
                                    "Content-Type": "application/json"})
d = (json.load(urllib.request.urlopen(r, timeout=200)).get("data") or {})
print("   ", (d.get("stdout") or "").strip())
PY

echo "==> 4/4 轮询到版本变成 ${VER}（安装器会先结束旧进程，所以中途会短暂不可达）"
for i in $(seq 1 40); do
  sleep 8
  V=$(curl -s -m 5 -H "Authorization: Bearer $DEV" \
        "http://${PC}:${PORT}/api/v1/meta" 2>/dev/null \
      | grep -oE '"version":"[^"]+"' | cut -d'"' -f4 | head -1)
  if [ -z "$V" ]; then
    printf '    %2d 次: 不可达（安装中）\n' "$i"
  else
    printf '    %2d 次: 版本=%s\n' "$i" "$V"
    if [ "$V" = "$VER" ]; then
      echo "完成：云电脑已是 ${VER}"
      exit 0
    fi
  fi
done

echo "超时未变成 ${VER}。若一直不可达，说明安装器结束进程后没能自己起来 ——" >&2
echo "到机器上打开一次客户端，或看 C:\\Users\\Administrator\\AppData\\Local\\Temp\\Setup Log*.txt" >&2
exit 1

#!/usr/bin/env python3
"""在 Windows 云电脑上执行一条命令（可靠的远程 shell 通道）。

## 为什么需要这个

直接往 `/api/v1/shell/exec` 塞 `powershell -Command "..."` 会反复踩坑：

- 远端 shell 会把 `"` 转义成 `\"`，`&` 就失去保护被 cmd 当分隔符（rc=3）
- `%%` 转义被吃掉、`%LOCALAPPDATA%` 不展开
- JSON → cmd.exe → powershell 三层引号叠加，反斜杠数量很难凑对

实测结论（见项目笔记）：改成 PowerShell 的 `-EncodedCommand`（base64 编码的
UTF-16LE 脚本）后，命令行里只剩字母数字，一次就能成。

## 用法

    # 直接给一段 PowerShell（推荐）
    python scripts/winrun.py -c "Get-ChildItem C:/Users/Administrator/AppData/Local/zhiwen"

    # 从文件读（长脚本更省事）
    python scripts/winrun.py -f /tmp/task.ps1

    # 走 cmd.exe 而不是 PowerShell
    python scripts/winrun.py --cmd "dir C:\\Users"

环境变量：
    ZW_CORE_IP    默认 192.168.12.162
    ZW_CORE_PORT  默认 8730
    ZW_DEVICE_ID  默认取项目里那台调试云电脑的机器码
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.request

DEFAULT_IP = os.environ.get("ZW_CORE_IP", "192.168.12.162")
DEFAULT_PORT = os.environ.get("ZW_CORE_PORT", "8730")
DEFAULT_DEV = os.environ.get(
    "ZW_DEVICE_ID", "zwd_HS4ClQKT3qD977dDSktRWWCSqD4N5S9W")


def ps_encoded(script: str) -> str:
    """PowerShell `-EncodedCommand` 要求 UTF-16LE + base64。"""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def run(command: str, *, ip: str, port: str, token: str,
        timeout: int = 60) -> dict:
    url = f"http://{ip}:{port}/api/v1/shell/exec"
    body = json.dumps({"command": command, "timeout": timeout}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout + 15) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser(description="在云电脑上执行命令")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("-c", "--ps", help="PowerShell 脚本内容")
    g.add_argument("-f", "--file", help="从文件读 PowerShell 脚本")
    g.add_argument("--cmd", help="直接走 cmd.exe 的一行命令")
    ap.add_argument("--ip", default=DEFAULT_IP)
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--token", default=os.environ.get("ZW_DEVICE_ID", DEFAULT_DEV))
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--raw", action="store_true", help="只打印 stdout")
    args = ap.parse_args()

    if args.cmd:
        command = args.cmd
    else:
        script = args.ps if args.ps else open(args.file, encoding="utf-8").read()
        # 前置一行：PowerShell 的进度流会以 CLIXML 塞进 stderr，把真正的
        # 错误信息淹没（实测：一次成功的调用 stderr 里有 1.5KB 的进度 XML）。
        script = "$ProgressPreference = 'SilentlyContinue'\n" + script
        command = ('powershell -NoProfile -NonInteractive '
                   f'-EncodedCommand {ps_encoded(script)}')

    try:
        res = run(command, ip=args.ip, port=args.port, token=args.token,
                  timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"[连不上] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if not res.get("ok"):
        print(json.dumps(res, ensure_ascii=False)[:600], file=sys.stderr)
        return 1
    d = res.get("data") or {}
    out = d.get("stdout") or ""
    err = d.get("stderr") or ""
    if args.raw:
        sys.stdout.write(out)
        if err:
            sys.stderr.write(err)
    else:
        print(f"rc={d.get('exit_code')}")
        if out:
            print("--- stdout ---")
            print(out.rstrip())
        if err:
            print("--- stderr ---")
            print(err.rstrip())
    return 0 if not d.get("exit_code") else 1


if __name__ == "__main__":
    raise SystemExit(main())

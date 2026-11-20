#!/usr/bin/env python3
"""常驻终端：通过 hub 中继在云电脑上开一个**长驻 shell 会话**。

## 为什么用这条路，而不是 `/api/v1/shell/exec`

`/shell/exec` 是**一问一答、每条独立**：cwd 不保持、长驻程序没法跑、
输出要等命令结束才回来。

常驻终端走设备到 hub 的持久 WebSocket（协议见
`core/src/zw_core/services/termhub.py` 顶部）：cwd 保持、输出流式、
可以接着上一条命令的目录继续操作。用户明确要求"全程留个终端连接"。

## 用法

    # 交互式（一行一条命令，exit 退出）
    python scripts/term.py

    # 一次性执行
    python scripts/term.py -c "cd C:/Users/Administrator && dir"

识别两种"终端被关掉"的提示：
    device_offline —— 设备实时通道没连上（客户端没开、命令行被关、网络不通）
    unauthenticated —— hub 要求面板登录（先跑 `--login` 或给 cookie）

鉴权：面板会话 cookie。hub 的 `ZW_HUB_TOKEN` 在本部署里是**空的**，
所以只能走 cookie —— 这条容易踩，记在这里。

    # 拿一份新 cookie（存到 ~/.dsh/zhiwen-term-cookie）
    python scripts/term.py --login
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pathlib
import sys
import urllib.request

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

DEFAULT_HUB = os.environ.get("ZW_HUB", "http://192.168.3.5:8700")
DEFAULT_DEV = os.environ.get(
    "ZW_DEVICE_ID", "zwd_HS4ClQKT3qD977dDSktRWWCSqD4N5S9W")
COOKIE_FILE = pathlib.Path.home() / ".dsh" / "zhiwen-term-cookie"


def login(hub: str, user: str, password: str) -> str:
    """登录面板，返回 `zw_panel` 的 cookie 值并落盘。"""
    body = json.dumps({"username": user, "password": password}).encode()
    req = urllib.request.Request(
        f"{hub}/panel/api/login", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.headers.get("Set-Cookie", "")
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith("zw_panel="):
            val = part.split("=", 1)[1]
            COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
            COOKIE_FILE.write_text(val, encoding="utf-8")
            COOKIE_FILE.chmod(0o600)
            return val
    raise RuntimeError(f"登录响应里没有 zw_panel cookie：{raw[:120]!r}")


def read_cookie() -> str:
    if COOKIE_FILE.exists():
        return COOKIE_FILE.read_text(encoding="utf-8").strip()
    return os.environ.get("ZW_PANEL_COOKIE", "")


class Session:
    """一条 WS 上的一个 shell 会话。"""

    def __init__(self, ws, sid: str = "main") -> None:
        self.ws = ws
        self.sid = sid

    async def recv_until_done(self, timeout: float = 120.0) -> tuple[str, int | None, str]:
        """读到 `done`，返回 (输出, rc, cwd)。"""
        outs: list[str] = []
        rc: int | None = None
        cwd = ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remain = deadline - loop.time()
            if remain <= 0:
                return "".join(outs), rc, cwd
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), remain))
            t = msg.get("t")
            if t == "out":
                outs.append(msg.get("data") or "")
            elif t == "done":
                return "".join(outs), msg.get("rc"), str(msg.get("cwd") or "")
            elif t == "err":
                return "".join(outs) + f"\n[错误] {msg.get('code')}: {msg.get('message')}", msg.get("rc"), cwd
            elif t == "ping":                     # hub 的服务端心跳，必须回 pong
                await self.ws.send(json.dumps({"t": "pong"}))

    async def run(self, command: str, timeout: float = 120.0) -> tuple[str, int | None, str]:
        await self.ws.send(json.dumps(
            {"t": "run", "sid": self.sid, "command": command, "timeout": int(timeout)}))
        return await self.recv_until_done(timeout + 15)


async def _main_async(args: argparse.Namespace) -> int:
    import websockets

    cookie = read_cookie()
    if not cookie:
        print("没有面板 cookie，先跑：python scripts/term.py --login", file=sys.stderr)
        return 2
    ws_url = args.hub.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_url}/v1/console/term/{args.device}"
    try:
        ws = await websockets.connect(
            url, max_size=8 * 1024 * 1024,
            extra_headers={"Cookie": f"zw_panel={cookie}"})
    except Exception as exc:  # noqa: BLE001
        print(f"[连不上] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if hello.get("t") == "device_offline":
            print(f"[设备离线] {hello.get('message')}", file=sys.stderr)
            return 3
        sess = Session(ws)
        await ws.send(json.dumps({"t": "open", "sid": sess.sid, "cwd": "", "timeout": 300}))
        # 等 opened（中间可能夹着服务端 ping）
        for _ in range(20):
            m = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if m.get("t") == "opened":
                cwd = m.get("cwd") or ""
                if args.command is None:
                    print(f"[会话已开] cwd={cwd or '(默认)'}")
                break
            if m.get("t") == "ping":
                await ws.send(json.dumps({"t": "pong"}))

        if args.command is not None:
            out, rc, cwd = await sess.run(args.command, timeout=args.timeout)
            sys.stdout.write(out)
            if rc not in (0, None):
                print(f"\n[rc={rc}]", file=sys.stderr)
            return 0 if rc in (0, None) else 1

        # 交互式：一行一条命令，cwd 保持
        while True:
            try:
                line = input("win> " if sys.stdin.isatty() else "")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = line.strip()
            if not line:
                continue
            if line in ("exit", "quit"):
                break
            out, rc, cwd = await sess.run(line, timeout=args.timeout)
            sys.stdout.write(out)
            if rc not in (0, None):
                print(f"[rc={rc}]", file=sys.stderr)
            if cwd and sys.stdin.isatty():
                print(f"[cwd] {cwd}")
        await ws.send(json.dumps({"t": "close", "sid": sess.sid}))
    finally:
        # 旧版 websockets 的 `WebSocketClientProtocol` 不支持 async context manager，
        # 所以自己 close（用 try/finally，异常路径也要关）。
        with contextlib.suppress(Exception):
            await ws.close()
    return 0


def _online_device(hub: str, cookie: str) -> str:
    """从 hub 里挑一台**当前在线**的设备。

    ## 为什么（2026-11-20）

    以前 `--device` 的默认值是**写死的一台机器码**。那台机器换过机器码之后
    （覆盖安装、配置目录变化都会导致），默认值就指向一台离线设备，
    于是终端永远报「设备离线」—— 而 hub 上明明另一台是 `realtime=True`。

    实测踩过：`DEFAULT_DEV` 指向 `zwd_HS4ClQKT…`（离线），
    真正在线的是 `zwd_toMfovGt…`，排查了半天才发现是脚本自己的默认值过期。

    现在：显式给了 `--device` 就用它；没给就挑在线的那台；
    只有这样最稳。
    """
    try:
        r = httpx.get(f"{hub}/v1/console/overview",
                      headers={"Cookie": f"zw_panel={cookie}"}, timeout=15)
        machines = ((r.json() or {}).get("data") or {}).get("machines") or []
    except Exception:  # noqa: BLE001 —— 挑不出来就退回默认值
        return ""
    # 优先"实时通道在线"的，其次"心跳在线"的
    for key in ("realtime", "online"):
        for m in machines:
            if m.get(key) and m.get("device_id"):
                return str(m["device_id"])
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="云电脑常驻终端（经 hub 中继）")
    ap.add_argument("--hub", default=DEFAULT_HUB)
    ap.add_argument("--device", default="", help="留空=自动挑当前在线的设备")
    ap.add_argument("-c", "--command", help="执行一条命令后退出")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--login", action="store_true", help="登录面板并保存 cookie")
    ap.add_argument("--user", default=os.environ.get("ZW_PANEL_USER", ""))
    ap.add_argument("--password", default=os.environ.get("ZW_PANEL_PASSWORD", ""))
    args = ap.parse_args()

    if args.login:
        if not args.user or not args.password:
            print("登录需要 --user 与 --password（或 ZW_PANEL_USER / ZW_PANEL_PASSWORD）",
                  file=sys.stderr)
            return 2
        try:
            login(args.hub, args.user, args.password)
        except Exception as exc:  # noqa: BLE001
            print(f"[登录失败] {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(f"已保存 cookie 到 {COOKIE_FILE}")
        return 0

    # 没显式给设备就自动挑在线的；挑不到再退回内置默认值（提示用户）
    if not args.device:
        cookie = ""
        try:
            cookie = COOKIE_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        if cookie:
            args.device = _online_device(args.hub, cookie)
        if not args.device:
            args.device = DEFAULT_DEV
            print(f"[提示] 没找到在线设备，退回默认 {args.device}"
                  f"（可用 --device 指定）", file=sys.stderr)

    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

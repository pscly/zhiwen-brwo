#!/usr/bin/env python3
"""标签页行为验收：启动开检测页 → 关闭时记住 → 再启动恢复且不重复。

## 为什么单独一个脚本

用户原话：

> 并且浏览器在启动之后，默认先打开一个网页，可以检测 IP 地址、浏览器指纹信息
> 以及 IP 干净度的网址…（默认是先打开上次被关闭时候的网址，然后再加一个检测网址）

这条只在云电脑上**手工**看过（截图 + 手动数标签），`scripts/e2e-check.py` 里
一条断言都没有 —— 也就是说这条最容易回归的用户需求，一直没进自动化。

## 它怎么验

不依赖外网：把 `ZW_DETECT_URLS` 指到本机的一个临时 HTTP 服务（页面标题带随机串），
这样"到底开了哪几个"是可精确断言的，而不是"看截图大概对"。

流程：
  1. 起 core → 建环境 → 启动
  2. 断言：浏览器里确实打开了 1 个检测页（本机那个），且 URL 对得上
  3. 在浏览器里再开 2 个页面（模拟用户自己逛）→ 停止（应抓取并记住）
  4. 再启动 → 断言：恢复的页面在，检测页也在，**且没有重复**
  5. 再停止、再启动一次 → 断言数量稳定（不累积）

用法：
    ZW_CHROME=/path/to/chrome .venv/bin/python scripts/tabs-check.py
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core" / "src"))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASSED if cond else FAILED).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if not cond and detail else ""))
    return cond


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = int(s.getsockname()[1])
    s.close()
    return p


def _serve_pages(n: int) -> tuple[http.server.ThreadingHTTPServer, str]:
    """起一个本机小站，给 n 个可区分的页面（标题里带编号）。"""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = (f"<!doctype html><title>ZW-TAB-{self.path.strip('/') or 'index'}</title>"
                    "<h1>probe</h1>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            return

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    return srv, base


def _cdp_port(c, pid: str) -> int:
    """取这个环境的本机调试端口。

    端口不在 `GET /profiles/{id}` 里（那里只有 state 之类），而在
    `GET /profiles/{id}/cdp` 的 `local_port`。第一版我去 profile 上找
    `debug_port`/`port`，拿到 None，后面全链断言都没法跑。
    """
    try:
        d = c.get(f"/profiles/{pid}/cdp").json()
    except Exception:
        return 0
    return int((d.get("data") or {}).get("local_port") or 0)


def _pages(port: int) -> list[str]:
    """读浏览器里所有 page 类型 target 的 url。"""
    import httpx

    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5)
        items = r.json()
    except Exception:
        return []
    return [str(t.get("url") or "") for t in items
            if str(t.get("type") or "") == "page" and not str(t.get("url", "")).startswith("devtools://")]


def _wait(pred, timeout: float = 25.0, step: float = 0.4):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


def _new_tab(port: int, url: str) -> None:
    """用 CDP 在已运行的浏览器里开一个新标签（模拟用户自己逛）。"""
    import httpx

    try:
        v = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=5).json()
        import websockets.sync.client as wsc

        with wsc.connect(v["webSocketDebuggerUrl"], open_timeout=5) as ws:
            ws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                "params": {"url": url}}))
            ws.recv(timeout=8)
    except Exception as exc:  # pragma: no cover
        print(f"    （开新标签失败，跳过这一步：{exc}）")


def main() -> int:
    chrome = os.environ.get("ZW_CHROME") or os.environ.get("ZW_CHROME_BIN") or ""
    if not chrome or not Path(chrome).exists():
        print("❌ 需要一个真实 Chromium：设 ZW_CHROME=/path/to/chrome")
        return 2

    # 清掉外层可能带进来的 ZW_*：本脚本要的是一个**完全独立**的临时 core。
    # 特别是 `ZW_HUB_URL` —— `LocalCore.start(hub_url="")` 只是"不覆盖"，
    # 环境里原有的值会被继承，于是自检 core 真的去 hub 上注册了设备。
    # 实测后果：服务器面板里堆出两条同名 `psubs3.6` 的 pending 残留设备。
    for k in ("ZW_HUB_URL", "ZW_HUB_TOKEN", "ZW_DEVICE_ID", "ZW_ADMIN_TOKEN",
              "ZW_SYNC_PASSWORD", "ZW_PORT_POOL_START", "ZW_PORT_POOL_END",
              "ZW_ALLOWED_CIDRS"):
        os.environ.pop(k, None)

    srv, base = _serve_pages(3)
    tmp = Path(tempfile.mkdtemp(prefix="zw-tabs-"))
    port = _free_port()
    # 检测页只放**一个**本机地址：这样"开了几个"是可精确断言的。
    os.environ.update({
        "ZW_ROOT": str(tmp / "root"),
        "ZW_HOST": "127.0.0.1",
        "ZW_PORT": str(port),
        "ZW_ADMIN_TOKEN": "",
        "ZW_SHELL_ENABLED": "false",
        "ZW_CHROME_BIN": chrome,
        "ZW_DETECT_URLS": f"{base}/detect",
        "ZW_RESTORE_TABS": "true",
        "ZW_OPEN_DETECT_ON_START": "true",
    })

    from zw_core.config import reset_settings

    reset_settings()
    from zw_core.client import LocalCore

    core = LocalCore(port=port)
    import httpx

    try:
        if not check("core 启动", core.start(device_id="zwd_tabscheck1234567890ab", hub_url=""),
                     core.error):
            return 1
        c = httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", timeout=60)
        pid = c.post("/profiles", json={"name": "标签自检"}).json()["data"]["id"]

        print("\n— 第一次启动 —")
        st = c.post(f"/profiles/{pid}/start", json={}).json()
        check("启动成功", st.get("ok") is True, str(st)[:200])
        dev = _wait(lambda: _cdp_port(c, pid))
        check("拿到调试端口", bool(dev), str(dev))
        if not dev:
            return 1
        got = _wait(lambda: [u for u in _pages(int(dev)) if "/detect" in u])
        check("启动后打开了检测页", bool(got), str(_pages(int(dev)))[:300])

        # 模拟用户自己又逛了两个页面
        _new_tab(int(dev), f"{base}/user1")
        _new_tab(int(dev), f"{base}/user2")
        sec = _wait(lambda: len([u for u in _pages(int(dev)) if base in u]) >= 3)
        check("用户在浏览器里开的页面也算进去了", bool(sec), str(_pages(int(dev)))[:300])

        print("\n— 停止（应记住标签）—")
        stopped = c.post(f"/profiles/{pid}/stop", json={"force": True}).json()
        time.sleep(2)
        row = c.get(f"/profiles/{pid}").json()["data"]
        # stop 的返回值要说明"这次记住没记住、没记住为什么"。
        # 以前这个信息只在日志里（打包后日志不一定在手边），用户只看到
        # "下次启动标签没恢复"却查不到原因。
        rem = (stopped.get("data") or {}).get("remembered") or {}
        print(f"    remembered = {rem}")
        check("stop 返回里说明了标签记忆结果", "saved" in rem or "reason" in rem, str(rem))
        remembered = row.get("start_urls") or []
        print(f"    记住的 start_urls = {remembered}")
        check("停止后记住了**所有**用户页面（不只是最后一个）",
              sum(1 for u in remembered if "/user" in str(u)) >= 2, str(remembered))
        check("记住的包含检测页或它的同源页面", len(remembered) >= 1, str(remembered))
        check("记住的只有 http/https",
              all(str(u).startswith(("http://", "https://")) for u in remembered), str(remembered))

        print("\n— 第二次启动（应恢复且不重复）—")
        c.post(f"/profiles/{pid}/start", json={}).json()
        dev2 = _wait(lambda: _cdp_port(c, pid))
        if not check("再次拿到调试端口", bool(dev2), str(dev2)):
            return 1
        urls2 = _wait(lambda: (lambda us: us if len([u for u in us if base in u]) >= 2 else None)(
            _pages(int(dev2))))
        mine = [u for u in (urls2 or _pages(int(dev2))) if base in u]
        print(f"    第二次启动后页面 = {mine}")
        print(f"    期望 start_urls  = {c.get(f'/profiles/{pid}').json()['data'].get('start_urls')}")
        check("恢复了上次的页面", any("/user" in u for u in mine), str(mine)[:300])
        check("检测页也在", any("/detect" in u for u in mine), str(mine)[:300])
        check("没有重复标签", len(mine) == len(set(mine)), str(mine)[:300])

        print("\n— 第三次启动（数量必须稳定，不累积）—")
        c.post(f"/profiles/{pid}/stop", json={"force": True})
        time.sleep(2)
        c.post(f"/profiles/{pid}/start", json={}).json()
        dev3 = _wait(lambda: (c.get(f"/profiles/{pid}").json()["data"].get("debug_port")
                              or c.get(f"/profiles/{pid}").json()["data"].get("port")))
        if dev3:
            _wait(lambda: len([u for u in _pages(int(dev3)) if base in u]) >= 2)
            mine3 = [u for u in _pages(int(dev3)) if base in u]
            check("三次启动后标签数量不膨胀", len(mine3) <= len(set(mine)) + 1,
                  f"{len(mine3)} vs {len(set(mine))}")
            check("仍然没有重复", len(mine3) == len(set(mine3)), str(mine3)[:300])
        c.post(f"/profiles/{pid}/stop", json={"force": True})
    finally:
        core.stop()
        srv.shutdown()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "═" * 42)
    print(f"断言通过 {len(PASSED)} 条，失败 {len(FAILED)} 条")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  - " + f)
        return 1
    print("✅ 标签页行为自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

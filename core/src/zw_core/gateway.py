"""CDP 网关：把只监听 127.0.0.1 的浏览器调试端口，安全地暴露到局域网。

## 为什么需要它

Chrome 的 DevTools 协议是**两段式**：

```
① GET /json/version  →  {"webSocketDebuggerUrl": "ws://127.0.0.1:21001/devtools/browser/<GUID>"}
② 连那个 ws
```

`webSocketDebuggerUrl` 里写死了 `127.0.0.1:<端口>`。跨机时客户端拿到它去连，
连的是**它自己**的回环地址，必然失败。

所以网关的核心职责**不是转发，是重写**：把响应里的地址换成网关自己的地址（并带上短期令牌）。

## 为什么每次连接都要令牌

CDP 等于浏览器的全部权限（能读所有 cookie、能注入任意脚本）。所以：
- 令牌是短期的（默认 15 分钟）、单环境的；
- 令牌只存 `sha256`，泄露数据库也不等于泄露令牌；
- 令牌里不含任何密钥派生信息。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
import websockets
from starlette.websockets import WebSocket, WebSocketDisconnect

logger = logging.getLogger("zw.gateway")

# 直连上游时的超时（毫秒）
HTTP_TIMEOUT = 20.0
WS_PING_INTERVAL = 25
WS_PING_TIMEOUT = 60

# 需要重写地址的字段
_WS_FIELDS = ("webSocketDebuggerUrl",)
_URL_FIELDS = ("devtoolsFrontendUrl",)


@dataclass
class Route:
    profile_id: str
    port: int
    name: str = ""


class Router:
    """网关路由表。进程内内存态，core 重启后由 manager 重建。"""

    def __init__(self) -> None:
        self._routes: dict[str, Route] = {}
        self._conns: dict[str, int] = {}

    def register(self, profile_id: str, port: int, name: str = "") -> None:
        self._routes[profile_id] = Route(profile_id=profile_id, port=port, name=name)
        logger.info("网关登记路由 profile=%s port=%s", profile_id, port)

    def unregister(self, profile_id: str) -> None:
        if self._routes.pop(profile_id, None):
            logger.info("网关撤销路由 profile=%s", profile_id)

    def get(self, profile_id: str) -> Route | None:
        return self._routes.get(profile_id)

    def all(self) -> list[dict[str, Any]]:
        return [
            {"profile_id": r.profile_id, "port": r.port, "name": r.name, "connections": self._conns.get(r.profile_id, 0)}
            for r in self._routes.values()
        ]

    def add_conn(self, profile_id: str, limit: int) -> bool:
        cur = self._conns.get(profile_id, 0)
        if cur >= limit:
            return False
        self._conns[profile_id] = cur + 1
        return True

    def drop_conn(self, profile_id: str) -> None:
        cur = self._conns.get(profile_id, 0)
        if cur <= 1:
            self._conns.pop(profile_id, None)
        else:
            self._conns[profile_id] = cur - 1


ROUTER = Router()


# ── 地址重写 ──────────────────────────────────────────────
def rewrite_payload(payload: Any, profile_id: str, port: int, public_base: str, token: str) -> Any:
    """把 CDP 发现接口里的回环地址改写成网关地址。

    public_base 形如 `http://192.168.3.6:8730`，websocket 时换成 ws://。
    """
    http_base = public_base.rstrip("/") + f"/cdp/{profile_id}"
    ws_base = http_base.replace("http://", "ws://").replace("https://", "wss://")
    q = f"?token={token}" if token else ""
    upstream_hosts = (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", f"0.0.0.0:{port}")

    def fix_ws(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        idx = value.find("/devtools/")
        tail = value[idx:] if idx >= 0 else ""
        return f"{ws_base}{tail}{q}"

    def fix_url(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        if value.startswith("/devtools/"):
            return f"{http_base}{value}{q}"
        for host in upstream_hosts:
            if host in value:
                return value.replace(f"http://{host}", http_base)
        return value

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in list(node.items()):
                if key in _WS_FIELDS:
                    node[key] = fix_ws(val)
                elif key in _URL_FIELDS:
                    node[key] = fix_url(val)
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    # 兜底：任何漏网的裸回环地址
    text = json.dumps(payload, ensure_ascii=False)
    for host in upstream_hosts:
        text = text.replace(host, public_base.split("://", 1)[-1].rsplit("/", 1)[0])
    return json.loads(text)


# ── HTTP 发现接口 ─────────────────────────────────────────
async def proxy_http(profile_id: str, path: str, public_base: str, token: str) -> tuple[int, bytes, dict[str, str]]:
    route = ROUTER.get(profile_id)
    if route is None:
        return 503, json.dumps({"ok": False, "code": "ROUTE_NOT_FOUND", "message": f"网关没有 {profile_id} 的路由（环境未启动？）"}).encode(), {
            "content-type": "application/json; charset=utf-8"
        }

    url = f"http://127.0.0.1:{route.port}{path}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        return 502, json.dumps({"ok": False, "code": "UPSTREAM_ERROR", "message": f"连不上浏览器调试端口：{exc}"}).encode(), {
            "content-type": "application/json; charset=utf-8"
        }

    body = resp.content
    headers = {"content-type": resp.headers.get("content-type", "application/json")}
    if "json" in headers["content-type"]:
        try:
            payload = json.loads(body)
            payload = rewrite_payload(payload, profile_id, route.port, public_base, token)
            body = json.dumps(payload, ensure_ascii=False).encode()
        except json.JSONDecodeError:
            pass
    return resp.status_code, body, headers


# ── WebSocket 反向代理 ────────────────────────────────────
async def proxy_ws(websocket: WebSocket, profile_id: str, path: str, limit: int) -> None:
    route = ROUTER.get(profile_id)
    if route is None:
        await websocket.close(code=1013, reason="route-not-found")
        return

    await websocket.accept()

    if not ROUTER.add_conn(profile_id, limit):
        await websocket.close(code=1013, reason="too-many-connections")
        return

    upstream_url = f"ws://127.0.0.1:{route.port}{path}"
    peer = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    logger.info("CDP 接入 profile=%s peer=%s path=%s", profile_id, peer, path)
    try:
        async with websockets.connect(
            upstream_url,
            max_size=256 * 1024 * 1024,
            ping_interval=WS_PING_INTERVAL,
            ping_timeout=WS_PING_TIMEOUT,
            open_timeout=15,
        ) as upstream:

            async def client_to_upstream() -> None:
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        raise WebSocketDisconnect(msg.get("code", 1000))
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])

            async def upstream_to_client() -> None:
                async for data in upstream:
                    if isinstance(data, str):
                        await websocket.send_text(data)
                    else:
                        await websocket.send_bytes(data)

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, websockets.ConnectionClosed)):
                    logger.warning("CDP 转发异常 profile=%s: %s", profile_id, exc)
    except websockets.InvalidURI:
        await websocket.close(code=1011, reason="bad-upstream-url")
    except (OSError, websockets.WebSocketException) as exc:
        logger.warning("CDP 上游连接失败 profile=%s: %s", profile_id, exc)
        try:
            await websocket.close(code=1011, reason="upstream-unavailable")
        except Exception:
            pass
    finally:
        ROUTER.drop_conn(profile_id)
        logger.info("CDP 断开 profile=%s peer=%s", profile_id, peer)
        try:
            await websocket.close()
        except Exception:
            pass

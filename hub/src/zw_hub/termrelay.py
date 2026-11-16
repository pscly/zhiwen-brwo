"""hub 侧的实时终端中继：把浏览器/CLI 的 WebSocket 接到设备的 WebSocket 上。

## 拓扑

```
管理员浏览器 ──WS──▶ /v1/console/term/{device_id} ──┐
                                                    ├─ 内存里按 device_id 配对
设备（客户端） ──WS──▶ /v1/devices/ws ──────────────┘
```

设备侧是**出站连接**，所以 NAT/云电脑后面也能用 —— 和远程命令队列同一个原则。
区别在于：队列是一问一答，WS 是双向流式。

## 为什么不用"hub 主动连设备"

设备在云电脑/NAT 后面，hub 根本没有它的可路由地址。
反过来（设备连出来）是唯一可行且不需要改客户网络的方案。

## 并发安全

- **同一台设备只有一条 WS**。重连时新连接顶掉旧的（旧连接上的会话仍在设备侧存活，
  只是没人看了 —— 管理员重连后按 sid 继续）。
- 向设备写消息用 `asyncio.Lock` 串行化：多个管理员同时敲命令时，
  不加锁会把 WebSocket 帧写乱（starlette 不支持并发 send）。
- 每个管理员一个 `asyncio.Queue`，设备消息**广播**给所有订阅者；
  队列满了丢最旧的并给该管理员发一条 `overflow` —— 宁可丢一点回滚历史，
  也不能让一个卡住的管理员把整台设备的转发堵死。

## 与 HTTP 队列的关系

两者并存：WS 不在线时管理员界面自动回退到 `/v1/devices/{id}/shell`（一问一答）。
所以这里**不做持久化**，纯内存，hub 重启就断 —— 断线后客户端会自动重连。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

logger = logging.getLogger("zw.termrelay")

# 单个管理员订阅队列上限（条）。超了丢最旧的，防止一个卡住的管理员拖垮转发。
_ADMIN_QUEUE_MAX = 512
# 设备超过这么久没有任何消息（含 pong）就认为掉线
DEVICE_IDLE_TIMEOUT = 90.0


class DeviceLink:
    """一台设备的一条 WS 连接。"""

    def __init__(self, device_id: str, ws: Any) -> None:
        self.device_id = device_id
        self.ws = ws
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.shell_enabled = True
        self._send_lock = asyncio.Lock()
        self._admins: set[asyncio.Queue] = set()

    async def send(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_text(json.dumps(payload, ensure_ascii=False))

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_ADMIN_QUEUE_MAX)
        self._admins.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._admins.discard(q)

    def fanout(self, payload: dict[str, Any]) -> None:
        """把设备消息广播给所有订阅的管理员。"""
        dropped = False
        for q in list(self._admins):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # 丢最旧的一条，再塞新的：保证"最新的输出"一定送得出去
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(payload)
                dropped = True
        if dropped:
            note = {"t": "overflow", "message": "输出太快，部分内容已被丢弃"}
            for q in list(self._admins):
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(note)

    def status(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "connected_at": self.connected_at,
            "last_seen_ago": round(max(0.0, time.time() - self.last_seen), 1),
            "shell_enabled": self.shell_enabled,
            "admins": len(self._admins),
        }


class TermRelay:
    """设备连接表。进程内单例。"""

    def __init__(self) -> None:
        self._links: dict[str, DeviceLink] = {}
        self._lock = asyncio.Lock()

    async def attach(self, device_id: str, ws: Any) -> DeviceLink:
        async with self._lock:
            old = self._links.get(device_id)
            link = DeviceLink(device_id, ws)
            self._links[device_id] = link
        if old is not None:
            # 新连接顶掉旧的：设备侧网络切换时会出现短暂的双连接
            with contextlib.suppress(Exception):
                await old.ws.close(code=1000, reason="replaced-by-new-connection")
            logger.info("设备 %s 的旧终端连接已被新连接替换", device_id)
        return link

    async def detach(self, link: DeviceLink) -> None:
        async with self._lock:
            current = self._links.get(link.device_id)
            if current is link:
                self._links.pop(link.device_id, None)
        # 通知订阅者设备下线了
        link.fanout({"t": "device_offline", "device_id": link.device_id})

    def get(self, device_id: str) -> DeviceLink | None:
        return self._links.get(device_id)

    def online(self, device_id: str) -> bool:
        link = self._links.get(device_id)
        if link is None:
            return False
        return (time.time() - link.last_seen) < DEVICE_IDLE_TIMEOUT

    def listing(self) -> list[dict[str, Any]]:
        return [link.status() for link in self._links.values()]

    def stats(self) -> dict[str, Any]:
        now = time.time()
        return {
            "online": sum(1 for l in self._links.values() if (now - l.last_seen) < DEVICE_IDLE_TIMEOUT),
            "connected": len(self._links),
            "devices": self.listing(),
        }

    async def relay(self, device_id: str, admin_ws: Any, *, on_message_from_admin=None) -> dict[str, Any]:
        """把一个管理员 WS 接到设备上，直到任一方断开。

        返回 {"reason": "..."} 便于调用方记日志。
        """
        link = self.get(device_id)
        if link is None or (time.time() - link.last_seen) >= DEVICE_IDLE_TIMEOUT:
            return {"reason": "device-offline"}

        q = link.subscribe()

        async def admin_to_device() -> None:
            while True:
                raw = await admin_ws.receive_text()
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if on_message_from_admin is not None:
                    on_message_from_admin(msg)
                await link.send(msg)

        async def device_to_admin() -> None:
            while True:
                payload = await q.get()
                await admin_ws.send_text(json.dumps(payload, ensure_ascii=False))

        tasks = [asyncio.create_task(admin_to_device()), asyncio.create_task(device_to_admin())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            reason = "closed"
            for t in done:
                exc = t.exception()
                if exc is not None:
                    reason = f"{type(exc).__name__}"
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return {"reason": reason}
        finally:
            link.unsubscribe(q)


_RELAY: TermRelay | None = None


def relay() -> TermRelay:
    global _RELAY
    if _RELAY is None:
        _RELAY = TermRelay()
    return _RELAY


def reset_relay() -> None:
    """给测试用：清掉全局状态。"""
    global _RELAY
    _RELAY = None

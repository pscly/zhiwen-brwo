"""设备侧的实时终端客户端：向 hub 建一条**持久 WebSocket**，把 shell 会话暴露出去。

## 为什么要 WebSocket，而不是继续用 HTTP 队列

原来的 `shellagent.py` 是"设备长轮询取命令 → 跑完 → 回传结果"。
它的问题是**一问一答**：命令跑完才有输出，中间什么都看不到。

实时终端要的是：输出边产生边推、随时能敲下一行、长驻程序也能跑。
这必须有一条双向长连接。所以这里做 WS：

```
浏览器/CLI ──WS──▶ hub ──WS(同一条)──▶ 设备 ──▶ 本地 shell 会话
```

设备侧**依然不监听端口**，全部出站 —— 和原来的设计原则一致（NAT 后面也能用）。

## 和旧链路的关系

两者**并存**，不是替代：

- WS 在线 → 控制台走 WS（实时、可交互、保持 cwd）
- WS 不在线 / 连不上 → 回退到 HTTP 队列（一问一答，但至少能用）

回退是必要的：hub 升级、网络抖动、企业防火墙挡 WS 升级，这些都会让 WS 断掉。
只支持 WS 的话，一断就完全没法用了。

## 消息协议（与 hub/src/zw_hub/termrelay.py 一致，改一边必须改另一边）

hub → 设备：
    {"t":"open","sid":"...","cwd":"","timeout":60}   建会话
    {"t":"run","sid":"...","command":"...","timeout":60}  执行命令（会话不存在则先 open）
    {"t":"input","sid":"...","data":"..."}           原始 stdin
    {"t":"signal","sid":"...","kind":"interrupt"}    Ctrl+C
    {"t":"close","sid":"..."}                        关会话
    {"t":"ping"}

设备 → hub：
    {"t":"hello","shell_enabled":true,"sessions":0}
    {"t":"opened","sid":"...","cwd":"..."}
    {"t":"out","sid":"...","data":"..."}             流式输出
    {"t":"done","sid":"...","rc":0,"cwd":"...","truncated":false}
    {"t":"exit","sid":"...","rc":0}                  会话进程结束
    {"t":"err","sid":"...","code":"...","message":"..."}
    {"t":"pong"}
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from .session import ShellSession, SessionError, SessionRegistry

logger = logging.getLogger("zw.termclient")

# 重连退避：网络断一下不要疯狂重连
_BACKOFF = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)
# 心跳间隔（秒）。hub 侧 90 秒没收到任何消息就认为设备掉线。
_PING_EVERY = 20.0


class TermClient:
    """维护到 hub 的终端 WS。

    设计上刻意做成"**永不放弃重连**"：客户端开机后一直跑，
    hub 重启、网络切换都不该需要人工干预。
    """

    def __init__(
        self,
        hub_url: str,
        device_id: str,
        token: str = "",
        *,
        shell_enabled: bool = True,
    ) -> None:
        self.hub_url = (hub_url or "").rstrip("/")
        self.device_id = device_id or ""
        self.token = token or ""
        self.shell_enabled = bool(shell_enabled)
        self.sessions = SessionRegistry()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._send_queue: asyncio.Queue[str] | None = None
        self.connected = False
        self.reconnects = 0
        self.last_error = ""
        # 每条 sid 对应"当前正在跑的命令的完成回调"（给非流式调用方用）
        self._waiters: dict[str, asyncio.Future] = {}

    # ── 生命周期 ────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.hub_url or not self.device_id:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="zw-termclient", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop).result(timeout=3)
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=4)
        self._thread = None
        try:
            self.sessions.close_all()
        except Exception:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "hub": self.hub_url,
            "device_id": self.device_id,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "sessions": self.sessions.listing(),
        }

    # ── 线程 / 事件循环 ─────────────────────────────────
    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        try:
            loop.run_until_complete(self._run_forever())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._loop = None

    async def _run_forever(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if not self.shell_enabled:
                # 开关关掉：**根本不连**。这是"关闭"真正生效的地方（纵深防御在会话层还有一道）
                await asyncio.sleep(1.0)
                continue
            try:
                await self._connect_and_serve()
                attempt = 0
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                attempt += 1
                self.reconnects += 1
                logger.debug("终端 WS 断开，%.0fs 后重连：%s", delay, exc)
                if await self._sleep(delay):
                    return

    async def _sleep(self, seconds: float) -> bool:
        """可被 stop() 打断的 sleep。返回 True 表示要退出。"""
        step = 0.05
        waited = 0.0
        while waited < seconds:
            if self._stop.is_set():
                return True
            await asyncio.sleep(step)
            waited += step
        return self._stop.is_set()

    async def _connect_and_serve(self) -> None:
        import websockets

        url = self.hub_url.replace("https://", "wss://").replace("http://", "ws://") + "/v1/devices/ws"
        headers = {"X-ZW-Device": self.device_id}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        async with websockets.connect(
            url,
            extra_headers=headers,
            ping_interval=None,          # 自己发应用层 ping，避免与库心跳重复
            open_timeout=10,
            max_size=8 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            self.connected = True
            self.last_error = ""
            logger.info("终端 WS 已连上 hub（%s）", self.hub_url)
            self._send_queue = asyncio.Queue()
            await self._send({"t": "hello", "shell_enabled": self.shell_enabled,
                              "sessions": len(self.sessions.listing())})
            tasks = [
                asyncio.create_task(self._send_loop(ws)),
                asyncio.create_task(self._recv_loop(ws)),
                asyncio.create_task(self._ping_loop()),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.connected = False
                self._ws = None
                self._send_queue = None
                # 连接断了但会话还在：保留，重连后客户端可以按 sid 续上
                logger.info("终端 WS 已断开")

    async def _send(self, payload: dict[str, Any]) -> None:
        q = self._send_queue
        if q is not None:
            await q.put(json.dumps(payload, ensure_ascii=False))

    async def _send_loop(self, ws: Any) -> None:
        while True:
            q = self._send_queue
            if q is None:
                return
            text = await q.get()
            await ws.send(text)

    async def _ping_loop(self) -> None:
        """定时给 hub 发应用层 ping。

        ## 为什么必须有（2026-11-19 云电脑真机）

        hub 侧的 `online()` 判据是"90 秒内收到过这条设备的任何消息"。而两端
        **都不主动发消息**：设备只在**收到** hub 的 ping 时回 pong，hub 也从不发 ping。

        结果：设备安静挂着 90 秒后就被判"实时通道未连" ——
        控制台 `realtime` 显示 False，`/v1/console/term/{id}` 直接回
        `device_offline` 并关连接。**常驻终端因此在正常空闲状态下永远建不起来。**

        `_PING_EVERY` 这个常量早就定义好了，却没有任何地方用它 —— 属于写了一半。
        现在真正用上：每 20 秒发一次，远小于 90 秒窗口。
        """
        try:
            while self.connected:
                await asyncio.sleep(_PING_EVERY)
                if not self.connected:
                    return
                await self._send({"t": "ping"})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 发不出去说明连接要断了，交给外层重连
            return

    async def _recv_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(msg, dict):
                continue
            try:
                await self._handle(msg)
            except Exception as exc:  # noqa: BLE001  单条消息出错不能拖垮连接
                logger.warning("处理终端消息失败：%s", exc)

    # ── 消息处理 ────────────────────────────────────────
    async def _handle(self, msg: dict[str, Any]) -> None:
        kind = str(msg.get("t") or "")
        if kind == "ping":
            await self._send({"t": "pong"})
            return
        if kind == "open":
            self._open(msg)
            return
        if kind == "run":
            self._run(msg)
            return
        if kind == "input":
            sid = str(msg.get("sid") or "")
            sess = self.sessions.get(sid)
            if sess is not None:
                try:
                    sess.write(str(msg.get("data") or ""))
                except SessionError as exc:
                    await self._send({"t": "err", "sid": sid, "code": exc.code, "message": exc.message})
            return
        if kind == "signal":
            sid = str(msg.get("sid") or "")
            sess = self.sessions.get(sid)
            if sess is not None and str(msg.get("kind") or "") == "interrupt":
                sess.interrupt()
            return
        if kind == "close":
            sid = str(msg.get("sid") or "")
            self.sessions.drop(sid)
            return

    def _make_callbacks(self, sid: str):
        """构造会话回调：把输出/结束推回 hub。

        回调在会话的读线程里执行，所以必须用 `call_soon_threadsafe` 交回事件循环 ——
        直接 await 会抛 "no running event loop"。
        """
        loop = self._loop

        def push(payload: dict[str, Any]) -> None:
            if loop is None or loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(self._send(payload)))
            except RuntimeError:
                pass

        def on_output(data: str) -> None:
            if data:
                push({"t": "out", "sid": sid, "data": data})

        def on_done(rc: int, cwd: str, timed_out: bool = False) -> None:
            sess = self.sessions.get(sid)
            push({
                "t": "done", "sid": sid, "rc": rc, "cwd": cwd,
                "truncated": bool(sess.truncated) if sess else False,
                "timed_out": bool(timed_out),
            })

        def on_exit(rc: int) -> None:
            push({"t": "exit", "sid": sid, "rc": rc})
            self.sessions.drop(sid)

        return on_output, on_done, on_exit

    def _open(self, msg: dict[str, Any]) -> None:
        sid = str(msg.get("sid") or "")
        if not sid:
            return
        out_cb, done_cb, exit_cb = self._make_callbacks(sid)
        try:
            sess = self.sessions.create(
                sid,
                cwd=str(msg.get("cwd") or ""),
                timeout=float(msg.get("timeout") or 0),
                on_output=out_cb,
                on_done=done_cb,
                on_exit=exit_cb,
            )
        except SessionError as exc:
            self._fire({"t": "err", "sid": sid, "code": exc.code, "message": exc.message})
            return
        self._fire({"t": "opened", "sid": sid, "cwd": sess.cwd})

    def _run(self, msg: dict[str, Any]) -> None:
        sid = str(msg.get("sid") or "")
        command = str(msg.get("command") or "")
        if not sid or not command:
            return
        sess = self.sessions.get(sid)
        if sess is None or not sess.alive():
            out_cb, done_cb, exit_cb = self._make_callbacks(sid)
            try:
                sess = self.sessions.create(
                    sid,
                    cwd=str(msg.get("cwd") or ""),
                    timeout=float(msg.get("timeout") or 0),
                    on_output=out_cb,
                    on_done=done_cb,
                    on_exit=exit_cb,
                )
            except SessionError as exc:
                self._fire({"t": "err", "sid": sid, "code": exc.code, "message": exc.message})
                return
        try:
            sess.run(command, timeout=msg.get("timeout"))
        except SessionError as exc:
            self._fire({"t": "err", "sid": sid, "code": exc.code, "message": exc.message})

    def _fire(self, payload: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._send(payload)))
        except RuntimeError:
            pass


# ── 全局单例 ──────────────────────────────────────────
_CLIENT: TermClient | None = None
_LOCK = threading.Lock()


def start_client(hub_url: str, device_id: str, token: str = "", shell_enabled: bool = True) -> TermClient | None:
    """启动全局终端 WS 客户端（幂等）。没有 hub 或设备号就不启动。"""
    global _CLIENT
    if not hub_url or not device_id:
        return None
    with _LOCK:
        if _CLIENT is None:
            _CLIENT = TermClient(hub_url, device_id, token, shell_enabled=shell_enabled)
        else:
            _CLIENT.hub_url = hub_url.rstrip("/")
            _CLIENT.device_id = device_id
            _CLIENT.token = token
            _CLIENT.shell_enabled = shell_enabled
        _CLIENT.start()
        return _CLIENT




def stop_client() -> None:
    global _CLIENT
    with _LOCK:
        if _CLIENT is not None:
            _CLIENT.stop()
            _CLIENT = None

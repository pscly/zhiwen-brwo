"""CDP 客户端与 cookie / localStorage 桥。

为什么必须走 CDP 而不能直接读文件：
Chromium 的 `Cookies` 是 OS 密钥加密的密文（Windows DPAPI / macOS Keychain），
换机器就解不开。而**运行中的浏览器内存里 cookie 是明文**，用 CDP 取出来即可跨平台搬运。

这里只依赖 `websockets`，不引入 Playwright —— 依赖越少，Windows 上越好装。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from typing import Any

import websockets

# 写回时允许保留的字段白名单（CDP 不接受多余字段）
_COOKIE_FIELDS = (
    "name", "value", "domain", "path", "expires", "httpOnly", "secure",
    "sameSite", "priority", "sameParty", "sourceScheme", "sourcePort", "partitionKey",
)


class CDPError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CDP:
    """极简 CDP 客户端。

    坑（必读）：浏览器级 ws 端点（`/devtools/browser/<GUID>`）上**没有** `Network` / `Page` /
    `Runtime` 这些域 —— 它们属于"页面会话"。直接对浏览器端点发 `Network.enable` 会得到
    `'Network.enable' wasn't found (-32601)`。
    所以连接后必须 `Target.attachToTarget(flatten=true)` 拿一个 sessionId，后续页面级命令都要带它。
    """

    # 这些域属于浏览器级端点，不需要 sessionId
    BROWSER_LEVEL = ("Target.", "Browser.", "Storage.", "SystemInfo.", "Schema.")

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._ws: Any = None
        self.session_id: str | None = None
        self.target_id: str | None = None

    async def __aenter__(self) -> "CDP":
        try:
            self._ws = await websockets.connect(
                self.ws_url, max_size=128 * 1024 * 1024, open_timeout=self.timeout, close_timeout=3
            )
        except Exception as exc:
            raise CDPError("CDP_CONNECT_FAILED", f"连不上浏览器的调试端口：{exc}") from exc
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._ws is not None:
            try:
                if self.session_id:
                    with contextlib.suppress(Exception):
                        await self._send_nowait("Target.detachFromTarget", {"sessionId": self.session_id})
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _send_nowait(self, method: str, params: dict[str, Any]) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps({"id": next(self._ids), "method": method, "params": params}))

    async def ensure_session(self) -> str:
        """确保已挂上一个页面会话（没有页面就新建一个）。"""
        if self.session_id:
            return self.session_id
        targets = (await self.call("Target.getTargets")).get("targetInfos", [])
        pages = [t for t in targets if t.get("type") == "page"]
        target_id = pages[0]["targetId"] if pages else (await self.call("Target.createTarget", {"url": "about:blank"}))[
            "targetId"
        ]
        res = await self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self.session_id = res["sessionId"]
        self.target_id = target_id
        return self.session_id

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if self._ws is None:
            raise CDPError("CDP_NOT_CONNECTED", "CDP 连接尚未建立")
        message: dict[str, Any] = {"id": next(self._ids), "method": method, "params": params or {}}
        if not method.startswith(self.BROWSER_LEVEL):
            message["sessionId"] = await self.ensure_session()
        msg_id = message["id"]
        await self._ws.send(json.dumps(message))
        deadline = asyncio.get_event_loop().time() + (timeout or self.timeout)
        while True:
            remain = deadline - asyncio.get_event_loop().time()
            if remain <= 0:
                raise CDPError("CDP_TIMEOUT", f"CDP 命令 {method} 超时")
            raw = await asyncio.wait_for(self._ws.recv(), timeout=remain)
            data = json.loads(raw)
            if data.get("id") != msg_id:
                continue  # 事件（含其他 session 的消息），忽略
            if "error" in data:
                err = data["error"]
                raise CDPError("CDP_COMMAND_FAILED", f"{method} 失败：{err.get('message')}（code {err.get('code')}）")
            return data.get("result", {})


# ── cookie ────────────────────────────────────────────────
async def _dump_cookies(cdp: CDP) -> list[dict[str, Any]]:
    await cdp.call("Network.enable")
    res = await cdp.call("Network.getAllCookies", timeout=60)
    return res.get("cookies", [])


async def dump_cookies(ws_url: str) -> list[dict[str, Any]]:
    async with CDP(ws_url) as cdp:
        return await _dump_cookies(cdp)


async def _load_cookies(cdp: CDP, cookies: list[dict[str, Any]], clear: bool = True) -> dict[str, int]:
    await cdp.call("Network.enable")
    if clear:
        await cdp.call("Network.clearBrowserCookies")

    ok = 0
    fail = 0
    expired = 0
    failures: list[str] = []

    def sanitize(c: dict[str, Any]) -> dict[str, Any] | None:
        out = {k: c[k] for k in _COOKIE_FIELDS if k in c and c[k] is not None}
        # sameSite 只接受 Strict/Lax/None；空串会被 CDP 拒
        if out.get("sameSite") not in ("Strict", "Lax", "None"):
            out.pop("sameSite", None)
        # None 必须同时 secure
        if out.get("sameSite") == "None" and not out.get("secure"):
            out["secure"] = True
        # 缺少 name/domain 的直接跳过
        if not out.get("name") or not out.get("domain"):
            return None
        return out

    valid: list[dict[str, Any]] = []
    now = 0.0
    import time as _t

    now = _t.time()
    for c in cookies:
        if not c.get("session") and c.get("expires") and float(c["expires"]) > 0 and float(c["expires"]) < now:
            expired += 1
            continue
        s = sanitize(c)
        if s is None:
            fail += 1
            failures.append(f"字段不全：{c.get('name')!r}")
            continue
        valid.append(s)

    for i in range(0, len(valid), 50):
        batch = valid[i : i + 50]
        try:
            await cdp.call("Network.setCookies", {"cookies": batch}, timeout=60)
            ok += len(batch)
        except CDPError:
            for c in batch:
                try:
                    await cdp.call("Network.setCookies", {"cookies": [c]}, timeout=30)
                    ok += 1
                except CDPError as exc:
                    fail += 1
                    failures.append(f"{c.get('domain')} {c.get('name')}: {exc.message}")
    return {"ok": ok, "fail": fail, "expired": expired, "failures": failures[:20]}


async def load_cookies(ws_url: str, cookies: list[dict[str, Any]], clear: bool = True) -> dict[str, int]:
    async with CDP(ws_url) as cdp:
        return await _load_cookies(cdp, cookies, clear=clear)


# ── localStorage ──────────────────────────────────────────
_READ_LS = "JSON.stringify(Object.fromEntries(Object.entries(localStorage)))"
_READ_SS = "JSON.stringify(Object.fromEntries(Object.entries(sessionStorage)))"


async def _goto(cdp: CDP, url: str) -> None:
    await cdp.call("Page.enable")
    await cdp.call("Page.navigate", {"url": url})
    # 等一个稳定的加载事件，最多 8 秒
    try:
        deadline = asyncio.get_event_loop().time() + 8
        while asyncio.get_event_loop().time() < deadline:
            res = await cdp.call(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
                timeout=5,
            )
            if res.get("result", {}).get("value") == "complete":
                return
            await asyncio.sleep(0.25)
    except CDPError:
        pass


async def _eval_json(cdp: CDP, expr: str) -> Any:
    res = await cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    value = res.get("result", {}).get("value")
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


async def _dump_storage(cdp: CDP, origins: list[str]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for origin in origins:
        try:
            await _goto(cdp, origin)
            out[origin] = await _eval_json(cdp, _READ_LS)
        except CDPError as exc:
            out[origin] = {"__error__": exc.message}
    return out


async def dump_storage(ws_url: str, origins: list[str]) -> dict[str, dict[str, str]]:
    async with CDP(ws_url) as cdp:
        return await _dump_storage(cdp, origins)


async def _load_storage(cdp: CDP, mapping: dict[str, dict[str, str]]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for origin, kv in mapping.items():
        try:
            await _goto(cdp, origin)
            payload = json.dumps(kv, ensure_ascii=False)
            expr = (
                "(() => { const d = %s; let n = 0; "
                "for (const [k, v] of Object.entries(d)) { localStorage.setItem(k, v); n++; } "
                "return n; })()" % payload
            )
            res = await cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            report[origin] = {"written": res.get("result", {}).get("value", 0)}
        except CDPError as exc:
            report[origin] = {"error": exc.message}
    return report


async def load_storage(ws_url: str, mapping: dict[str, dict[str, str]]) -> dict[str, Any]:
    async with CDP(ws_url) as cdp:
        return await _load_storage(cdp, mapping)


# ── 页面能力（给 AI 用）──────────────────────────────────
async def list_targets(ws_url: str) -> list[dict[str, Any]]:
    async with CDP(ws_url) as cdp:
        res = await cdp.call("Target.getTargets")
        return [t for t in res.get("targetInfos", []) if t.get("type") == "page"]


async def navigate(ws_url: str, url: str) -> dict[str, Any]:
    async with CDP(ws_url) as cdp:
        await _goto(cdp, url)
        res = await cdp.call("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
        cur = await cdp.call("Runtime.evaluate", {"expression": "location.href", "returnByValue": True})
        return {
            "url": cur.get("result", {}).get("value", url),
            "title": res.get("result", {}).get("value", ""),
        }


async def evaluate(ws_url: str, expression: str) -> Any:
    async with CDP(ws_url) as cdp:
        res = await cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if res.get("exceptionDetails"):
            raise CDPError("JS_EXCEPTION", str(res["exceptionDetails"].get("text") or res["exceptionDetails"]))
        return res.get("result", {}).get("value")


async def screenshot(ws_url: str, fmt: str = "png", quality: int = 80) -> bytes:
    import base64

    async with CDP(ws_url) as cdp:
        await cdp.call("Page.enable")
        params: dict[str, Any] = {"format": fmt, "captureBeyondViewport": False}
        if fmt in ("jpeg", "webp"):
            params["quality"] = quality
        res = await cdp.call("Page.captureScreenshot", params, timeout=60)
        return base64.b64decode(res.get("data", ""))


async def page_text(ws_url: str) -> str:
    async with CDP(ws_url) as cdp:
        res = await cdp.call(
            "Runtime.evaluate",
            {"expression": "document.body ? document.body.innerText : ''", "returnByValue": True},
        )
        return res.get("result", {}).get("value", "") or ""


async def click(ws_url: str, selector: str | None = None, x: int | None = None, y: int | None = None) -> dict[str, Any]:
    async with CDP(ws_url) as cdp:
        if selector:
            expr = (
                "(() => { const el = document.querySelector(%s); if (!el) return null;"
                " const r = el.getBoundingClientRect();"
                " return {x: r.left + r.width/2, y: r.top + r.height/2}; })()" % json.dumps(selector)
            )
            res = await cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            box = res.get("result", {}).get("value")
            if not box:
                raise CDPError("SELECTOR_NOT_FOUND", f"页面上找不到元素：{selector}")
            x, y = int(box["x"]), int(box["y"])
        if x is None or y is None:
            raise CDPError("INVALID_ARGS", "click 需要 selector 或 (x, y)")
        for kind in ("mousePressed", "mouseReleased"):
            await cdp.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})
            await asyncio.sleep(0.06)
        return {"clicked": True, "x": x, "y": y}


async def type_text(ws_url: str, text: str, selector: str | None = None) -> dict[str, Any]:
    async with CDP(ws_url) as cdp:
        if selector:
            expr = "(() => { const el = document.querySelector(%s); if (el) { el.focus(); return true; } return false; })()" % json.dumps(selector)
            res = await cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            if not res.get("result", {}).get("value"):
                raise CDPError("SELECTOR_NOT_FOUND", f"页面上找不到元素：{selector}")
        for ch in text:
            await cdp.call("Input.insertText", {"text": ch})
            await asyncio.sleep(0.02)
        return {"typed": len(text)}


# ── 同步入口（给同步层用）────────────────────────────────
def run(coro: Any) -> Any:
    """在同步代码里安全地跑异步 CDP 调用（处理已存在事件循环的情况）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # 已经在事件循环里（FastAPI 的 async 路由）——交给独立线程跑，避免嵌套
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ── 同步包装：给同步层这类同步代码用 ─────────────────────
def dump_cookies_sync(ws_url: str) -> list[dict[str, Any]]:
    return run(dump_cookies(ws_url))


def load_cookies_sync(ws_url: str, cookies: list[dict[str, Any]], clear: bool = True) -> dict[str, Any]:
    return run(load_cookies(ws_url, cookies, clear=clear))


def dump_storage_sync(ws_url: str, origins: list[str]) -> dict[str, dict[str, str]]:
    return run(dump_storage(ws_url, origins))


def load_storage_sync(ws_url: str, mapping: dict[str, dict[str, str]]) -> dict[str, Any]:
    return run(load_storage(ws_url, mapping))


def screenshot_sync(ws_url: str, fmt: str = "png") -> bytes:
    return run(screenshot(ws_url, fmt=fmt))


def navigate_sync(ws_url: str, url: str) -> dict[str, Any]:
    return run(navigate(ws_url, url))


def evaluate_sync(ws_url: str, expression: str) -> Any:
    return run(evaluate(ws_url, expression))

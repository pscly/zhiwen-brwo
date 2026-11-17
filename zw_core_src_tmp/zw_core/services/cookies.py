"""CDP 客户端与 cookie / localStorage / 页面操作桥。

为什么必须走 CDP 而不能直接读文件：
Chromium 的 `Cookies` 是 OS 密钥加密的密文（Windows DPAPI / macOS Keychain），
换机器就解不开。而**运行中的浏览器内存里 cookie 是明文**，用 CDP 取出来即可跨平台搬运。

这里只依赖 `websockets`，不引入 Playwright —— 依赖越少，Windows 上越好装。

## 两条必须记住的约束

1. **页面级命令必须挂页面会话。**
   浏览器级 ws 端点（`/devtools/browser/<GUID>`）上没有 `Network`/`Page`/`Runtime`
   这些域，直接发 `Network.enable` 会得到 `'Network.enable' wasn't found (-32601)`。
   所以连接后要 `Target.attachToTarget(flatten=true)` 拿 sessionId。

2. **绝不在用户正在看的标签页里做"会改变页面"的操作。**
   0.1.0 读写 localStorage 时会把你当前打开的页导航到目标 origin —— 等于把你正在
   看的页面刷掉。现在页面类操作（导航/截图/执行 JS/localStorage）统一跑在一个
   **专属管理标签页**里：本进程首次需要时 `Target.createTarget` 新建一个，
   之后就复用它。cookie 读写只 attach 不导航，所以允许复用任意页面。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from typing import Any

import websockets

# 写回时允许保留的字段白名单（CDP 不接受多余字段）
_COOKIE_FIELDS = (
    "name", "value", "domain", "path", "expires", "httpOnly", "secure",
    "sameSite", "priority", "sameParty", "sourceScheme", "sourcePort", "partitionKey",
)

# 管理标签页：{profile 键: targetId}。进程内记忆，core 重启后重新建一个。
_MANAGED: dict[str, str] = {}


class CDPError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class CDP:
    """极简 CDP 客户端（单连接、命令串行）。"""

    # 这些域属于浏览器级端点，不需要 sessionId
    BROWSER_LEVEL = ("Target.", "Browser.", "Storage.", "SystemInfo.", "Schema.")

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        self.ws_url = ws_url
        self.timeout = timeout
        self._seq = 0
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
        self._seq += 1
        await self._ws.send(json.dumps({"id": self._seq, "method": method, "params": params}))

    async def targets(self) -> list[dict[str, Any]]:
        return (await self.call("Target.getTargets")).get("targetInfos", [])

    async def attach(self, target_id: str) -> str:
        res = await self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        self.session_id = res["sessionId"]
        self.target_id = target_id
        return self.session_id

    async def ensure_session(self) -> str:
        """确保已挂上一个页面会话。优先复用管理页，其次任意页面，最后新建。"""
        if self.session_id:
            return self.session_id
        pages = [t for t in await self.targets() if t.get("type") == "page"]
        managed = _MANAGED.get(self.ws_url)
        for t in pages:
            if managed and t.get("targetId") == managed:
                return await self.attach(t["targetId"])
        if pages:
            return await self.attach(pages[0]["targetId"])
        res = await self.call("Target.createTarget", {"url": "about:blank"})
        return await self.attach(res["targetId"])

    async def managed_session(self) -> str:
        """挂到专属管理标签页（必要时新建）。页面类操作一律走这里。"""
        if self.session_id and self.target_id and _MANAGED.get(self.ws_url) == self.target_id:
            return self.session_id
        pages = [t for t in await self.targets() if t.get("type") == "page"]
        desired = _MANAGED.get(self.ws_url)
        for t in pages:
            if desired and t.get("targetId") == desired:
                return await self.attach(t["targetId"])
        if desired is None:
            # 没有管理页时，找一个 about:blank 的空页来认领，避免无谓地多开标签
            for t in pages:
                if (t.get("url") or "").startswith("about:blank"):
                    _MANAGED[self.ws_url] = t["targetId"]
                    return await self.attach(t["targetId"])
        res = await self.call("Target.createTarget", {"url": "about:blank"})
        _MANAGED[self.ws_url] = res["targetId"]
        return await self.attach(res["targetId"])

    async def call(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        if self._ws is None:
            raise CDPError("CDP_NOT_CONNECTED", "CDP 连接尚未建立")
        self._seq += 1
        msg_id = self._seq
        message: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if not method.startswith(self.BROWSER_LEVEL):
            message["sessionId"] = await self.ensure_session()
        await self._ws.send(json.dumps(message))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout or self.timeout)
        while True:
            remain = deadline - loop.time()
            if remain <= 0:
                raise CDPError("CDP_TIMEOUT", f"CDP 命令 {method} 超时（{(timeout or self.timeout):.0f}s）")
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


def _sanitize_cookie(c: dict[str, Any]) -> dict[str, Any] | None:
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


async def _load_cookies(cdp: CDP, cookies: list[dict[str, Any]], clear: bool = True) -> dict[str, Any]:
    await cdp.call("Network.enable")
    if clear:
        await cdp.call("Network.clearBrowserCookies")

    ok = 0
    fail = 0
    expired = 0
    failures: list[str] = []

    valid: list[dict[str, Any]] = []
    now = time.time()
    for c in cookies:
        if not c.get("session") and c.get("expires") and float(c["expires"]) > 0 and float(c["expires"]) < now:
            expired += 1
            continue
        s = _sanitize_cookie(c)
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


async def load_cookies(ws_url: str, cookies: list[dict[str, Any]], clear: bool = True) -> dict[str, Any]:
    async with CDP(ws_url) as cdp:
        return await _load_cookies(cdp, cookies, clear=clear)


async def clear_cookies(ws_url: str) -> dict[str, Any]:
    async with CDP(ws_url) as cdp:
        await cdp.call("Network.enable")
        await cdp.call("Network.clearBrowserCookies")
        left = len((await cdp.call("Network.getAllCookies")).get("cookies", []))
        return {"cleared": True, "remaining": left}


# ── 管理标签页 ────────────────────────────────────────────
_READ_LS = "JSON.stringify(Object.fromEntries(Object.entries(localStorage)))"


async def _goto(cdp: CDP, url: str, wait_seconds: float = 12.0) -> dict[str, Any]:
    """在**管理标签页**里导航并等待加载完成。"""
    await cdp.managed_session()
    await cdp.call("Page.enable")
    nav = await cdp.call("Page.navigate", {"url": url})
    if nav.get("errorText"):
        raise CDPError("NAVIGATE_FAILED", f"导航到 {url} 失败：{nav['errorText']}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while loop.time() < deadline:
        res = await cdp.call("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True}, timeout=5)
        if res.get("result", {}).get("value") == "complete":
            break
        await asyncio.sleep(0.2)
    cur = await cdp.call("Runtime.evaluate", {"expression": "location.href", "returnByValue": True}, timeout=5)
    return {"url": cur.get("result", {}).get("value") or url, "frame_id": nav.get("frameId", "")}


async def _eval_json(cdp: CDP, expr: str) -> Any:
    res = await cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
    if res.get("exceptionDetails"):
        raise CDPError("JS_EXCEPTION", _exception_text(res["exceptionDetails"]))
    value = res.get("result", {}).get("value")
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _exception_text(details: dict[str, Any]) -> str:
    parts = [str(details.get("text") or "")]
    exc = details.get("exception") or {}
    if exc.get("description"):
        parts.append(str(exc["description"]))
    elif exc.get("value") is not None:
        parts.append(str(exc["value"]))
    return "：".join([p for p in parts if p]) or "页面里执行出错"


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
            payload = json.dumps({k: str(v) for k, v in kv.items()}, ensure_ascii=False)
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
        return [t for t in await cdp.targets() if t.get("type") == "page"]


async def navigate(ws_url: str, url: str) -> dict[str, Any]:
    async with CDP(ws_url) as cdp:
        got = await _goto(cdp, url)
        res = await cdp.call("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
        return {"url": got["url"], "title": res.get("result", {}).get("value", ""), "target_id": cdp.target_id or ""}


async def evaluate(ws_url: str, expression: str) -> Any:
    async with CDP(ws_url) as cdp:
        await cdp.managed_session()
        res = await cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if res.get("exceptionDetails"):
            raise CDPError("JS_EXCEPTION", _exception_text(res["exceptionDetails"]))
        return res.get("result", {}).get("value")


async def screenshot(ws_url: str, fmt: str = "png", quality: int = 80, full_page: bool = False) -> bytes:
    async with CDP(ws_url) as cdp:
        await cdp.managed_session()
        await cdp.call("Page.enable")
        params: dict[str, Any] = {"format": fmt, "captureBeyondViewport": bool(full_page)}
        if fmt in ("jpeg", "webp"):
            params["quality"] = quality
        res = await cdp.call("Page.captureScreenshot", params, timeout=60)
        return base64.b64decode(res.get("data", ""))


async def page_text(ws_url: str) -> str:
    async with CDP(ws_url) as cdp:
        await cdp.managed_session()
        res = await cdp.call(
            "Runtime.evaluate",
            {"expression": "document.body ? document.body.innerText : ''", "returnByValue": True},
        )
        return res.get("result", {}).get("value", "") or ""


async def click(ws_url: str, selector: str | None = None, x: int | None = None, y: int | None = None) -> dict[str, Any]:
    async with CDP(ws_url) as cdp:
        await cdp.managed_session()
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
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            await cdp.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})
            await asyncio.sleep(0.06)
        return {"clicked": True, "x": x, "y": y}


async def type_text(ws_url: str, text: str, selector: str | None = None, delay: float = 0.02) -> dict[str, Any]:
    async with CDP(ws_url) as cdp:
        await cdp.managed_session()
        if selector:
            expr = "(() => { const el = document.querySelector(%s); if (el) { el.focus(); return true; } return false; })()" % json.dumps(selector)
            res = await cdp.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            if not res.get("result", {}).get("value"):
                raise CDPError("SELECTOR_NOT_FOUND", f"页面上找不到元素：{selector}")
        for ch in text:
            await cdp.call("Input.insertText", {"text": ch})
            if delay:
                await asyncio.sleep(delay)
        return {"typed": len(text)}


# ── 指纹采样 ──────────────────────────────────────────────
_PROBE_JS = r"""
(() => {
  const safe = fn => { try { return fn(); } catch (e) { return null; } };
  const uaData = safe(() => {
    if (!navigator.userAgentData) return null;
    return {
      brands: navigator.userAgentData.brands,
      mobile: navigator.userAgentData.mobile,
      platform: navigator.userAgentData.platform,
    };
  });
  const gl = safe(() => {
    const c = document.createElement('canvas');
    const ctx = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!ctx) return null;
    const dbg = ctx.getExtension('WEBGL_debug_renderer_info');
    return {
      vendor: dbg ? ctx.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : ctx.getParameter(ctx.VENDOR),
      renderer: dbg ? ctx.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : ctx.getParameter(ctx.RENDERER),
    };
  });
  const canvasHash = safe(() => {
    const c = document.createElement('canvas');
    c.width = 220; c.height = 60;
    const ctx = c.getContext('2d');
    ctx.textBaseline = 'top';
    ctx.font = '16px Arial';
    ctx.fillStyle = '#f60';
    ctx.fillRect(10, 10, 160, 30);
    ctx.fillStyle = '#069';
    ctx.fillText('知纹 zhiwen 1234', 12, 14);
    const data = c.toDataURL();
    let h = 0;
    for (let i = 0; i < data.length; i++) { h = ((h << 5) - h + data.charCodeAt(i)) | 0; }
    return (h >>> 0).toString(16);
  });
  const audioHash = new Promise(resolve => {
    try {
      const AC = window.OfflineAudioContext || window.webkitOfflineAudioContext;
      if (!AC) return resolve(null);
      const ctx = new AC(1, 44100, 44100);
      const osc = ctx.createOscillator();
      osc.type = 'triangle';
      osc.frequency.value = 10000;
      const comp = ctx.createDynamicsCompressor();
      osc.connect(comp); comp.connect(ctx.destination);
      osc.start(0);
      ctx.startRendering();
      ctx.oncomplete = ev => {
        const buf = ev.renderedBuffer.getChannelData(0);
        let sum = 0;
        for (let i = 4500; i < 5000; i++) sum += Math.abs(buf[i]);
        resolve(sum.toFixed(6));
      };
    } catch (e) { resolve(null); }
  });
  return audioHash.then(audio => ({
    ua: navigator.userAgent,
    platform: navigator.platform,
    vendor: navigator.vendor,
    ua_data: uaData,
    timezone: safe(() => Intl.DateTimeFormat().resolvedOptions().timeZone),
    timezone_offset: new Date().getTimezoneOffset(),
    languages: safe(() => Array.from(navigator.languages || [])),
    lang: navigator.language,
    hardware_concurrency: navigator.hardwareConcurrency,
    device_memory: safe(() => navigator.deviceMemory || null),
    webgl_vendor: gl ? gl.vendor : null,
    webgl_renderer: gl ? gl.renderer : null,
    canvas_hash: canvasHash,
    audio_hash: audio,
    webdriver: safe(() => navigator.webdriver),
    screen: safe(() => ({
      width: screen.width, height: screen.height,
      colorDepth: screen.colorDepth,
      availWidth: screen.availWidth, availHeight: screen.availHeight,
    })),
    outer: safe(() => ({ w: outerWidth, h: outerHeight, ratio: devicePixelRatio })),
  }));
})()
"""


async def probe_fingerprint(ws_url: str) -> dict[str, Any]:
    """在管理标签页里读真机指纹。拿不到的字段给 null，不省略键。"""
    async with CDP(ws_url) as cdp:
        await cdp.managed_session()
        res = await cdp.call(
            "Runtime.evaluate",
            {"expression": _PROBE_JS, "returnByValue": True, "awaitPromise": True},
            timeout=45,
        )
        if res.get("exceptionDetails"):
            raise CDPError("JS_EXCEPTION", _exception_text(res["exceptionDetails"]))
        value = res.get("result", {}).get("value") or {}
        # 字体探测：用 30 个常见字体做宽度差分，粗略计数
        fonts = await cdp.call(
            "Runtime.evaluate",
            {
                "expression": """(() => {
                  const bases = ['monospace','sans-serif','serif'];
                  const probe = ['Arial','Helvetica','Times New Roman','Courier New','Verdana','Tahoma',
                    'Trebuchet MS','Georgia','Garamond','Comic Sans MS','Impact','Palatino','Calibri','Cambria',
                    'Consolas','Segoe UI','Microsoft YaHei','SimSun','SimHei','PingFang SC','Hiragino Sans GB',
                    'Noto Sans CJK SC','Source Han Sans SC','WenQuanYi Micro Hei','DejaVu Sans','Liberation Sans',
                    'Ubuntu','Roboto','Arial Black','Lucida Console'];
                  const span = document.createElement('span');
                  span.style.cssText = 'position:absolute;left:-9999px;font-size:72px';
                  span.textContent = 'mmmmmmmmmmlli';
                  document.body.appendChild(span);
                  const base = {};
                  for (const b of bases) { span.style.fontFamily = b; base[b] = span.offsetWidth; }
                  let n = 0;
                  for (const f of probe) {
                    for (const b of bases) {
                      span.style.fontFamily = `'${f}',${b}`;
                      if (span.offsetWidth !== base[b]) { n++; break; }
                    }
                  }
                  span.remove();
                  return n;
                })()""",
                "returnByValue": True,
            },
            timeout=20,
        )
        value["fonts_count"] = fonts.get("result", {}).get("value")
        return value


# ── 同步入口（给同步层用）────────────────────────────────
def run(coro: Any) -> Any:
    """在同步代码里安全地跑异步 CDP 调用（处理已存在事件循环的情况）。"""
    try:
        asyncio.get_running_loop()
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


def clear_cookies_sync(ws_url: str) -> dict[str, Any]:
    return run(clear_cookies(ws_url))


def dump_storage_sync(ws_url: str, origins: list[str]) -> dict[str, dict[str, str]]:
    return run(dump_storage(ws_url, origins))


def load_storage_sync(ws_url: str, mapping: dict[str, dict[str, str]]) -> dict[str, Any]:
    return run(load_storage(ws_url, mapping))


def screenshot_sync(ws_url: str, fmt: str = "png", full_page: bool = False) -> bytes:
    return run(screenshot(ws_url, fmt=fmt, full_page=full_page))


async def open_tabs(ws_url: str, urls: list[str]) -> dict[str, Any]:
    """在浏览器里逐个开标签（每个 URL 一个新标签）。

    为什么需要：`--headless=new` **不接受命令行传多个网址**
    （会直接退出，报 "Multiple targets are not supported in headless mode"）。
    而"恢复上次标签 + 追加检测页"天然是多网址，所以无头模式下只能靠 CDP 补开。
    """
    opened: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    if not urls:
        return {"opened": opened, "failed": failed}
    conn = CDP(ws_url)
    try:
        await conn.connect()
        for url in urls:
            try:
                res = await conn.call("Target.createTarget", {"url": url})
                opened.append({"url": url, "target_id": res.get("targetId", "")})
            except Exception as exc:  # noqa: BLE001  单个失败不影响其它
                failed.append({"url": url, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        await conn.close()
    return {"opened": opened, "failed": failed}


def open_tabs_sync(ws_url: str, urls: list[str]) -> dict[str, Any]:
    return run(open_tabs(ws_url, urls))


def navigate_sync(ws_url: str, url: str) -> dict[str, Any]:
    return run(navigate(ws_url, url))


def evaluate_sync(ws_url: str, expression: str) -> Any:
    return run(evaluate(ws_url, expression))


def probe_fingerprint_sync(ws_url: str) -> dict[str, Any]:
    return run(probe_fingerprint(ws_url))

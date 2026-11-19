"""实测"这个内核到底会不会加载命令行传入的扩展"。

## 为什么必须实测，不能靠版本号

一开始我用"版本 ≥137 就不支持"来判断，结果**判错了**：

| 内核 | 版本 | 实际结果 |
|---|---|---|
| `/usr/bin/google-chrome`（正式版） | 149.0.7827.53 | 扩展**不加载**（内容脚本不执行） |
| `/opt/…/ungoogled-chromium-150…/chrome` | 150.0.7871.186 | 扩展**正常加载**（内容脚本执行） |

同一个 `--load-extension`，正式版被挡住、ungoogled 照常работа。版本号一样在
137 以上，结论却相反 —— 所以判据只能是"跑一次看看"。

## 还有一个坑：不能用 chrome-extension:// 目标判断

我最初去看 `/json/list` 里有没有 `chrome-extension://<id>` 目标，ungoogled 上
**没有**这个目标（MV3 的 service worker 不一定被列出），但扩展其实生效了。
唯一可靠的信号是**内容脚本有没有在页面里留下痕迹**。

## 做法

1. 造一个一次性探针扩展：manifest v3 + 一条 content_script，把 document.title 改掉；
2. 本地起一个只返回一个空页面的 HTTP 服务（content_script 不匹配 `about:blank`/`data:`，
   所以必须给它一个真的 http 页面）；
3. 无头启动内核，带 `--load-extension=<探针>` 与调试端口；
4. 用 CDP 导航到那个页面，读 `document.title`；
5. 看到标记 = 会加载；超时/看不到 = 不会。

结果按 (路径, mtime) 缓存在数据目录里，同一个内核只探一次（约 2–4 秒）。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from ..config import get_settings

logger = logging.getLogger("zw.extprobe")

PROBE_TITLE = "ZW-EXT-PROBE-OK"
_CACHE_FILE = "extension-probe.json"
_MANIFEST = {
    "manifest_version": 3,
    "name": "ZW Extension Probe",
    "version": "1.0",
    "content_scripts": [{
        "matches": ["http://127.0.0.1/*"],
        "js": ["probe.js"],
        "run_at": "document_end",
    }],
}
_PROBE_JS = f"document.title = {PROBE_TITLE!r};\n"


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _cache_path() -> Path:
    return get_settings().root / _CACHE_FILE


def _load_cache() -> dict[str, Any]:
    p = _cache_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(data: dict[str, Any]) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        logger.debug("扩展探测缓存写入失败", exc_info=True)


def _cache_key(bin_path: Path) -> str:
    try:
        st = bin_path.stat()
        return f"{bin_path.resolve()}|{int(st.st_mtime)}|{st.st_size}"
    except OSError:
        return f"{bin_path}|missing"


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"<!doctype html><html><head><title>probe</title></head><body>probe</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a: Any) -> None:  # 静音
        return


def _start_page_server() -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", 0), _PageHandler)
    port = int(srv.server_address[1])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


def _wait_json(url: str, timeout: float) -> dict[str, Any] | None:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:  # noqa: S310  固定本机地址
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            time.sleep(0.3)
    return None


def peek(engine_path: Path | str) -> dict[str, Any] | None:
    """**只读缓存**，不启动浏览器。没有缓存时返回 None。

    为什么需要它：`GET /api/v1/engine` 这种状态接口不能同步跑探针 ——
    探针要真启动一次浏览器，慢的时候 10–25 秒，界面会像卡死一样
    （实测把 ui-check 的直接拖到 120s 超时）。
    """
    bin_path = Path(str(engine_path)).expanduser()
    if not bin_path.exists():
        return None
    hit = _load_cache().get(_cache_key(bin_path))
    return dict(hit) if isinstance(hit, dict) and "loadable" in hit else None


def probe_async(engine_path: Path | str, *, timeout: float = 25.0) -> None:
    """后台跑一次探针把结果写进缓存，立刻返回。"""
    import threading

    def _run() -> None:
        try:
            probe(engine_path, timeout=timeout, use_cache=True)
        except Exception:  # noqa: BLE001 —— 后台任务失败只影响"下次能否显示"
            pass

    threading.Thread(target=_run, name="zw-extprobe", daemon=True).start()


def probe(engine_path: Path | str, *, timeout: float = 25.0, use_cache: bool = True) -> dict[str, Any]:
    """实测这个内核会不会加载 `--load-extension`。返回 {loadable, reason, cached, ms}。"""
    bin_path = Path(str(engine_path)).expanduser()
    if not bin_path.exists():
        return {"loadable": False, "reason": f"内核文件不存在：{bin_path}", "cached": False, "ms": 0}

    key = _cache_key(bin_path)
    if use_cache:
        hit = _load_cache().get(key)
        if isinstance(hit, dict) and "loadable" in hit:
            return {**hit, "cached": True}

    started = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="zw-extprobe-"))
    ext_dir = tmp / "ext"
    udd = tmp / "udd"
    srv = None
    proc = None
    try:
        ext_dir.mkdir(parents=True, exist_ok=True)
        udd.mkdir(parents=True, exist_ok=True)
        (ext_dir / "manifest.json").write_text(json.dumps(_MANIFEST), encoding="utf-8")
        (ext_dir / "probe.js").write_text(_PROBE_JS, encoding="utf-8")

        srv, page_port = _start_page_server()
        dbg_port = _free_port()
        cmd = [
            str(bin_path), "--headless=new", "--no-sandbox", "--disable-gpu",
            f"--user-data-dir={udd}", f"--load-extension={ext_dir}",
            f"--remote-debugging-port={dbg_port}", "about:blank",
        ]
        from .proc import popen_hidden

        proc = popen_hidden(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ver = _wait_json(f"http://127.0.0.1:{dbg_port}/json/version", timeout=min(15.0, timeout))
        if not ver:
            return {"loadable": False,
                    "reason": "内核没起来（调试端口连不上），无法判断；已按不加载处理",
                    "cached": False, "ms": int((time.time() - started) * 1000)}

        targets = _wait_json(f"http://127.0.0.1:{dbg_port}/json/list", timeout=6.0) or []
        pages = [t for t in targets if t.get("type") == "page"] if isinstance(targets, list) else []
        if not pages:
            return {"loadable": False, "reason": "内核没有页面目标，无法判断；已按不加载处理",
                    "cached": False, "ms": int((time.time() - started) * 1000)}

        from .cookies import run as _run

        async def _check() -> str:
            import websockets

            async with websockets.connect(pages[0]["webSocketDebuggerUrl"],
                                          max_size=None, open_timeout=8) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Page.navigate",
                                          "params": {"url": f"http://127.0.0.1:{page_port}/"}}))
                deadline = time.time() + 10
                while time.time() < deadline:
                    await ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                                              "params": {"expression": "document.title",
                                                         "returnByValue": True}}))
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    if msg.get("id") == 2:
                        title = (msg.get("result") or {}).get("result", {}).get("value")
                        if title == PROBE_TITLE:
                            return PROBE_TITLE
                        if title and title != "probe":
                            return str(title)
                    await _sleep(0.4)
                return ""

        async def _sleep(sec: float) -> None:
            import asyncio

            await asyncio.sleep(sec)

        title = _run(_check()) or ""
        loadable = title == PROBE_TITLE
        reason = ("实测：这个内核会加载命令行传入的扩展" if loadable else
                  "实测：这个内核忽略 --load-extension（内容脚本没有执行），"
                  "扩展的代码与数据会带进环境，但浏览器不会加载它")
        out = {"loadable": loadable, "reason": reason, "cached": False,
               "ms": int((time.time() - started) * 1000)}
        # 正反都缓存：结论对同一个二进制（路径+大小+mtime）是确定性的，
        # 而"不支持"这条路要等满 10 秒超时，不缓存的话每次 apply 都卡 10 秒。
        data = _load_cache()
        data[key] = {"loadable": loadable, "reason": reason}
        _save_cache(data)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("扩展探测失败", exc_info=True)
        # **出错时给"未知"（None），不能给 False。**
        #
        # 实测（2026-11-19，云电脑 Windows，内核 ungoogled-157.0.8103.78）：
        # 探测抛 `PermissionError: [WinError 5] 拒绝访问`（杀软/权限拦住了
        # 探针进程），旧代码返回 `loadable: False` —— 于是界面显示
        # 「不能加载扩展」，用户以为插件坏了，实际是**探测根本没跑成**。
        #
        # 三态必须保住：True=能 / False=不能 / None=不知道。
        # 而且在 finally 里也不写缓存（见 `_save_cache` 只在成功路径调用），
        # 这样下次还能重试 —— 旧代码把 False 缓存住，之后永远显示"不能加载"。
        return {"loadable": None,
                "reason": f"探测没能完成（{type(exc).__name__}: {exc}），"
                          f"无法判断这个内核能否加载扩展；下次会重试",
                "cached": False, "ms": int((time.time() - started) * 1000)}
    finally:
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

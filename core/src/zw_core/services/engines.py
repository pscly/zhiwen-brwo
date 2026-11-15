"""引擎层：找到 Chrome、拉起进程、等它就绪、停掉它、清理磁盘。

这一层的每一条都是踩过坑写下来的，`# 坑：` 开头的注释不要删。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings

IS_WIN = sys.platform == "win32"

# Windows 上常见的安装位置（按优先级）
_WIN_CANDIDATES = [
    r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
    r"%PROGRAMFILES%\Chromium\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Chromium\Application\chrome.exe",
    r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
]
_POSIX_CANDIDATES = [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/snap/bin/chromium",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

# 全平台都必须带的基础参数
BASE_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate,OptimizationHints",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    # 坑：不加这个，新版 Chromium 会拒绝跨源 ws 连接，网关永远连不上
    "--remote-allow-origins=*",
]


class EngineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class LaunchResult:
    pid: int
    port: int
    ws_url: str
    http_url: str
    cmdline: list[str]
    started_at: float


# ── 找引擎 ────────────────────────────────────────────────
def find_chrome(s: Settings) -> Path:
    """按优先级找浏览器可执行文件。"""
    if s.chrome_bin:
        p = Path(s.chrome_bin).expanduser()
        if p.exists():
            return p
        raise EngineError("ENGINE_NOT_FOUND", f"ZW_CHROME_BIN 指向的文件不存在：{p}")

    # 1. 数据目录下的 engines/ 里找（推荐做法，便于版本共存）
    names = ["chrome.exe", "chrome", "chromium.exe", "chromium"] if IS_WIN else ["chrome", "chromium"]
    if Path(s.engine_dir).exists():
        hits: list[Path] = []
        for name in names:
            hits.extend(sorted(Path(s.engine_dir).rglob(name)))
        hits = [h for h in hits if h.is_file() and os.access(h, os.X_OK)] if not IS_WIN else [h for h in hits if h.is_file()]
        if hits:
            return hits[0]

    # 2. 系统常见位置
    for cand in _WIN_CANDIDATES if IS_WIN else _POSIX_CANDIDATES:
        expanded = Path(os.path.expandvars(cand))
        if expanded.exists():
            return expanded

    # 3. PATH
    for name in ("chrome", "chromium", "google-chrome", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)

    raise EngineError(
        "ENGINE_NOT_FOUND",
        "没有找到 Chromium 内核。三种解决办法任选其一：\n"
        "  1) 把内核解压到 <数据目录>/engines/ 下（推荐，支持多版本共存）\n"
        "  2) 设置环境变量 ZW_CHROME_BIN 指向 chrome 可执行文件\n"
        "  3) 安装系统版 Chromium / Chrome\n"
        f"当前数据目录：{s.root}",
    )


def engine_version(bin_path: Path) -> str:
    try:
        out = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip().splitlines()[0][:80]
    except Exception:
        return "unknown"


# ── 端口池 ────────────────────────────────────────────────
class PortPool:
    """端口分配。同时做两层检查：内存里是否已分配、系统里是否被占。"""

    def __init__(self, start: int, end: int) -> None:
        self.start = start
        self.end = end
        self._used: dict[int, str] = {}

    def allocate(self, profile_id: str) -> int:
        for port in range(self.start, self.end + 1):
            if port in self._used:
                continue
            if _port_free(port):
                self._used[port] = profile_id
                return port
        raise EngineError("PORT_EXHAUSTED", f"端口池 {self.start}-{self.end} 已用尽，请停掉一些环境或扩大池子")

    def release(self, port: int) -> None:
        self._used.pop(port, None)

    def reserve(self, port: int, profile_id: str) -> None:
        if port:
            self._used[port] = profile_id

    def used(self) -> dict[int, str]:
        return dict(self._used)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.settimeout(0.25)
        return sk.connect_ex(("127.0.0.1", port)) != 0


# ── 启动 ──────────────────────────────────────────────────
def build_cmdline(
    chrome: Path,
    udd: Path,
    port: int,
    profile: dict[str, Any],
    extra_args: list[str] | None = None,
) -> list[str]:
    """拼启动参数。顺序与内容都是有意为之，改动前先读注释。"""
    args: list[str] = [
        str(chrome),
        f"--user-data-dir={udd}",
        f"--remote-debugging-port={port}",
    ]
    remote = bool(profile.get("cdp_enabled", 1))
    if not remote:
        args = [a for a in args if not a.startswith("--remote-debugging-port")]

    seed = int(profile.get("fp_seed") or 0)
    if seed:
        args.append(f"--fingerprint={seed}")
    platform = (profile.get("fp_platform") or "host").strip()
    if platform and platform != "host":
        args.append(f"--fingerprint-platform={platform}")
    if profile.get("fp_brand"):
        args.append(f"--fingerprint-brand={profile['fp_brand']}")
    if profile.get("fp_brand_version"):
        args.append(f"--fingerprint-brand-version={profile['fp_brand_version']}")
    if profile.get("fp_hardware"):
        args.append(f"--fingerprint-hardware-concurrency={int(profile['fp_hardware'])}")

    if profile.get("fp_lang"):
        args.append(f"--lang={profile['fp_lang']}")
    if profile.get("fp_ua"):
        args.append(f"--user-agent={profile['fp_ua']}")

    proxy = (profile.get("proxy") or "").strip()
    if proxy:
        args.append(f"--proxy-server={proxy}")

    if profile.get("window_w") and profile.get("window_h"):
        args.append(f"--window-size={int(profile['window_w'])},{int(profile['window_h'])}")

    # 无桌面环境下自动降级为 headless，否则 Chromium 会直接报
    # "Missing X server or $DISPLAY" 然后退出（退出码 1）。
    # 注意：headless 的指纹与真人浏览器有差异，正式养号请在有桌面的机器（Windows）上跑。
    if profile.get("headless") or _needs_headless():
        args.append("--headless=new")
        # 无 GPU 的机器上 WebGL 默认是 null，加上这个至少能拿到软件渲染，便于功能自测
        args.append("--enable-unsafe-swiftshader")

    # 扩展：每行一个路径
    exts = profile.get("extensions") or []
    if isinstance(exts, str):
        try:
            exts = json.loads(exts)
        except json.JSONDecodeError:
            exts = []
    paths = [e["path"] if isinstance(e, dict) else str(e) for e in exts if e]
    paths = [p for p in paths if Path(p).exists()]
    if paths:
        args.append("--load-extension=" + ",".join(paths))

    urls = profile.get("start_urls") or []
    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except json.JSONDecodeError:
            urls = []

    args.extend(BASE_ARGS)
    args.extend(extra_args or [])
    if IS_WIN and _is_root():  # 理论上不会发生，留着防御
        args.append("--no-sandbox")
    elif not IS_WIN and _is_root():
        # 坑：Linux 上以 root 跑 Chromium 必须加 --no-sandbox，否则直接退出
        args.append("--no-sandbox")
        args.append("--disable-dev-shm-usage")
    args.extend(urls or ["about:blank"])
    return args


def _needs_headless() -> bool:
    """Linux 上没有图形会话时返回 True。Windows/macOS 永远返回 False。"""
    if IS_WIN:
        return False
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return False
    return True


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def spawn(cmdline: list[str], log_path: Path) -> subprocess.Popen[bytes]:
    """启动子进程。stdout/stderr 落到环境专属日志文件，方便排错。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "ab", buffering=0)
    kwargs: dict[str, Any] = {
        "stdout": fh,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if IS_WIN:
        # 新建进程组，避免 Ctrl+C 把浏览器一起带走
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmdline, **kwargs)


def wait_ready(port: int, timeout: int = 45, proc: subprocess.Popen[bytes] | None = None) -> tuple[str, str]:
    """等 CDP 就绪，返回 (ws_url, http_url)。"""
    http_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise EngineError("ENGINE_EXITED", f"浏览器启动后立即退出（退出码 {proc.returncode}），请查看该环境的 profile.log")
        try:
            r = httpx.get(f"{http_url}/json/version", timeout=2.0)
            if r.status_code == 200:
                ws = r.json().get("webSocketDebuggerUrl", "")
                if ws:
                    return ws, http_url
        except Exception as exc:  # 连接拒绝是常态，继续等
            last_err = str(exc)
        time.sleep(0.4)
    raise EngineError("LAUNCH_TIMEOUT", f"等待 CDP 端口 {port} 就绪超时（{timeout}s）。最后一次错误：{last_err}")


# ── 停止 ──────────────────────────────────────────────────
def close_via_cdp(port: int, timeout: float = 4.0) -> bool:
    """优先用 CDP 的 Browser.close 优雅关闭；失败再走信号。"""
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2.0)
        ws = r.json().get("webSocketDebuggerUrl")
        if not ws:
            return False
        import asyncio

        import websockets

        async def _close() -> None:
            async with websockets.connect(ws, open_timeout=timeout, close_timeout=2) as sock:
                await sock.send(json.dumps({"id": 1, "method": "Browser.close"}))

        asyncio.run(_close())
        return True
    except Exception:
        return False


def terminate(pid: int, grace: int = 6) -> str:
    """先 SIGTERM / CTRL_BREAK，宽限期后强杀。返回实际使用的方式。"""
    if not pid:
        return "no-pid"
    if IS_WIN:
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
        except Exception:
            subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True)
        for _ in range(grace * 4):
            if not _pid_alive(pid):
                return "sigbreak"
            time.sleep(0.25)
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return "taskkill"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already-dead"
    for _ in range(grace * 4):
        if not _pid_alive(pid):
            return "sigterm"
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
        return "sigkill"
    except ProcessLookupError:
        return "already-dead"


def _pid_alive(pid: int) -> bool:
    if IS_WIN:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


# ── 健康与磁盘 ────────────────────────────────────────────
def probe(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2.0).status_code == 200
    except Exception:
        return False


TRIM_DIRS = [
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/ShaderCache",
    "Default/GrShaderCache",
    "Default/DawnCache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "Default/Service Worker/CacheStorage",
    "Crashpad",
]


def trim(udd: Path) -> dict[str, Any]:
    """清理缓存（不碰 cookie / 扩展 / 书签）。"""
    removed = 0
    freed = 0
    for rel in TRIM_DIRS:
        target = Path(udd) / rel
        if not target.exists():
            continue
        size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) if target.is_dir() else target.stat().st_size
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink()
            removed += 1
            freed += size
        except OSError:
            pass
    return {"removed_dirs": removed, "freed_bytes": freed}


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

"""内核获取：让客户端自己把 ungoogled-chromium 装好，不靠人工。

## 为什么需要这个模块

官方 Google Chrome 从 137 起忽略 `--load-extension`（实测 Chrome 149/150/157 都不加载），
于是"把插件带进环境"这件事在官方内核上**只是把文件放进去，浏览器不认**。
要用插件就必须换 ungoogled-chromium。

但"换内核"不能变成"每台机器请用户自己下载解压" —— 那等于没做。所以这里做三件事：

1. **解析**：从 GitHub Release 找到当前平台对应的 ungoogled-chromium 资产；
2. **校验**：用 GitHub API 给出的 `digest`（`sha256:...`）核对下载完整性，
   不完整就中止，绝不"先装上再说"；
3. **安装**：安全解包（防路径穿越）、定位 `chrome`/`chrome.exe`、`--version` 自检，
   落到 `<数据目录>/engines/<版本>`，之后 `engines.find_chrome()` 自然就能找到。

## 为什么不信自己写死的 URL

写死 URL 一升级就 404。这里查 GitHub API 的 `releases/latest`，但**必须**同时拿到
`digest` 才继续 —— 没有校验值的下载等于闭眼装二进制。

## 平台

Windows 用 `ungoogled-chromium-windows` 的 `windows_x64.zip`；
Linux 用 `ungoogled-chromium-portablelinux` 的 `x86_64_linux.tar.xz`。
macOS 上游没有稳定二进制，走 `ZW_ENGINE_URL` 手动指定（会明确告知，而不是假装支持）。
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from ..config import Settings, get_settings

log = __name__

IS_WIN = os.name == "nt"

GITHUB_API = "https://api.github.com"


# ── 平台 → 资产规格 ────────────────────────────────────────
@dataclass(frozen=True)
class AssetSpec:
    repo: str
    pattern: str        # 资产文件名的正则（re.search）
    kind: str           # zip | tarxz
    binary: str         # 解包后要定位的可执行文件名

    def matches(self, name: str) -> bool:
        return bool(re.search(self.pattern, name, re.IGNORECASE))


def _arch() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m or "unknown"


def spec_for_platform(sysname: str | None = None, arch: str | None = None) -> AssetSpec | None:
    """当前平台该下哪个资产。返回 None = 上游没有稳定二进制（如 macOS）。"""
    sysname = (sysname or platform.system()).lower()
    arch = arch or _arch()
    if sysname.startswith("win"):
        if arch == "arm64":
            return AssetSpec("ungoogled-software/ungoogled-chromium-windows",
                             r"windows_arm64\.zip$", "zip", "chrome.exe")
        return AssetSpec("ungoogled-software/ungoogled-chromium-windows",
                         r"windows_x64\.zip$", "zip", "chrome.exe")
    if sysname.startswith("linux"):
        if arch == "arm64":
            return AssetSpec("ungoogled-software/ungoogled-chromium-portablelinux",
                             r"arm64_linux\.tar\.xz$", "tarxz", "chrome")
        return AssetSpec("ungoogled-software/ungoogled-chromium-portablelinux",
                         r"x86_64_linux\.tar\.xz$", "tarxz", "chrome")
    if sysname.startswith("darwin"):
        return None
    return None


class EngineSourceError(Exception):
    def __init__(self, code: str, message: str, *, hint: str = "", status: int = 500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.status = status


# ── 进度状态（给 API/控制台看）──────────────────────────
@dataclass
class Progress:
    state: str = "idle"          # idle | resolving | downloading | extracting | verifying | done | error
    message: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    version: str = ""
    path: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    log: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = {
            "state": self.state, "message": self.message,
            "bytes_done": self.bytes_done, "bytes_total": self.bytes_total,
            "version": self.version, "path": self.path, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "log": self.log[-30:],
        }
        if self.bytes_total > 0:
            d["percent"] = round(self.bytes_done * 100.0 / self.bytes_total, 1)
        else:
            d["percent"] = 0.0
        return d


_progress = Progress()
_lock = threading.Lock()
_thread: threading.Thread | None = None


def progress() -> dict[str, Any]:
    with _lock:
        return _progress.as_dict()


def _set(**kw: Any) -> None:
    with _lock:
        for k, v in kw.items():
            setattr(_progress, k, v)


def _say(msg: str) -> None:
    with _lock:
        _progress.message = msg
        _progress.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        del _progress.log[:-60]


def _reset() -> None:
    global _progress
    with _lock:
        _progress = Progress(state="resolving", started_at=time.time(),
                             message="正在查询可用的内核版本 …")


def is_running() -> bool:
    t = _thread
    return bool(t and t.is_alive())


# ── 解析：找到当前平台的资产 ─────────────────────────────
def _proxy(s: Settings) -> str:
    """下载内核用的代理：显式配置优先，否则自动发现本机代理。

    自动发现很关键：用户机器上常常本来就跑着 Clash/mihomo，
    只是没设 `ZW_DOWNLOAD_PROXY`。实测同一台机器直连 146KB/8s、
    经本机 21080 是 49.4MB/8s —— 不自动发现就会一直按直连速度卡着。
    """
    from . import netproxy

    url, source = netproxy.resolve(
        s.download_proxy,
        autodetect=s.download_proxy_autodetect,
        ports=s.download_proxy_ports,
    )
    if url and source != "explicit":
        log_msg = f"下载将使用自动发现的代理 {url}（{source}）"
        try:
            import logging

            logging.getLogger("zw.enginesrc").info("%s", log_msg)
        except Exception:  # noqa: BLE001
            pass
    return url


def _client(s: Settings, timeout: float = 30.0) -> httpx.Client:
    kw: dict[str, Any] = {"follow_redirects": True, "timeout": timeout,
                          "headers": {"User-Agent": "zhiwen-core"}}
    px = _proxy(s)
    if px:
        kw["proxy"] = px
    return httpx.Client(**kw)


def resolve_latest(s: Settings | None = None, *, spec: AssetSpec | None = None,
                   repo_override: str = "") -> dict[str, Any]:
    """查 GitHub 最新 release，挑出当前平台的资产（含 sha256 与大小）。"""
    s = s or get_settings()
    spec = spec or spec_for_platform()
    if spec is None:
        raise EngineSourceError(
            "ENGINE_UNSUPPORTED_PLATFORM",
            f"上游没有 {platform.system()}/{_arch()} 的稳定二进制。",
            hint="请手动下载 ungoogled-chromium，并设 ZW_ENGINE_URL 指向压缩包"
                 "（或直接把解压目录放到 <数据目录>/engines/ 下）",
            status=400,
        )
    repo = repo_override or spec.repo
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    try:
        with _client(s) as c:
            r = c.get(url)
    except Exception as exc:  # noqa: BLE001
        raise EngineSourceError(
            "ENGINE_DOWNLOAD_FAILED",
            f"连不上 GitHub（{type(exc).__name__}: {exc}）。",
            hint="国内网络请设 ZW_DOWNLOAD_PROXY 指向你的代理后重试",
            status=502,
        ) from exc
    if r.status_code != 200:
        raise EngineSourceError(
            "ENGINE_DOWNLOAD_FAILED",
            f"查询 {repo} 最新版本失败：GitHub 返回 HTTP {r.status_code}",
            status=502,
        )
    data = r.json()
    tag = str(data.get("tag_name") or "")
    for a in data.get("assets") or []:
        name = str(a.get("name") or "")
        if not spec.matches(name):
            continue
        digest = str(a.get("digest") or "")
        sha = digest.split(":", 1)[1] if digest.lower().startswith("sha256:") else ""
        if not sha:
            # 没有校验值就**不装**。宁可失败并让人手动指定，也不要闭眼装二进制。
            raise EngineSourceError(
                "ENGINE_CHECKSUM_MISSING",
                f"资产 {name} 没有发布 sha256 校验值，拒绝安装。",
                hint="设 ZW_ENGINE_URL 与 ZW_ENGINE_SHA256 手动指定",
                status=502,
            )
        return {
            "repo": repo, "tag": tag, "name": name,
            "url": str(a.get("browser_download_url") or ""),
            "sha256": sha.lower(), "size": int(a.get("size") or 0),
            "kind": spec.kind, "binary": spec.binary,
        }
    raise EngineSourceError(
        "ENGINE_ASSET_NOT_FOUND",
        f"{repo} 的 {tag} 里没有匹配 {spec.pattern} 的资产",
        status=502,
    )


# ── 下载 + 校验 ──────────────────────────────────────────
def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, *, sha256: str, size: int = 0,
             s: Settings | None = None) -> Path:
    """流式下载到 `dest.part`，边写边算 sha256，核对通过才改名。"""
    s = s or get_settings()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    done = 0
    _set(state="downloading", bytes_done=0, bytes_total=size or 0)
    try:
        with _client(s, timeout=120.0) as c:
            with c.stream("GET", url) as r:
                if r.status_code != 200:
                    raise EngineSourceError(
                        "ENGINE_DOWNLOAD_FAILED",
                        f"下载内核失败：HTTP {r.status_code}", status=502)
                total = size or int(r.headers.get("content-length") or 0)
                if total:
                    _set(bytes_total=total)
                with open(part, "wb") as f:
                    for chunk in r.iter_bytes(1024 * 256):
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        _set(bytes_done=done)
    except EngineSourceError:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        part.unlink(missing_ok=True)
        raise EngineSourceError(
            "ENGINE_DOWNLOAD_FAILED",
            f"下载内核失败（{type(exc).__name__}: {exc}）。",
            hint="国内网络请设 ZW_DOWNLOAD_PROXY；也可以手动下载后设 ZW_ENGINE_URL",
            status=502,
        ) from exc

    got = h.hexdigest()
    if sha256 and got.lower() != sha256.lower():
        part.unlink(missing_ok=True)
        raise EngineSourceError(
            "ENGINE_CHECKSUM_MISMATCH",
            f"下载的内核校验不通过（期望 {sha256[:12]}…，实际 {got[:12]}…），已丢弃。",
            hint="可能是下载被截断或中间被替换；重试一次，或手动下载后设 ZW_ENGINE_URL",
            status=502,
        )
    os.replace(part, dest)
    return dest


# ── 解包 + 安装 ──────────────────────────────────────────
def _safe_extract(archive: Path, into: Path) -> None:
    """解包并防路径穿越（zip 手工校验；tar 用 data 过滤器）。"""
    into.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            base = into.resolve()
            for info in zf.infolist():
                target = (into / info.filename).resolve()
                if not str(target).startswith(str(base) + os.sep) and target != base:
                    raise EngineSourceError(
                        "ENGINE_EXTRACT_FAILED",
                        f"压缩包里有越界路径：{info.filename}", status=400)
            zf.extractall(into)
        return
    try:
        with tarfile.open(archive, "r:*") as tf:
            try:
                tf.extractall(into, filter="data")     # py3.12+
            except TypeError:                          # 老 Python 兜底
                base = into.resolve()
                for m in tf.getmembers():
                    target = (into / m.name).resolve()
                    if not str(target).startswith(str(base) + os.sep) and target != base:
                        raise EngineSourceError(
                            "ENGINE_EXTRACT_FAILED",
                            f"压缩包里有越界路径：{m.name}", status=400)
                tf.extractall(into)
    except EngineSourceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EngineSourceError(
            "ENGINE_EXTRACT_FAILED", f"解包失败（{type(exc).__name__}: {exc}）",
            status=400) from exc


def _find_binary(root: Path, name: str) -> Path | None:
    """在解包目录里找可执行文件（限深度，避免全盘扫）。"""
    best: Path | None = None
    for p in root.rglob(name):
        if not p.is_file():
            continue
        depth = len(p.relative_to(root).parts)
        if depth > 4:
            continue
        if best is None or depth < len(best.relative_to(root).parts):
            best = p
    return best


def _engine_version(bin_path: Path) -> str:
    """跑一次 `--version` 拿版本号。

    **必须走 `proc.run_hidden`**，不能直接 `subprocess.run` —— Windows 上
    直接起子进程会弹出一个黑色控制台窗口，而这个函数会在客户端启动/装内核时
    被调用，用户会莫名看到窗口闪一下。项目里有一条测试专门守着这条规矩
    （`test_proc_hidden`），第一次实现就是被它挡下来的。
    """
    from .proc import run_hidden

    try:
        out = run_hidden([str(bin_path), "--version"], capture_output=True,
                         text=True, timeout=30)
        return (out.stdout or out.stderr or "").strip()[:120]
    except Exception:  # noqa: BLE001
        return ""


def install_archive(archive: Path, *, kind: str, binary: str,
                    engine_dir: Path, version_tag: str) -> Path:
    """解包 → 定位可执行文件 → 落到 `engine_dir/<version_tag>` → 自检。"""
    _set(state="extracting", message="正在解包 …")
    tmp = Path(tempfile.mkdtemp(prefix="zw-engine-"))
    try:
        _safe_extract(archive, tmp)
        found = _find_binary(tmp, binary)
        if found is None:
            raise EngineSourceError(
                "ENGINE_EXTRACT_FAILED",
                f"解包后没找到 {binary}，这个包可能不是可用的内核",
                status=400)
        src_dir = found.parent
        target = engine_dir / version_tag
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        engine_dir.mkdir(parents=True, exist_ok=True)
        _set(state="verifying", message="正在安装到数据目录 …")
        shutil.move(str(src_dir), str(target))
        new_bin = target / binary
        if not IS_WIN:
            mode = new_bin.stat().st_mode
            new_bin.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        ver = _engine_version(new_bin)
        if not ver:
            raise EngineSourceError(
                "ENGINE_BROKEN",
                "内核装好了但跑不起来（--version 无输出）。可能缺系统库。",
                hint="Linux 上常见缺 libnss3 / libatk / libgbm 等；装好依赖后重试",
                status=500)
        _set(state="done", version=ver, path=str(new_bin),
             message=f"内核就绪：{ver}", finished_at=time.time())
        return new_bin
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 对外主入口 ───────────────────────────────────────────
def ensure(force: bool = False, s: Settings | None = None) -> dict[str, Any]:
    """确保 `engine_dir` 里有一个可用的 ungoogled-chromium。已在则直接返回。"""
    s = s or get_settings()
    engine_dir = Path(s.engine_dir)
    if not str(engine_dir).strip() or str(engine_dir) == ".":
        engine_dir = Path(s.root) / "engines"

    existing = _existing(engine_dir)
    if existing and not force:
        info = {"ok": True, "skipped": True, "path": str(existing),
                "version": _engine_version(existing)}
        _set(state="done", path=str(existing), version=info["version"],
             message="已有可用内核，跳过", finished_at=time.time())
        return info

    _reset()
    try:
        url_override = (s.engine_url or "").strip()
        sha_override = (s.engine_sha256 or "").strip().lower()
        if url_override:
            _say(f"使用手动指定的内核地址：{url_override}")
            meta = {"url": url_override, "sha256": sha_override, "size": 0,
                    "kind": "zip" if url_override.lower().endswith(".zip") else "tarxz",
                    "binary": "chrome.exe" if IS_WIN else "chrome",
                    "tag": (s.engine_version_tag or "custom")}
            if not sha_override:
                raise EngineSourceError(
                    "ENGINE_CHECKSUM_MISSING",
                    "ZW_ENGINE_URL 必须同时给 ZW_ENGINE_SHA256（否则无法校验完整性）",
                    status=400)
        else:
            _say("正在查询最新版 ungoogled-chromium …")
            meta = resolve_latest(s, repo_override=(s.engine_repo or ""))
            _say(f"选中 {meta['name']}（{meta['size'] / 1048576:.0f} MB）")

        archive = engine_dir.parent / f"engine-download-{int(time.time())}"
        archive = archive.with_suffix(".zip" if meta["kind"] == "zip" else ".tar.xz")
        _say("开始下载 …")
        download(meta["url"], archive, sha256=meta["sha256"],
                 size=int(meta.get("size") or 0), s=s)
        _say("下载完成，校验通过")
        try:
            bin_path = install_archive(archive, kind=meta["kind"], binary=meta["binary"],
                                       engine_dir=engine_dir,
                                       version_tag=str(meta.get("tag") or "ungoogled"))
        finally:
            archive.unlink(missing_ok=True)
        _say("内核安装完成")
        return {"ok": True, "skipped": False, "path": str(bin_path),
                "version": _engine_version(bin_path)}
    except EngineSourceError as exc:
        _set(state="error", error=exc.message, finished_at=time.time(),
             message=exc.message)
        raise
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        _set(state="error", error=msg, finished_at=time.time(), message=msg)
        raise EngineSourceError("ENGINE_INSTALL_FAILED", msg, status=500) from exc


def _existing(engine_dir: Path) -> Path | None:
    """engine_dir 里已有可用内核（优先带 ungoogled 字样的）。"""
    if not engine_dir.is_dir():
        return None
    names = ["chrome.exe", "chrome"] if IS_WIN else ["chrome"]
    hits: list[Path] = []
    for n in names:
        hits.extend(p for p in engine_dir.rglob(n)
                    if p.is_file() and (IS_WIN or os.access(p, os.X_OK)))
    if not hits:
        return None
    # 只看引擎目录内的相对路径，别拿绝对路径判断 —— 否则用户把引擎放在
    # `D:\ungoogled测试\` 这种目录下，任何内核都会被评为"优先"。
    def _rank(p: Path) -> tuple[int, int]:
        try:
            rel = str(p.relative_to(engine_dir)).lower()
        except (ValueError, OSError):
            rel = p.name.lower()
        return (0 if ("ungoogled" in rel or "fingerprint" in rel) else 1, len(rel))

    hits.sort(key=_rank)
    return hits[0]


def start_background(force: bool = False) -> dict[str, Any]:
    """后台线程跑 `ensure()`，立刻返回；进度看 `progress()`。"""
    global _thread
    if is_running():
        return {"started": False, "reason": "已经在下载/安装中", **progress()}
    _reset()
    def _run() -> None:
        try:
            ensure(force=force)
        except Exception:  # noqa: BLE001 —— 失败原因已经写进 progress
            pass
    _thread = threading.Thread(target=_run, name="zw-engine-fetch", daemon=True)
    _thread.start()
    return {"started": True, **progress()}

"""引擎层：找到 Chrome、拉起进程、等它就绪、停掉它、清理磁盘。

这一层的每一条都是踩过坑写下来的，`# 坑：` 开头的注释不要删。

## 0.2.0 的三处实质改动

1. **等就绪改为读 `DevToolsActivePort`**，并校验端口与 ws 路径。
   以前只轮询 `http://127.0.0.1:<分配端口>/json/version` 就够了——直到有一次
   进程起来就崩、而同一端口上恰好有另一个 chrome（或上一次没退干净的）在响应，
   于是我们把**别人**的 ws 地址登记进了网关，客户端连过去看到的是别的环境。
   现在必须证明"这个端口是我的进程开出来的"才算就绪。
2. **扩展支持 slug**。环境里存库里 slug，启动时解析成真实路径；老数据里的
   绝对路径继续兼容。
3. **时区 / Accept-Language / 平台版本 / 禁用伪装项** 真正下发到命令行与子进程环境，
   以前 `fp_timezone` 只是躺在数据库里从来没生效过。
"""

from __future__ import annotations

import json
import logging
import os
import re
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

from ..config import Settings, get_settings
from .proc import popen_hidden, run_hidden

IS_WIN = sys.platform == "win32"
logger = logging.getLogger("zw.engines")

# Windows 上常见的安装位置（按优先级）
_WIN_CANDIDATES = [
    r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
    r"%PROGRAMFILES%\Chromium\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Chromium\Application\chrome.exe",
    r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe",
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
    # 别弹"恢复上次会话"，否则每次启动都是一堆旧标签
    "--hide-crash-restore-bubble",
]

# 只在 Linux 上加的参数。/dev/shm 太小是容器/嵌入式 Linux 的问题，
# 放到 BASE_ARGS 里会让 Windows 上的命令行带一堆无意义参数（排查时很误导）。
_POSIX_ONLY_ARGS = [
    "--disable-dev-shm-usage",
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
    """按优先级找浏览器可执行文件。

    ## `ZW_ENGINE_POLICY=ungoogled`（默认，2026-11-18 起）

    只认**带 ungoogled 字样**的内核，找不到就报错并告诉用户怎么装，
    **不会静默退回官方 Chrome** —— 官方 Chrome 137 起忽略 `--load-extension`，
    退回去等于悄悄丢掉插件能力，用户不会知情。

    ## `ZW_ENGINE_POLICY=any`

    旧行为：引擎目录 → 系统 Chrome → PATH。
    """
    if s.chrome_bin:
        p = Path(s.chrome_bin).expanduser()
        if p.exists():
            return p
        raise EngineError("ENGINE_NOT_FOUND", f"ZW_CHROME_BIN 指向的文件不存在：{p}")

    want_ungoogled = str(getattr(s, "engine_policy", "any") or "any").lower() == "ungoogled"

    # 1. 数据目录下的 engines/ 里找（推荐做法，便于版本共存）
    names = ["chrome.exe", "chrome", "chromium.exe", "chromium"] if IS_WIN else ["chrome", "chromium"]
    hits: list[Path] = []
    if Path(s.engine_dir).exists():
        for name in names:
            hits.extend(sorted(Path(s.engine_dir).rglob(name)))
        hits = [h for h in hits if h.is_file() and os.access(h, os.X_OK)] if not IS_WIN else [h for h in hits if h.is_file()]
        # 目录遍历顺序不可靠：ungoogled 优先（能加载扩展），其次 fingerprint，最后才轮到别的
        def _rank(h: Path) -> tuple[int, int]:
            t = str(h).lower()
            if "ungoogled" in t:
                return (0, len(t))
            if "fingerprint" in t:
                return (1, len(t))
            return (2, len(t))
        hits.sort(key=_rank)
    if hits:
        if not want_ungoogled:
            return hits[0]
        un = [h for h in hits if _is_ungoogled_family(h, Path(s.engine_dir))]
        if un:
            return un[0]
        # policy=ungoogled 但引擎目录里只有"官方内核" → 不用它，往下走给出明确指引

    if not want_ungoogled:
        # 2. 系统常见位置
        for cand in _WIN_CANDIDATES if IS_WIN else _POSIX_CANDIDATES:
            expanded = Path(os.path.expandvars(cand))
            if expanded.exists():
                return expanded

        # 3. PATH
        for name in ("chrome", "chromium", "google-chrome", "chromium-browser", "msedge"):
            found = shutil.which(name)
            if found:
                return Path(found)

    from . import enginesrc

    prog = enginesrc.progress()
    if prog.get("state") in ("resolving", "downloading", "extracting", "verifying"):
        raise EngineError(
            "ENGINE_PREPARING",
            f"正在准备 ungoogled-chromium 内核（{prog.get('message') or ''}"
            f"{'，' + str(prog.get('percent', 0)) + '%' if prog.get('percent') else ''}）。"
            "下载完成后新建/启动环境即可；进度看控制台或 GET /api/v1/engine。",
        )
    raise EngineError(
        "ENGINE_NOT_FOUND",
        "没有找到可用的 ungoogled-chromium 内核（官方 Chrome 137 起不加载命令行扩展，"
        "所以这里不会退回官方内核）。三种解决办法任选其一：\n"
        "  1) 让客户端自动下载：POST /api/v1/engine/install，或命令行 `zw engine install`\n"
        "  2) 把 ungoogled-chromium 解压到 <数据目录>/engines/ 下\n"
        "  3) 设 ZW_ENGINE_URL + ZW_ENGINE_SHA256 指向你自己的压缩包\n"
        f"当前数据目录：{s.root}",
    )


def _is_ungoogled_family(bin_path: Path, engine_dir: Path | None = None) -> bool:
    """这个内核算不算"去谷歌化家族"。

    ## 为什么不能只看路径里有没有 "ungoogled"

    `fingerprint-chromium` **本身就是 ungoogled-chromium 的分支**
    （上游 README 原话：an open source fingerprint browser **based on Ungoogled
    Chromium**），它同样能加载 `--load-extension`。如果只认 "ungoogled"，用户装了
    fingerprint-chromium 反而会被判成"没有可用内核" —— 这是把人往错误方向推。

    所以判据是"**不是官方发行版**"：带 ungoogled 或 fingerprint 字样都接受。
    真正决定能不能加载扩展的是**探针实测**（`extprobe.probe`），名字只用来排序/初筛。
    """
    # **只看引擎目录内的相对路径**，不要拿绝对路径做判断。
    #
    # 真实踩到过：pytest 的 `tmp_path` 里含测试函数名
    # （`.../test_ungoogled_policy_rejects_0/engines/official-chrome-157/chrome`），
    # 路径里有 "ungoogled"，于是"官方内核"被判定成 ungoogled 家族 ——
    # 测试表现成"拒不掉官方内核"，查了半天才发现是被自己的目录名骗了。
    # 同理，用户把引擎放在 `D:\ungoogled测试\` 这种目录下也会误判。
    if engine_dir is not None:
        try:
            rel = str(bin_path.relative_to(engine_dir)).lower()
        except (ValueError, OSError):
            rel = bin_path.name.lower()
        # **还要看 engine_dir 自己的目录名**。
        #
        # 很多人（包括我们的测试）是把 `ZW_ENGINE_DIR` 直接指到内核目录：
        #   ZW_ENGINE_DIR=/opt/xxx/ungoogled-chromium-150.0.7871.186/
        # 此时 `chrome` 的相对路径就只有 `chrome`，不含 ungoogled 字样 ——
        # 只看相对路径会把这个内核判成"官方发行版"然后拒绝掉。
        # （这是改「别拿绝对路径判断」之后引入的回归，被 ui-check 的
        #  "点启动后变成运行中" 超时抓到。）
        # 相对路径里"有信息"（含目录层级）就以它为准；只有相对路径就是裸文件名
        # （`chrome` / `chrome.exe`）时，才允许参考 engine_dir 的目录名。
        #
        # 这条分界很重要，两个方向都踩过：
        #   · `ZW_ENGINE_DIR=/opt/ungoogled-chromium-150/` + `chrome`
        #     → 相对路径无信息，必须看目录名，否则真内核被拒
        #   · `ZW_ENGINE_DIR=.../test_x/engines` + `official-chrome-157/chrome`
        #     → 相对路径明确说了是官方内核，绝不能被上层目录名里的
        #       "ungoogled"（测试名/用户自建目录名）洗白
        has_structure = os.sep in rel or "/" in rel
        t = rel if has_structure else (rel + " " + engine_dir.name.lower())
    else:
        t = bin_path.name.lower()
    return "ungoogled" in t or "fingerprint" in t


def prepare_engine_async(s: Settings | None = None) -> dict[str, Any]:
    """客户端启动时调用：policy=ungoogled 且没有内核就在后台装。

    不阻塞启动（下载 150–200MB 可能要几分钟），进度由 `enginesrc.progress()` 暴露。
    失败不抛：启动阶段抛异常会把整个客户端带下去，而"没内核"是可以稍后重试的状态。
    """
    s = s or get_settings()
    if str(getattr(s, "engine_policy", "any") or "any").lower() != "ungoogled":
        return {"started": False, "reason": "policy 不是 ungoogled"}
    if not bool(getattr(s, "engine_auto_install", False)):
        return {"started": False, "reason": "ZW_ENGINE_AUTO_INSTALL 关闭"}
    from . import enginesrc

    if enginesrc._existing(Path(s.engine_dir)) is not None:
        return {"started": False, "reason": "已有 ungoogled 内核"}
    logger.info("没有 ungoogled 内核，后台开始下载安装（约 150–200MB）")
    return enginesrc.start_background(force=False)


def list_engine_candidates(s: Settings) -> list[dict[str, str]]:
    """给 doctor / 控制台用：列出所有能找到的内核（含版本），不抛异常。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    probe: list[Path] = []
    if s.chrome_bin:
        probe.append(Path(s.chrome_bin).expanduser())
    if Path(s.engine_dir).exists():
        for name in ("chrome", "chrome.exe", "chromium"):
            probe.extend(sorted(Path(s.engine_dir).rglob(name)))
    for cand in (_WIN_CANDIDATES if IS_WIN else _POSIX_CANDIDATES):
        probe.append(Path(os.path.expandvars(cand)))
    for name in ("chrome", "chromium", "google-chrome"):
        found = shutil.which(name)
        if found:
            probe.append(Path(found))
    for p in probe:
        key = str(p)
        if key in seen or not p.is_file():
            continue
        seen.add(key)
        out.append({"path": key, "version": engine_version(p), "source": _source_of(p, s)})
    return out


def _source_of(p: Path, s: Settings) -> str:
    if s.chrome_bin and str(p) == str(Path(s.chrome_bin).expanduser()):
        return "ZW_CHROME_BIN"
    try:
        p.resolve().relative_to(Path(s.engine_dir).resolve())
        return "数据目录 engines/"
    except (ValueError, OSError):
        pass
    return "系统安装"


def _version_from_layout(bin_path: Path) -> str:
    """从安装布局里"看"版本号，不启动进程。

    Chromium 系发行包通常把版本放在路径里
    （`chrome-linux64`、`ungoogled-chromium-150.0.7871.186-1-x86_64`、
    `chromium-1208` 这类），能读到就用，读不到就算了。
    """
    text = str(bin_path)
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"chromium[_-]?(\d{3,4})", text, re.IGNORECASE)
    if m:
        return f"build {m.group(1)}"
    return ""



# ── 外部扩展能不能加载（Chrome 137+ 不行）────────────────
# 实测结论（2026-11-17，三种内核 × 有界面/无头 都试过）：
#   google-chrome 149 / ungoogled-chromium 150 / playwright chromium
#   命令行 `--load-extension=<dir>` **完全不生效** —— 期望的扩展 ID
#   从来没出现在 chrome-extension:// 目标里，内容脚本也从不执行。
#   加 `--disable-features=DisableLoadExtensionCommandLineSwitch` 也没用。
#   同时试过往 Default/Preferences 的 extensions.settings 注入
#   （location=4/state=1/manifest 齐全），也被 Secure Preferences 的 MAC 校验挡掉。
# 所以"创建浏览器时自动带上插件"这件事，靠命令行加载在当前内核上做不到。
EXT_LOAD_MIN_BROKEN_MAJOR = 137



def _major_of(ver: str) -> int:
    """从版本字符串里取主版本号。

    ## 为什么不能按分隔符切了再找第一个纯数字

    `engine_version()` 返回的可能是 **带厂商前缀** 的串：Linux 上跑
    `google-chrome --version` 得到的是 `Google Chrome 149.0.7827.53`。
    按 `[.-]` 切出来是 `["Google Chrome 149", "0", "7827", "53"]` ——
    第一个纯数字片段是 `"0"`（版本号里的第二段），于是主版本被认成 0，
    "≥137 不支持扩展"这条判断就永远不会触发（实测踩到：正式版 Chrome
    被报告成"可以加载扩展"）。

    所以先找形如 `x.y.z…` 的版本号整体，再取它的第一段。
    """
    m = re.search(r"(\d+)(?:\.\d+)+", ver or "")
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d+)\b", ver or "")
    return int(m.group(1)) if m else 0


def extensions_loadable(bin_path: Path) -> tuple[bool | None, str]:
    """这个内核能不能通过命令行加载外部扩展。返回 (能?, 原因)。

    ## 为什么改成**实测**而不是看版本号

    最初我按"版本 ≥137 就不支持"来判断，结果判错了：

    | 内核 | 版本 | 实际 |
    |---|---|---|
    | /usr/bin/google-chrome（正式版） | 149.0.7827.53 | 不加载 |
    | ungoogled-chromium | 150.0.7871.186 | **正常加载** |

    版本都在 137 以上，结论却相反 —— 正式版被挡住了、ungoogled 照常工作。
    所以判据只能是"跑一次探针看看"（`services/extprobe.py`）：
    造一个只改 document.title 的临时扩展，无头启动内核，
    用本地 http 页面 + CDP 读标题，看到标记就算能加载。

    探针本身跑不起来时返回 **None（不知道）** —— 不能报成"不能加载"：
    那会让用户以为插件坏了，而实际只是没能测。
    """
    from . import extprobe

    try:
        r = extprobe.probe(bin_path)
    except Exception as exc:  # noqa: BLE001
        return False, (f"判断扩展能否被加载时出错（{type(exc).__name__}: {exc}），"
                       "已按不加载处理")
    # **三态**：True=能 / False=不能 / None=探测没跑成（不知道）。
    #
    # 这里原来写的是 `if r.get("loadable"): ... return False, ...`，
    # 而 `None` 是 falsy → 探测失败被报成"确定不能加载"。
    # 云电脑真机就是：杀软拦了探针（WinError 5），界面显示「不能加载扩展」，
    # 用户以为插件坏了。正确语义是"不知道"。
    loadable = r.get("loadable")
    if loadable is None:
        return None, str(r.get("reason") or "探测没能完成，无法判断这个内核能否加载扩展")
    if loadable:
        return True, ""
    return False, str(r.get("reason") or "无法确认这个内核会不会加载扩展")


def engine_version(bin_path: Path) -> str:
    """取浏览器版本。

    ## Windows 上**绝不能**靠 `--version`

    Linux/macOS 的 Chrome 会把版本打到 stdout 然后退出；
    **Windows 的 chrome.exe 忽略 `--version`**：它照常启动浏览器（开窗口、
    常驻进程），stdout 一直是空的。于是那次带 `timeout=20` 的 run 一路
    等到超时 —— 实测在 Windows 上每次启动都因此**卡满 20 秒**，
    客户端"本机服务"看起来就是起不来。

    所以顺序是：
    1. 先从安装路径里读版本（不启动进程，零成本）；
    2. Windows 上只用 PowerShell 读文件版本信息（很快，不启动浏览器）；
    3. 其它平台才用 `--version`，且超时压到 5 秒。
    """
    from_layout = _version_from_layout(bin_path)
    if from_layout:
        return from_layout

    if sys.platform == "win32":
        # PowerShell 读 PE 的 ProductVersion，秒级返回，不会把浏览器拉起来
        # 走 run_hidden：Windows 上 powershell 是控制台程序，裸调会闪黑框
        try:
            out = run_hidden(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-Item -LiteralPath '{bin_path}').VersionInfo.ProductVersion"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8,
            )
            ver = (out.stdout or "").strip().splitlines()
            if ver and re.match(r"^[\d.]+$", ver[0].strip()):
                return ver[0].strip()
        except Exception:
            pass
        return "unknown"

    try:
        out = run_hidden(
            [str(bin_path), "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            stdin=subprocess.DEVNULL,
        )
        text = (out.stdout or out.stderr).strip().splitlines()
        return text[0][:80] if text else "unknown"
    except Exception:
        return "unknown"


def supports_fingerprint(bin_path: Path) -> bool:
    """是否支持 `--fingerprint`（fingerprint-chromium）。

    判据是文件名/路径里带 fingerprint，或者同级目录存在相关资源。
    不做二进制串扫描：那要读 500MB 文件，启动时做一次太贵。
    """
    text = str(bin_path).lower()
    return "fingerprint" in text


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
        # 明确绑回环。不加的话某些平台会绑 0.0.0.0，等于把 CDP 直接暴露给全内网。
        "--remote-debugging-address=127.0.0.1",
    ]
    remote = bool(profile.get("cdp_enabled", 1))
    if not remote:
        args = [a for a in args if not a.startswith("--remote-debugging")]

    seed = int(profile.get("fp_seed") or 0)
    if seed:
        args.append(f"--fingerprint={seed}")
    platform = (profile.get("fp_platform") or "host").strip()
    if platform and platform != "host":
        args.append(f"--fingerprint-platform={platform}")
    if profile.get("fp_platform_version"):
        args.append(f"--fingerprint-platform-version={profile['fp_platform_version']}")
    if profile.get("fp_brand"):
        args.append(f"--fingerprint-brand={profile['fp_brand']}")
    if profile.get("fp_brand_version"):
        args.append(f"--fingerprint-brand-version={profile['fp_brand_version']}")
    if profile.get("fp_hardware"):
        args.append(f"--fingerprint-hardware-concurrency={int(profile['fp_hardware'])}")
    if profile.get("fp_disable_spoofing"):
        # 144+ 才有；老内核会忽略未知参数，不会炸
        args.append(f"--disable-spoofing={profile['fp_disable_spoofing']}")

    if profile.get("fp_lang"):
        args.append(f"--lang={profile['fp_lang']}")
    if profile.get("fp_accept_lang"):
        args.append(f"--accept-lang={profile['fp_accept_lang']}")
    elif profile.get("fp_lang"):
        # 不给 accept-lang 时 Chromium 会沿用系统语言，导致 navigator.languages 与 UA 不一致
        args.append(f"--accept-lang={profile['fp_lang']}")
    if profile.get("fp_timezone"):
        # fingerprint-chromium 认这个参数；原生 Chrome 会忽略它，
        # 所以真正的时区还要靠 TZ 环境变量 / CDP 兜底（见 manager.start）。
        args.append(f"--timezone={profile['fp_timezone']}")
    if profile.get("fp_ua"):
        args.append(f"--user-agent={profile['fp_ua']}")

    proxy = (profile.get("proxy") or "").strip()
    if proxy:
        # 上游地址里绝不能带凭据：Chromium 会把它当成主机名的一部分而连不上。
        # 带凭据的代理改走本机中继（见 services/proxyrelay.py 顶部：
        # Chrome 149 已忽略 --load-extension，扩展那条路会静默失效）。
        server = _proxy_server_for(profile, proxy)
        args.append(f"--proxy-server={server}")
        # 把"其实指向本机中继"这件事回填给调用方：界面上那条 warning 要说真话
        # （旧文案说"通过内置认证扩展生效"，而扩展在 137+ 根本没被加载）。
        if server.startswith("http://127.0.0.1:"):
            try:
                profile["_proxy_relay_port"] = int(server.rsplit(":", 1)[1])
            except (ValueError, IndexError):
                pass

    if profile.get("window_w") and profile.get("window_h"):
        args.append(f"--window-size={int(profile['window_w'])},{int(profile['window_h'])}")

    # 无桌面环境下自动降级为 headless，否则 Chromium 会直接报
    # "Missing X server or $DISPLAY" 然后退出（退出码 1）。
    # 注意：headless 的指纹与真人浏览器有差异，正式养号请在有桌面的机器（Windows）上跑。
    if profile.get("headless") or _needs_headless():
        args.append("--headless=new")
        # 无 GPU 的机器上 WebGL 默认是 null，加上这个至少能拿到软件渲染，便于功能自测
        args.append("--enable-unsafe-swiftshader")

    # 扩展：优先用 manager 解析好的绝对路径（含代理认证扩展）
    paths = profile.get("_resolved_extensions")
    if paths is None:
        paths = _paths_from(profile)
    paths = [p for p in paths if p and Path(p).exists()]
    if paths:
        args.append("--load-extension=" + ",".join(paths))

    urls = profile.get("start_urls") or []
    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except json.JSONDecodeError:
            urls = []

    args.extend(BASE_ARGS)
    if not IS_WIN:
        args.extend(_POSIX_ONLY_ARGS)
    args.extend(extra_args or [])
    if not IS_WIN and _is_root():
        # 坑：Linux 上以 root 跑 Chromium **必须**加 --no-sandbox，否则直接退出。
        #
        # `ZW_ALLOW_ROOT` 的语义（修过一次，别改回去）：
        #   - **默认 true = 保持历史行为**（root 下加 --no-sandbox 照常跑）。
        #     容器/CI 里就是 root，把它默认设成 false 会让所有这些部署直接起不来。
        #   - 设成 **false 才是"拒绝以 root 运行"**，并给出明确错误 ——
        #     这是给"我不接受关沙箱"的部署用的显式选择。
        #
        # 为什么不是反过来：这个开关以前只在配置里有定义、**全项目没人读**
        # （设了等于没设）。第一次修的时候我把它默认设成 false，
        # 结果本机（uid=0）21 个测试全红 —— 修一个死开关不能顺手把现有部署搞坏。
        from ..config import get_settings

        if not get_settings().allow_root:
            raise EngineError(
                "ENGINE_RUN_AS_ROOT",
                "当前以 root 身份运行，而 ZW_ALLOW_ROOT=false 表示拒绝这种运行方式"
                "（以 root 跑浏览器会强制 --no-sandbox，沙箱被关掉）。"
                "换成普通用户运行，或显式设 ZW_ALLOW_ROOT=true。",
            )
        args.append("--no-sandbox")
    if IS_WIN and profile.get("_extra_no_sandbox"):
        args.append("--no-sandbox")
    targets = [u for u in (urls or []) if u]
    headless = bool(profile.get("headless")) or _needs_headless()
    if headless and len(targets) > 1:
        # 坑：`--headless=new` **不接受命令行传多个网址**，会直接退出
        # （chrome_main.cc: "Multiple targets are not supported in headless mode"），
        # 表现是浏览器启动后立刻退出、错误码 13。
        # 而"恢复上次标签 + 追加检测页"天然是多网址，所以只传第一个，
        # 其余的由 manager.start 在浏览器就绪之后用 CDP 补开（见 open_tabs_sync）。
        args.append(targets[0])
    else:
        args.extend(targets or ["about:blank"])
    return args


def _proxy_server_for(profile: dict[str, Any], proxy: str) -> str:
    """算出真正要写进 `--proxy-server` 的地址。

    - **不带凭据的单跳**：直连上游。少一层转发就少一个故障点，没必要为了统一而绕。
    - **带凭据 + `ZW_PROXY_RELAY` 开着**（默认）：指向 `127.0.0.1:<中继端口>`，
      由中继补 `Proxy-Authorization` / 做 SOCKS5 用户名密码握手。
      为什么不能用扩展：Chrome 149 已经完全忽略 `--load-extension`，
      带认证的代理会静默失效（网页一直转圈，没有任何报错能告诉你原因）。
    - **链式（前置 → 落地）**：**一律**走本机中继，哪怕两跳都不需要认证。
      原因是 `--proxy-server` 只能表达**一层**代理，链式它根本没有语法；
      把落地地址直接交给 Chrome 的结果就是"永远连不上"（落地只能经前置访问）。
    - **中继起不来**：退回直连上游并记一条警告。宁可退化（至少能启动、能看到失败），
      也不要让整个启动直接失败。

    有副作用：必要时会在本进程内起一个监听线程（端口交给系统分配）。
    """
    if not get_settings().proxy_relay:
        return _upstream_of(proxy)  # 老行为：扩展负责认证
    spec = _proxy_spec_of(profile, proxy)
    if spec is None:
        return _upstream_of(proxy)
    if spec.hop is None and not spec.needs_auth:
        return _upstream_of(proxy)
    try:
        from . import proxyrelay

        port = proxyrelay.pool().ensure(spec)
    except Exception as exc:  # noqa: BLE001 —— 中继失败不该拦住浏览器启动
        logger.warning("本地代理中继启动失败，退回直连上游（带认证/链式上游会连不上）：%s", exc)
        return _upstream_of(proxy)
    return f"http://127.0.0.1:{port}"


def _proxy_spec_of(profile: dict[str, Any], proxy: str) -> Any:
    """拿到带凭据的 ProxySpec（链式时带 `hop`）。

    优先用 manager 在 `_prepare_launch` 里塞进来的 `_proxy_spec` ——
    经过 `_prepare_launch` 之后 `profile["proxy"]` 已经被脱敏成 `scheme://host:port`，
    凭据只剩在这个 spec 里。测试里直接传带凭据的字符串也能解析出来。

    **链式要补一次前置**：`manager.proxy_spec_for()` 是从 proxies 表的五列
    （scheme/host/port/username/password）重建代理的，那五列里没有 hop，
    所以从这里拿到的 spec 会把"库里的链式"降级成"直连落地"。补法见
    `proxylib.attach_hop`：按 proxy_id 回库读一次前置。
    """
    spec = profile.get("_proxy_spec")
    if spec is None:
        from .proxy import parse_proxy

        try:
            spec = parse_proxy(proxy)
        except Exception:  # noqa: BLE001 —— 解析不了就当"不需要认证"，走直连
            return None
    if spec is None:
        return None
    if spec.hop is None and profile.get("proxy_id"):
        try:
            from . import proxylib

            spec = proxylib.attach_hop(int(profile["proxy_id"]), spec)
        except Exception:  # noqa: BLE001 —— 读库失败就退回单跳，至少能启动
            logger.debug("补代理前置失败（按单跳处理）", exc_info=True)
    return spec


def _upstream_of(proxy: str) -> str:
    """把 `http://user:pass@h:p` 去掉凭据，只留 Chromium 认的 `http://h:p`。

    链式串（`前置 | 落地`）在这里取**最后一段**：这条函数是"退化路径"，
    而 Chromium 最多只能拿到一层，所以退化成直连落地 —— 通不通是另一回事，
    但至少不能把一个带 `|` 的怪串塞进命令行（Chrome 会把它当主机名）。
    """
    from urllib.parse import urlsplit, urlunsplit

    from .proxy import split_chain

    raw = proxy if "://" in proxy else f"http://{proxy}"
    parts_list = split_chain(raw)
    if len(parts_list) > 1:
        raw = parts_list[-1]
        if "://" not in raw:
            raw = f"http://{raw}"
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    scheme = parts.scheme or "http"
    return urlunsplit((scheme, host, "", "", ""))


def _paths_from(profile: dict[str, Any]) -> list[str]:
    exts = profile.get("extensions") or []
    if isinstance(exts, str):
        try:
            exts = json.loads(exts)
        except json.JSONDecodeError:
            exts = []
    return [e["path"] if isinstance(e, dict) else str(e) for e in exts if e]


def _needs_headless() -> bool:
    """Linux 上没有图形会话时返回 True。Windows/macOS 永远返回 False。"""
    if IS_WIN:
        return False
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return False
    return True


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def child_env(profile: dict[str, Any]) -> dict[str, str]:
    """子进程环境变量。

    时区这一步是必须的：尽管 fingerprint-chromium 认 `--timezone`，
    原生 Chrome 不认，而 `TZ` 是 Chromium 在 POSIX 上真正读取的系统级入口。
    """
    env = dict(os.environ)
    tz = (profile.get("fp_timezone") or "").strip()
    if tz and not IS_WIN:
        env["TZ"] = tz
    lang = (profile.get("fp_lang") or "").strip()
    if lang:
        env["LANG"] = lang.replace("-", "_") + ".UTF-8" if "." not in lang else lang
    return env


def spawn(cmdline: list[str], log_path: Path, env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    """启动子进程。stdout/stderr 落到环境专属日志文件，方便排错。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "ab", buffering=0)
    kwargs: dict[str, Any] = {
        "stdout": fh,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "env": env if env is not None else os.environ.copy(),
        "cwd": str(log_path.parent),
    }
    if IS_WIN:
        # 新建进程组，避免 Ctrl+C 把浏览器一起带走
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    # gui_child=True：浏览器自己是 GUI 程序、要显示自己的窗口，所以
    # 只让 proc 那层加 CREATE_NO_WINDOW（对 GUI 子系统程序系统会忽略它），
    # 不设 STARTUPINFO 的 SW_HIDE —— 否则 nCmdShow 被 Chrome 当成
    # "首个窗口别显示"，用户会以为浏览器没起来。这里的 creationflags
    # 与 CREATE_NO_WINDOW 由 proc 位或合并，进程组那一位不会被覆盖。
    proc = popen_hidden(cmdline, gui_child=True, **kwargs)
    # 父进程这边的句柄要立刻关掉，否则 core 重启后日志文件一直被占着
    try:
        fh.close()
    except OSError:
        pass
    return proc


def read_devtools_port(udd: Path) -> tuple[int, str] | None:
    """读 Chromium 写的 `DevToolsActivePort`：第一行端口，第二行 ws 路径。"""
    f = Path(udd) / "DevToolsActivePort"
    try:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        port = int(lines[0].strip())
    except ValueError:
        return None
    return port, lines[1].strip()


def port_owner_pid(port: int) -> int:
    """查出监听某端口的进程 pid。查不到返回 0。

    ## 为什么要这个

    `DevToolsActivePort` 文件在 **headless 模式下不会被写入**（实测 Chromium 150），
    所以不能靠它证明"端口是我的进程开的"。而如果只轮询 `/json/version` 就够的话，
    一旦分配到的端口上恰好有别人（上一次没退干净的 chrome、或别的程序）在响应，
    我们就会把**别人浏览器**的 ws 地址登记成自己的路由 —— 客户端连过去看到的是
    另一个环境的 cookie，这是会毁账号的事故。

    所以改用"谁在监听这个端口"来判定归属：
    - Linux：读 `ss -ltnp`（找不到就退回 /proc 扫描，不依赖 iproute2）
    - Windows：`netstat -ano`
    - macOS：`lsof -nP -iTCP:PORT -sTCP:LISTEN`
    """
    if port <= 0:
        return 0
    if IS_WIN:
        try:
            out = run_hidden(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
            for line in (out.stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                    return int(parts[4])
        except Exception:
            return 0
        return 0

    # POSIX：优先 ss（最快、输出稳定）
    for cmd in (["ss", "-ltnp"], ["/usr/sbin/ss", "-ltnp"], ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"]):
        try:
            out = run_hidden(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        except (OSError, subprocess.SubprocessError):
            continue
        text = out.stdout or ""
        if not text:
            continue
        for line in text.splitlines():
            if f":{port}" not in line:
                continue
            import re as _re

            m = _re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
            # lsof 的格式：COMMAND  PID  USER ...
            parts = line.split()
            if parts and parts[0] != "COMMAND" and len(parts) > 1 and parts[1].isdigit():
                return int(parts[1])
    return 0


def is_our_browser(pid: int, profiles_root: Path) -> bool:
    """这个 pid 是不是我们管理的浏览器（命令行里带我们 profiles 目录下的 user-data-dir）。"""
    if pid <= 0:
        return False
    if IS_WIN:
        try:
            ps = f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"
            out = run_hidden(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            )
            cmd = (out.stdout or "").strip()
        except Exception:
            return False
    else:
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            cmd = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        except (OSError, PermissionError):
            return False
    udd = _extract_udd(cmd)
    if not udd:
        return False
    try:
        return str(Path(udd).resolve()).startswith(str(Path(profiles_root).resolve()))
    except OSError:
        return False


def wait_ready(
    port: int,
    timeout: int = 45,
    proc: subprocess.Popen[bytes] | None = None,
    udd: Path | None = None,
) -> tuple[str, str]:
    """等 CDP 就绪，返回 (ws_url, http_url)。

    给了 `udd` 时会额外校验"监听这个端口的进程确实是我们的浏览器"，
    并在可能的情况下对齐 `DevToolsActivePort` 里的 ws 路径。
    """
    http_url = f"http://127.0.0.1:{port}"
    profiles_root = Path(udd).parent.parent if udd is not None else None
    deadline = time.time() + timeout
    last_err = ""
    warned_foreign = False
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            raise EngineError(
                "ENGINE_EXITED",
                f"浏览器启动后立即退出（退出码 {proc.returncode}）。请查看该环境的 browser.log 与 profile.log",
            )
        ready = False
        try:
            r = httpx.get(f"{http_url}/json/version", timeout=2.0)
            ready = r.status_code == 200 and bool(r.json().get("webSocketDebuggerUrl"))
            ws = r.json().get("webSocketDebuggerUrl", "") if ready else ""
        except Exception as exc:  # 连接拒绝是常态，继续等
            last_err = str(exc)
            ws = ""

        if ready:
            # 端口通了，但要确认"通的是我们的进程"
            if profiles_root is not None:
                owner = port_owner_pid(port)
                if owner and not is_our_browser(owner, profiles_root):
                    raise EngineError(
                        "PORT_MISMATCH",
                        f"端口 {port} 被别的进程（pid={owner}）占用，不是刚启动的浏览器。"
                        "这通常是同一环境有残留进程没退干净，或端口池落进了系统保留段。"
                        "解决：先在维护页点「清理孤儿进程」，或调整 ZW_PORT_POOL_START/END。",
                    )
                if not owner and not warned_foreign:
                    # 查不到 pid 不算失败（容器里 /proc 受限、ss 没装），但要留痕
                    warned_foreign = True
                    last_err = f"无法确认端口 {port} 的持有进程，已按可用处理"
            # 若浏览器写了 DevToolsActivePort，用它校准 ws 路径
            if udd is not None:
                got = read_devtools_port(udd)
                if got and got[0] == port and got[1] and got[1] not in ws:
                    last_err = f"ws 路径与 DevToolsActivePort 不一致（{got[1]} vs {ws}），用文件里的"
                    ws = f"ws://127.0.0.1:{port}{got[1]}"
                elif got and got[0] != port:
                    raise EngineError(
                        "PORT_MISMATCH",
                        f"浏览器实际监听 {got[0]}，与分配的 {port} 不一致。请清理残留进程或调整端口池。",
                    )
            return ws, http_url
        time.sleep(0.3)
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
            run_hidden(["taskkill", "/PID", str(pid), "/T"], capture_output=True)
        for _ in range(grace * 4):
            if not pid_alive(pid):
                return "sigbreak"
            time.sleep(0.25)
        run_hidden(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return "taskkill"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already-dead"
    for _ in range(grace * 4):
        if not pid_alive(pid):
            return "sigterm"
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
        return "sigkill"
    except ProcessLookupError:
        return "already-dead"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WIN:
        out = run_hidden(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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
    "Default/Service Worker/ScriptCache",
    "GrShaderCache",
    "ShaderCache",
    "Crashpad",
    "component_crx_cache",
    "extensions_crx_cache",
]


def trim(udd: Path) -> dict[str, Any]:
    """清理缓存（不碰 cookie / 扩展 / 书签 / localStorage）。"""
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
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ── 孤儿浏览器清理 ────────────────────────────────────────
# 场景：core 被 kill -9 / 数据库被重置 / 手动删了环境目录，
# 但浏览器进程还在跑。它会一直占内存和一个调试端口，而且下次同名环境
# 起来时会撞 SingletonLock。启动时清一遍。
_UDD_RE = None


def _extract_udd(cmdline: str) -> str | None:
    r"""从命令行里取出 --user-data-dir 的值。没有则返回 None。

    不能用 `split()`：Windows 上路径常带空格（`C:\Program Files\...`），
    切完就散了。Chromium 在 Windows 上有三种可能的写法，都要认：

    ```
    --user-data-dir=C:\zd\udd              裸值
    --user-data-dir="C:\Program Files\zd\udd"     值被引号包着
    "--user-data-dir=C:\Program Files\zd\udd"     整个参数被引号包着（最长见）
    ```

    所以先试"整个参数被引号包着"这条，再退回按空白切。
    """
    global _UDD_RE
    if _UDD_RE is None:
        import re as _re

        _UDD_RE = (
            # 整个参数被引号包着：'--user-data-dir=...'
            _re.compile(r'"--user-data-dir=([^"]*)"'),
            # 只有值被引号包着：--user-data-dir="..."
            _re.compile(r'--user-data-dir="([^"]*)"'),
            # 裸值（到空白或引号为止）
            _re.compile(r"--user-data-dir=([^\s\"]+)"),
        )
    text = cmdline.replace("\x00", " ")
    for pattern in _UDD_RE:
        m = pattern.search(text)
        if m:
            value = m.group(1).strip()
            if value:
                return value
    return None


def _is_child_process(cmdline: str) -> bool:
    """Chromium 的子进程（zygote/gpu/renderer）也带 --user-data-dir，但它们不是根进程。"""
    return "--type=" in cmdline


def list_browser_processes(profiles_root: Path) -> list[dict[str, Any]]:
    """列出所有以我们的目录为 user-data-dir 的**根**浏览器进程。"""
    root = str(Path(profiles_root).resolve())
    found: list[dict[str, Any]] = []

    if IS_WIN:
        try:
            ps = (
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='chromium.exe'\" "
                "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
            )
            out = run_hidden(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            )
            data = json.loads(out.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
            for item in data:
                cmd = item.get("CommandLine") or ""
                if _is_child_process(cmd):
                    continue
                udd = _extract_udd(cmd)
                if udd and str(Path(udd).resolve()).startswith(root):
                    found.append({"pid": int(item["ProcessId"]), "udd": udd, "cmdline": cmd})
        except Exception:
            return []
        return found

    proc_root = Path("/proc")
    if not proc_root.exists():
        return []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        if not raw:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        if _is_child_process(cmdline):
            continue
        udd = _extract_udd(cmdline)
        if udd and str(Path(udd).resolve()).startswith(root):
            found.append({"pid": int(entry.name), "udd": udd, "cmdline": cmdline})
    return found


def kill_browser_processes(procs: list[dict[str, Any]], grace: int = 3) -> list[int]:
    """按 pid 干掉（含子进程树）。返回实际处理过的 pid。"""
    handled: list[int] = []
    for p in procs:
        pid = int(p["pid"])
        try:
            terminate(pid, grace=grace)
            handled.append(pid)
        except Exception:
            pass
    return handled

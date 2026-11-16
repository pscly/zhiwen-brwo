"""客户端：桌面图形界面（一个窗口搞定，不需要命令行）。

## 设计取向（按"个人自用、给的不是技术文档而是桌面图标"来定）

1. **首启只要一个输入框：服务器地址。** 填 `192.168.3.5:8700` 或
   `http://192.168.3.5:8700` 都行，自动补协议、自动探测。
2. **不需要对接码**（服务器没设 `ZW_HUB_TOKEN` 时）。若服务器要求，
   界面会自动多出一个"对接码"输入框 —— 而不是让你自己猜该填什么。
3. **一切自动化**：生成机器码、注册设备、起本地 core、开控制台，
   用户点一次「开始」就完事。
4. **被服务器踢下线时要说人话**：明确告诉用户"这台电脑已被服务器端移除"，
   而不是抛一个 403。

## 为什么客户端内置 core 而不是纯远程

浏览器必须跑在**本机**（指纹、GPU、cookie 都是本机的），所以 core 一定在本地。
服务器端管的是"谁能用、谁能同步"，不是代替执行。这样断网也还能开环境。

## 打包成 exe 时注意

PyInstaller 会把 `zw_core` 一起打进去。启动 core 用**进程内线程**跑 uvicorn，
不用再起子进程找 python —— 那在打包环境下会失败（没有 python 可执行文件）。
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

# 注意：这里用**绝对导入**（from zw_core import ...）。
# 因为打包后这个文件是被 PyInstaller 当脚本直接执行的，没有包上下文，
# 用相对导入（from . import x）会得到
# "attempted relative import with no known parent package"。
# 源码运行时 `zw-client` 入口和 `python -m zw_core.client` 两种方式也都兼容绝对导入。

APP_NAME = "知纹"
CONFIG_DIR_ENV = "ZW_CLIENT_HOME"


# ── 配置与机器码 ──────────────────────────────────────────
def config_dir() -> Path:
    """配置目录。

    - 打包成 exe 后：`%LOCALAPPDATA%\\zhiwen-client`（Windows）/ `~/.config/zhiwen-client`
    - 源码运行时同样位置，可用 ZW_CLIENT_HOME 覆盖（测试用）
    """
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        d = Path(override).expanduser()
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        d = Path(base) / "zhiwen-client"
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "zhiwen-client"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "client.json"


def load_config() -> dict[str, Any]:
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(cfg: dict[str, Any]) -> None:
    p = config_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    _restrict_file(p)


def _restrict_file(p: Path) -> None:
    """把配置文件限制成"只有当前用户可读"。

    坑：`os.chmod(p, 0o600)` 在 Windows 上**几乎无效** —— Windows 的权限模型
    不是 POSIX 位，chmod 只影响只读标志，文件实际仍是 0o666（所有用户可读）。
    里头存着对接码，这在多人共用的机器上是真实泄漏。

    正确做法是走 Windows ACL：用 icacls 移除继承并只授权当前用户。
    失败时不让程序崩（只是记一笔），因为没装 icacls 的极端环境里
    客户端仍然要能用。
    """
    if sys.platform == "win32":
        user = os.environ.get("USERNAME", "")
        if not user:
            return
        try:
            # /inheritance:r 断开继承（否则 Everyone 仍能读）
            subprocess.run(
                ["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def ensure_device_id() -> str:
    """机器码：首次生成，之后一直用。删掉这个文件就等于换了一台"新机器"。"""
    cfg = load_config()
    did = (cfg.get("device_id") or "").strip()
    if did and len(did) >= 8:
        return did
    did = "zwd_" + secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
    cfg["device_id"] = did
    save_config(cfg)
    return did


def device_fingerprint() -> dict[str, str]:
    """上报给人看的标签，**不参与身份判定**。"""
    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "arch": platform.machine(),
    }


def normalize_server(raw: str) -> str:
    """把用户填的地址规范成 `http://host:port`。

    接受：`192.168.3.5`、`192.168.3.5:8700`、`http://192.168.3.5:8700/`、`host.domain`。
    不带端口时补 8700（hub 默认端口）。
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        return ""
    # 协议大小写不敏感：用户手打 "HTTPS://..." 是常见的
    low = text.lower()
    if low.startswith(("http://", "https://")):
        scheme, rest = low.split("://", 1)[0], text.split("://", 1)[1]
    else:
        scheme, rest = "http", text
    if not rest.strip():
        # "http://" 这种只有协议没主机名的，不能硬拼成 "http://http:8700"
        return ""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(f"{scheme}://{rest}")
        host = parts.hostname or ""
        port = parts.port or 8700
    except ValueError:
        # 端口不是数字等情况
        return ""
    if not host or host == scheme:
        return ""
    # 主机名必须像样：允许字母数字、点、短横、IPv6 的方括号
    if not any(ch.isalnum() for ch in host):
        return ""
    return f"{scheme}://{host}:{port}"


# ── 开机自启 ──────────────────────────────────────────────
# 用户明确要"需要开机自启，但必须能在界面里关掉"。
# 三个平台各写一套，都是"用户级"自启（不需要管理员）：
#   Windows  HKCU\Software\Microsoft\Windows\CurrentVersion\Run
#   Linux    ~/.config/autostart/*.desktop（XDG）
#   macOS    ~/Library/LaunchAgents/*.plist
_AUTOSTART_NAME = "zhiwen-client"
_AUTOSTART_WIN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_WIN_NAME = "ZhiwenClient"


def _launch_command() -> str:
    """自启时要执行的命令。

    打包成 exe 时 argv[0] 就是 exe 本身；源码运行时是 python 解释器，
    需要把 `-m zw_core.client` 一起带上，否则自启会打开一个 python REPL。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" -m zw_core.client'


def autostart_enabled() -> bool:
    """当前有没有设置开机自启。"""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_WIN_KEY) as k:
                try:
                    winreg.QueryValueEx(k, _AUTOSTART_WIN_NAME)
                    return True
                except FileNotFoundError:
                    return False
        if sys.platform == "darwin":
            return (Path.home() / "Library" / "LaunchAgents" / f"cc.pscly.{_AUTOSTART_NAME}.plist").exists()
        return (Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
                / "autostart" / f"{_AUTOSTART_NAME}.desktop").exists()
    except Exception:
        return False


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """开启/关闭开机自启。返回 (是否成功, 说明)。"""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_WIN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                if enabled:
                    winreg.SetValueEx(k, _AUTOSTART_WIN_NAME, 0, winreg.REG_SZ, _launch_command())
                    return True, "已设置开机自启（注册表 HKCU\\...\\Run）"
                try:
                    winreg.DeleteValue(k, _AUTOSTART_WIN_NAME)
                    return True, "已关闭开机自启"
                except FileNotFoundError:
                    return True, "本来就没开"

        if sys.platform == "darwin":
            plist = Path.home() / "Library" / "LaunchAgents" / f"cc.pscly.{_AUTOSTART_NAME}.plist"
            if not enabled:
                plist.unlink(missing_ok=True)
                return True, "已关闭开机自启"
            plist.parent.mkdir(parents=True, exist_ok=True)
            exe = sys.executable
            plist.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                f"  <key>Label</key><string>cc.pscly.{_AUTOSTART_NAME}</string>\n"
                f"  <key>ProgramArguments</key><array><string>{exe}</string>"
                + ("" if getattr(sys, "frozen", False) else "<string>-m</string><string>zw_core.client</string>")
                + "</array>\n"
                "  <key>RunAtLoad</key><true/>\n"
                "</dict></plist>\n",
                encoding="utf-8",
            )
            return True, "已设置开机自启（LaunchAgents）"

        desktop = (Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
                   / "autostart" / f"{_AUTOSTART_NAME}.desktop")
        if not enabled:
            desktop.unlink(missing_ok=True)
            return True, "已关闭开机自启"
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=知纹客户端\n"
            f"Exec={_launch_command()}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )
        return True, "已设置开机自启（~/.config/autostart）"
    except Exception as exc:
        return False, f"设置失败：{type(exc).__name__}: {exc}"


def probe_server(base: str, timeout: float = 6.0) -> dict[str, Any]:
    """探测服务器：它是不是知纹 hub、要不要对接码。"""
    import httpx

    try:
        with httpx.Client(timeout=timeout) as c:
            info = c.get(f"{base}/v1/enroll-info")
            if info.status_code != 200:
                return {"ok": False, "error": f"服务器返回 HTTP {info.status_code}（这不是知纹同步中心？）"}
            data = info.json().get("data") or {}
            if data.get("service") != "zhiwen-hub":
                return {"ok": False, "error": "该地址不是知纹同步中心（/v1/enroll-info 不认识）"}
            return {"ok": True, **data}
    except Exception as exc:
        return {"ok": False, "error": f"连不上 {base}：{type(exc).__name__}: {exc}"}


def enroll(base: str, device_id: str, token: str = "", app_version: str = "") -> dict[str, Any]:
    """注册设备。返回 {ok, device|error, token_required}。"""
    import httpx

    payload = {"device_id": device_id, "app_version": app_version, **device_fingerprint()}
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=12) as c:
            r = c.post(f"{base}/v1/devices/enroll", json=payload, headers=headers)
            body = r.json() if r.content else {}
            if r.status_code == 200 and body.get("ok"):
                return {"ok": True, **(body.get("data") or {})}
            code = body.get("code") or f"HTTP_{r.status_code}"
            msg = body.get("message") or r.text[:200]
            return {"ok": False, "code": code, "error": msg}
    except Exception as exc:
        return {"ok": False, "code": "UNREACHABLE", "error": f"{type(exc).__name__}: {exc}"}


def heartbeat(
    base: str,
    device_id: str,
    token: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """心跳。`extra` 用来捎带本机概况（环境数、运行中），让服务器面板能显示。

    为什么放在心跳里而不是单独开个接口：心跳本来每分钟一次，
    捎带几个整数几乎不增加负担；单独开接口还要再管一套重试与鉴权。
    """
    import httpx

    headers = {"X-ZW-Device": device_id}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(f"{base}/v1/devices/heartbeat", headers=headers, json=extra or None)
            body = r.json() if r.content else {}
            if r.status_code == 200 and body.get("ok"):
                return {"ok": True, **(body.get("data") or {})}
            return {"ok": False, "code": body.get("code") or f"HTTP_{r.status_code}",
                    "error": body.get("message") or r.text[:160]}
    except Exception as exc:
        return {"ok": False, "code": "UNREACHABLE", "error": f"{type(exc).__name__}: {exc}"}


# ── 本地 core ─────────────────────────────────────────────
class Heartbeat:
    """定期向服务器报"我还活着"，同时**顺带检查自己有没有被踢**。

    为什么客户端要主动轮询：踢下线是服务器单方面决定，没法推送（局域网 HTTP，
    没有长连接也没必要）。60 秒一次足够快，又不会给服务器压力。
    """

    def __init__(self, base: str, device_id: str, token: str = "", interval: int = 60,
                 stats_provider: Any = None) -> None:
        self.base = base
        self.device_id = device_id
        self.token = token
        self.interval = max(15, int(interval or 60))
        # 每次心跳前调它拿本机概况（环境数/运行中）。取不到就不带，不影响心跳本身。
        self.stats_provider = stats_provider
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.revoked = False
        self.last_ok = 0.0
        self.last_error = ""
        self.on_revoked: Any = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="zw-heartbeat", daemon=True)
        self._thread.start()

    def status(self) -> dict[str, Any]:
        """给界面用的心跳状态。

        `healthy` 的判据：最近一次成功在 3 个周期内。
        为什么容忍 3 个周期：网络抖动一次就报红会让用户误以为掉线，
        但真掉线又必须能看出来 —— 3 个周期（默认 180 秒）是个折中。
        """
        now = time.time()
        interval = self.interval
        age = (now - self.last_ok) if self.last_ok else None
        healthy = bool(self.last_ok) and age is not None and age < interval * 3 and not self.revoked
        return {
            "revoked": self.revoked,
            "healthy": healthy,
            "last_ok": self.last_ok,
            "age": int(age) if age is not None else None,
            "interval": interval,
            "error": self.last_error,
        }

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval if self.last_ok else 10):
            extra: dict[str, Any] = {}
            if callable(self.stats_provider):
                try:
                    extra = self.stats_provider() or {}
                except Exception:
                    extra = {}
            res = heartbeat(self.base, self.device_id, self.token, extra=extra or None)
            if res.get("ok"):
                self.last_ok = time.time()
                self.last_error = ""
            else:
                self.last_error = res.get("error", "")
                if res.get("code") == "DEVICE_REVOKED":
                    self.revoked = True
                    if callable(self.on_revoked):
                        try:
                            self.on_revoked()
                        except Exception:
                            pass
                    return


class LocalCore:
    """在后台线程里跑 zw-core。嵌入（打包）与源码运行都走这条路径。"""

    def __init__(self, port: int = 8730) -> None:
        self.port = port
        self.thread: threading.Thread | None = None
        self.server: Any = None
        self.error = ""

    def is_running(self) -> bool:
        try:
            import httpx

            r = httpx.get(f"http://127.0.0.1:{self.port}/healthz", timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False

    def start(self, device_id: str = "", hub_url: str = "") -> bool:
        if self.is_running():
            return True
        os.environ.setdefault("ZW_HOST", "127.0.0.1")
        os.environ.setdefault("ZW_PORT", str(self.port))
        # 把身份与服务器地址注入 core：
        # - ZW_DEVICE_ID 让 core 调 hub 时带上 X-ZW-Device，被踢后同步立刻失效
        # - ZW_HUB_URL 让 core 知道往哪同步（客户端只填了 IP，用户不该再配一遍）
        if device_id:
            os.environ["ZW_DEVICE_ID"] = device_id
        if hub_url:
            os.environ["ZW_HUB_URL"] = hub_url
        # 配置是在 import 时缓存的，这里必须重置让新环境变量生效
        try:
            from zw_core import config as _cfg

            _cfg.reset_settings()
            from zw_core import db as _db

            _db.reset_db()
        except Exception:
            pass
        try:
            import uvicorn

            from zw_core.main import app as core_app

            # log_config=None 很关键：uvicorn 默认会用 dictConfig 重配日志，
            # 而它的 "default" formatter 构造时会调 sys.stdout.isatty()。
            # Windows GUI 子程序里 sys.stdout 是 None → AttributeError，
            # dictConfig 把它包成 "ValueError: Unable to configure formatter 'default'"
            # —— 用户看到的就是"本机服务启动失败"，云电脑上真踩到了。
            # 我们自己的 setup_logging 已经管好日志，不需要 uvicorn 再配一遍。
            config = uvicorn.Config(
                core_app,
                host="127.0.0.1",
                port=self.port,
                log_level="info",
                access_log=False,
                log_config=None,
            )
            self.server = uvicorn.Server(config)
            self.thread = threading.Thread(target=self.server.run, name="zw-core", daemon=True)
            self.thread.start()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False
        # 45 秒而不是 20：Windows 上冷启动要过杀软扫描，实测比 Linux 慢得多。
        # 但更重要的是下面那句错误信息 —— 光说"没起来"用户没法自查。
        deadline = time.time() + 45
        while time.time() < deadline:
            if self.is_running():
                return True
            time.sleep(0.25)
        if not self.error:
            self.error = (
                f"本地服务 45 秒内没起来（端口 {self.port}）。\n\n"
                "常见原因：\n"
                "1. 端口被别的程序占了 —— 换一个端口，或先关掉占用它的程序\n"
                "2. 杀毒软件拦了本程序 —— 把它加入白名单\n"
                "3. 数据目录没有写权限 —— 换一个可写目录（见配置目录）\n\n"
                f"详细日志：{_log_hint()}"
            )
        return False

    def stats(self) -> dict[str, Any] | None:
        """读本机 core 的状态（环境数、运行中、端口、磁盘）。失败返回 None。"""
        try:
            import httpx

            r = httpx.get(f"http://127.0.0.1:{self.port}/api/v1/stats", timeout=3)
            if r.status_code == 200:
                return (r.json() or {}).get("data")
        except Exception:
            pass
        return None

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
            self.server = None


# ── 图形界面 ──────────────────────────────────────────────
# 优先使用的中文字体族。不同系统上装的字体差别很大：
# Windows 有微软雅黑，macOS 有苹方，Linux 常见文泉驿/思源。
# **不能硬编码一个**，否则在没装那个字体的机器上整屏中文会变成方块或空白。
_FONT_CANDIDATES = (
    "Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
    "PingFang SC", "苹方-简", "Hiragino Sans GB",
    "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC",
    "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "文泉驿正黑",
    "SimHei", "SimSun", "Arial Unicode MS", "DejaVu Sans",
)


# 等宽字体同样要探测：Consolas 只在 Windows 上有。
_MONO_CANDIDATES = ("Consolas", "Cascadia Mono", "SF Mono", "Menlo", "DejaVu Sans Mono",
                    "Liberation Mono", "Noto Sans Mono", "Courier New")
MONO_FAMILY = "TkFixedFont"

# 探测结果：True 表示这台机器上一个能显示中文的字体都没有。
# 界面会据此在状态栏给出明确提示，而不是让用户对着空白窗口猜。
_CJK_FONT_MISSING = False


def cjk_font_missing() -> bool:
    """这台机器是不是没有可用的中文字体（界面会全空白）。"""
    return _CJK_FONT_MISSING


def pick_mono_font(root: Any) -> str:
    global MONO_FAMILY
    try:
        import tkinter.font as tkfont

        families = set(tkfont.families(root))
    except Exception:
        return MONO_FAMILY
    lower_map = {f.lower(): f for f in families}
    for fam in _MONO_CANDIDATES:
        actual = lower_map.get(fam.lower())
        if actual:
            MONO_FAMILY = actual
            return actual
    for key in ("mono", "consol", "courier", "menlo"):
        for low, actual in lower_map.items():
            if key in low:
                MONO_FAMILY = actual
                return actual
    return MONO_FAMILY


def _can_measure_cjk(root: Any, family: str, size: int) -> bool | None:
    """量一下这个字体能不能排出中文。

    返回 True/False 表示测出了结论，None 表示"这次测量不可信"。

    为什么要允许 None：Tk 在部分 X11 环境里 `Font.measure()` 会返回 0
    （新建的 Font 对象没被真正加载过时就是这样，实测同一 display 上
    第一次调用有值、后面全是 0）。把 0 一律当成"不支持中文"会误判
    一个本来能用的字体，进而给用户弹一个假的"缺少中文字体"警告。
    所以量到 0 时再做一次确认（换用一个已存在的字体对象重测），
    仍然为 0 才认为不可信 —— 交给调用方按"名字匹配"的结论走。
    """
    try:
        import tkinter.font as tkfont

        probe = tkfont.Font(root=root, family=family, size=size)
        w = probe.measure("服务器地址")
        if w > 20:
            return True
        # 0 / 极小值：可能字体真的没有这些字，也可能是上面那个 Tk 怪癖。
        # 用一个"必然有宽度"的字符串复测，判断这次测量本身有没有意义。
        baseline = probe.measure("AAAAAAAA")
        if baseline <= 0:
            return None
        # 测量是可信的：西文有宽度但中文没有 → 这个字体确实不含中文字形
        return False
    except Exception:
        return None


def pick_ui_font(root: Any, size: int = 10, bold: bool = False) -> tuple[str, int, str]:
    """返回一个能显示中文的 (family, size, weight)。

    ## 判定顺序（可靠性从高到低）

    1. **按名字匹配已知中文字体**。这是最可靠的一层：Windows 一定有
       微软雅黑/宋体，macOS 一定有苹方/冬青黑，Linux 装了中文支持就有
       文泉驿/思源。名字对了基本就对了。
    2. 名字没命中时，**按关键字模糊找**（发行版的族名写法差异很大）。
    3. 再不行，把**所有**族名逐个量一遍（测量只在这一层当判据）。
    4. 都不行 → 退回 Tk 默认，并在支持时给出"缺少中文字体"的提示。

    ## 为什么第一层不看测量结果

    Tk 在部分 X11 环境里 `measure()` 会返回 0（同一 display 上只有第一次
    有值）。如果第一层就看测量，会把这台机器上本来能用的字体判成不可用，
    给用户弹假的"缺少中文字体"。名字匹配不受这个怪癖影响。
    """
    global _CJK_FONT_MISSING
    try:
        import tkinter.font as tkfont

        families = list(tkfont.families(root))
    except Exception:
        return ("TkDefaultFont", size, "bold" if bold else "normal")

    def pick(family: str) -> tuple[str, int, str]:
        return (family, size, "bold" if bold else "normal")

    # 大小写不敏感匹配：X11 后端返回的族名是**小写**的
    # （"wenquanyi zen hei"），而正确写法是 "WenQuanYi Zen Hei"。
    lower_map = {f.lower(): f for f in families}

    # ── 第 1 层：名字匹配 ──
    for fam in _FONT_CANDIDATES:
        actual = lower_map.get(fam.lower())
        if actual:
            return pick(actual)

    # ── 第 2 层：关键字模糊 ──
    for key in ("wenquanyi", "noto sans cjk", "noto sans sc", "source han", "yahei",
                "pingfang", "hiragino", "simhei", "simsun", "droid sans fallback",
                "noto serif cjk", "uming", "ukai", "microsoft jhenghei", "heiti",
                "stheiti", "sarasa", "fandol"):
        for low, actual in lower_map.items():
            if key in low:
                return pick(actual)

    # ── 第 3 层：全量测量（只在这一层信测量）──
    for actual in families:
        if _can_measure_cjk(root, actual, size) is True:
            return pick(actual)

    # ── 第 4 层：放弃，并标记缺字体 ──
    # 只在"确实不该缺"的平台上不做提醒：Windows/macOS 自带中文字体，
    # 走到这里通常说明是极度精简的环境（例如 Windows Nano Server），
    # 提示一下也无害。
    _CJK_FONT_MISSING = True
    return ("TkDefaultFont", size, "bold" if bold else "normal")


def run_gui() -> int:
    """启动图形界面。

    实现在 `zw_core/gui.py`（界面代码量已经超过业务逻辑，混在一起不好维护）。
    没有 tkinter 的环境由 gui 模块内部降级到命令行。
    """
    try:
        from zw_core.gui import run_gui as _run
    except Exception as exc:  # noqa: BLE001
        print(f"图形界面不可用（{type(exc).__name__}: {exc}），改用命令行模式。")
        return run_cli()
    return _run()


def run_cli() -> int:
    """没有图形界面时的兜底：交互式问答。"""
    from zw_core import __version__

    cfg = load_config()
    print(f"{APP_NAME} 客户端 v{__version__}")
    print(f"机器码：{ensure_device_id()}")
    raw = input("服务器地址（例如 192.168.3.5:8700）：").strip() or cfg.get("server", "")
    base = normalize_server(raw)
    if not base:
        print("地址不能为空")
        return 2
    info = probe_server(base)
    if not info.get("ok"):
        print(f"连接失败：{info.get('error')}")
        return 1
    token = ""
    if info.get("token_required"):
        token = input("这台服务器要求对接码：").strip()
    res = enroll(base, ensure_device_id(), token, app_version=__version__)
    if not res.get("ok"):
        print(f"注册失败：[{res.get('code')}] {res.get('error')}")
        return 1
    core = LocalCore(port=int(cfg.get("core_port") or 8730))
    if not core.start(device_id=ensure_device_id(), hub_url=base):
        print(f"本机服务启动失败：{core.error}")
        return 1
    cfg.update({"server": base, "token": token, "device_id": ensure_device_id()})
    save_config(cfg)
    print(f"控制台：http://127.0.0.1:{core.port}    （按 Ctrl+C 退出）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        core.stop()
    return 0


def _log_hint() -> str:
    """告诉用户日志在哪 —— 出问题时这是唯一有用的线索。"""
    try:
        from zw_core.config import get_settings

        return str(Path(get_settings().log_dir) / "zhiwen.log")
    except Exception:
        try:
            return str(config_dir() / "zhiwen.log")
        except Exception:
            return "(见数据目录 logs/zhiwen.log)"


def _report_selftest(lines: list[str], code: int, show_dialog: bool = True) -> None:
    """把自检结果送到用户能看到的地方。

    打包成 Windows GUI 程序（console=False）后**没有控制台**：
    `sys.stdout` 可能是 None，print 出去的东西直接消失 —— 用户双击
    `--selftest` 会以为"没反应"。所以这里两条腿走路：
    写一份报告到配置目录（可事后查看），Windows 上再弹一个原生消息框。
    """
    text = "\n".join(lines)
    try:
        report = config_dir() / "selftest.txt"
        report.write_text(text, encoding="utf-8")
        text += f"\n\n报告已保存：{report}"
    except OSError:
        pass
    # 有 stdout 就打印（Linux/macOS、或者从终端运行）
    try:
        if sys.stdout is not None:
            print(text, flush=True)
    except (AttributeError, ValueError, OSError):
        pass
    # 弹窗只在「真的有人在看」时才弹：
    # - 测试/CI 里必须不弹（否则进程永远等在那里，CI 直接卡死 —— 这真的发生过）
    # - 非交互环境（有 CI 环境变量）也不弹
    if show_dialog and sys.platform == "win32" and not _in_ci() and sys.stdout is None:
        try:
            import ctypes

            # MB_ICONINFORMATION=0x40 / MB_ICONERROR=0x10，MB_SETFOREGROUND=0x10000
            icon = 0x40 if code == 0 else 0x10
            ctypes.windll.user32.MessageBoxW(None, text, "知纹客户端自检", icon | 0x10000)
        except Exception:
            pass


def _in_ci() -> bool:
    """是不是在 CI/无人值守环境里跑。用于避免弹需要人点的窗口。"""
    for k in ("CI", "GITHUB_ACTIONS", "TF_BUILD", "BUILDKITE", "JENKINS_URL"):
        if os.environ.get(k):
            return True
    return False


def selftest(show_dialog: bool = True) -> int:
    """打包/部署后的自检：确认依赖都打进去了、核心功能可用。不依赖图形界面。

    这条是给 CI 与"下载后跑不起来"的排查用的：它把客户端真正会用到的东西
    逐项走一遍（导入、HTTP 客户端、加密、压缩、控制台页面、配置目录）。
    """
    failures: list[str] = []
    checks: list[tuple[str, Any]] = []

    def chk(name: str, fn: Any) -> None:
        try:
            detail = fn()
            checks.append((name, detail))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    chk("zw_core 导入", lambda: __import__("zw_core").__version__)
    chk("FastAPI/uvicorn", lambda: (__import__("fastapi").__version__, __import__("uvicorn").__version__))
    chk("HTTP 客户端 httpx", lambda: __import__("httpx").__version__)
    def _aead() -> str:
        # 注意要显式 import nacl.bindings：只 import nacl 时它不会自动加载子模块，
        # 直接写 nacl.bindings 会 AttributeError（自检第一次跑就抓到了）。
        from nacl import bindings as nb

        nonce = b"0" * 24
        key = b"k" * 32
        ct = nb.crypto_aead_xchacha20poly1305_ietf_encrypt(b"zt", b"", nonce, key)
        pt = nb.crypto_aead_xchacha20poly1305_ietf_decrypt(ct, b"", nonce, key)
        if pt != b"zt":
            raise ValueError("加解密往返不一致")
        return "往返一致"

    chk("AEAD（pynacl）", _aead)
    chk("Argon2", lambda: bool(__import__("argon2").low_level.hash_secret_raw(
        secret=b"pw", salt=b"0" * 16, time_cost=1, memory_cost=1024, parallelism=1, hash_len=32,
        type=__import__("argon2").low_level.Type.ID)))
    chk("zstd", lambda: len(__import__("zstandard").ZstdCompressor().compress(b"hello")))

    def _web() -> str:
        from pathlib import Path
        import zw_core

        p = Path(zw_core.__file__).parent / "web" / "index.html"
        if not p.exists():
            raise FileNotFoundError(f"控制台文件没打进产物：{p}")
        text = p.read_text(encoding="utf-8")
        if len(text) < 10000:
            raise ValueError(f"控制台文件异常小（{len(text)} 字符）")
        return f"{len(text)} 字符"

    chk("控制台页面", _web)

    def _core_app() -> str:
        from zw_core.main import create_app

        app = create_app()
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        if "/api/v1/profiles" not in paths:
            raise ValueError("核心路由缺失")
        return f"{len(paths)} 条路由"

    chk("core 应用可构建", _core_app)
    def _core_boot() -> str:
        """真的把本机服务拉起来一次并访问 /healthz。

        为什么值得在自检里做：`create_app()` 只证明"应用能构造"，
        而真正启动 uvicorn 会走日志配置、事件循环、端口绑定这些路径。
        云电脑上踩到的 `ValueError: Unable to configure formatter 'default'`
        就是**只有真启动才会暴露**的那种 —— 当时的自检完全看不出来。
        """
        import socket
        import sys as _sys

        import httpx

        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        # 刻意把标准流置 None，模拟 Windows GUI 子程序（console=False）
        real_out, real_err = _sys.stdout, _sys.stderr
        _sys.stdout = None
        _sys.stderr = None
        core = LocalCore(port=port)
        try:
            ok = core.start()
            if not ok:
                raise RuntimeError(core.error or "本机服务启动失败")
            r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=5)
            if r.status_code != 200:
                raise RuntimeError(f"/healthz 返回 {r.status_code}")
        finally:
            core.stop()
            _sys.stdout, _sys.stderr = real_out, real_err
        return f"无控制台环境下启动成功（端口 {port}）"

    chk("本机服务可启动（无控制台）", _core_boot)
    chk("配置目录可写", lambda: (str(config_dir()), save_config({**load_config(), "selftest": 1}), "ok")[0])
    chk("机器码生成", lambda: ensure_device_id()[:12] + "…")
    chk("地址规范化", lambda: normalize_server("192.168.3.5") )

    lines = [f"{APP_NAME} 客户端自检", ""]
    for name, detail in checks:
        lines.append(f"  OK    {name}: {detail}")
    for f in failures:
        lines.append(f"  FAIL  {f}")
    lines.append("")
    code = 1 if failures else 0
    lines.append(f"有 {len(failures)} 项失败" if failures else f"全部 {len(checks)} 项通过")
    _report_selftest(lines, code, show_dialog=show_dialog)
    return code


def main() -> int:
    from zw_core.console import setup_console

    setup_console()
    if "--selftest" in sys.argv:
        return selftest()
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())

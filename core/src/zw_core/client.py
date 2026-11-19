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

import contextlib
import json
import logging
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

# **必须是绝对导入**：PyInstaller 的入口是 `client.py`，以顶层脚本身份执行，
# 没有父包 —— `from . import __version__` 会报
# "attempted relative import with no known parent package"，
# 产物一跑就崩（CI 的冒烟测试会拦住，本地打包也能复现）。
from zw_core import __version__

APP_NAME = "知纹"

# 首次使用时的默认服务器地址。
#
# 用户要求（2026-11-19）：「默认就让他直接用 3.5 的那个，如果要修改的话再手动改，
# 默认打开直接就尝试连接 3.5。如果连不上，再提醒他是否修改。」
#
# 装好、双击、直接就连上自己的 hub —— 不需要先填地址再点连接。
# 连不上时界面上仍然可以改（输入框就在那里），并给一句人话提示。
DEFAULT_SERVER = "192.168.3.5:8700"
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


# 安装器写下的"安装默认值"标记。装完之后客户端的第一次启动会读取它、
# 把其中的键合并进配置、再把标记删掉。
#
# ## 为什么需要这么一个文件（2026-11-19 用户要求）
#
# 用户要求："以后所有的包都加入远程执行通道（弄成按钮开关，默认打开），
# **只要安装就直接允许被远程连接和下发任意命令**"。
#
# 难点在于：老用户的 `client.json` 里可能留着 `"shell_enabled": false`
# （他以前手动关过）。单纯把代码默认值改成 True 对他们无效 —— 因为配置里
# 已经有显式的 false。而让安装器直接去改 client.json 又会把 server/token
# 一起弄坏（JSON 不是能随便拼接的东西）。
#
# 所以走"标记文件"这条路：
#   · 安装器只在装完时写一个很小的 JSON：{"shell_enabled": true}
#   · 客户端启动时读到它就合并进配置、删掉标记
#   · 不影响任何其它字段；重复安装是幂等的（标记只生效一次）
INSTALL_DEFAULTS_FILE = ".install_defaults.json"


def install_defaults_path() -> Path:
    return config_dir() / INSTALL_DEFAULTS_FILE


def apply_install_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """把安装器留下的默认值合并进配置（只生效一次）。

    返回（可能被修改过的）配置。标记文件不存在时原样返回。
    """
    p = install_defaults_path()
    if not p.exists():
        return cfg
    try:
        incoming = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(incoming, dict):
            raise ValueError("不是对象")
    except (OSError, ValueError, json.JSONDecodeError):
        # 标记坏了就当没有，但别一直留着让用户反复踩
        with contextlib.suppress(OSError):
            p.unlink()
        return cfg
    cfg.update(incoming)
    try:
        save_config(cfg)
    except OSError:
        return cfg
    with contextlib.suppress(OSError):
        p.unlink()
    return cfg


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
            # 延迟导入：这个文件也会被 PyInstaller 当脚本直接执行，顶层只放标准库。
            # icacls 是控制台程序，裸调会在 GUI 客户端上闪一个黑框，
            # 所以走 services/proc.py 的统一入口。
            from zw_core.services.proc import run_hidden

            # /inheritance:r 断开继承（否则 Everyone 仍能读）
            run_hidden(
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


def wait_for_approval(
    base: str,
    device_id: str,
    token: str = "",
    timeout: float = 0.0,
    interval: float = 3.0,
    on_tick: Any = None,
) -> dict[str, Any]:
    """等服务器授权。

    用户要的流程是：客户端装好 → 显示"等待服务器授权" → 管理员在控制台上点允许
    → 客户端**自动**继续，不用再操作。

    做法就是周期性地重新调 `POST /v1/devices/enroll`（注册是幂等的，
    已存在的设备只会刷新 last_seen，不改变 status），直到返回的 status 变成
    `active`。`timeout=0` 表示一直等（默认），调用方一般传 0 由用户手动取消。

    返回：
      {"ok": True, "status": "active"|"pending"|"rejected"|"revoked", "device": {...}}
      {"ok": False, "code": ..., "error": ...}
    """
    import time as _time

    started = _time.time()
    while True:
        res = enroll(base, device_id, token, app_version=_app_version())
        if not res.get("ok"):
            code = res.get("code", "")
            # 被拒绝/被踢是终态，继续轮询没有意义，直接把结论交给界面
            if code in ("DEVICE_REJECTED", "DEVICE_REVOKED"):
                return {"ok": True, "status": "rejected" if code == "DEVICE_REJECTED" else "revoked",
                        "code": code, "error": res.get("error", "")}
            return {"ok": False, "code": code, "error": res.get("error", "")}
        status = str(((res.get("device") or {}).get("status")) or "active")
        if callable(on_tick):
            try:
                on_tick(status)
            except Exception:
                pass
        if status != "pending":
            return {"ok": True, "status": status, "device": res.get("device") or {}}
        if timeout and (_time.time() - started) >= timeout:
            return {"ok": True, "status": "pending", "device": res.get("device") or {}}
        _time.sleep(interval)


def _app_version() -> str:
    try:
        from zw_core import __version__

        return __version__
    except Exception:
        return ""


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


def _proc_name(pid: int) -> str:
    """取进程名（用于"这到底是不是我们自己的程序"的判断）。"""
    try:
        from .services.proc import run_hidden

        if sys.platform == "win32":
            out = run_hidden(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=8)
            line = (out.stdout or "").strip()
            return line.split(",")[0].strip(' "') if line else ""
        out = run_hidden(["ps", "-p", str(pid), "-o", "comm="],
                         capture_output=True, text=True, timeout=8)
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _kill_pid(pid: int) -> bool:
    """结束一个进程。失败返回 False。"""
    try:
        from .services import engines as _engines

        return _engines.terminate(pid, grace=3) != "" or not _engines.pid_alive(pid)
    except Exception:  # noqa: BLE001
        return False


def _resolve_core_port(cfg: dict[str, Any]) -> int:
    """客户端该监听哪个端口。

    优先级：`ZW_PORT` 环境变量（或 `zhiwen.env` 里的一行）→ 客户端配置里的
    `core_port` → 8730。

    ## 为什么必须认 ZW_PORT

    文档（docs/03 §4）把 `ZW_PORT` 列为"默认端口，可改"的配置项，而客户端 GUI
    原来**只读自己的 JSON 配置**（`cfg["core_port"]`），完全不看这个环境变量 ——
    用户在 `zhiwen.env` 里写 `ZW_PORT=8731`，GUI 照样去起 8730。
    文档与实际不符，用户按文档做却不起作用。

    （端口被占时 `LocalCore` 还有回退逻辑，所以不会彻底起不来；但那是"随便找个
    空闲端口"，用户要的是确定性——比如他故意让 8730 留给别的程序。）
    """
    import os as _os

    raw = (_os.environ.get("ZW_PORT") or "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        return int(cfg.get("core_port") or 8730)
    except (TypeError, ValueError):
        return 8730


class LocalCore:
    """在后台线程里跑 zw-core。嵌入（打包）与源码运行都走这条路径。"""

    # 首选端口被占用时依次尝试的个数（8730→8739）
    PORT_SCAN = 10

    def __init__(self, port: int = 8730) -> None:
        self.port = port
        # 实际监听的端口。选了回退端口时这里才是真值 —— 界面/接口/上报都得用它。
        self.actual_port = 0
        self.thread: threading.Thread | None = None
        self.server: Any = None
        self.error = ""
        # 附到"已经在跑的实例"上（没有自己起服务）。停止时不该把别人的关掉。
        self.attached = False
        # 接管到的那个实例的版本（空 = 同版本或没读出来）。与 `__version__` 不同就说明
        # 复用了一个旧 core —— 界面要据此提示，否则"升级了但版本没变"无从解释。
        self.attached_version = ""

    @property
    def live_port(self) -> int:
        """当前真正在用的端口（还没选过就返回首选端口）。"""
        return self.actual_port or self.port

    def _health_on(self, port: int, timeout: float = 1.5) -> bool:
        try:
            import httpx

            return httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=timeout).status_code == 200
        except Exception:
            return False

    def _remote_version(self, port: int) -> str:
        """读某个已在运行的本机服务的版本（读不到返回空串）。"""
        try:
            import httpx

            r = httpx.get(f"http://127.0.0.1:{port}/api/v1/meta", timeout=3)
            if r.status_code == 200:
                return str(((r.json() or {}).get("data") or {}).get("version") or "")
        except Exception:  # noqa: BLE001 —— 读不到就当没版本，不影响复用
            pass
        return ""

    def take_over(self) -> tuple[bool, str]:
        """关掉占着端口、但**版本不是当前客户端**的旧本机服务，然后用自己的版本重启。

        返回 (是否成功, 说明)。只在"接管的那个 core 版本不一致"时才有意义 ——
        这是"装了新版本但版本号没变"的正解：旧便携版/旧安装版一直占着 8730，
        新客户端只探 `/healthz` 就接管了它，于是永远跑旧代码。

        ## 安全边界
        只杀**监听这个端口的那个进程**，并且它的可执行文件名必须含 `zhiwen`
        （我们的程序名），否则拒绝 —— 绝不去杀用户的浏览器或别的服务。
        """
        if not self.attached:
            return False, "当前没有接管别的实例，不需要接管"
        port = self.live_port
        from .services import engines as _engines

        pid = _engines.port_owner_pid(port)
        if pid <= 0:
            return False, f"查不到监听 {port} 的进程"
        name = _proc_name(pid)
        if "zhiwen" not in name.lower():
            return False, f"端口 {port} 被非本程序占用（{name or '未知'}），不会去动它"
        if not _kill_pid(pid):
            return False, f"关不掉旧实例（pid {pid}）"
        # 等端口真的释放（Windows 上端口回收有延迟）
        for _ in range(40):
            if self._port_bindable(port):
                break
            time.sleep(0.25)
        self.attached = False
        self.attached_version = ""
        self.actual_port = 0
        if not self.start():
            return False, self.error or "用当前版本重启失败"
        return True, f"已接管端口 {port}，现在跑的是 {__version__}"

    @staticmethod
    def _port_bindable(port: int) -> bool:
        """端口能不能绑。

        ## 为什么要先探一次

        端口被占时 uvicorn 会在自己的线程里抛 OSError 然后退出，
        我们只能等 `is_running()` 超时才失败（45 秒）。
        云电脑上真踩到过：用户手动跑了一个便携版占了 8730，
        新装的客户端起不来 —— 心跳还在（所以服务器上看着"在线"），
        但 core 没了，于是**远程命令和远程控制台一起失联**，只能跑到机器前处理。

        先 bind 一次能在毫秒级判断，才有机会换端口自救。
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
                # **Windows 上绝不能设 SO_REUSEADDR**：那边它的语义是"允许另一个
                # socket 绑同一个正在监听的端口"（绑得成功但不生效），
                # 于是探测会把"已被占用"误判成"可用"，随后 uvicorn 真去绑就失败 ——
                # Windows CI 上就是这么红的（探测说 50438 可用，实际起不来）。
                # Windows 用 SO_EXCLUSIVEADDRUSE 才是准确的"占用"判据；
                # POSIX 上不设 REUSEADDR 时 bind 本身就会因占用而失败。
                if sys.platform == "win32":
                    sk.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sk.bind(("0.0.0.0", int(port)))
            return True
        except OSError:
            return False

    def is_running(self) -> bool:
        return self._health_on(self.live_port)

    def start(self, device_id: str = "", hub_url: str = "") -> bool:
        # 已在跑（自己之前起的，或用户另开了一个）：复用，不要起第二个。
        #
        # ## 但必须校验版本（2026-11-19 云电脑真机）
        #
        # 用户反复反馈"装了新版本，版本号还是旧的"。查下来是这样：
        # 他之前手动跑过一个**便携版**（在 Downloads 里双击的），它一直占着 8730；
        # 之后每次覆盖安装新版本，新客户端启动时**只探了 /healthz 就接管了**这个
        # 旧 core —— 版本号当然永远是旧的，升级等于没生效。
        #
        # 而且这种"静默接管"比报错更糟：界面一切正常，只是跑的是旧代码。
        # 所以这里记下对方的版本，不一致就**大声说出来**（GUI 会显示警告 +
        # 「使用当前版本」按钮，见 `take_over`）。
        for cand in [self.port] + [self.port + i for i in range(1, self.PORT_SCAN)]:
            if self._health_on(cand):
                self.actual_port = cand
                self.attached = True
                if cand != self.port:
                    self.error = ""      # 复用一个非首选端口是正常状态，不算错
                other = self._remote_version(cand)
                if other and other != __version__:
                    self.attached_version = other
                    logging.getLogger("zw.client").warning(
                        "复用了一个**旧版本**的本机服务：%s（当前客户端是 %s，端口 %s）。"
                        "升级不会生效 —— 点界面上的「使用当前版本」或先关掉那个旧实例。",
                        other, __version__, cand)
                return True

        # 首选端口被占（常见：另一个客户端实例 / 上次没退干净 / 别的程序）→ 往后找
        chosen = 0
        for cand in [self.port] + [self.port + i for i in range(1, self.PORT_SCAN)]:
            if self._port_bindable(cand):
                chosen = cand
                break
        if not chosen:
            self.error = (
                f"端口 {self.port}-{self.port + self.PORT_SCAN - 1} 全被占用，本机服务起不来。\n\n"
                "请关掉占用这些端口的程序（常见：另一个知纹客户端窗口、上次没退干净的进程），"
                "或在配置里把端口改成别的。"
            )
            return False
        if chosen != self.port:
            # 换过端口必须让用户/服务器知道，否则远程还会按 8730 去连
            logging.getLogger("zw.client").warning(
                "首选端口 %s 被占用，本机服务改用 %s", self.port, chosen)
        self.port = chosen
        # 不要再把 ZW_HOST 硬写成 127.0.0.1：用户要求"客户端控制台监听 0.0.0.0"，
        # 以便同网段/远程用机器码直接打开。安全边界交给 require_auth：
        # 回环直通、远程必须给机器码（device_id 一定存在）。
        # 只有在用户自己显式设了 ZW_HOST 时才会覆盖（setdefault 语义）。
        os.environ.setdefault("ZW_HOST", "0.0.0.0")
        # 必须用赋值而不是 setdefault：换了回退端口时，环境里可能还留着旧的 8730，
        # setdefault 不会覆盖，core 就会去绑那个被占的端口 → 白换。
        os.environ["ZW_PORT"] = str(self.port)
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
        # **桌面客户端也要自动准备内核。**
        #
        # 真踩到（2026-11-19，云电脑 0.7.91）：用户双击客户端图标启动，
        # 建环境时却报「没有找到可用的 ungoogled-chromium 内核」，
        # `GET /api/v1/engine` 的 `download.state` 一直是 `idle` —— 说明
        # 自动下载**根本没被触发**。
        #
        # 原因：`prepare_engine_async()` 只写在 `main.run()` 里（命令行 serve 走那条），
        # 而 GUI 是通过 `LocalCore` 直接 `from zw_core.main import app` 起 uvicorn 的，
        # **完全绕过 `run()`**。于是 0.7.72 起号称的"客户端自己下载内核"
        # 在用户实际使用的入口上从未生效。
        #
        # 放在这里而不是 `create_app()` 的 startup 事件里：测试也用 `create_app()`，
        # 若在 startup 里自动下载，每跑一次测试就会去下 150MB。
        try:
            # `get_settings` 在**下面那个 try** 里才 import，而函数内 import 是局部名 ——
            # 这里必须自己 import 一次，否则 NameError 会被下面的 except 吞掉，
            # 表现成"加了代码但依然不下载"。
            from zw_core.config import get_settings as _get_settings
            from zw_core.services import engines as _engines

            _info = _engines.prepare_engine_async(_get_settings())
            if _info.get("started"):
                logging.getLogger("zw.client").info(
                    "后台开始下载 ungoogled-chromium 内核（约 150–200MB，进度看控制台「运维→内核」）")
            elif _info.get("reason"):
                logging.getLogger("zw.client").info("内核准备跳过：%s", _info["reason"])
        except Exception:  # noqa: BLE001 —— 内核是增值项，失败不能拦住 core 启动
            logging.getLogger("zw.client").warning("内核准备检查失败（不影响启动）", exc_info=True)
        try:
            import uvicorn

            from zw_core.config import get_settings
            from zw_core.main import app as core_app

            # log_config=None 很关键：uvicorn 默认会用 dictConfig 重配日志，
            # 而它的 "default" formatter 构造时会调 sys.stdout.isatty()。
            # Windows GUI 子程序里 sys.stdout 是 None → AttributeError，
            # dictConfig 把它包成 "ValueError: Unable to configure formatter 'default'"
            # —— 用户看到的就是"本机服务启动失败"，云电脑上真踩到了。
            # 我们自己的 setup_logging 已经管好日志，不需要 uvicorn 再配一遍。
            config = uvicorn.Config(
                core_app,
                # **必须取 settings.host，不能写死 127.0.0.1**：
                # 用户要求控制台监听 0.0.0.0（远程用机器码打开）。
                # 之前这里硬编码回环，导致"config 改了、环境变量也设了，
                # 但实际还是只听回环" —— 三层里最后一层没改，最难发现。
                host=get_settings().host,
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
            if self._health_on(self.port):
                self.actual_port = self.port
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

            r = httpx.get(f"http://127.0.0.1:{self.live_port}/api/v1/stats", timeout=3)
            if r.status_code == 200:
                return (r.json() or {}).get("data")
        except Exception:
            pass
        return None

    def stop(self) -> None:
        # `attached` = 这个实例只是连到别人已经跑着的 core 上，关它不该把别人的服务停掉
        if self.server is not None and not self.attached:
            self.server.should_exit = True
            self.server = None
        self.attached = False


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
    """没有图形界面时的兜底：交互式问答。

    ## 非交互环境要优雅退出

    这条路径会在"tkinter 加载失败"时被走到 —— 例如 Linux 产物在 glibc
    比构建机旧的系统上运行（`libtcl8.6.so` 要求 GLIBC_2.42 之类的）。
    此时如果直接 `input()`，stdin 不是 TTY 就抛 EOFError，用户看到的是
    一个闪过的黑框加一堆栈，完全不知道发生了什么。

    所以：读不到输入就打印**为什么**以及**怎么修**，然后返回非 0。
    """
    from zw_core import __version__

    cfg = load_config()
    print(f"{APP_NAME} 客户端 v{__version__}")
    print(f"机器码：{ensure_device_id()}")
    try:
        interactive = bool(sys.stdin) and sys.stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if not interactive:
        print()
        print("当前没有可用的图形界面，也没有可交互的终端，客户端无法继续。")
        print("常见原因与解决办法：")
        print("  1. 这台 Linux 的 glibc 比安装包构建机旧（tkinter 起不来）")
        print("     → 用源码运行：uv pip install -e ./core && zw-client")
        print("     → 或换用与系统匹配的包（我们按 Ubuntu 22.04 打 Linux 包）")
        print("  2. 在纯命令行环境下运行：直接跑 `zw serve` 起服务，再用浏览器开控制台")
        print()
        print(f"诊断：跑一次 `{Path(sys.argv[0]).name} --selftest` 看是哪一项不满足")
        return 3
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
    core = LocalCore(port=_resolve_core_port(cfg))
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
    warns: list[str] = []
    checks: list[tuple[str, Any]] = []

    def _trace(name: str) -> None:
        """逐项打进度到 stderr（立即 flush）。

        为什么需要：自检如果**在某一项崩掉**（原生崩溃、段错误），
        最后的汇总报告根本打不出来，CI 里只能看到 exit code 139，
        完全不知道是哪一项。逐项打点后，日志最后一行就是"崩在这一项"。
        """
        try:
            if sys.stderr is not None:
                print(f"[selftest] {name} …", file=sys.stderr, flush=True)
        except (AttributeError, ValueError, OSError):
            pass

    def chk(name: str, fn: Any) -> None:
        _trace(name)
        try:
            detail = fn()
            checks.append((name, detail))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    def warn_check(name: str, fn: Any) -> None:
        """不合格只记警告、不算失败。

        用于"用户能看见但改不了/不该因此判死"的检查，例如安装路径里
        带了中文（那是 Windows 用户名的锅，不是程序坏了）。
        """
        _trace(name)
        try:
            detail = fn()
            checks.append((name, detail))
        except Exception as exc:  # noqa: BLE001
            warns.append(f"{name}: {exc}")

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

    def _shell_ok() -> str:
        """远程命令行的执行器必须真的能跑通一条命令。

        这个功能一旦坏了，用户是"从服务器点执行、永远没反应"，
        在客户端界面上完全看不出来 —— 所以放进自检。
        """
        from zw_core.services import shell as shellsvc

        probe = "echo zw-selftest" if sys.platform != "win32" else "echo zw-selftest"
        r = shellsvc.execute(probe, timeout=15)
        if r.error:
            raise RuntimeError(r.error)
        if "zw-selftest" not in (r.stdout + r.stderr):
            raise RuntimeError(f"输出不符合预期：{(r.stdout + r.stderr)[:120]!r}")
        return f"{shellsvc.shell_hint()} 可用，{r.duration_ms}ms"

    chk("远程命令行执行器", _shell_ok)

    def _ascii_paths() -> str:
        """安装路径与数据目录必须是纯英文。

        中文路径会在旧命令行工具、备份脚本、日志 grep、跨机复制里反复出问题。
        这条**只告警不失败**：Windows 用户名本身是中文时，用户改不了
        `%LOCALAPPDATA%`，把自检判死只会让人以为程序坏了。
        """
        import sys as _sys

        bad: list[str] = []
        targets = []
        if getattr(_sys, "frozen", False):
            targets.append(("程序目录", Path(_sys.executable).parent))
        targets.append(("配置目录", config_dir()))
        try:
            from zw_core.config import get_settings

            targets.append(("数据目录", Path(get_settings().root)))
        except Exception:
            pass
        for label, p in targets:
            try:
                if not str(p).isascii():
                    bad.append(f"{label}：{p}")
            except Exception:
                pass
        if bad:
            raise ValueError(
                "路径里有非 ASCII 字符（通常是 Windows 用户名是中文）："
                + "；".join(bad)
                + "。程序可以正常运行，但建议把安装位置换到纯英文路径，"
                "例如 D:\\ZhiwenClient"
            )
        return "；".join(f"{label}={p}" for label, p in targets) or "ok"

    warn_check("安装路径为纯英文", _ascii_paths)
    chk("配置目录可写", lambda: (str(config_dir()), save_config({**load_config(), "selftest": 1}), "ok")[0])
    chk("机器码生成", lambda: ensure_device_id()[:12] + "…")
    chk("地址规范化", lambda: normalize_server("192.168.3.5") )

    lines = [f"{APP_NAME} 客户端自检", ""]
    for name, detail in checks:
        lines.append(f"  OK    {name}: {detail}")
    for w in warns:
        lines.append(f"  WARN  {w}")
    for f in failures:
        lines.append(f"  FAIL  {f}")
    lines.append("")
    code = 1 if failures else 0
    if failures:
        lines.append(f"有 {len(failures)} 项失败" + (f"，{len(warns)} 项警告" if warns else ""))
    elif warns:
        lines.append(f"全部 {len(checks)} 项通过，但有 {len(warns)} 项警告")
    else:
        lines.append(f"全部 {len(checks)} 项通过")
    _report_selftest(lines, code, show_dialog=show_dialog)
    return code


def main() -> int:
    from zw_core.console import setup_console

    setup_console()
    if "--selftest" in sys.argv:
        return selftest()

    # ── 子命令转交（打包成单文件后 entrypoint 是 **client.py**，不是 cli.py）
    #
    # 于是 `zhiwen-client serve` 这种写法以前**根本不生效**：`serve` 被当成多余
    # 参数忽略，程序照样去起图形界面 —— 在没有显示器的服务器上直接
    # `TclError: no display name` 崩掉。而 README 与 docs 里写的是 `zw serve`。
    #
    # 这里把已知的 CLI 子命令转给 `cli.main()`，让单文件产物也能当服务端用。
    _CLI_SUBCOMMANDS = {
        "serve", "mcp", "doctor", "stats", "manifest", "meta", "events", "shell",
        "shell-status", "audit", "trim", "cleanup", "reap", "profile", "cookie",
        "sync", "proxy", "page", "extension",
    }
    if len(sys.argv) > 1 and sys.argv[1] in _CLI_SUBCOMMANDS:
        from zw_core.cli import main as cli_main

        # `cli.main()` 自己读 `sys.argv`，所以**不能**传参进去；
        # 这里只想在它返回后统一成退出码。
        cli_main()
        return 0

    # 无图形环境时不要抛 TclError 了事：直接告诉用户该用哪个子命令。
    if sys.platform != "win32" and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        print(
            f"{APP_NAME}：当前没有图形环境（DISPLAY / WAYLAND_DISPLAY 都没设），\n"
            "桌面客户端起不来。要在这台机器上跑服务端，用：\n"
            f"  {Path(sys.argv[0]).name} serve          # 起 core（控制台 + API + 网关）\n"
            f"  {Path(sys.argv[0]).name} --help         # 看所有子命令\n"
            f"  {Path(sys.argv[0]).name} --selftest     # 只做自检",
            file=sys.stderr,
        )
        return 2
    try:
        return run_gui()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001  兜底：绝不把英文栈甩给用户
        # 打包后的程序崩栈时只显示 "Failed to execute script"，用户完全懵。
        # 这里给一句人话 + 一条自查命令；同时把详情写进配置目录，方便回传。
        import traceback

        detail = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        try:
            (config_dir() / "crash.txt").write_text(detail, encoding="utf-8")
        except OSError:
            pass
        msg = (
            f"{APP_NAME} 客户端启动失败：{type(exc).__name__}: {exc}\n\n"
            "请在本程序的目录里执行一次自检，把输出发给我们：\n"
            f"  {Path(sys.argv[0]).name} --selftest\n\n"
            f"详细堆栈已写入：{config_dir() / 'crash.txt'}"
        )
        print(msg, file=sys.stderr or sys.stdout)
        _report_selftest([msg], 1, show_dialog=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

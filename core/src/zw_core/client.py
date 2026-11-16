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


def heartbeat(base: str, device_id: str, token: str = "") -> dict[str, Any]:
    import httpx

    headers = {"X-ZW-Device": device_id}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(f"{base}/v1/devices/heartbeat", headers=headers)
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

    def __init__(self, base: str, device_id: str, token: str = "", interval: int = 60) -> None:
        self.base = base
        self.device_id = device_id
        self.token = token
        self.interval = max(15, int(interval or 60))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.revoked = False
        self.last_ok = 0.0
        self.last_error = ""
        self.on_revoked: Any = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="zw-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval if self.last_ok else 10):
            res = heartbeat(self.base, self.device_id, self.token)
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

            config = uvicorn.Config(core_app, host="127.0.0.1", port=self.port, log_level="info", access_log=False)
            self.server = uvicorn.Server(config)
            self.thread = threading.Thread(target=self.server.run, name="zw-core", daemon=True)
            self.thread.start()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.is_running():
                return True
            time.sleep(0.25)
        self.error = self.error or "本地服务 20 秒内没起来"
        return False

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


def pick_ui_font(root: Any, size: int = 10, bold: bool = False) -> tuple[str, int, str]:
    """返回一个能显示中文的 (family, size, weight)。

    判据不是"字体名在不在列表里"，而是**量一下中文的实际宽度** ——
    字体名存在但缺字形的字体（Linux 上很常见）量出来是 0 或退化值，不能用。
    """
    try:
        import tkinter.font as tkfont

        families = list(tkfont.families(root))
    except Exception:
        return ("TkDefaultFont", size, "bold" if bold else "normal")
    # 大小写不敏感匹配：X11 后端返回的族名是**小写**的
    # （"wenquanyi zen hei"），而正确写法是 "WenQuanYi Zen Hei"。
    # 之前用精确集合匹配，于是在 Linux 上一个都命中不了，整屏中文空白。
    lower_map = {f.lower(): f for f in families}
    for fam in _FONT_CANDIDATES:
        actual = lower_map.get(fam.lower())
        if actual is None:
            continue
        try:
            probe = tkfont.Font(root=root, family=actual, size=size)
            if probe.measure("服务器地址") > 20:
                return (actual, size, "bold" if bold else "normal")
        except Exception:
            continue
    # 再做一轮"按关键字模糊找"：不同发行版的族名差异很大
    for key in ("wenquanyi", "noto sans cjk", "noto sans sc", "source han", "yahei",
                "pingfang", "hiragino", "simhei", "simsun", "droid sans fallback"):
        for low, actual in lower_map.items():
            if key in low:
                try:
                    probe = tkfont.Font(root=root, family=actual, size=size)
                    if probe.measure("服务器地址") > 20:
                        return (actual, size, "bold" if bold else "normal")
                except Exception:
                    continue
    # 一个都不合适：退回 Tk 默认（至少西文正常，中文靠系统 fallback）
    return ("TkDefaultFont", size, "bold" if bold else "normal")


def run_gui() -> int:
    """启动 GUI。没有 tkinter 时退回到命令行引导，而不是直接崩。"""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as exc:  # noqa: BLE001
        print(f"这台机器上的 Python 没有 tkinter（{exc}），改用命令行模式。")
        return run_cli()

    from zw_core import __version__

    cfg = load_config()
    core = LocalCore(port=int(cfg.get("core_port") or 8730))

    root = tk.Tk()
    root.title(f"{APP_NAME} · 客户端")
    root.geometry("620x520")
    root.minsize(560, 470)
    # 不要手动改 tk scaling：它会放大字体但不放大控件高度，
    # 结果中文字被纵向裁掉一半（实测过）。让 tk 用系统 DPI，另外把
    # 行高/内边距调够即可。

    BG = "#111418"
    CARD = "#191d24"
    INK = "#e8ebf2"
    DIM = "#8b93a7"
    ACC = "#5b9dff"
    root.configure(bg=BG)
    pick_mono_font(root)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    # 字体必须探测：硬编码 "Microsoft YaHei UI" 在 Linux/mac 上不存在，
    # 结果就是整屏中文变方块或空白（实测过）。
    body = pick_ui_font(root, 10)
    small = pick_ui_font(root, 9)
    title = pick_ui_font(root, 15, bold=True)
    body_b = pick_ui_font(root, 10, bold=True)

    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=INK, font=body, padding=(0, 3))
    style.configure("Dim.TLabel", background=BG, foreground=DIM, font=small, padding=(0, 2))
    style.configure("H.TLabel", background=BG, foreground=INK, font=title, padding=(0, 4))
    style.configure("TEntry", fieldbackground="#0d1014", foreground=INK, insertcolor=INK,
                    borderwidth=0, font=body)
    style.configure("Accent.TButton", background=ACC, foreground="#ffffff", borderwidth=0,
                    font=body_b, padding=(16, 9))
    style.map("Accent.TButton",
              background=[("active", "#6fabff"), ("disabled", "#2c3340")],
              foreground=[("disabled", "#7b8494")])
    style.configure("TButton", background=CARD, foreground=INK, borderwidth=0,
                    font=body, padding=(12, 8))
    style.map("TButton", background=[("active", "#232935")])

    status_var = tk.StringVar(value="填好服务器地址，点「连接并开始」。")
    server_var = tk.StringVar(value=cfg.get("server") or "")
    token_var = tk.StringVar(value=cfg.get("token") or "")
    need_token = tk.BooleanVar(value=bool(cfg.get("token_required")))

    wrap = ttk.Frame(root, padding=(24, 20))
    wrap.pack(fill="both", expand=True)

    ttk.Label(wrap, text=f"{APP_NAME} 客户端", style="H.TLabel").pack(anchor="w")
    ttk.Label(wrap, text=f"v{__version__} · 机器码 {ensure_device_id()[:16]}…",
              style="Dim.TLabel").pack(anchor="w", pady=(3, 18))

    # 服务器地址
    addr_card = ttk.Frame(wrap, style="Card.TFrame", padding=14)
    addr_card.pack(fill="x")
    ttk.Label(addr_card, text="服务器地址", style="Dim.TLabel").pack(anchor="w")
    addr_entry = ttk.Entry(addr_card, textvariable=server_var, font=(MONO_FAMILY, 11))
    addr_entry.pack(fill="x", pady=(6, 4), ipady=6)
    ttk.Label(addr_card, text="例如 192.168.3.5:8700", style="Dim.TLabel").pack(anchor="w")
    ttk.Label(addr_card, text="不带端口时默认 8700", style="Dim.TLabel").pack(anchor="w")

    # 对接码（默认隐藏）
    token_card = ttk.Frame(wrap, style="Card.TFrame", padding=14)
    tok_entry = ttk.Entry(token_card, textvariable=token_var, show="•", font=(MONO_FAMILY, 11))
    ttk.Label(token_card, text="对接码（服务器要求时填写）", style="Dim.TLabel").pack(anchor="w")
    tok_entry.pack(fill="x", pady=(6, 0), ipady=6)

    # 状态
    status_lbl = ttk.Label(wrap, textvariable=status_var, style="Dim.TLabel", wraplength=490, justify="left")
    status_lbl.pack(anchor="w", pady=(14, 6))

    btns = ttk.Frame(wrap)
    btns.pack(fill="x", side="bottom")

    def set_status(text: str) -> None:
        status_var.set(text)
        root.update_idletasks()

    def connect() -> None:
        raw = server_var.get().strip()
        base = normalize_server(raw)
        if not base:
            messagebox.showwarning(APP_NAME, "请先填服务器地址，例如 192.168.3.5:8700")
            return
        set_status(f"正在连接 {base} …")
        info = probe_server(base)
        if not info.get("ok"):
            set_status(f"连接失败：{info.get('error')}")
            messagebox.showerror(APP_NAME, f"连不上服务器。\n\n{info.get('error')}\n\n"
                                           "检查：服务器地址对不对、那台机器上 hub 有没有跑起来、防火墙有没有放行端口。")
            return
        if info.get("token_required") and not token_var.get().strip():
            need_token.set(True)
            token_card.pack(fill="x", pady=(10, 0), after=addr_card)
            tok_entry.focus_set()
            set_status("这台服务器要求对接码，请填写后再次点「连接并开始」。")
            return
        need_token.set(False)
        token_card.pack_forget()

        set_status("正在注册这台电脑 …")
        res = enroll(base, ensure_device_id(), token_var.get().strip(), app_version=__version__)
        if not res.get("ok"):
            code = res.get("code", "")
            if code == "DEVICE_REVOKED":
                set_status("这台电脑已被服务器端移除。")
                messagebox.showerror(
                    APP_NAME,
                    "这台电脑已被服务器端「踢下线」。\n\n"
                    "请联系管理员在服务器管理面板里恢复这台设备，恢复后重新点「连接并开始」。",
                )
            elif code == "UNAUTHENTICATED":
                need_token.set(True)
                token_card.pack(fill="x", pady=(10, 0), after=addr_card)
                set_status("对接码不正确，请检查。")
            else:
                set_status(f"注册失败：{res.get('error')}")
                messagebox.showerror(APP_NAME, f"注册失败：\n\n{res.get('error')}")
            return

        cfg.update({"server": base, "device_id": ensure_device_id(),
                    "token": token_var.get().strip(), "token_required": bool(info.get("token_required"))})
        save_config(cfg)

        set_status("正在启动本机浏览器服务 …")
        if not core.start(device_id=ensure_device_id(), hub_url=base):
            set_status(f"本机服务启动失败：{core.error}")
            messagebox.showerror(APP_NAME, f"本机服务启动失败：\n\n{core.error}")
            return

        # 启动心跳：60 秒一次，被踢时立刻反映到界面上
        if not hasattr(root, "_hb") or root._hb is None:  # type: ignore[attr-defined]
            hb = Heartbeat(base, ensure_device_id(), token_var.get().strip(),
                           interval=int(res.get("heartbeat_seconds") or 60))

            def _on_revoked() -> None:
                def apply() -> None:
                    set_status("这台电脑已被服务器端移除，本机服务仍在运行但已无法同步。")
                    try:
                        messagebox.showwarning(
                            APP_NAME,
                            "这台电脑已被服务器端「踢下线」。\n\n"
                            "本机已开着的浏览器还能继续用，但同步/注册会全部失败。\n"
                            "请联系管理员在服务器面板里恢复。",
                        )
                    except Exception:
                        pass
                try:
                    root.after(0, apply)
                except Exception:
                    pass

            hb.on_revoked = _on_revoked
            hb.start()
            root._hb = hb  # type: ignore[attr-defined]
            hb_run = hb
        else:
            hb_run = root._hb  # type: ignore[attr-defined]

        set_status(f"已连接 · 控制台 http://127.0.0.1:{core.port}")
        try:
            webbrowser.open(f"http://127.0.0.1:{core.port}")
        except Exception:
            pass
        open_btn.state(["!disabled"])
        start_btn.state(["disabled"])

    def open_console() -> None:
        webbrowser.open(f"http://127.0.0.1:{core.port}")

    start_btn = ttk.Button(btns, text="连接并开始", style="Accent.TButton", command=connect)
    start_btn.pack(side="right")
    open_btn = ttk.Button(btns, text="打开控制台", command=open_console)
    open_btn.pack(side="right", padx=(0, 8))
    open_btn.state(["disabled"])

    addr_entry.focus_set()
    root.bind("<Return>", lambda _e: connect())

    # 已经配过就直接尝试自动连（用户双击图标就该看到浏览器打开）
    if cfg.get("server"):
        root.after(300, connect)
    if need_token.get():
        token_card.pack(fill="x", pady=(10, 0), after=addr_card)

    def on_close() -> None:
        hb = getattr(root, "_hb", None)
        if hb is not None:
            hb.stop()
        core.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


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


def _report_selftest(lines: list[str], code: int) -> None:
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
    if sys.platform == "win32":
        try:
            import ctypes

            # MB_ICONINFORMATION=0x40 / MB_ICONERROR=0x10，MB_SETFOREGROUND=0x10000
            icon = 0x40 if code == 0 else 0x10
            ctypes.windll.user32.MessageBoxW(None, text, "知纹客户端自检", icon | 0x10000)
        except Exception:
            pass


def selftest() -> int:
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
    _report_selftest(lines, code)
    return code


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())

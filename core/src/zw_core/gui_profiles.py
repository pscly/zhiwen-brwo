"""客户端 GUI 的两块本地面板：**环境列表** 与 **日志/事件**。

## 为什么单独一个文件

`gui.py` 已经 850 行。再往里塞"环境列表 + 日志面板 + 自绘输入浮层"，改起来
就开始互相踩了。而且这两块只跟**本机 core 的 HTTP 接口**打交道，与
"连接服务器 / 注册 / 心跳"是完全独立的两条链路：独立成模块后可以单独
实例化（截图验证、以后搬到网页版都方便），也不会和连接逻辑纠缠。

## 三条刻意的取舍（改之前先读）

1. **不弹原生输入框**。项目约定（见 `docs/11-客户端与服务器端.md` 的
   「客户端界面」与控制台里的 `askText`）要求自绘。这里用
   `place(relwidth=1, relheight=1)` 覆盖父容器的浮层，**不新建 Toplevel**：
   没有窗口管理器的环境（远程桌面、精简 Linux、CI 里的 Xvfb）里
   Toplevel 的位置和焦点都不可控，会跑到屏幕左上角盖住顶栏。
   删除确认同样走自绘浮层 —— 语义仍然是「确认 / 取消」，只是不经过
   `messagebox`（原生对话框在这里会阻塞 mainloop，自动化验证时直接卡死）。

2. **网络调用一律放后台线程**。本机 core 的 `POST /profiles/{id}/start`
   要等浏览器真的起来（`ZW_LAUNCH_TIMEOUT`，默认 45 秒）才返回；在主线程里
   调会让整个窗口卡死，连「正在启动…」这行字都刷不出来。结果通过
   `root.after(0, ...)` 回到主线程再动控件 —— Tk 不是线程安全的，
   后台线程里直接改控件在 Windows 上会随机崩。

3. **自动刷新不能打断输入**。每 5 秒刷新前先看三件事：
   浮层开着（用户在打字）、有操作在飞、上一个请求还没回来 → 整轮跳过；
   就算要刷，也先比对「渲染签名」（id / 名字 / 状态 / 分组），
   没变化就一个控件都不重建。于是既不会把光标踢走，也不会每 5 秒闪一下。

## 只调接口，不碰数据库

这里所有状态都来自 core 的 HTTP 接口（`GET/POST/DELETE /api/v1/profiles`、
`GET /api/v1/events`、`GET /api/v1/stats`）。**不直接 import manager/db**：
客户端是独立进程，直接读库会绕过租约与审计，而且库可能被 core 锁着。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from typing import Any, Callable
from urllib.parse import quote

__all__ = [
    "CoreApi",
    "Theme",
    "ProfilePanel",
    "LogPanel",
    "call_async",
    "data_root",
    "open_in_file_manager",
    "logs_dir",
]


# ── 主题 ──────────────────────────────────────────────────
@dataclass
class Theme:
    """面板用到的颜色。

    默认值和 `gui.py` 里的常量一模一样，但 gui.py 会把自己那份**显式传进来**，
    这样配色只有一份真相：改 gui.py 的常量就够了，这里不会偷偷留在旧值上。
    """

    bg: str = "#0f1216"
    card: str = "#171b22"
    card2: str = "#1d222b"
    ink: str = "#e8ebf2"
    dim: str = "#8b93a7"
    faint: str = "#5d6675"
    acc: str = "#5b9dff"
    ok: str = "#3ecf8e"
    warn: str = "#ffb020"
    bad: str = "#ff6b6b"
    line: str = "#262c37"
    field: str = "#0b0e12"


# 状态徽章的配色：底色比前景暗一档，才能在深色卡片上看出"一块牌子"而不是一行彩字
_STATE_STYLE = {
    "running": ("运行中", "#15321f", "#3ecf8e"),
    "starting": ("启动中", "#33290f", "#ffb020"),
    "stopping": ("停止中", "#33290f", "#ffb020"),
    "stopped": ("已停止", "#20242d", "#8b93a7"),
    "failed": ("启动失败", "#33191c", "#ff6b6b"),
    "crashed": ("已崩溃", "#33191c", "#ff6b6b"),
}


def _state_badge(state: str) -> tuple[str, str, str]:
    return _STATE_STYLE.get(state or "stopped", (state or "未知", "#20242d", "#8b93a7"))


def _clip(text: str, limit: int) -> str:
    """按字符数截断（中文一个字算一个，Tk 的 Label 不会自己省略）。"""
    text = str(text or "")
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


# ── 本机数据目录 ──────────────────────────────────────────
def data_root() -> Path:
    """本机数据目录（`logs/` 在它下面）。

    优先问设置：客户端是**同进程**跑 core 的，两边本来就是同一份配置，
    所以这里读到的就是 core 真正用的目录。拿不到再退回环境变量与平台默认值。

    这个函数**永不抛异常** —— 它被按钮回调直接调用，抛一次就是界面报错。
    """
    try:
        from zw_core.config import get_settings

        return Path(get_settings().root)
    except Exception:
        pass
    raw = (os.environ.get("ZW_ROOT") or "").strip()
    if raw:
        return Path(raw)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "zhiwen"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "zhiwen"


def logs_dir() -> Path:
    return data_root() / "logs"


# ── 子进程：绝不弹控制台 ──────────────────────────────────
def _spawn_hidden(args: list[str]) -> None:
    """拉起一个子进程，且**不创建任何控制台窗口**。

    Windows 上有个坑：`subprocess` 默认继承父进程的控制台。打包成
    `console=False` 的 GUI 程序里父进程没有控制台，子进程（explorer、
    xdg-open）会自己新建一个黑框闪一下 —— 用户明确要求"不能弹任何命令行
    窗口"，所以这里一律走 `services/proc.popen_hidden`：它负责
    `CREATE_NO_WINDOW` + `STARTUPINFO/SW_HIDE` 双保险，也是全项目唯一的
    起进程出口（`tests/test_proc_hidden.py` 有一条 AST 审计盯着裸调用）。

    为什么用 `popen_hidden` 而不是 `run_hidden`：文件管理器是**长驻**程序，
    用 run 会把它等成僵尸、把界面卡住；我们要的正是"拉起就走"。
    """
    from zw_core.services.proc import popen_hidden

    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform != "win32":
        # 新会话：文件管理器不该跟着客户端一起被信号带走（Windows 无此参数）
        kwargs["start_new_session"] = True
    # gui_child=True：文件管理器自己是要显示窗口的 GUI 程序。默认会顺手设
    # STARTUPINFO.wShowWindow=SW_HIDE，而它会被 GUI 程序当成 WinMain 的
    # nCmdShow —— 那个位一旦是 SW_HIDE，"点了打开目录却什么都没弹出来"。
    # 这个标志只是不再要求隐藏窗口，CREATE_NO_WINDOW 依然保留（对 GUI
    # 子系统程序系统会忽略它，所以不会退化成"闪黑框"）。
    popen_hidden(args, gui_child=True, **kwargs)


def open_in_file_manager(path: str | Path) -> tuple[bool, str]:
    """用系统的文件管理器打开一个目录/文件。返回 (是否成功, 说明或错误)。

    Windows 用 `os.startfile`（系统 API，不经过 shell，也不建控制台）；
    macOS 用 `open`；Linux 用 `xdg-open`（精简系统上可能没有，
    这时如实把错误报回去，而不是静默失败）。
    """
    target = str(path)
    try:
        if sys.platform == "win32":
            try:
                os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
                return True, target
            except Exception:
                # 没有关联程序时（极少见）退回 explorer，同样不弹窗口
                _spawn_hidden(["explorer", target])
                return True, target
        _spawn_hidden(["open", target] if sys.platform == "darwin" else ["xdg-open", target])
        return True, target
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ── 后台线程 + 回主线程 ───────────────────────────────────
def call_async(widget: tk.Misc, fn: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
    """在后台线程跑 `fn()`，再把结果送回主线程交给 `on_done`。

    为什么必须有这一层：Tk 的控件只能在创建它的线程里动。后台线程直接
    `label.configure(...)` 在 Linux 上多半"看起来能跑"，在 Windows 上会
    直接崩（`TclError: out of stack space` / 随机卡死）。所以统一用
    `after(0, ...)` 排队回主线程。

    窗口已经销毁时（用户点了退出，而请求还在飞）静默丢弃结果：
    这时 `winfo_exists` 会抛或返回 0，两种都当"没人收"处理。
    """

    def work() -> None:
        try:
            res = fn()
        except Exception as exc:  # noqa: BLE001  后台线程的异常不能静默，也不能杀进程
            res = (False, None, {"code": "CLIENT_ERROR",
                                 "message": f"{type(exc).__name__}: {exc}"})

        def finish() -> None:
            try:
                if not widget.winfo_exists():
                    return
            except Exception:
                return
            try:
                on_done(res)
            except Exception:
                pass

        try:
            widget.after(0, finish)
        except Exception:
            pass

    threading.Thread(target=work, name="zw-gui-io", daemon=True).start()


# ── 本机 core 的 HTTP 客户端 ──────────────────────────────
class CoreApi:
    """调本机 core 的薄封装：统一鉴权、统一解析 `{ok,data}` / `{ok,code,message}`。"""

    def __init__(self, port: int = 8730) -> None:
        self.port = int(port or 8730)
        # 令牌从环境变量取（和 gui.py 里 toggle_shell 的做法保持一致）：
        # 本机 core 通常没设 ZW_ADMIN_TOKEN，那就是开发模式，不用带。
        self.token = ""

    # -- 基础 --
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _token(self) -> str:
        return (self.token or os.environ.get("ZW_ADMIN_TOKEN", "") or "").strip()

    def call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> tuple[bool, Any, dict[str, Any] | None]:
        """返回 `(ok, data, err)`。err = {code, message, hint}。

        永远不抛异常：调用方是界面回调，抛出去就是一次界面报错（更糟的是
        在后台线程里抛，用户只会看到"什么都没发生"）。
        """
        import httpx

        url = self.base() + path
        headers: dict[str, str] = {}
        tok = self._token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        try:
            # trust_env=False：本机回环请求绝不能被 HTTP_PROXY 劫走。
            # 用户开了全局代理（这个项目的用户几乎都开着）时，走代理去访问
            # 127.0.0.1 会 502/超时，那种失败看起来像"core 挂了"，极难自查。
            with httpx.Client(timeout=timeout, trust_env=False) as c:
                r = c.request(method, url, json=json_body, params=params, headers=headers)
        except Exception as exc:  # noqa: BLE001
            return False, None, {
                "code": "CORE_UNREACHABLE",
                "message": f"连不上本机服务 127.0.0.1:{self.port}（{type(exc).__name__}）",
                "hint": "本机服务可能还没起来，或端口被别的程序占了",
            }
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            return False, None, {
                "code": f"HTTP_{r.status_code}",
                "message": (r.text or "").strip()[:200] or "响应不是 JSON",
            }
        if r.status_code == 200 and isinstance(body, dict) and body.get("ok"):
            return True, body.get("data"), None
        code = str((body or {}).get("code") or f"HTTP_{r.status_code}")
        err = {
            "code": code,
            "message": str((body or {}).get("message") or (r.text or "")[:200]),
            "hint": str((body or {}).get("hint") or ""),
        }
        if code == "UNAUTHENTICATED":
            err["hint"] = (err["hint"] or "") + "（本机服务设了 ZW_ADMIN_TOKEN，但客户端进程里没拿到）"
        return False, None, err

    # -- 环境 --
    def list_profiles(self) -> tuple[bool, Any, dict[str, Any] | None]:
        return self.call("GET", "/api/v1/profiles")

    def create_profile(
        self, name: str, group: str = "", note: str = ""
    ) -> tuple[bool, Any, dict[str, Any] | None]:
        # 注意字段名是 `group_name`（见 api.py 的 ProfileCreate）：
        # 传 `group` 会被 pydantic 当未知字段忽略，分组就静默丢了。
        return self.call(
            "POST",
            "/api/v1/profiles",
            json_body={"name": name, "group_name": group, "note": note},
        )

    def start_profile(self, ref: str) -> tuple[bool, Any, dict[str, Any] | None]:
        # 拉起浏览器是同步的：要等 /json/version 就绪，可能几十秒。
        # 超时必须比 ZW_LAUNCH_TIMEOUT（默认 45s）宽，否则我们这边先超时、
        # 浏览器却在 core 里正常起来了，用户会看到"失败"但环境其实是好的。
        return self.call(
            "POST", f"/api/v1/profiles/{quote(ref, safe='')}/start",
            json_body={"wait": True}, timeout=120.0,
        )

    def stop_profile(self, ref: str) -> tuple[bool, Any, dict[str, Any] | None]:
        # 停止要等进程真的退出（ZW_STOP_GRACE，默认 6s），留足余量
        return self.call("POST", f"/api/v1/profiles/{quote(ref, safe='')}/stop", timeout=90.0)

    def delete_profile(self, ref: str, purge: bool = True) -> tuple[bool, Any, dict[str, Any] | None]:
        return self.call(
            "DELETE", f"/api/v1/profiles/{quote(ref, safe='')}",
            params={"purge": "true" if purge else "false"}, timeout=60.0,
        )

    # -- 运维 --
    def stats(self) -> tuple[bool, Any, dict[str, Any] | None]:
        return self.call("GET", "/api/v1/stats", timeout=8.0)

    def events(self, since: float = 0.0) -> tuple[bool, Any, dict[str, Any] | None]:
        return self.call("GET", "/api/v1/events", params={"since": float(since)}, timeout=8.0)


# ── 自绘浮层（输入 / 确认）────────────────────────────────
class _Overlay:
    """覆盖父容器的暗色浮层 + 居中卡片。

    用法：`ov = _Overlay(parent, theme, fonts, "标题", "副标题")`，往
    `ov.body` 里塞控件，然后 `ov.open()`。`is_open()` 供自动刷新判断
    "用户是不是正在打字"。
    """

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 title: str, subtitle: str = "") -> None:
        self.theme = theme
        self.fonts = fonts
        self._open = False
        # 盖住**整个窗口**而不是只盖住面板：面板（tab 内容区）只有两百来像素高，
        # 浮层卡片比它高，上下两头会被裁掉 —— 标题和「创建/取消」按钮都不见了。
        # 挂在 toplevel 上，卡片按整窗居中，空间永远够。
        host = master.winfo_toplevel()
        self.root = tk.Frame(host, bg=theme.bg, bd=0, highlightthickness=0)
        card = tk.Frame(self.root, bg=theme.card2, bd=0, highlightthickness=1,
                        highlightbackground=theme.line)
        card.place(relx=0.5, rely=0.5, anchor="center")
        pad = tk.Frame(card, bg=theme.card2, padx=16, pady=14)
        pad.pack(fill="both", expand=True)
        tk.Label(pad, text=title, bg=theme.card2, fg=theme.ink, font=fonts["bold"],
                 anchor="w").pack(fill="x")
        if subtitle:
            tk.Label(pad, text=subtitle, bg=theme.card2, fg=theme.dim, font=fonts["tiny"],
                     anchor="w", justify="left", wraplength=300).pack(fill="x", pady=(3, 10))
        # 错误行由子类往 self.body 里塞；变量放基类，子类漏设也不会 AttributeError
        self.err_var = tk.StringVar(value="")
        self.error_label: tk.Label | None = None
        self.body = tk.Frame(pad, bg=theme.card2)
        self.body.pack(fill="x", pady=(8, 0))
        self.buttons = tk.Frame(pad, bg=theme.card2)
        self.buttons.pack(fill="x", pady=(12, 0))
        self.card = card

    # -- 生命周期 --
    def open(self) -> None:
        self._open = True
        self.root.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.root.lift()
        self.on_open()

    def close(self) -> None:
        self._open = False
        try:
            self.root.place_forget()
        except Exception:
            pass

    def is_open(self) -> bool:
        return self._open

    def on_open(self) -> None:  # 子类覆盖：给输入框抢焦点
        pass

    def error(self, msg: str) -> None:
        self.err_var.set(msg)
        try:
            if self.error_label is not None:
                self.error_label.configure(text=msg)
        except Exception:
            pass


class TextOverlay(_Overlay):
    """自绘的名称输入浮层（替代 `simpledialog.askstring`）。"""

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 title: str, subtitle: str, fields: list[tuple[str, str, str]],
                 submit_text: str, on_submit: Callable[[dict[str, str]], bool]) -> None:
        super().__init__(master, theme, fonts, title, subtitle)
        self.on_submit = on_submit
        self.vars: dict[str, tk.StringVar] = {}
        self.entries: list[ttk.Entry] = []
        self.err_var = tk.StringVar(value="")
        for idx, (key, label, initial) in enumerate(fields):
            tk.Label(self.body, text=label, bg=theme.card2, fg=theme.dim,
                     font=fonts["tiny"], anchor="w").pack(fill="x", pady=(6 if idx else 0, 2))
            var = tk.StringVar(value=initial)
            self.vars[key] = var
            entry = ttk.Entry(self.body, textvariable=var, font=fonts["body"])
            entry.pack(fill="x", ipady=5)
            entry.bind("<Return>", lambda _e: self._submit())
            entry.bind("<Escape>", lambda _e: self.close())
            self.entries.append(entry)
        self.error_label = tk.Label(self.body, text="", bg=theme.card2, fg=theme.bad,
                                    font=fonts["tiny"], anchor="w", wraplength=300)
        self.error_label.pack(fill="x", pady=(6, 0))
        ttk.Button(self.buttons, text="取消", style="Tiny.TButton",
                   command=self.close).pack(side="right")
        ttk.Button(self.buttons, text=submit_text, style="TinyAccent.TButton",
                   command=self._submit).pack(side="right", padx=(0, 8))

    def on_open(self) -> None:
        if self.entries:
            # 稍等一下再抢焦点：place 之后立刻 focus_set 在某些 WM 上会被主窗口抢回去
            try:
                self.entries[0].focus_set()
            except Exception:
                pass

    def _submit(self) -> None:
        values = {k: v.get() for k, v in self.vars.items()}
        try:
            keep_open = bool(self.on_submit(values))
        except Exception as exc:  # noqa: BLE001
            self.error(f"提交失败：{type(exc).__name__}: {exc}")
            return
        if not keep_open:
            self.close()

    def set_error(self, msg: str) -> None:  # 兼容调用方写法
        self.error(msg)


class ConfirmOverlay(_Overlay):
    """自绘的确认浮层（替代 `messagebox.askyesno`）。

    为什么不直接用 messagebox：原生对话框是**阻塞**的，会卡住 mainloop；
    在无窗口管理器的环境（自动化验证、远程桌面）里还会挡住整个界面，
    而且样式不可控（浅色系统框）。这里保持"确认/取消"的语义不变。
    """

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 title: str, message: str, confirm_text: str,
                 on_confirm: Callable[[], None], danger: bool = True) -> None:
        super().__init__(master, theme, fonts, title)
        self.on_confirm = on_confirm
        self.err_var = tk.StringVar(value="")
        tk.Label(self.body, text=message, bg=theme.card2, fg=theme.ink, font=fonts["small"],
                 anchor="w", justify="left", wraplength=310).pack(fill="x")
        self.error_label = tk.Label(self.body, text="", bg=theme.card2, fg=theme.bad,
                                    font=fonts["tiny"], anchor="w", wraplength=310)
        self.error_label.pack(fill="x")
        ttk.Button(self.buttons, text="取消", style="Tiny.TButton",
                   command=self.close).pack(side="right")
        ttk.Button(self.buttons, text=confirm_text,
                   style="Danger.TButton" if danger else "TinyAccent.TButton",
                   command=self._confirm).pack(side="right", padx=(0, 8))

    def _confirm(self) -> None:
        self.close()
        self.on_confirm()


# ── 环境面板 ──────────────────────────────────────────────
class ProfilePanel(tk.Frame):
    """本机环境列表：列出 / 新建 / 启动 / 停止 / 删除。

    数据只走 `GET /api/v1/profiles`；操作结果（成功或失败原因）都会写进
    客户端底部的事件流，并同时显示在自己那行状态文字里 —— 用户不该为了
    知道"为什么没建成"去别处找。
    """

    ROW_LIMIT_NAME = 18
    ROW_LIMIT_GROUP = 8

    def __init__(self, master: tk.Misc, *, api: CoreApi, theme: Theme,
                 fonts: dict[str, Any], on_log: Callable[[str, str], None],
                 on_changed: Callable[[], None],
                 enabled: Callable[[], bool] | None = None) -> None:
        super().__init__(master, bg=theme.card, bd=0, highlightthickness=0)
        self.api = api
        self.theme = theme
        self.fonts = fonts
        self.on_log = on_log
        self.on_changed = on_changed
        # 自动刷新前的"服务可用吗"判断。本机 core 在客户端连上服务器之后才起，
        # 没有它的话第一帧就去请求，日志里会先出现一条假的"连不上本机服务"。
        self.enabled = enabled

        self._profiles: list[dict[str, Any]] = []
        self._signature: tuple[Any, ...] | None = None
        self._inflight = False
        self._busy = 0                     # 正在飞的操作数（启停/删除/新建）
        self._last_error = ""              # 同一个错误只报一次，别每 5 秒刷屏
        self._overlay: _Overlay | None = None
        self.status_var = tk.StringVar(value="")
        self.count_var = tk.StringVar(value="—")

        self._build()
        self.set_status("正在读取本机环境 …")

    # -- 装配 --
    def _build(self) -> None:
        theme, fonts = self.theme, self.fonts
        head = tk.Frame(self, bg=theme.card)
        head.pack(fill="x", padx=12, pady=(8, 6))
        tk.Label(head, text="本机环境", bg=theme.card, fg=theme.ink, font=fonts["bold"],
                 anchor="w").pack(side="left")
        tk.Label(head, textvariable=self.count_var, bg=theme.card, fg=theme.faint,
                 font=fonts["tiny"], anchor="w").pack(side="left", padx=(8, 0))
        ttk.Button(head, text="新建环境", style="TinyAccent.TButton",
                   command=self.ask_create).pack(side="right")
        ttk.Button(head, text="刷新", style="Tiny.TButton",
                   command=lambda: self.refresh(force=True)).pack(side="right", padx=(0, 6))

        # 可滚动列表：Tk 没有"可滚动框架"，标准做法是 Canvas 里放一个 Frame
        wrap = tk.Frame(self, bg=theme.card)
        wrap.pack(fill="both", expand=True, padx=(12, 6), pady=(0, 4))
        # height=120：Canvas 默认请求高度很小，但**请求**大了会把 Notebook 的
        # 自然高度顶上去，进而把 640×720 的布局顶出窗口（底部栏被挤掉）。
        # 这里给一个刚够 3 行的请求高度，真正的空间由 expand 分配。
        self.canvas = tk.Canvas(wrap, bg=theme.card, bd=0, highlightthickness=0, height=120)
        self.vsb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview,
                                bd=0, width=8, troughcolor=theme.card,
                                bg=theme.card2, activebackground=theme.faint)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=theme.card)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        foot = tk.Frame(self, bg=theme.card)
        foot.pack(fill="x", padx=12, pady=(0, 6))
        self.status_label = tk.Label(foot, textvariable=self.status_var, bg=theme.card,
                                     fg=theme.faint, font=fonts["tiny"], anchor="w")
        self.status_label.pack(fill="x")

        self._render([])

    def _on_inner_configure(self, _event: Any) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas_configure(self, event: Any) -> None:
        # 内层宽度跟随画布：否则 create_window 里的控件宽度是 0，
        # 一行行会挤成一列（这个坑第一次做可滚动框架时必踩）
        try:
            self.canvas.itemconfigure(self._win, width=event.width)
        except Exception:
            pass

    def _bind_wheel(self, widget: tk.Misc) -> None:
        """给列表里的每个控件都绑滚轮。

        只绑 Canvas 不够：指针停在行内的 Label 上时滚轮事件不会冒泡到 Canvas，
        用户会以为"列表不能滚"。
        """
        widget.bind("<MouseWheel>", self._on_wheel)
        widget.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-2, "units"))
        widget.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(2, "units"))
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _on_wheel(self, event: Any) -> None:
        delta = 1 if getattr(event, "delta", 0) > 0 else -1
        try:
            self.canvas.yview_scroll(-delta * 2, "units")
        except Exception:
            pass

    # -- 状态文字 --
    def set_status(self, text: str, bad: bool = False) -> None:
        self.status_var.set(text)
        try:
            self.status_label.configure(fg=self.theme.bad if bad else self.theme.faint)
        except Exception:
            pass

    # -- 数据刷新 --
    def _typing(self) -> bool:
        """用户是不是正在浮层里打字（这时绝不能重建控件）。"""
        return bool(self._overlay is not None and self._overlay.is_open())

    def autorefresh(self) -> None:
        """自动刷新入口（由 gui.py 的 5 秒循环调用）。

        三条"跳过"条件都在 `refresh(force=False)` 里判断：
        - 面板没显示（用户还停在首次配置页）→ 不刷，省一次请求；
        - 浮层开着（用户正在打字说环境名）→ 不刷，否则重建控件会踢走光标；
        - 有操作在飞（启动浏览器要几十秒）→ 不刷，避免半路重排让用户以为点空了。
        """
        if not self.winfo_ismapped():
            return
        if self.enabled is not None and not self.enabled():
            return
        self.refresh()

    def refresh(self, force: bool = False) -> None:
        """拉一次环境列表。

        `force=True` 用于"用户主动点了刷新/刚做完一个操作"，
        这时忽略浮层与忙碌状态（否则点完按钮列表不动，像卡住了）。
        """
        if self._inflight:
            return                                    # 上一次还没回来，别叠加请求
        if not force and (self._typing() or self._busy):
            return                                    # 不打断输入 / 不在操作中途重排
        self._inflight = True
        call_async(self, self.api.list_profiles, self._on_list)

    def _on_list(self, res: tuple[bool, Any, dict[str, Any] | None]) -> None:
        self._inflight = False
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            self.set_status(f"读不到环境列表：{msg}", bad=True)
            # 同一个错误只写一次事件流：否则每 5 秒一行，把真正有用的日志冲掉
            if code + msg != self._last_error:
                self._last_error = code + msg
                self.on_log(f"环境列表读取失败：[{code}] {msg}", "bad")
            return
        self._last_error = ""
        profiles = list((data or {}).get("profiles") or [])
        self._profiles = profiles
        running = sum(1 for p in profiles if str(p.get("state") or "") == "running")
        self.count_var.set(f"{len(profiles)} 个 · 运行中 {running}")
        signature = tuple(
            (str(p.get("id")), str(p.get("name")), str(p.get("state") or ""),
             str(p.get("group_name") or ""), str(p.get("last_error") or ""))
            for p in profiles
        )
        if signature != self._signature:
            # 只有真的变了才重建控件：保住按钮焦点，也避免每 5 秒闪一下
            self._signature = signature
            self._render(profiles)
        # 空列表的引导语已经显示在列表中央，状态行再说一遍是啰嗦
        self.set_status(f"共 {len(profiles)} 个环境")

    # -- 渲染 --
    def _render(self, profiles: list[dict[str, Any]]) -> None:
        for child in self.inner.winfo_children():
            child.destroy()
        if not profiles:
            self._render_empty()
            return
        for p in profiles:
            self._render_row(p)
        if self._overlay is not None and self._overlay.is_open():
            # 浮层得保持在最上面（新行是刚创建的控件，层级可能盖住它）
            try:
                self._overlay.root.lift()
            except Exception:
                pass
        self._bind_wheel(self.inner)
        self.after_idle(self._on_inner_configure, None)

    def _render_empty(self) -> None:
        theme, fonts = self.theme, self.fonts
        box = tk.Frame(self.inner, bg=theme.card)
        box.pack(fill="both", expand=True, pady=16)
        tk.Label(box, text="还没有环境，点「新建环境」开始", bg=theme.card, fg=theme.faint,
                 font=fonts["small"]).pack(anchor="center")
        tk.Label(box, text="每个环境是一个独立的浏览器身份（cookie、指纹互不影响）",
                 bg=theme.card, fg=theme.faint, font=fonts["tiny"]).pack(anchor="center", pady=(3, 0))

    def _render_row(self, p: dict[str, Any]) -> None:
        theme, fonts = self.theme, self.fonts
        pid = str(p.get("id") or "")
        name = str(p.get("name") or "")
        state = str(p.get("state") or "stopped")
        label, badge_bg, badge_fg = _state_badge(state)

        row = tk.Frame(self.inner, bg=theme.card2)
        row.pack(fill="x", pady=(0, 4))
        line = tk.Frame(row, bg=theme.card2, padx=9, pady=5)
        line.pack(fill="x")

        tk.Label(line, text=_clip(name, self.ROW_LIMIT_NAME), bg=theme.card2, fg=theme.ink,
                 font=fonts["bold"], anchor="w").pack(side="left")
        tk.Label(line, text=label, bg=badge_bg, fg=badge_fg, font=fonts["tiny"],
                 padx=5, pady=1).pack(side="left", padx=(7, 0))
        group = str(p.get("group_name") or "").strip()
        if group:
            tk.Label(line, text=_clip(group, self.ROW_LIMIT_GROUP), bg=theme.card2,
                     fg=theme.faint, font=fonts["tiny"], anchor="w").pack(side="left", padx=(7, 0))

        ttk.Button(line, text="删除", style="Danger.TButton",
                   command=lambda: self.ask_delete(pid, name)).pack(side="right")
        stop_like = state in ("running", "starting")
        ttk.Button(line, text="停止" if stop_like else "启动", style="Tiny.TButton",
                   command=lambda: self.toggle(pid, name, state)).pack(side="right", padx=(0, 6))

    # -- 操作：新建 --
    def ask_create(self) -> None:
        if self._overlay is not None and self._overlay.is_open():
            self._overlay.close()
        ov = TextOverlay(
            self, self.theme, self.fonts,
            title="新建环境",
            subtitle="名称必填，最多 64 个字；分组可以留空。\n环境建好后点「启动」才会真的开浏览器。",
            fields=[("name", "名称", ""), ("group", "分组（可选）", "")],
            submit_text="创建",
            on_submit=self._do_create,
        )
        self._overlay = ov
        ov.open()

    def _do_create(self, values: dict[str, str]) -> bool:
        """提交新建。**总是返回 True（浮层先留着），等服务器回话再决定关不关**。

        为什么不在这里就关：重名（core 返回 NAME_TAKEN）是最常见的失败，
        关掉浮层再让用户重新点「新建环境」、重新敲一遍名字很烦。
        留着浮层，失败原因直接写在输入框下面，改个名字就能再提交。
        """
        ov = self._overlay

        def fail(msg: str) -> bool:
            if isinstance(ov, TextOverlay):
                ov.set_error(msg)
            return True

        name = (values.get("name") or "").strip()
        group = (values.get("group") or "").strip()
        if not name:
            return fail("名称不能为空")
        if len(name) > 64:
            return fail("名称不要超过 64 个字")
        if self._busy:
            return fail("上一个操作还没完成，稍等一下")
        self._busy += 1
        self.set_status(f"正在创建「{name}」…")
        self.on_log(f"正在创建环境「{name}」", "info")
        call_async(self, lambda: self.api.create_profile(name, group),
                   lambda res: self._after_create(res, name, ov))
        return True

    def _after_create(self, res: tuple[bool, Any, dict[str, Any] | None],
                      name: str, ov: Any = None) -> None:
        self._busy = max(0, self._busy - 1)
        ok, data, err = res
        if ok:
            self.on_log(f"已创建环境「{name}」", "ok")
            self.set_status(f"已创建「{name}」")
            if isinstance(ov, TextOverlay) and ov.is_open():
                ov.close()          # 只有成功了才收起浮层
        else:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            hint = str((err or {}).get("hint") or "")
            extra = f"（{hint}）" if hint else ""
            if code == "NAME_TAKEN":
                msg = f"环境名已被占用：{name}"
            if isinstance(ov, TextOverlay) and ov.is_open():
                ov.set_error(f"{msg}{extra or ''}")   # 原因就写在输入框下面
            self.on_log(f"创建环境失败：{name} [{code}] {msg}{extra}", "bad")
            self.set_status(f"创建失败：[{code}] {msg}", bad=True)
        self.on_changed()
        self.refresh(force=True)

    # -- 操作：启动 / 停止 --
    def toggle(self, pid: str, name: str, state: str) -> None:
        stop_like = state in ("running", "starting")
        verb = "停止" if stop_like else "启动"
        if self._busy:
            # 一次只做一件事：连点两个"启动"会让同一个环境并发启停，
            # core 侧虽然有租约挡着，但用户看到的是"点了没反应"。
            # 拒绝也要写进事件流 —— 状态行会被下一次刷新盖掉，事件流不会。
            self.set_status("上一个操作还没完成，稍等一下", bad=True)
            self.on_log(f"没执行{verb}「{name}」：上一个操作还没完成", "warn")
            return
        self._busy += 1
        self.set_status(f"正在{verb}「{name}」…（启动要等浏览器起来，可能要几十秒）")
        self.on_log(f"正在{verb}环境「{name}」", "info")
        fn = (lambda: self.api.stop_profile(pid)) if stop_like else (lambda: self.api.start_profile(pid))
        call_async(self, fn, lambda res: self._after_toggle(res, verb, name))

    def _after_toggle(self, res: tuple[bool, Any, dict[str, Any] | None], verb: str, name: str) -> None:
        self._busy = max(0, self._busy - 1)
        ok, data, err = res
        if ok:
            self.on_log(f"已{verb}环境「{name}」", "ok")
            self.set_status(f"已{verb}「{name}」")
        else:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            hint = str((err or {}).get("hint") or "")
            extra = f"（{hint}）" if hint else ""
            self.on_log(f"{verb}环境失败：{name} [{code}] {msg}{extra}", "bad")
            self.set_status(f"{verb}失败：[{code}] {msg}", bad=True)
        self.on_changed()
        self.refresh(force=True)

    # -- 操作：删除 --
    def ask_delete(self, pid: str, name: str) -> None:
        if self._overlay is not None and self._overlay.is_open():
            self._overlay.close()
        ov = ConfirmOverlay(
            self, self.theme, self.fonts,
            title="删除环境",
            message=(f"确定删除「{name}」吗？\n\n"
                     "它的浏览器数据（cookie、指纹配置、本地存储）会一起被清掉，"
                     "这一步不能撤销。"),
            confirm_text="删除",
            on_confirm=lambda: self._do_delete(pid, name),
        )
        self._overlay = ov
        ov.open()

    def _do_delete(self, pid: str, name: str) -> None:
        if self._busy:
            self.set_status("上一个操作还没完成，稍等一下", bad=True)
            self.on_log(f"没执行删除「{name}」：上一个操作还没完成", "warn")
            return
        self._busy += 1
        self.set_status(f"正在删除「{name}」…")
        self.on_log(f"正在删除环境「{name}」", "info")
        call_async(self, lambda: self.api.delete_profile(pid, purge=True),
                   lambda res: self._after_delete(res, name))

    def _after_delete(self, res: tuple[bool, Any, dict[str, Any] | None], name: str) -> None:
        self._busy = max(0, self._busy - 1)
        ok, data, err = res
        if ok:
            self.on_log(f"已删除环境「{name}」", "ok")
            self.set_status(f"已删除「{name}」")
        else:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            hint = str((err or {}).get("hint") or "")
            extra = f"（{hint}）" if hint else ""
            self.on_log(f"删除环境失败：{name} [{code}] {msg}{extra}", "bad")
            self.set_status(f"删除失败：[{code}] {msg}", bad=True)
        self.on_changed()
        self.refresh(force=True)


# ── 日志 / 事件面板 ───────────────────────────────────────
def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "--:--:--"


def _brief_detail(detail: Any, limit: int = 80) -> str:
    """把 detail 压成一行人能读的文字。

    直接 `json.dumps` 会把整屏塞满（有些 action 的 detail 带完整清单），
    所以先挑几个通用键，挑不到再退回整段 JSON 截断。
    """
    if not detail:
        return ""
    if not isinstance(detail, dict):
        s = str(detail)
        return s[:limit] + ("…" if len(s) > limit else "")
    keys = ("name", "state", "from", "to", "method", "port", "pid", "freed_human",
            "total", "count", "enabled", "reason")
    parts: list[str] = []
    for k in keys:
        v = detail.get(k)
        if v in ("", None, [], {}):
            continue
        if isinstance(v, (list, dict)):
            continue
        parts.append(f"{k}={v}")
    if not parts:
        parts.append(json.dumps(detail, ensure_ascii=False))
    s = " ".join(parts)
    return s[:limit] + ("…" if len(s) > limit else "")


def _is_error(row: dict[str, Any]) -> bool:
    """一条事件算不算"错误"。

    `audit_log` 里没有 level 字段，只有 `result`（ok/fail）与 `error` 文本，
    所以"错误"= result 不是 ok，或 error 非空。按 code 猜是猜不准的。
    """
    result = str(row.get("result") or "ok").strip().lower()
    return (result not in ("", "ok")) or bool(str(row.get("error") or "").strip())


class LogPanel(tk.Frame):
    """日志/事件面板：最近事件 + 只看错误 + 打开日志目录 + 复制诊断信息。

    事件来自 core 的 `GET /api/v1/events`（audit_log 表）。它记的是"动作"，
    而 `<数据目录>/logs/` 里的文件记的是"运行细节"（浏览器启动参数、CDP 握手）。
    排查时两者都要看，所以两个入口都给。
    """

    def __init__(self, master: tk.Misc, *, api: CoreApi, theme: Theme,
                 fonts: dict[str, Any], on_log: Callable[[str, str], None],
                 context: Callable[[], dict[str, Any]],
                 enabled: Callable[[], bool] | None = None) -> None:
        super().__init__(master, bg=theme.card, bd=0, highlightthickness=0)
        self.api = api
        self.theme = theme
        self.fonts = fonts
        self.on_log = on_log
        self.context = context
        self.enabled = enabled

        self.var_err_only = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="")
        self._rows: list[dict[str, Any]] = []
        self._signature: tuple[Any, ...] | None = None
        self._inflight = False
        self._last_error = ""

        self._build()
        self.set_status("正在读取事件 …")

    def _build(self) -> None:
        theme, fonts = self.theme, self.fonts
        bar = tk.Frame(self, bg=theme.card)
        bar.pack(fill="x", padx=12, pady=(8, 2))
        tk.Label(bar, text="最近事件", bg=theme.card, fg=theme.ink, font=fonts["bold"],
                 anchor="w").pack(side="left")
        ttk.Button(bar, text="刷新", style="Tiny.TButton",
                   command=lambda: self.refresh(force=True)).pack(side="right")
        ttk.Checkbutton(bar, text="只看错误", variable=self.var_err_only,
                        style="Card.TCheckbutton",
                        command=self._on_filter).pack(side="right", padx=(0, 8))

        bar2 = tk.Frame(self, bg=theme.card)
        bar2.pack(fill="x", padx=12, pady=(2, 6))
        ttk.Button(bar2, text="打开日志文件", style="Tiny.TButton",
                   command=self.open_logs).pack(side="left")
        ttk.Button(bar2, text="复制诊断信息", style="Tiny.TButton",
                   command=self.copy_diagnostics).pack(side="left", padx=(6, 0))
        tk.Label(bar2, text="排查问题时把诊断信息贴给别人", bg=theme.card, fg=theme.faint,
                 font=fonts["tiny"], anchor="e").pack(side="right")

        wrap = tk.Frame(self, bg=theme.field)
        wrap.pack(fill="both", expand=True, padx=(12, 6), pady=(0, 4))
        self.text = tk.Text(wrap, bg=theme.field, fg=theme.dim, bd=0, highlightthickness=0,
                            font=(self.fonts.get("mono") or "TkFixedFont", 8), wrap="none",
                            state="disabled", height=5)
        vsb = tk.Scrollbar(wrap, command=self.text.yview, bd=0, width=8,
                           troughcolor=theme.field, bg=theme.card2)
        self.text.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        for tag, color in (("ok", theme.dim), ("bad", theme.bad), ("warn", theme.warn),
                           ("ts", theme.faint), ("act", theme.ink)):
            self.text.tag_configure(tag, foreground=color)

        foot = tk.Frame(self, bg=theme.card)
        foot.pack(fill="x", padx=12, pady=(0, 6))
        self.status_label = tk.Label(foot, textvariable=self.status_var, bg=theme.card,
                                     fg=theme.faint, font=fonts["tiny"], anchor="w")
        self.status_label.pack(fill="x")

    def set_status(self, text: str, bad: bool = False) -> None:
        self.status_var.set(text)
        try:
            self.status_label.configure(fg=self.theme.bad if bad else self.theme.faint)
        except Exception:
            pass

    def _on_filter(self) -> None:
        # 过滤条件变了，签名也跟着变，下次渲染必然重画
        self._signature = None
        self.refresh(force=True)

    # -- 刷新 --
    def autorefresh(self) -> None:
        """自动刷新入口：只在「日志」tab 真正显示时才拉事件。

        事件表是"看一眼"的信息，用户盯着「环境」列表时没必要每 5 秒拉一次
        （core 那边每次都要跑一条 200 行的 SQL）。
        """
        if not self.winfo_ismapped():
            return
        if self.enabled is not None and not self.enabled():
            return
        self.refresh()

    def refresh(self, force: bool = False) -> None:
        if self._inflight:
            return
        self._inflight = True
        call_async(self, lambda: self.api.events(0.0), self._on_events)

    def _on_events(self, res: tuple[bool, Any, dict[str, Any] | None]) -> None:
        self._inflight = False
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            self.set_status(f"读不到事件：{msg}", bad=True)
            if code + msg != self._last_error:
                self._last_error = code + msg
                self.on_log(f"读取事件失败：[{code}] {msg}", "bad")
            return
        self._last_error = ""
        rows = list((data or {}).get("events") or [])
        # 接口按 id DESC 返回；转成正序，最新的一行在下面（和事件流习惯一致）
        rows.reverse()
        self._rows = rows
        signature = (len(rows), rows[-1].get("id") if rows else 0,
                     bool(self.var_err_only.get()))
        if signature != self._signature:
            self._signature = signature
            self._render(rows)
        shown = sum(1 for r in rows if (not self.var_err_only.get()) or _is_error(r))
        self.set_status(f"共 {len(rows)} 条事件" + ("（已过滤出 %d 条错误）" % shown
                                                   if self.var_err_only.get() else ""))

    def _render(self, rows: list[dict[str, Any]]) -> None:
        err_only = bool(self.var_err_only.get())
        shown = [r for r in rows if (not err_only) or _is_error(r)][-120:]
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            if not shown:
                tip = "还没有事件（本机服务刚起来时是空的）" if not err_only \
                    else "没有错误事件，一切正常"
                self.text.insert("end", tip + "\n", "ts")
            for row in shown:
                self.text.insert("end", _fmt_ts(row.get("ts")) + "  ", "ts")
                self.text.insert("end", str(row.get("action") or "?") + "  ", "act")
                detail = _brief_detail(row.get("detail"))
                err_text = str(row.get("error") or "").strip()
                body = err_text or detail
                self.text.insert("end", body + "\n", "bad" if _is_error(row) else "ok")
            self.text.see("end")
            self.text.configure(state="disabled")
        except Exception:
            pass

    # -- 打开日志目录 --
    def open_logs(self) -> None:
        path = logs_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"日志目录不可用：{exc}", bad=True)
            self.on_log(f"打开日志目录失败：{exc}", "bad")
            return
        ok, info = open_in_file_manager(path)
        if ok:
            self.on_log(f"已打开日志目录：{path}", "ok")
            self.set_status(f"已打开：{path}")
        else:
            self.on_log(f"打开日志目录失败：{info}（路径：{path}）", "bad")
            self.set_status(f"打开失败：{info}", bad=True)

    # -- 复制诊断信息 --
    def copy_diagnostics(self) -> None:
        self.set_status("正在收集诊断信息 …")
        call_async(self, lambda: self.api.events(0.0), self._fill_diagnostics)

    def _fill_diagnostics(self, res: tuple[bool, Any, dict[str, Any] | None]) -> None:
        ok, data, err = res
        rows = list((data or {}).get("events") or []) if ok else []
        ctx: dict[str, Any] = {}
        try:
            ctx = dict(self.context() or {})
        except Exception:
            ctx = {}
        root = data_root()
        lines = [
            "知纹客户端 诊断信息",
            f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"客户端版本：{ctx.get('version') or '—'}",
            f"设备号：{ctx.get('device_id') or '—'}",
            f"服务器：{ctx.get('server') or '未连接'}",
            f"本机服务端口：{ctx.get('core_port') or self.api.port}",
            f"数据目录：{root}",
            f"日志目录：{root / 'logs'}",
            "",
        ]
        if ok:
            lines.append(f"最近 {min(20, len(rows))} 条事件（新的在上面）：")
            for row in rows[:20]:
                flag = "错误" if _is_error(row) else "正常"
                extra = str(row.get("error") or "").strip() or _brief_detail(row.get("detail"), 60)
                lines.append(f"[{_fmt_ts(row.get('ts'))}] {flag} "
                             f"{row.get('action')} {extra}".rstrip())
        else:
            lines.append(f"事件读取失败：{(err or {}).get('message') or '未知原因'}")
        text = "\n".join(lines)
        try:
            top = self.winfo_toplevel()
            top.clipboard_clear()
            top.clipboard_append(text)
            self.on_log("诊断信息已复制到剪贴板", "ok")
            self.set_status("诊断信息已复制（直接粘贴给别人即可）")
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"复制失败：{exc}", bad=True)
            self.on_log(f"复制诊断信息失败：{exc}", "bad")

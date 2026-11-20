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
import re
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
    "TextOverlay",
    "ConfirmOverlay",
    "NoticeOverlay",
    "ChoiceOverlay",
    "CreateProfileOverlay",
    "ProxyOverlay",
    "ImportProxiesOverlay",
    "ProxyLibraryOverlay",
    "PacksOverlay",
    "PackApplyOverlay",
    "call_async",
    "data_root",
    "open_in_file_manager",
    "logs_dir",
    "mask_proxy_text",
    "proxy_display_for",
    "format_probe",
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


# ── 代理串的脱敏与展示 ────────────────────────────────────

def _default_chrome_userdata() -> str:
    """Chrome 用户数据目录的常见位置（给输入框做默认值，省得用户去找）。

    找不到就返回空串 —— 让人自己填，而不是填一个不存在的路径让他改。
    """
    import os as _os
    import sys as _sys
    from pathlib import Path as _P

    cands: list[_P] = []
    if _sys.platform == "win32":
        base = _os.environ.get("LOCALAPPDATA") or str(_P.home() / "AppData" / "Local")
        cands += [_P(base) / "Google" / "Chrome" / "User Data",
                  _P(base) / "Chromium" / "User Data"]
    elif _sys.platform == "darwin":
        cands.append(_P.home() / "Library" / "Application Support" / "Google" / "Chrome")
    else:
        cands += [_P.home() / ".config" / "google-chrome",
                  _P.home() / ".config" / "chromium",
                  _P.home() / ".config" / "google-chrome-beta"]
    for c in cands:
        try:
            if (c / "Default" / "Extensions").is_dir() or (c / "Default").is_dir():
                return str(c)
        except OSError:
            continue
    return str(cands[0]) if cands else ""

def mask_proxy_text(raw: str) -> str:
    """把一串代理里的密码换成 `***`，其余原样。

    为什么自己实现（不 import `services.proxy` 的解析器）：面板只与 HTTP 接口
    打交道，解析器的错误码/认证扩展是接口层的关注点；这里只需要保证"不管
    用户粘进来多畸形的串，密码都不会被回显到界面上"。宁可显示得丑，不能漏密码。

    覆盖两种常见形态：`scheme://user:pass@host:port` 与 `host:port:user:pass`。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "@" in text:
        head, _, tail = text.rpartition("@")
        scheme = ""
        cred = head
        if "://" in head:
            scheme, _, cred = head.partition("://")
            scheme += "://"
        if ":" in cred:
            user, _, _pw = cred.partition(":")
            return f"{scheme}{user}:***@{tail}"
        # 只有用户名没有密码：用户名也遮掉大半（有人把 token 当用户名用）
        return f"{scheme}{_clip(cred, 1)}***@{tail}"
    parts = text.split(":")
    if len(parts) == 4:            # host:port:user:pass
        return f"{parts[0]}:{parts[1]}:{parts[2]}:***"
    return text


# 环境行里代理列的宽度上限（字符数）。**只在这里定义一次**：
# `proxy_upstream_of()` 要用它决定"要不要省掉协议名"，
# `ProfilePanel._render_row()` 要用它 `_clip()`。两处各写一个 22 迟早会不同步，
# 而不同步的后果就是之前修过的那个 bug 又回来（IP 尾部被切掉、分不清是哪台代理）。
ROW_LIMIT_PROXY = 22


def proxy_upstream_of(row: dict[str, Any]) -> str:
    """代理库一行 → 给人看的脱敏串。

    ## 为什么有认证时把用户名也压成 `***`

    原来返回 `socks5://user:***@66.80.12.66:20000`（32 字符），而行里只留
    `ROW_LIMIT_PROXY = 22` 个字符 → 截成 `socks5://user:***@66.80.…`，
    **IP 尾部被切掉**。而 IP 正是"这条代理是哪台"的唯一有用信息：
    66.80.12.66 和 66.80.12.65 在界面上长得一模一样，用户根本分不清。

    改成 `socks5://***@66.80.12.66:20000`（28 字符，仍会截断但保住 IP 主体），
    并且**优先保住 host:port**：真超长时先省协议名。
    用户名对"这是谁的代理"有点用，但比不上"是哪台机器"重要。
    """
    scheme = str(row.get("scheme") or "").strip()
    host = str(row.get("host") or "").strip()
    port = row.get("port") or 0
    user = str(row.get("username") or "").strip()
    auth = "***@" if user else ""
    full = f"{scheme}://{auth}{host}:{port}"
    # 超长时先砍协议名（host:port 是不能丢的那部分）
    if len(full) > ROW_LIMIT_PROXY:
        short = f"{auth}{host}:{port}"
        if len(short) <= ROW_LIMIT_PROXY:
            return short
    return full


def proxy_display_for(profile: dict[str, Any], library: dict[int, dict[str, Any]]) -> str:
    """这个环境实际用哪个代理（脱敏）。空串 = 没设代理。

    为什么需要 `library`：`assign_to_profile()` 会把 `profiles.proxy` 置空、
    只留 `proxy_id`（见 `services/proxylib.py` 顶部说明），所以光看环境行
    是看不出代理地址的，必须按 id 关联代理库。
    """
    pid = profile.get("proxy_id")
    if pid not in (None, "", 0):
        try:
            row = library.get(int(pid))
        except (TypeError, ValueError):
            row = None
        if row is not None:
            return proxy_upstream_of(row)
        return f"代理 #{pid}（库里已没有这一条）"
    raw = str(profile.get("proxy") or "").strip()
    return mask_proxy_text(raw) if raw else ""


def proxy_display_from_assign(data: dict[str, Any]) -> str:
    """`PUT /profiles/{ref}/proxy` 的返回 → 脱敏串（清除时返回空串）。"""
    if not isinstance(data, dict) or data.get("cleared"):
        return ""
    scheme = str(data.get("scheme") or "")
    host = str(data.get("host") or "")
    port = data.get("port") or 0
    user = str(data.get("username") or "")
    if user:
        return f"{scheme}://{user}:***@{host}:{port}"
    return f"{scheme}://{host}:{port}" if (scheme or host) else ""


def format_probe(data: Any) -> tuple[str, bool]:
    """把一次出口检测的结果压成一行字。返回 (文字, 是否算失败)。"""
    if not isinstance(data, dict):
        return "没有拿到检测结果", True
    if data.get("ok"):
        bits = [f"出口 IP {data.get('exit_ip') or '未知'}"]
        country = str(data.get("country") or "").strip()
        if country:
            bits.append(country)
        ms = int(data.get("latency_ms") or 0)
        if ms:
            bits.append(f"{ms} ms")
        return " · ".join(bits), False
    code = str(data.get("code") or "").strip()
    err = str(data.get("error") or "出口检测没通过").strip()
    return (f"检测失败：[{code}] {err}" if code else f"检测失败：{err}"), True


def _unwrap_business(ok: bool, data: Any, err: dict[str, Any] | None
                     ) -> tuple[bool, Any, dict[str, Any] | None]:
    """把 `proxylib` / `packs` 的"业务失败"折成统一的失败三元组。

    这两个服务层的失败是 **HTTP 200 + `data.ok=false`**（见 `proxylib.test_one`、
    `assign_to_profile`），而 `CoreApi.call` 的 `ok` 只表示"接口调通了"。
    不折这一下，界面上会出现"保存代理成功"但其实代理没绑上的假成功。
    """
    if not ok:
        return ok, data, err
    if isinstance(data, dict) and data.get("ok") is False:
        return False, data, {
            "code": str(data.get("code") or "FAILED"),
            "message": str(data.get("error") or data.get("message") or "操作没有成功"),
            "hint": str(data.get("hint") or ""),
        }
    return ok, data, err


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
            except Exception:  # noqa: BLE001
                # 回调自己抛异常时**不能静默**。Tk 自己的回调错误会打到 stderr，
                # 但这里是我们直接在主线程里调 on_done，异常会被这层吃掉 ——
                # 结果就是"界面某项只是不动了"，查起来极难（踩过：after_idle 调在
                # 一个非控件对象上，render 抛 AttributeError，状态行永远停在
                # 「正在读取 …」）。打包后 stderr 可能是 None，所以整段都兜住。
                try:
                    import traceback

                    traceback.print_exc()
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
        self, name: str, group: str = "", note: str = "",
        with_default_packs: bool = True,
        pack_ids: list[str] | None = None,
    ) -> tuple[bool, Any, dict[str, Any] | None]:
        """建环境。`pack_ids` 非空时**只带这些包**（优先于 with_default_packs）。"""
        # 注意字段名是 `group_name`（见 api.py 的 ProfileCreate）：
        # 传 `group` 会被 pydantic 当未知字段忽略，分组就静默丢了。
        body: dict[str, Any] = {"name": name, "group_name": group, "note": note,
                                "with_default_packs": with_default_packs}
        if pack_ids:
            body["pack_ids"] = list(pack_ids)
        return self.call("POST", "/api/v1/profiles", json_body=body)

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

    # -- 代理 --
    # 这一组里"业务失败"是 HTTP 200 + data.ok=false，所以统一过 _unwrap_business，
    # 调用方只看 (ok, data, err) 三元组就够。
    def list_proxies(self) -> tuple[bool, Any, dict[str, Any] | None]:
        return self.call("GET", "/api/v1/proxies", timeout=8.0)

    def import_proxies(self, text: str, label_prefix: str = "") -> tuple[bool, Any, dict[str, Any] | None]:
        # 一次可能粘几百行、每行都要过一遍解析与唯一性检查，给足超时
        return _unwrap_business(*self.call(
            "POST", "/api/v1/proxies/import",
            json_body={"text": text, "label_prefix": label_prefix}, timeout=120.0,
        ))

    def test_proxy_raw(self, raw: str) -> tuple[bool, Any, dict[str, Any] | None]:
        # 出口检测要真的经代理抓一次页面；服务端单次超时 15s，这里留余量
        return _unwrap_business(*self.call(
            "POST", "/api/v1/proxies/test", json_body={"raw": raw}, timeout=90.0,
        ))

    def test_proxies(self, ids: list[int]) -> tuple[bool, Any, dict[str, Any] | None]:
        # 服务端并发 4、单条 15s：代理多时整批按分钟算，超时给很宽
        return self.call(
            "POST", "/api/v1/proxies/test-many",
            json_body={"ids": [int(i) for i in ids]}, timeout=600.0,
        )

    def set_profile_proxy(
        self, ref: str, *, proxy_id: int | None = None, raw: str = ""
    ) -> tuple[bool, Any, dict[str, Any] | None]:
        """给环境设/换代理。`proxy_id` 与 `raw` 都空 = 清除代理。"""
        body: dict[str, Any] = {}
        if proxy_id is not None:
            body["proxy_id"] = int(proxy_id)
        text = (raw or "").strip()
        if text:
            body["raw"] = text
        return _unwrap_business(*self.call(
            "PUT", f"/api/v1/profiles/{quote(ref, safe='')}/proxy",
            json_body=body, timeout=60.0,
        ))

    # -- 扩展包 --
    def list_packs(self) -> tuple[bool, Any, dict[str, Any] | None]:
        return self.call("GET", "/api/v1/packs", timeout=10.0)

    def start_engine_install(self) -> tuple[bool, Any, dict[str, Any] | None]:
        """让 core 后台下载 ungoogled-chromium。"""
        return _unwrap_business(*self.call(
            "POST", "/api/v1/engine/install", timeout=30.0))

    def scan_browsers(self) -> tuple[bool, Any, dict[str, Any] | None]:
        """扫本机"有扩展可导入"的浏览器配置目录（给导入弹窗的「扫描本机」用）。

        扫的是 `%LOCALAPPDATA%` / `%APPDATA%` / `Programs` / `Program Files…`，
        限深度；Windows 上磁盘慢时可能要几秒，所以调用方必须走后台线程。
        """
        return _unwrap_business(*self.call(
            "GET", "/api/v1/packs/scan-browsers", timeout=120.0))

    def import_pack(self, *, path: str = "", from_profile: str = "",
                    from_chrome: str = "",
                    name: str = "") -> tuple[bool, Any, dict[str, Any] | None]:
        """导入扩展包。三种来源：

        - `from_profile`：从知纹自己的环境导出
        - `from_chrome`：从任意 Chrome/Roxy 的用户数据目录导出
          （"把 RXYCDP 的插件带走"就是这条）
        - `path`：扩展目录或 zip

        从环境/Chrome 导入要复制扩展代码 + 本地存储（可能几十 MB），慢是正常的。
        """
        return _unwrap_business(*self.call(
            "POST", "/api/v1/packs/import",
            json_body={"path": path, "from_profile": from_profile,
                       "from_chrome": from_chrome, "name": name},
            timeout=600.0,
        ))

    def pull_hub_packs(self, *, as_default: bool = False
                       ) -> tuple[bool, Any, dict[str, Any] | None]:
        """从控制台（hub）拉取扩展包并导入本机。

        用户要的是「把插件打包放到控制台，以后建环境默认带上」——
        在这个按钮出现之前，包虽然能传到 hub，但**没有任何界面入口能取回来**，
        等于只有服务端能力、没有可用功能。

        拉包要下载 + 解包，可能几十 MB，超时给足。
        """
        return _unwrap_business(*self.call(
            "POST", f"/api/v1/packs/pull-hub?as_default={1 if as_default else 0}",
            timeout=600.0,
        ))

    def push_hub_packs(self, pack_id: str = "") -> tuple[bool, Any, dict[str, Any] | None]:
        """把本机扩展包上传到控制台（不给 pack_id = 全部）。

        以前只有"从控制台拉"，没有"推到控制台" —— 而控制台上传要的是 zip，
        用户手上根本没有那个文件。这条补上后双向才通。
        """
        return _unwrap_business(*self.call(
            "POST", f"/api/v1/packs/push-hub?pack_id={quote(pack_id, safe='')}",
            timeout=600.0,
        ))

    def install_extension(self, *, url: str = "", path: str = "",
                          name: str = "", make_pack: bool = True,
                          as_default: bool = False
                          ) -> tuple[bool, Any, dict[str, Any] | None]:
        """从 Chrome 商店链接或本地 .crx/.zip 装扩展。

        下载由**这台机器**完成（服务器常没有外网，也没有用户的代理）。
        装完默认顺手做成扩展包；`as_default=True` 再标成"新建环境自动带上"。
        """
        return _unwrap_business(*self.call(
            "POST", "/api/v1/extensions/install",
            json_body={"url": url, "path": path, "name": name,
                       "make_pack": make_pack, "as_default": as_default},
            timeout=180.0,
        ))

    def check_extension_updates(self) -> tuple[bool, Any, dict[str, Any] | None]:
        return _unwrap_business(*self.call(
            "GET", "/api/v1/extensions/updates", timeout=180.0))

    def set_pack_default(self, pack_id: str, enabled: bool = True
                         ) -> tuple[bool, Any, dict[str, Any] | None]:
        return _unwrap_business(*self.call(
            "POST", f"/api/v1/packs/{quote(pack_id, safe='')}/default",
            json_body={"enabled": bool(enabled)}, timeout=30.0,
        ))

    def delete_pack(self, pack_id: str,
                    force: bool = False) -> tuple[bool, Any, dict[str, Any] | None]:
        """删包。有环境在用会返回 `PACK_IN_USE`，确认要删就传 `force=True`。"""
        q = "?force=1" if force else ""
        return _unwrap_business(*self.call(
            "DELETE", f"/api/v1/packs/{quote(pack_id, safe='')}{q}", timeout=60.0,
        ))

    def pack_usage(self, pack_id: str) -> tuple[bool, Any, dict[str, Any] | None]:
        """这个包里的扩展被哪些环境用着（删包前判断影响）。"""
        return _unwrap_business(*self.call(
            "GET", f"/api/v1/packs/{quote(pack_id, safe='')}/usage", timeout=30.0,
        ))

    def remove_extensions(self, ref: str,
                          slugs: list[str]) -> tuple[bool, Any, dict[str, Any] | None]:
        """从环境里摘掉指定扩展（只改环境记录，不动扩展库与包）。"""
        return _unwrap_business(*self.call(
            "POST", f"/api/v1/profiles/{quote(ref, safe='')}/extensions/remove",
            json_body={"slugs": list(slugs)}, timeout=120.0,
        ))

    def apply_packs(self, ref: str, pack_ids: list[str]) -> tuple[bool, Any, dict[str, Any] | None]:
        return _unwrap_business(*self.call(
            "POST", f"/api/v1/profiles/{quote(ref, safe='')}/packs/apply",
            json_body={"pack_ids": list(pack_ids)}, timeout=600.0,
        ))

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


class _Placeholder:
    """给 `tk.Entry` 加灰色占位符（提示示例），并在取值时把占位符当成空。

    为什么这一格用 `tk.Entry` 而不是 `ttk.Entry`：ttk 的前景色由 style 决定，
    `configure(foreground=...)` 直接抛 unknown option；占位符必须是灰色，
    否则用户会把示例串当成真内容一起提交（"填了代理却报格式错"就是这么来的）。

    为什么不用 `insert(0, text)` 硬塞：那样提交时拿到的就是示例文本。这里
    用一个"是否处于占位状态"的标记，`value()` 在占位状态下返回空串。
    """

    def __init__(self, entry: tk.Entry, var: tk.StringVar, text: str, theme: Theme) -> None:
        self.entry = entry
        self.var = var
        self.text = text
        self.theme = theme
        self.active = False
        entry.bind("<FocusIn>", self._hide, add="+")
        entry.bind("<FocusOut>", self._show, add="+")
        # 必须盯变量本身，不能只靠焦点事件：鼠标右键粘贴、或者调用方直接
        # `var.set(...)` 都不会触发 <FocusIn>，占位标记会赖着不走，于是
        # `value()` 一直返回空串 —— 表现就是"明明填了代理，点测试却说输入框是空的"。
        var.trace_add("write", self._on_write)
        if not var.get():
            self._show()

    def _on_write(self, *_args: Any) -> None:
        if self.active and self.var.get() != self.text:
            self.active = False
            try:
                self.entry.configure(fg=self.theme.ink)
            except Exception:
                pass

    def _show(self, _event: Any = None) -> None:
        if self.var.get():
            return
        self.active = True
        self.var.set(self.text)
        try:
            self.entry.configure(fg=self.theme.faint)
        except Exception:
            pass

    def _hide(self, _event: Any = None) -> None:
        if not self.active:
            return
        self.active = False
        self.var.set("")
        try:
            self.entry.configure(fg=self.theme.ink)
        except Exception:
            pass

    def value(self) -> str:
        """真实内容（还停留在占位符上时算空）。"""
        current = self.var.get()
        if self.active and current == self.text:
            return ""
        return current

    def reset(self) -> None:
        """清空并重新显示占位符（保存成功后用，避免用户以为还没写进去）。"""
        self.active = False
        self.var.set("")
        self._show()


class TextOverlay(_Overlay):
    """自绘的名称输入浮层（替代 `simpledialog.askstring`）。

    `fields` 的每一项是 `(key, 标签, 初始值)`，也可以给第 4 项当**占位符**：
    `(key, 标签, "", "socks5://user:pass@1.2.3.4:20000（可留空）")`。
    """

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 title: str, subtitle: str, fields: list[tuple[str, ...]],
                 submit_text: str, on_submit: Callable[[dict[str, str]], bool]) -> None:
        super().__init__(master, theme, fonts, title, subtitle)
        self.on_submit = on_submit
        self.vars: dict[str, tk.StringVar] = {}
        self.entries: list[tk.Widget] = []
        self._placeholders: dict[str, _Placeholder] = {}
        self.result_label: tk.Label | None = None
        self.err_var = tk.StringVar(value="")
        for idx, field in enumerate(fields):
            key, label, initial = field[0], field[1], field[2]
            placeholder = str(field[3]) if len(field) > 3 else ""
            tk.Label(self.body, text=label, bg=theme.card2, fg=theme.dim,
                     font=fonts["tiny"], anchor="w").pack(fill="x", pady=(6 if idx else 0, 2))
            var = tk.StringVar(value=initial)
            self.vars[key] = var
            if placeholder:
                entry: tk.Widget = tk.Entry(
                    self.body, textvariable=var, bg=theme.field, fg=theme.ink,
                    insertbackground=theme.ink, relief="flat", bd=0,
                    highlightthickness=1, highlightbackground=theme.line,
                    highlightcolor=theme.acc, font=fonts["body"],
                )
                entry.pack(fill="x", ipady=5)
                self._placeholders[key] = _Placeholder(entry, var, placeholder, theme)
            else:
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

    def _values(self) -> dict[str, str]:
        """提交用的真实值（占位符不算内容）。"""
        out: dict[str, str] = {}
        for key, var in self.vars.items():
            ph = self._placeholders.get(key)
            out[key] = ph.value() if ph is not None else var.get()
        return out

    def add_action_row(self, text: str, command: Callable[[], None]) -> tk.Label:
        """在输入框下面插一行"动作按钮 + 结果文字"，返回结果 Label。

        为什么用 `before=self.error_label`：结果行要永远在错误行**上面**
        （先听到"出口 IP 是多少"，再看到"哪一步报错了"），而 Tk 的 pack
        顺序由调用顺序决定，后加的控件默认落到底部。
        """
        row = tk.Frame(self.body, bg=self.theme.card2)
        row.pack(fill="x", pady=(8, 0), before=self.error_label)
        ttk.Button(row, text=text, style="Tiny.TButton",
                   command=command).pack(anchor="w")
        # 结果放按钮**下面一行**（不是右边）：出口 IP 可能很长（有的人的检测源
        # 返回一整段 JSON），挤在按钮右边会被压成一两个字宽。
        self.result_label = tk.Label(row, text="", bg=self.theme.card2, fg=self.theme.faint,
                                     font=self.fonts["tiny"], anchor="w", justify="left",
                                     wraplength=320)
        self.result_label.pack(fill="x", pady=(3, 0))
        return self.result_label

    def add_pack_picker(self, packs: list[dict[str, Any]]) -> None:
        """在浮层里列出所有扩展包，让用户**逐个勾选**要带哪些。

        ## 为什么（2026-11-20 用户反馈）

        以前只有一个「带上默认扩展包」的勾选框：要么全带、要么全不带。
        用户的原话是「新建浏览器的时候，我希望是可以直接继承插件的」——
        他要的是**能选**，比如只要 Bitwarden + 脚本猫 + 沉浸式翻译，
        不要那两个测试用的。

        勾选结果放进 `self.vars["pack_ids"]`，`_values()` 会把它当普通字段收走，
        提交时由 `pack_ids` 参数传给后端（后端优先级高于 with_default_packs）。
        """
        row = tk.Frame(self.body, bg=self.theme.card2)
        row.pack(fill="x", pady=(8, 0), before=self.error_label)
        tk.Label(row, text="插件（勾选要带的包）", bg=self.theme.card2,
                 fg=self.theme.dim, font=self.fonts["tiny"], anchor="w").pack(fill="x")
        if not packs:
            tk.Label(row, text="还没有扩展包 —— 先去「扩展包」面板导入或从控制台拉取。",
                     bg=self.theme.card2, fg=self.theme.faint, font=self.fonts["tiny"],
                     anchor="w", justify="left", wraplength=320).pack(fill="x", pady=(2, 0))
            self.vars["pack_ids"] = tk.StringVar(value="")
            return

        # **既要全列出，又不能让浮层被撑破**（2026-11-20 两轮修正）。
        #
        # 第一版写死 `packs[:12]`：超过 12 个的包看不到也选不了，界面还不提示。
        # 第二版全列出：结果 5 个包就把标题和「创建」按钮顶出窗口（浮层是
        # `place(relx=.5, rely=.5)` 居中的，没有滚动），用户勾完点不到「创建」。
        #
        # 现在用 `_ScrollList`：固定高度 + 滚动条，多少个包都不会撑破浮层。
        # 高度按"浮层在 640×658 的默认窗口里还放得下「创建」按钮"来定。
        # 实测：120 超 57px、96 超 13px —— 新建浮层本身已经很满，
        # 所以这里只给 64px（约两行半）；包再多靠滚轮，不再撑高浮层。
        _sc = _ScrollList(row, self.theme, height=64)
        _sc.wrap.pack(fill="x", pady=(2, 0))
        # **不要用 `canvas.configure(name=...)`**（2026-11-20 实测踩到）：
        # Tk 的 Canvas 根本没有 `-name` 这个选项，会抛
        #   `_tkinter.TclError: unknown option "-name"`
        # 而这一行在 `add_pack_picker` 里、外层又被 `try/except` 包着 ——
        # 结果是**整个插件勾选器都渲染不出来**，界面静默降级成"只有默认包开关"，
        # 而且一个错都不报。给自检脚本的标识改用属性挂载（不碰 Tk 选项）。
        _sc.canvas.zw_is_pack_picker = True  # type: ignore[attr-defined]
        box = _sc.inner
        picked: list[tuple[tk.StringVar, str]] = []
        for p in packs:
            pid = str(p.get("id") or "")
            label = f"{p.get('name') or pid}（{p.get('extension_count', 0)} 个扩展" \
                    + ("，带数据" if p.get("with_data") else "") + "）"
            var = tk.StringVar(value="1" if p.get("is_default") else "")
            ttk.Checkbutton(box, text=label, style="Card.TCheckbutton",
                            variable=var, onvalue="1", offvalue="").pack(anchor="w")
            picked.append((var, pid))
        tk.Label(row, text="勾了就只带这些；都不勾 = 干净环境。",
                 bg=self.theme.card2, fg=self.theme.faint, font=self.fonts["tiny"],
                 anchor="w", justify="left", wraplength=320).pack(fill="x", pady=(2, 0))

        # 用一个 StringVar 承载"逗号分隔的选中 id"，这样能直接复用 _values()
        holder = tk.StringVar(value=",".join(pid for v, pid in picked if v.get() == "1"))
        for var, pid in picked:
            def _sync(*_a, _v=var, _p=pid) -> None:
                cur = [x for x in (holder.get() or "").split(",") if x]
                if _v.get() == "1":
                    if _p not in cur:
                        cur.append(_p)
                else:
                    cur = [x for x in cur if x != _p]
                holder.set(",".join(cur))
            var.trace_add("write", _sync)
        self.vars["pack_ids"] = holder
        self._placeholders.pop("pack_ids", None)

    def add_check_row(self, key: str, text: str, *, checked: bool = True,
                      hint: str = "") -> None:
        """在输入框下面插一个勾选项，值会以 `"1"` / `""` 出现在 `_values()` 里。

        为什么需要它：`fields` 只支持文本输入，而"要不要带默认扩展包"这种开关
        必须是个勾选。塞进文本框让用户输 y/n 是不可接受的。
        """
        row = tk.Frame(self.body, bg=self.theme.card2)
        row.pack(fill="x", pady=(8, 0), before=self.error_label)
        var = tk.StringVar(value="1" if checked else "")
        box = tk.Frame(row, bg=self.theme.card2)
        box.pack(anchor="w")
        ttk.Checkbutton(box, text=text, style="Card.TCheckbutton",
                        variable=var, onvalue="1", offvalue="").pack(side="left")
        if hint:
            tk.Label(row, text=hint, bg=self.theme.card2, fg=self.theme.faint,
                     font=self.fonts["tiny"], anchor="w", justify="left",
                     wraplength=320).pack(fill="x", pady=(2, 0))
        self.vars[key] = var          # 复用 _values() 的收集逻辑
        self._placeholders.pop(key, None)

    def set_result(self, msg: str, bad: bool = False) -> None:
        if self.result_label is None:
            return
        try:
            self.result_label.configure(text=msg, fg=self.theme.bad if bad else self.theme.ok)
        except Exception:
            pass

    def on_open(self) -> None:
        if self.entries:
            # 稍等一下再抢焦点：place 之后立刻 focus_set 在某些 WM 上会被主窗口抢回去
            try:
                self.entries[0].focus_set()
            except Exception:
                pass

    def _submit(self) -> None:
        values = self._values()
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


class _ScrollList:
    """Canvas + Frame 拼出来的可滚动竖排列表。

    Tk 没有现成的"可滚动容器"。面板里的环境列表、代理库、扩展包列表要的是
    同一个东西，所以抽一份出来：滚轮绑定、宽度跟随、清空重建都只实现一次。

    为什么滚轮要递归绑到每个子控件：指针停在一行里的 Label 上时，滚轮事件
    不会冒泡到 Canvas，只绑 Canvas 的话用户会以为"列表滚不动"。
    """

    def __init__(self, parent: tk.Misc, theme: Theme, *, height: int = 140,
                 bg: str | None = None) -> None:
        self.theme = theme
        self.bg = bg or theme.card2
        self.wrap = tk.Frame(parent, bg=self.bg)
        self.canvas = tk.Canvas(self.wrap, bg=self.bg, bd=0, highlightthickness=0,
                                height=height)
        self.vsb = tk.Scrollbar(self.wrap, orient="vertical", command=self.canvas.yview,
                                bd=0, width=8, troughcolor=self.bg, bg=theme.card,
                                activebackground=theme.faint)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=self.bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def pack(self, **kwargs: Any) -> None:
        self.wrap.pack(**kwargs)

    def _on_inner_configure(self, _event: Any = None) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas_configure(self, event: Any) -> None:
        # 内层宽度必须跟随画布：create_window 里的控件宽度默认是 0，
        # 一行行会挤成一列（第一次做可滚动框架必踩的坑）
        try:
            self.canvas.itemconfigure(self._win, width=event.width)
        except Exception:
            pass

    def clear(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()

    def bind_wheel(self, widget: tk.Misc | None = None) -> None:
        target = widget if widget is not None else self.inner
        target.bind("<MouseWheel>", self._on_wheel, add="+")
        target.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-2, "units"), add="+")
        target.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(2, "units"), add="+")
        for child in target.winfo_children():
            self.bind_wheel(child)

    def _on_wheel(self, event: Any) -> None:
        delta = 1 if getattr(event, "delta", 0) > 0 else -1
        try:
            self.canvas.yview_scroll(-delta * 2, "units")
        except Exception:
            pass


class NoticeOverlay(_Overlay):
    """纯提示浮层（替代 `messagebox.showinfo/showwarning`）。

    只用于"必须有回执"的事：比如"环境建好了但代理没设上"。普通的成功/失败
    仍然走事件流 + 状态行 —— 弹窗打断成本很高，别滥用。
    """

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 title: str, message: str, *, danger: bool = False,
                 close_text: str = "知道了") -> None:
        super().__init__(master, theme, fonts, title)
        self.err_var = tk.StringVar(value="")
        tk.Label(self.body, text=message, bg=theme.card2,
                 fg=theme.bad if danger else theme.ink, font=fonts["small"],
                 anchor="w", justify="left", wraplength=330).pack(fill="x")
        ttk.Button(self.buttons, text=close_text, style="TinyAccent.TButton",
                   command=self.close).pack(side="right")


class ChoiceOverlay(_Overlay):
    """从一组条目里挑一个（`multi=True` 时多选）。

    为什么不用 `Listbox`：条目要带副标题（分组、扩展数），Listbox 只能一行
    纯文本；而这里的条目常常是"点一下就开始干活"，用按钮最直接。

    `options` 每项：`{"value": ..., "label": "...", "sub": "...", "checked": bool}`。
    """

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 title: str, subtitle: str, options: list[dict[str, Any]],
                 on_pick: Callable[[list[str]], None], *, multi: bool = False,
                 empty_hint: str = "没有可选项", confirm_text: str = "应用") -> None:
        super().__init__(master, theme, fonts, title, subtitle)
        self.on_pick = on_pick
        self.multi = multi
        self.vars: dict[str, tk.BooleanVar] = {}
        self.err_var = tk.StringVar(value="")
        self.list = _ScrollList(self.body, theme, height=170)
        self.list.pack(fill="both", expand=True)
        if not options:
            tk.Label(self.list.inner, text=empty_hint, bg=theme.card2, fg=theme.faint,
                     font=fonts["tiny"], anchor="w", justify="left",
                     wraplength=300).pack(fill="x", padx=6, pady=8)
        for opt in options:
            value = str(opt.get("value"))
            line = tk.Frame(self.list.inner, bg=theme.card2)
            line.pack(fill="x", pady=(0, 3))
            box = tk.Frame(line, bg=theme.card2, padx=6, pady=3)
            box.pack(side="left", fill="x", expand=True)
            if multi:
                var = tk.BooleanVar(value=bool(opt.get("checked")))
                self.vars[value] = var
                ttk.Checkbutton(box, text="", variable=var,
                                style="Card.TCheckbutton").pack(side="left")
            else:
                ttk.Button(box, text=_clip(str(opt.get("label") or value), 22),
                           style="Tiny.TButton",
                           command=lambda v=value: self._pick(v)).pack(side="left")
            text_box = tk.Frame(box, bg=theme.card2)
            text_box.pack(side="left", padx=(6, 0), fill="x", expand=True)
            if multi:
                tk.Label(text_box, text=_clip(str(opt.get("label") or value), 24),
                         bg=theme.card2, fg=theme.ink, font=fonts["small"],
                         anchor="w").pack(fill="x")
            sub = str(opt.get("sub") or "")
            if sub:
                tk.Label(text_box, text=_clip(sub, 40), bg=theme.card2, fg=theme.faint,
                         font=fonts["tiny"], anchor="w").pack(fill="x")
        self.list.bind_wheel()
        ttk.Button(self.buttons, text="取消", style="Tiny.TButton",
                   command=self.close).pack(side="right")
        if multi:
            ttk.Button(self.buttons, text=confirm_text, style="TinyAccent.TButton",
                       command=self._confirm).pack(side="right", padx=(0, 8))
        self.card.after_idle(self.list._on_inner_configure, None)

    def _pick(self, value: str) -> None:
        self.close()
        try:
            self.on_pick([value])
        except Exception:
            pass

    def _confirm(self) -> None:
        picked = [key for key, var in self.vars.items() if var.get()]
        if not picked:
            self.error("一个都没选")
            return
        self.close()
        try:
            self.on_pick(picked)
        except Exception:
            pass


class CreateProfileOverlay(TextOverlay):
    """新建环境：名称 / 分组 / **可选代理**，并且能先把代理测一遍。

    为什么在这里就放代理：用户拿到代理的第一件事就是"配一个新环境试一下"。
    逼他先建一个不带代理的环境、再去列表里改一遍，等于同一个动作做两次。
    """

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 *, api: CoreApi, on_submit: Callable[[dict[str, str]], bool]) -> None:
        super().__init__(
            master, theme, fonts,
            title="新建环境",
            subtitle=("名称必填，最多 64 个字；分组与代理可以留空。\n"
                      "代理填错不要紧：先点「测试这个代理」看到出口 IP 再创建。"),
            fields=[
                ("name", "名称", ""),
                ("group", "分组（可选）", ""),
                ("proxy", "代理（可选）", "",
                 "socks5://user:pass@1.2.3.4:20000（可留空）"),
            ],
            submit_text="创建",
            on_submit=on_submit,
        )
        self.api = api
        # 「当然也可以选择关闭」：取消勾选就建一个不带插件/数据的干净环境。
        # 用途：临时环境、做指纹对照、排查"是不是某个插件导致的问题"。
        self.add_check_row(
            "with_default_packs", "带上默认扩展包",
            checked=True,
            hint="默认会把「设为默认」的扩展包（插件+数据）一起带上；"
                 "取消勾选则建一个干净的。",
        )
        try:
            # `list_packs()` 返回的 data 是 `{"packs": [...]}`（**不是**列表）。
            # 第一版直接 `[1] or []` 当列表传，`for p in packs` 迭代出字符串
            # `"packs"` → `p.get(...)` AttributeError，被外面的 except 吞成
            # "降级成全带/全不带" —— 用户根本看不到勾选器。
            _ok, _d, _e = self.api.list_packs()
            _packs = (_d or {}).get("packs") if isinstance(_d, dict) else (_d or [])
            self.add_pack_picker(list(_packs or []))
        except Exception as exc:  # noqa: BLE001
            # **不要静默**：拉不到包列表时至少留一条日志，否则"界面没勾选器"
            # 会被当成设计如此，而不是故障。
            try:
                self.on_log(f"读取扩展包列表失败（新建时无法勾选插件）：{exc}", "warn")
            except Exception:
                pass
        self.add_action_row("测试这个代理", self._test)

    def _test(self) -> None:
        raw = (self._values().get("proxy") or "").strip()
        if not raw:
            self.set_result("先把代理串填进上面的输入框", bad=True)
            return
        self.set_result("正在检测出口 …")
        call_async(self.card, lambda: self.api.test_proxy_raw(raw), self._on_test)

    def _on_test(self, res: tuple[bool, Any, dict[str, Any] | None]) -> None:
        if not self.is_open():
            return
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            self.set_result(f"测试失败：[{code}] {msg}", bad=True)
            return
        msg, bad = format_probe(data)
        self.set_result(msg, bad=bad)


class ProxyOverlay(_Overlay):
    """给一个环境改代理：看当前值、粘新的、先测一遍、清除。

    为什么单独一个浮层而不是行内编辑：一行只有三十来像素高，代理串往往
    上百字符（带认证），行内编辑一碰就错；浮层里还能同时摆"当前是什么"、
    "测一遍"和"清除"。

    网络动作由 `ProfilePanel` 执行（它管忙碌标记、事件流、刷新），本类只负责
    显示与收集输入：`mark_saved()` / `mark_failed()` 由面板回调。
    """

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 *, api: CoreApi, profile: dict[str, Any],
                 library: dict[int, dict[str, Any]]) -> None:
        pid = str(profile.get("id") or "")
        super().__init__(master, theme, fonts, "环境代理",
                         f"{profile.get('name') or ''}（{pid}）")
        self.api = api
        self.theme = theme
        self.vars: dict[str, tk.StringVar] = {}
        self.on_save: Callable[[str], None] | None = None
        self.on_clear: Callable[[], None] | None = None
        state = str(profile.get("state") or "stopped")

        tk.Label(self.body, text="当前代理", bg=theme.card2, fg=theme.dim,
                 font=fonts["tiny"], anchor="w").pack(fill="x")
        current = proxy_display_for(profile, library) or "未设置"
        self.current_label = tk.Label(self.body, text=current, bg=theme.card2, fg=theme.ink,
                                      font=fonts["small"], anchor="w", justify="left",
                                      wraplength=320)
        self.current_label.pack(fill="x")
        hint = ("改完必须重启这个环境才生效（代理是浏览器的启动参数，不能热改）。"
                if state in ("running", "starting") else
                "环境当前没在运行，保存后下次启动就走新代理。")
        tk.Label(self.body, text=hint, bg=theme.card2, fg=theme.faint, font=fonts["tiny"],
                 anchor="w", justify="left", wraplength=320).pack(fill="x", pady=(2, 8))

        tk.Label(self.body, text="新代理（粘贴整串，会自动入代理库）", bg=theme.card2,
                 fg=theme.dim, font=fonts["tiny"], anchor="w").pack(fill="x", pady=(0, 2))
        var = tk.StringVar(value="")
        self.vars["proxy"] = var
        self.entry = tk.Entry(self.body, textvariable=var, bg=theme.field, fg=theme.ink,
                              insertbackground=theme.ink, relief="flat", bd=0,
                              highlightthickness=1, highlightbackground=theme.line,
                              highlightcolor=theme.acc, font=fonts["body"])
        self.entry.pack(fill="x", ipady=5)
        self.entry.bind("<Return>", lambda _e: self._save())
        self.entry.bind("<Escape>", lambda _e: self.close())
        self._placeholder = _Placeholder(self.entry, var,
                                        "socks5://user:pass@1.2.3.4:20000", theme)

        row = tk.Frame(self.body, bg=theme.card2)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="测试", style="Tiny.TButton",
                   command=self._test).pack(anchor="w")
        self.result_var = tk.StringVar(value="")
        self.result_label = tk.Label(row, textvariable=self.result_var, bg=theme.card2,
                                     fg=theme.faint, font=fonts["tiny"], anchor="w",
                                     justify="left", wraplength=320)
        self.result_label.pack(fill="x", pady=(3, 0))
        self.error_label = tk.Label(self.body, text="", bg=theme.card2, fg=theme.bad,
                                    font=fonts["tiny"], anchor="w", justify="left",
                                    wraplength=320)
        self.error_label.pack(fill="x", pady=(6, 0))

        ttk.Button(self.buttons, text="取消", style="Tiny.TButton",
                   command=self.close).pack(side="right")
        self.save_btn = ttk.Button(self.buttons, text="保存", style="TinyAccent.TButton",
                                  command=self._save)
        self.save_btn.pack(side="right", padx=(0, 8))
        ttk.Button(self.buttons, text="清除代理", style="Danger.TButton",
                   command=self._clear).pack(side="right", padx=(0, 6))

    # -- 面板回调用 --
    def note(self, msg: str, kind: str = "dim") -> None:
        colors = {"dim": self.theme.faint, "ok": self.theme.ok,
                  "warn": self.theme.warn, "bad": self.theme.bad}
        try:
            self.result_var.set(msg)
            self.result_label.configure(fg=colors.get(kind, self.theme.faint))
        except Exception:
            pass

    def _busy_ui(self, busy: bool) -> None:
        try:
            self.save_btn.state(["disabled"] if busy else ["!disabled"])
        except Exception:
            pass

    def mark_saved(self, display: str, *, restart: bool = False, hint: str = "") -> None:
        """保存成功：更新「当前代理」，清空输入框，并说清楚什么时候生效。"""
        self._busy_ui(False)
        try:
            self.current_label.configure(text=display or "未设置")
        except Exception:
            pass
        self._placeholder.reset()
        msg = ("已保存，重启这个环境后生效" if restart else "已保存，下次启动就走这个代理")
        if hint:
            msg += f"（{hint}）"
        self.note(msg, "warn" if restart else "ok")

    def mark_failed(self, msg: str) -> None:
        self._busy_ui(False)
        self.note(msg, "bad")
        self.error(msg)

    # -- 动作 --
    def _save(self) -> None:
        raw = (self._placeholder.value() or "").strip()
        if not raw:
            self.error("代理串是空的：要清空代理请点「清除代理」")
            return
        self.error("")
        self._busy_ui(True)
        self.note("正在保存 …")
        if self.on_save is not None:
            self.on_save(raw)

    def _clear(self) -> None:
        self.error("")
        self._busy_ui(True)
        self.note("正在清除 …")
        if self.on_clear is not None:
            self.on_clear()

    def _test(self) -> None:
        raw = (self._placeholder.value() or "").strip()
        if not raw:
            self.note("先在输入框里粘一个代理串，再点测试", "bad")
            return
        self.note("正在检测出口 …")
        call_async(self.card, lambda: self.api.test_proxy_raw(raw), self._on_test)

    def _on_test(self, res: tuple[bool, Any, dict[str, Any] | None]) -> None:
        if not self.is_open():
            return
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            self.note(f"测试失败：[{code}] {msg}", "bad")
            return
        msg, bad = format_probe(data)
        self.note(msg, "bad" if bad else "ok")


class ImportProxiesOverlay(_Overlay):
    """批量导入代理：整段粘贴，一行一个。"""

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 on_import: Callable[[str], None]) -> None:
        super().__init__(
            master, theme, fonts, "导入代理",
            "一行一个，直接整段粘进来。支持 socks5://u:p@h:p、http://…、h:p、h:p:u:p；\n"
            "行尾可写 # 备注。重复的会自动跳过，坏行只跳过它自己。",
        )
        self.on_import = on_import
        self.err_var = tk.StringVar(value="")
        box = tk.Frame(self.body, bg=theme.field, highlightthickness=1,
                       highlightbackground=theme.line)
        box.pack(fill="both", expand=True)
        self.text = tk.Text(box, height=9, bg=theme.field, fg=theme.ink, bd=0,
                            highlightthickness=0, insertbackground=theme.ink,
                            font=(fonts.get("mono") or "TkFixedFont", 9), wrap="none")
        vsb = tk.Scrollbar(box, command=self.text.yview, bd=0, width=8,
                           troughcolor=theme.field, bg=theme.card2)
        self.text.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.result_var = tk.StringVar(value="")
        self.result_label = tk.Label(self.body, textvariable=self.result_var, bg=theme.card2,
                                     fg=theme.faint, font=fonts["tiny"], anchor="w",
                                     justify="left", wraplength=320)
        self.result_label.pack(fill="x", pady=(8, 0))
        ttk.Button(self.buttons, text="取消", style="Tiny.TButton",
                   command=self.close).pack(side="right")
        ttk.Button(self.buttons, text="导入", style="TinyAccent.TButton",
                   command=self._submit).pack(side="right", padx=(0, 8))
        self.text.bind("<Escape>", lambda _e: self.close())

    def note(self, msg: str, bad: bool = False) -> None:
        try:
            self.result_var.set(msg)
            self.result_label.configure(fg=self.theme.bad if bad else self.theme.faint)
        except Exception:
            pass

    def on_open(self) -> None:
        try:
            self.text.focus_set()
        except Exception:
            pass

    def _submit(self) -> None:
        text = self.text.get("1.0", "end")
        self.note("正在导入 …")
        self.on_import(text)


class ProxyLibraryOverlay(_Overlay):
    """代理库：全部代理的脱敏串、出口 IP、上次检测时间，可单条测 / 批量测。

    这个浮层自己发请求（只读 + 检测，不改环境状态），面板只负责打开它并
    把结果写进事件流，避免把面板的状态机搅乱。
    """

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 *, api: CoreApi, on_log: Callable[[str, str], None],
                 on_changed: Callable[[], None]) -> None:
        super().__init__(master, theme, fonts, "代理库",
                         "密码只进库、不回显；这里显示的是脱敏串。")
        self.api = api
        self.on_log = on_log
        self.on_changed = on_changed
        self._rows: list[dict[str, Any]] = []
        self._inflight = False
        # 上一次"批量/单条检测"的汇总。要单独存一份：检测完必然跟着刷一次列表，
        # 如果只把它写进状态行，1 秒后就被"共 N 条"覆盖掉了 —— 用户刚好错过。
        self._summary = ""
        self.err_var = tk.StringVar(value="")
        self.list = _ScrollList(self.body, theme, height=200)
        self.list.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value="")
        self.status_label = tk.Label(self.body, textvariable=self.status_var, bg=theme.card2,
                                     fg=theme.faint, font=fonts["tiny"], anchor="w",
                                     justify="left", wraplength=320)
        self.status_label.pack(fill="x", pady=(6, 0))
        ttk.Button(self.buttons, text="关闭", style="Tiny.TButton",
                   command=self.close).pack(side="right")
        ttk.Button(self.buttons, text="批量检测", style="TinyAccent.TButton",
                   command=self._test_many).pack(side="right", padx=(0, 8))
        ttk.Button(self.buttons, text="刷新", style="Tiny.TButton",
                   command=self.reload).pack(side="right", padx=(0, 6))

    def set_status(self, msg: str, bad: bool = False) -> None:
        try:
            self.status_var.set(msg)
            self.status_label.configure(fg=self.theme.bad if bad else self.theme.faint)
        except Exception:
            pass

    # -- 数据 --
    def reload(self) -> None:
        if self._inflight:
            return
        self._inflight = True
        self.set_status("正在读取代理库 …")
        call_async(self.card, self.api.list_proxies, self._on_list)

    def _status_text(self) -> str:
        """状态行 = 上次检测汇总 + 当前库统计（汇总不会被刷新吞掉）。"""
        ok_n = sum(1 for r in self._rows if r.get("last_ok") == 1)
        base = f"共 {len(self._rows)} 条代理 · 最近检测可用 {ok_n} 条"
        return f"{self._summary} · {base}" if self._summary else base

    def _on_list(self, res: tuple[bool, Any, dict[str, Any] | None]) -> None:
        self._inflight = False
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            self.set_status(f"读不到代理库：[{code}] {msg}", bad=True)
            self.on_log(f"代理库读取失败：[{code}] {msg}", "bad")
            return
        self._rows = list((data or {}).get("proxies") or [])
        self.render()
        self.set_status(self._status_text())

    def render(self) -> None:
        theme, fonts = self.theme, self.fonts
        self.list.clear()
        if not self._rows:
            tk.Label(self.list.inner,
                     text="代理库还是空的。用「导入代理」整段粘贴，或在新建环境时填一个。",
                     bg=theme.card2, fg=theme.faint, font=fonts["tiny"], anchor="w",
                     justify="left", wraplength=300).pack(fill="x", padx=8, pady=8)
        for row in self._rows:
            pid = int(row.get("id") or 0)
            card = tk.Frame(self.list.inner, bg=theme.card2)
            card.pack(fill="x", pady=(0, 4))
            line = tk.Frame(card, bg=theme.card2, padx=8, pady=4)
            line.pack(fill="x")
            tk.Label(line, text=_clip(str(row.get("label") or f"#{pid}"), 14), bg=theme.card2,
                     fg=theme.ink, font=fonts["bold"], anchor="w").pack(side="left")
            tk.Label(line, text=_clip(proxy_upstream_of(row), 30), bg=theme.card2,
                     fg=theme.dim, font=fonts["tiny"], anchor="w").pack(side="left", padx=(8, 0))
            ttk.Button(line, text="测", style="Tiny.TButton",
                       command=lambda p=pid: self._test_one(p)).pack(side="right")
            used = [str(x) for x in (row.get("used_by") or [])]
            meta = self._meta_text(row)
            if used:
                meta += " · 被 " + "、".join(used[:2]) + ("…" if len(used) > 2 else "") + " 使用"
            tk.Label(card, text=_clip(meta, 70), bg=theme.card2, fg=theme.faint,
                     font=fonts["tiny"], anchor="w").pack(fill="x", padx=8, pady=(0, 3))
        self.list.bind_wheel()
        self.card.after_idle(self.list._on_inner_configure, None)

    @staticmethod
    def _meta_text(row: dict[str, Any]) -> str:
        ip = str(row.get("last_exit_ip") or "").strip() or "—"
        when = row.get("last_check")
        checked = _fmt_ts(when) if when else "从未检测"
        if row.get("last_ok") == 1:
            state = "可用"
        elif row.get("last_ok") == 0:
            state = "不可用"
        else:
            state = "未检测"
        return f"出口 {ip} · {checked} · {state}"

    # -- 检测 --
    def _test_one(self, proxy_id: int) -> None:
        self.set_status(f"正在检测 #{proxy_id} …")
        call_async(self.card,
                   lambda: self.api.test_proxies([proxy_id]),
                   lambda res: self._after_many(res, [proxy_id]))

    def _test_many(self) -> None:
        ids = [int(r.get("id") or 0) for r in self._rows if r.get("id")]
        if not ids:
            self.set_status("库里还没有代理，先导入", bad=True)
            return
        self.set_status(f"正在检测 {len(ids)} 条代理 …（并发 4，可能几十秒）")
        self.on_log(f"开始批量检测 {len(ids)} 条代理", "info")
        call_async(self.card, lambda: self.api.test_proxies(ids),
                   lambda res: self._after_many(res, ids))

    def _after_many(self, res: tuple[bool, Any, dict[str, Any] | None],
                    ids: list[int]) -> None:
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            self.set_status(f"检测失败：[{code}] {msg}", bad=True)
            self.on_log(f"代理检测失败：[{code}] {msg}", "bad")
            return
        d = data or {}
        total = int(d.get("total") or 0)
        ok_n = int(d.get("ok_count") or 0)
        fail_n = int(d.get("fail_count") or 0)
        summary = f"检测完成：共 {total} 条，可用 {ok_n} 条，不可用 {fail_n} 条"
        self._summary = summary
        self.set_status(summary, bad=bool(fail_n))
        self.on_log(summary, "warn" if fail_n else "ok")
        self.on_changed()
        self.reload()


class PacksOverlay(_Overlay):
    """扩展包管理：列表 / 设为默认 / 从环境导入 / 删除。

    「从环境导入」就是用户要的那件事：把已经装好插件的环境（比如 RXYCDP
    那个浏览器）连数据一起打包；勾上「默认」以后，新建环境会自动带上。

    为什么这里也自己发请求：这些动作只动"包"，不动环境状态机；面板只负责
    打开它、把结果写进事件流，两边不互相踩。
    """

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 *, api: CoreApi, on_log: Callable[[str, str], None],
                 on_changed: Callable[[], None],
                 profiles_provider: Callable[[], list[dict[str, Any]]]) -> None:
        super().__init__(
            master, theme, fonts, "扩展包",
            "三种加扩展的方式：从环境导入 / 从 Chrome 目录导入 / 从商店链接安装。\n"
            "勾了「默认」的包，新建环境时会自动带上。",
        )
        self.api = api
        self.on_log = on_log
        self.on_changed = on_changed
        self.profiles_provider = profiles_provider
        self._packs: list[dict[str, Any]] = []
        self._default_vars: dict[str, tk.BooleanVar] = {}
        self._inflight = False
        self.err_var = tk.StringVar(value="")
        self.list = _ScrollList(self.body, theme, height=200)
        self.list.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value="")
        self.status_label = tk.Label(self.body, textvariable=self.status_var, bg=theme.card2,
                                     fg=theme.faint, font=fonts["tiny"], anchor="w",
                                     justify="left", wraplength=320)
        self.status_label.pack(fill="x", pady=(6, 0))
        ttk.Button(self.buttons, text="关闭", style="Tiny.TButton",
                   command=self.close).pack(side="right")
        ttk.Button(self.buttons, text="从环境导入", style="TinyAccent.TButton",
                   command=self.ask_import_from).pack(side="right", padx=(0, 8))
        # 另一条路：那个浏览器不是知纹起的（Roxy / RXYCDP 等），扩展装在它自己的
        # 用户数据目录里，所以让它给路径。默认值填 Chrome 的标准位置，省得找。
        ttk.Button(self.buttons, text="从 Chrome 目录导入", style="Tiny.TButton",
                   command=self.ask_import_from_chrome).pack(side="right", padx=(0, 6))
        # 用户要的：直接粘商店链接就能装；也能自己拖/选一个 .crx 文件
        ttk.Button(self.buttons, text="商店链接安装", style="Tiny.TButton",
                   command=self.ask_install_from_store).pack(side="right", padx=(0, 6))
        ttk.Button(self.buttons, text="选 .crx 文件", style="Tiny.TButton",
                   command=self.pick_crx_file).pack(side="right", padx=(0, 6))
        # 「下载内核」：插件能不能生效取决于内核能否加载扩展。
        # 官方 Chrome 137 起不加载命令行扩展，所以这一步必须能在客户端里一键做，
        # 不能让用户去翻文档找命令。
        ttk.Button(self.buttons, text="下载内核", style="Tiny.TButton",
                   command=self.ask_install_engine).pack(side="right", padx=(0, 6))
        ttk.Button(self.buttons, text="检查更新", style="Tiny.TButton",
                   command=self.check_updates).pack(side="right", padx=(0, 6))
        ttk.Button(self.buttons, text="刷新", style="Tiny.TButton",
                   command=lambda: self.reload(force=True)).pack(side="right", padx=(0, 6))

    def set_status(self, msg: str, bad: bool = False) -> None:
        try:
            self.status_var.set(msg)
            self.status_label.configure(fg=self.theme.bad if bad else self.theme.faint)
        except Exception:
            pass

    # -- 数据 --
    def reload(self, force: bool = False) -> None:
        if self._inflight and not force:
            return
        self._inflight = True
        self.set_status("正在读取扩展包 …")
        call_async(self.card, self.api.list_packs, self._on_list)

    def _on_list(self, res: tuple[bool, Any, dict[str, Any] | None]) -> None:
        self._inflight = False
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            self.set_status(f"读不到扩展包：[{code}] {msg}", bad=True)
            self.on_log(f"扩展包读取失败：[{code}] {msg}", "bad")
            return
        self._packs = list((data or {}).get("packs") or [])
        self.render()
        defaults = sum(1 for p in self._packs if p.get("is_default"))
        self.set_status(f"共 {len(self._packs)} 个扩展包 · 默认带上 {defaults} 个")

    def render(self) -> None:
        theme, fonts = self.theme, self.fonts
        self.list.clear()
        self._default_vars.clear()
        if not self._packs:
            tk.Label(self.list.inner,
                     text=("还没有扩展包。点「从环境导入」，选一个已经装好插件的环境"
                           "（例如 RXYCDP 那个），就能把插件连数据一起打包。"),
                     bg=theme.card2, fg=theme.faint, font=fonts["tiny"], anchor="w",
                     justify="left", wraplength=300).pack(fill="x", padx=8, pady=8)
        for pack in self._packs:
            self._render_row(pack)
        self.list.bind_wheel()
        self.card.after_idle(self.list._on_inner_configure, None)

    def _render_row(self, pack: dict[str, Any]) -> None:
        theme, fonts = self.theme, self.fonts
        pack_id = str(pack.get("id") or "")
        card = tk.Frame(self.list.inner, bg=theme.card2)
        card.pack(fill="x", pady=(0, 4))
        line = tk.Frame(card, bg=theme.card2, padx=8, pady=4)
        line.pack(fill="x")
        tk.Label(line, text=_clip(str(pack.get("name") or pack_id), 18), bg=theme.card2,
                 fg=theme.ink, font=fonts["bold"], anchor="w").pack(side="left")
        counts = f"{int(pack.get('extension_count') or 0)} 个扩展"
        with_data = int(pack.get("with_data") or 0)
        if with_data:
            counts += f"（{with_data} 个带数据）"
        tk.Label(line, text=f"{counts} · {pack.get('size_human') or '0 B'}",
                 bg=theme.card2, fg=theme.dim, font=fonts["tiny"],
                 anchor="w").pack(side="left", padx=(8, 0))
        ttk.Button(line, text="删除", style="Danger.TButton",
                   command=lambda p=pack: self.ask_delete(p)).pack(side="right")
        ttk.Button(line, text="用在哪些浏览器", style="Tiny.TButton",
                   command=lambda p=pack: self.ask_usage(p)).pack(side="right", padx=(0, 6))
        var = tk.BooleanVar(value=bool(pack.get("is_default")))
        self._default_vars[pack_id] = var
        ttk.Checkbutton(line, text="默认", variable=var, style="Card.TCheckbutton",
                        command=lambda: self._toggle_default(pack_id, bool(var.get()))
                        ).pack(side="right", padx=(0, 8))
        tk.Label(card, text=_clip(self._source_text(pack), 70), bg=theme.card2,
                 fg=theme.faint, font=fonts["tiny"], anchor="w").pack(fill="x", padx=8, pady=(0, 3))

    @staticmethod
    def _source_text(pack: dict[str, Any]) -> str:
        kind = str(pack.get("source") or "").strip()
        prof = str(pack.get("source_profile") or "").strip()
        if kind == "profile":
            return f"来源：环境 {prof or '（未知）'}"
        if kind:
            return f"来源：{kind}"
        return f"来源：{pack.get('path') or '—'}"

    # -- 动作 --
    def _toggle_default(self, pack_id: str, enabled: bool) -> None:
        self.set_status(("正在把包设为默认 …" if enabled else "正在取消默认 …"))
        call_async(self.card, lambda: self.api.set_pack_default(pack_id, enabled),
                   lambda res: self._after_default(res, pack_id, enabled))

    def _after_default(self, res: tuple[bool, Any, dict[str, Any] | None],
                       pack_id: str, enabled: bool) -> None:
        ok, data, err = res
        name = str((data or {}).get("name") or pack_id)
        if ok:
            self.set_status(f"「{name}」{'已设为' if enabled else '已取消'}默认"
                            + ("（新建环境会自动带上）" if enabled else ""))
            self.on_log(f"扩展包「{name}」默认带上={enabled}", "ok")
            self.on_changed()
            self.reload(force=True)
            return
        code = str((err or {}).get("code") or "")
        msg = str((err or {}).get("message") or "")
        self.set_status(f"设置默认失败：[{code}] {msg}", bad=True)
        self.on_log(f"扩展包默认设置失败：{name} [{code}] {msg}", "bad")
        # 勾选框要回到真实状态，否则界面在骗人
        var = self._default_vars.get(pack_id)
        if var is not None:
            try:
                var.set(not enabled)
            except Exception:
                pass

    def ask_import_from(self) -> None:
        profiles = list(self.profiles_provider() or [])
        options = []
        for p in profiles:
            sub_bits = [str(p.get("group_name") or "").strip(),
                        str(p.get("state") or "").strip()]
            options.append({
                "value": str(p.get("id") or p.get("name") or ""),
                "label": str(p.get("name") or ""),
                "sub": " · ".join([b for b in sub_bits if b]),
            })
        ov = ChoiceOverlay(
            self.card, self.theme, self.fonts, "从环境导入扩展包",
            "把选中的环境里的插件（连本地数据）打包成一个扩展包。\n"
            "建议用那个已经装好全部插件的环境（例如 RXYCDP）。",
            options, on_pick=lambda picked: self._import_from(picked[0]),
            empty_hint="这台机器上还没有环境。先建一个、把插件装好，再回来打包。",
        )
        ov.open()

    # -- 从商店链接 / .crx 文件装扩展 ----------------------------------
    def ask_install_from_store(self) -> None:
        """粘 Chrome 应用商店的扩展链接（或 32 位 ID）就能装。

        下载在**这台机器**上做：服务器常常没有外网，也没有你的代理；
        客户机才有。装完默认做成扩展包，勾了"设为默认"以后新建环境自动带。
        """
        ov = TextOverlay(
            self.card, self.theme, self.fonts, "从 Chrome 商店安装扩展",
            "粘商店详情页的链接即可（也可以只填 32 位扩展 ID）。\n"
            "这台机器需要能访问 clients2.google.com；国内网络请先配好代理。",
            fields=[
                ("url", "扩展链接或 ID", "",
                 "https://chromewebstore.google.com/detail/…/hdhngoamekjhmnpenphenpaiindoinpo"),
                ("name", "包名称（可留空）", ""),
            ],
            submit_text="安装",
            on_submit=self._do_install_store,
        )
        ov.open()

    def _do_install_store(self, values: dict[str, str]) -> bool:
        url = str(values.get("url") or "").strip()
        if not url:
            self.set_status("请填写扩展链接或 ID", bad=True)
            return False
        self._start_install(url=url, name=str(values.get("name") or "").strip())
        return True

    def pick_crx_file(self) -> None:
        """手动选一个 .crx / .zip 装进去（等价于"拖一个扩展放上来"）。

        为什么是选择框而不是拖拽：tkinter 原生不支持拖放，
        要做就得再引一个 `tkinterdnd2` 依赖。选文件能达到同样效果，少一个依赖。
        """
        try:
            from tkinter import filedialog
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"打不开文件选择框：{exc}", bad=True)
            return
        path = filedialog.askopenfilename(
            title="选择一个扩展文件",
            filetypes=[("Chrome 扩展", "*.crx *.zip"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self._start_install(url="", path=str(path))

    def _start_install(self, *, url: str, path: str = "", name: str = "") -> None:
        if self._inflight:
            self.set_status("上一个操作还没完成，稍等一下", bad=True)
            return
        self._inflight = True
        self.set_status("正在下载并安装扩展 …（要下载 + 解包，可能要几十秒）")
        self.on_log(f"开始安装扩展（{url or path}）", "info")
        call_async(self.card,
                   lambda: self.api.install_extension(url=url, path=path, name=name,
                                                      make_pack=True, as_default=False),
                   self._on_installed)

    def _on_installed(self, res: tuple[bool, Any, dict[str, Any] | None]) -> None:
        self._inflight = False
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            hint = str((err or {}).get("hint") or "")
            extra = f"（{hint}）" if hint else ""
            self.set_status(f"安装失败：[{code}] {msg}{extra}", bad=True)
            self.on_log(f"安装扩展失败：[{code}] {msg}{extra}", "bad")
            return
        d = data or {}
        msg = f"已装「{d.get('name')}」{d.get('version')}，已建包"
        if d.get("pack_error"):
            msg = f"已装「{d.get('name')}」{d.get('version')}，但自动建包失败：{d['pack_error']}"
            self.set_status(msg, bad=True)
            self.on_log(msg, "warn")
        else:
            self.set_status(msg)
            self.on_log(msg + "。想要新环境自动带上，把这个包勾「默认」", "ok")
        self.on_changed()
        self.reload(force=True)

    def check_updates(self) -> None:
        """回商店比对已装扩展的版本（只查带 ID 标记的）。"""
        if self._inflight:
            self.set_status("上一个操作还没完成，稍等一下", bad=True)
            return
        self._inflight = True
        self.set_status("正在检查扩展更新 …")
        call_async(self.card, lambda: self.api.check_extension_updates(),
                   self._on_updates)

    def _on_updates(self, res: tuple[bool, Any, dict[str, Any] | None]) -> None:
        self._inflight = False
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            self.set_status(f"检查更新失败：[{code}] {msg}", bad=True)
            self.on_log(f"检查扩展更新失败：[{code}] {msg}", "bad")
            return
        d = data or {}
        items = list(d.get("items") or [])
        up = list(d.get("updatable") or [])
        if not items:
            self.set_status("库里没有带商店 ID 的扩展（手工导入的包查不了更新）")
            self.on_log("检查更新：没有可查的扩展", "info")
            return
        if not up:
            self.set_status(f"已检查 {len(items)} 个扩展，都是最新版")
            self.on_log(f"检查更新：{len(items)} 个扩展均为最新", "ok")
            return
        detail = "、".join(f"{u.get('path', '').split(chr(92))[-1]} {u.get('current')}→{u.get('latest')}"
                          for u in up[:4])
        self.set_status(f"{len(up)} 个扩展有新版本：{detail}", bad=True)
        self.on_log(f"可更新：{detail}（重新用链接装一次即可升级）", "warn")

    def ask_install_engine(self) -> None:
        """后台下载 ungoogled-chromium，并在状态行显示进度。

        为什么要做在这里：「扩展包」面板是用户装插件的地方，而"插件到底会不会生效"
        由内核决定。把内核下载放在插件旁边，用户就不必知道两条命令的关系。
        """
        if self._inflight:
            self.set_status("上一个操作还没完成，稍等一下", bad=True)
            return
        try:
            ok_, data, err = self.api.start_engine_install()
        except Exception as exc:  # noqa: BLE001
            self.set_status(f"发起下载失败：{type(exc).__name__}: {exc}", bad=True)
            return
        if not ok_:
            self.set_status(f"发起下载失败：{(err or {}).get('message', '')}", bad=True)
            return
        started = bool((data or {}).get("started"))
        self.set_status("已开始后台下载内核（约 150–200MB），完成后新建环境就会用它"
                        if started else "内核已在下载中，稍等", ok=True)
        self.on_log("开始下载内核（ungoogled-chromium）" if started else "内核已在下装中", "info")

    def ask_import_from_chrome(self) -> None:
        """从任意 Chrome / Roxy 的用户数据目录导入扩展包。

        为什么需要这条：RXYCDP / Roxy 这类浏览器不是知纹启动的，
        它们的扩展不在知纹的 profiles 目录里，而是在标准 Chrome 的
        `User Data/Default/Extensions` 下面。给一个路径就能打包。
        """
        default = _default_chrome_userdata()
        ov = TextOverlay(
            self.card, self.theme, self.fonts, "从 Chrome 目录导入扩展包",
            "填那个浏览器的「用户数据」目录（也可以直接给里面的 Default）。\n"
            "会把这个用户里装的所有扩展连同它们的本地数据一起打包；\n"
            "不含缓存、Cookie、账号数据。",
            fields=[
                ("path", "用户数据目录", default, "例如 C:\\Users\\你\\AppData\\Local\\Google\\Chrome\\User Data"),
                ("name", "包名称（可留空）", ""),
            ],
            submit_text="开始导入",
            on_submit=self._import_from_chrome_path,
        )
        # 「扫描本机」：不知道 RXYCDP / Roxy 装在哪、Profile 叫什么，这是最常见的卡点。
        # web 控制台已有这个按钮（0.7.68），桌面客户端没有 —— 两边不一致，
        # 而用户日常用的是桌面客户端。
        scan_row = tk.Frame(ov.body, bg=self.theme.card2)
        scan_row.pack(fill="x", pady=(8, 0))
        scan_btn = ttk.Button(scan_row, text="扫描本机", style="Tiny.TButton")
        scan_btn.pack(side="left")
        scan_msg = tk.Label(scan_row, text="", bg=self.theme.card2, fg=self.theme.faint,
                            font=self.fonts["tiny"], anchor="w")
        scan_msg.pack(side="left", padx=(8, 0))
        found_box = tk.Frame(ov.body, bg=self.theme.card2)
        found_box.pack(fill="x", pady=(4, 0))

        def _show(res: Any) -> None:
            ok_, data, err = res
            if not ok_:
                scan_msg.configure(text=f"扫描失败：{err.get('message', '')}")
                return
            cands = (data or {}).get("candidates") or []
            if not cands:
                scan_msg.configure(text="没找到带扩展的浏览器配置目录（可手动填路径）")
                return
            scan_msg.configure(text=f"找到 {len(cands)} 个，点一下选它")
            for w in found_box.winfo_children():
                w.destroy()
            for c in cands[:6]:
                row = tk.Frame(found_box, bg=self.theme.card2)
                row.pack(fill="x", pady=1)
                ttk.Button(
                    row, text="选这个", style="Tiny.TButton",
                    command=lambda p_=c["path"]: (
                        ov.vars["path"].set(p_), scan_msg.configure(text="已填入")),
                ).pack(side="left")
                tk.Label(
                    row, text=f"{c.get('browser', '?')} · {c.get('profile', '')} · "
                              f"{c.get('extension_count', 0)} 个扩展",
                    bg=self.theme.card2, fg=self.theme.dim, font=self.fonts["tiny"],
                ).pack(side="left", padx=(6, 0))
                tk.Label(
                    row, text=str(c.get("path", "")), bg=self.theme.card2,
                    fg=self.theme.faint, font=self.fonts["tiny"],
                    anchor="w", justify="left",
                ).pack(side="left", padx=(6, 0), fill="x", expand=True)

        def _scan() -> None:
            scan_btn.state(["disabled"])
            scan_msg.configure(text="正在扫描 …")

            def done(res: Any) -> None:
                try:
                    scan_btn.state(["!disabled"])
                except Exception:
                    pass
                _show(res)

            call_async(ov, lambda: self.api.scan_browsers(), done)

        scan_btn.configure(command=_scan)

        # 「从控制台拉取」：把 hub 里存着的扩展包取回本机。
        #
        # 用户要的是「打包放到控制台，以后建环境默认带上」。hub 侧和 core 侧都
        # 已经能存能拉了，但**桌面客户端没有入口** —— 只有接口等于没有功能。
        # 这一排就是那个入口。
        hub_row = tk.Frame(ov.body, bg=self.theme.card2)
        hub_row.pack(fill="x", pady=(8, 0))
        hub_btn = ttk.Button(hub_row, text="从控制台拉取", style="Tiny.TButton")
        hub_btn.pack(side="left")
        set_def = tk.BooleanVar(value=False)
        tk.Checkbutton(
            hub_row, text="同时设为默认", variable=set_def, bg=self.theme.card2,
            fg=self.theme.dim, font=self.fonts["tiny"], selectcolor=self.theme.card2,
            activebackground=self.theme.card2, activeforeground=self.theme.ink,
            highlightthickness=0, bd=0,
        ).pack(side="left", padx=(8, 0))
        hub_msg = tk.Label(hub_row, text="", bg=self.theme.card2, fg=self.theme.faint,
                           font=self.fonts["tiny"], anchor="w")
        hub_msg.pack(side="left", padx=(8, 0))

        def _pull_done(res: Any) -> None:
            try:
                hub_btn.state(["!disabled"])
            except Exception:
                pass
            ok_, data, err = res
            if not ok_:
                msg = (err or {}).get("message", "")
                # 这两个错误最常见，直接给出下一步该做什么
                if "待" in msg or "pending" in msg.lower():
                    hub_msg.configure(text="这台机器还没被服务器授权，去控制台点「允许」后重试")
                elif "注册" in msg or "enroll" in msg or "DEVICE" in msg:
                    hub_msg.configure(text="这台机器还没在服务器注册，先在客户端连一次服务器")
                else:
                    hub_msg.configure(text=f"拉取失败：{msg or '未知原因'}")
                return
            d = data or {}
            added = d.get("added") or []
            skipped = d.get("skipped") or []
            if added:
                hub_msg.configure(text=f"拉下 {len(added)} 个：{'、'.join(added)}")
            elif skipped:
                hub_msg.configure(text=f"没有新包（{skipped[0]}）")
            else:
                hub_msg.configure(text="控制台里还没有扩展包")
            try:
                self.refresh(force=True)
            except Exception:
                pass

        def _pull() -> None:
            hub_btn.state(["disabled"])
            hub_msg.configure(text="正在从控制台拉取 …")
            call_async(ov, lambda: self.api.pull_hub_packs(as_default=bool(set_def.get())),
                       _pull_done)

        hub_btn.configure(command=_pull)

        # 「上传到控制台」：把本机包推上去，别的机器就能拉。
        # 控制台上传要 zip，而在此之前客户端既没有导出接口也没有按钮 ——
        # 用户手上有包、控制台要 zip，这一步是断的。
        push_btn = ttk.Button(hub_row, text="上传到控制台", style="Tiny.TButton")
        push_btn.pack(side="left", padx=(8, 0))

        def _push_done(res: Any) -> None:
            try:
                push_btn.state(["!disabled"])
            except Exception:
                pass
            ok_, data, err = res
            if not ok_:
                hub_msg.configure(text=f"上传失败：{(err or {}).get('message', '未知原因')}")
                return
            d = data or {}
            pushed = d.get("pushed") or []
            skipped = d.get("skipped") or []
            if pushed:
                hub_msg.configure(text=f"已上传 {len(pushed)} 个：{'、'.join(pushed)}")
            elif skipped:
                hub_msg.configure(text=f"没有上传（{skipped[0]}）")
            else:
                hub_msg.configure(text="本机还没有扩展包")

        def _push() -> None:
            push_btn.state(["disabled"])
            hub_msg.configure(text="正在上传到控制台 …")
            call_async(ov, lambda: self.api.push_hub_packs(), _push_done)

        push_btn.configure(command=_push)
        ov.open()

    def _import_from_chrome_path(self, values: dict[str, str]) -> None:
        path = str(values.get("path") or "").strip()
        if not path:
            self.set_status("请填用户数据目录", bad=True)
            return
        self._do_import(from_chrome=path, name=str(values.get("name") or "").strip(),
                        label="Chrome 目录")

    def _import_from(self, ref: str) -> None:
        if self._inflight:
            self.set_status("上一个操作还没完成，稍等一下", bad=True)
            return
        name = ref
        for p in self.profiles_provider() or []:
            if ref in (str(p.get("id") or ""), str(p.get("name") or "")):
                name = str(p.get("name") or ref)
                break
        self._do_import(from_profile=ref, label=f"环境「{name}」")

    def _do_import(self, *, from_profile: str = "", from_chrome: str = "",
                   path: str = "", name: str = "", label: str = "") -> None:
        """统一的导入入口（环境 / Chrome 目录 / 路径三条来源都走这里）。"""
        if self._inflight:
            self.set_status("上一个操作还没完成，稍等一下", bad=True)
            return
        self._inflight = True
        self.set_status(f"正在从{label or '所选来源'}打包 …（插件多、带数据时要几十秒）")
        self.on_log(f"开始从{label or '所选来源'}导入扩展包", "info")
        call_async(self.card,
                   lambda: self.api.import_pack(from_profile=from_profile,
                                                from_chrome=from_chrome,
                                                path=path, name=name),
                   self._on_imported)

    def _on_imported(self, res: tuple[bool, Any, dict[str, Any] | None]) -> None:
        self._inflight = False
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            hint = str((err or {}).get("hint") or "")
            extra = f"（{hint}）" if hint else ""
            self.set_status(f"导入失败：[{code}] {msg}{extra}", bad=True)
            self.on_log(f"从环境导入扩展包失败：[{code}] {msg}{extra}", "bad")
            return
        d = data or {}
        exts = list(d.get("extensions") or [])
        pack_name = str(d.get("name") or d.get("id") or "扩展包")
        msg = f"已导入「{pack_name}」：{len(exts)} 个扩展，{d.get('size_human') or '—'}"
        self.set_status(msg)
        self.on_log(msg, "ok")
        self.on_changed()
        self.reload(force=True)

    def ask_usage(self, pack: dict[str, Any]) -> None:
        """这个包里的插件被哪些浏览器用着。

        网页控制台有这个入口，桌面端以前没有 —— 用户日常用桌面端，
        想删包时看不到"会影响谁"。
        """
        pack_id = str(pack.get("id") or "")
        name = str(pack.get("name") or pack_id)

        def show(res: tuple[bool, Any, dict[str, Any] | None]) -> None:
            ok, data, err = res
            if not ok:
                self.set_status(f"查询失败：{(err or {}).get('message', '')}", bad=True)
                return
            rows = (data or {}).get("profiles") or []
            if not rows:
                self.set_status(f"「{name}」目前没有任何环境在用")
                self.on_log(f"扩展包「{name}」无环境占用", "info")
                return
            lines = [f"{i+1}. {r.get('name')}{'（运行中）' if r.get('running') else ''}"
                     for i, r in enumerate(rows[:20])]
            body = (f"「{name}」被 {len(rows)} 个环境用着：\n\n" + "\n".join(lines)
                    + (f"\n…等 {len(rows)} 个" if len(rows) > 20 else ""))
            NoticeOverlay(self.card, self.theme, self.fonts,
                          title="用在哪些浏览器", message=body).open()

        call_async(self.card, lambda: self.api.pack_usage(pack_id), show)

    def ask_delete(self, pack: dict[str, Any]) -> None:
        """删包前**先查占用**：把"会影响哪些环境"摆在确认框里。

        ## 为什么（2026-11-20 审查指出）

        以前直接删，用户根本不知道这个包被哪些环境用着。删完正在跑的环境不受影响
        （插件已复制进扩展库），但**以后新建环境就带不上了**，而且没有任何提示。
        """
        pack_id = str(pack.get("id") or "")
        name = str(pack.get("name") or pack_id)

        def open_confirm(users: list[dict[str, Any]]) -> None:
            extra = ""
            if users:
                names = "、".join(str(u.get("name") or u.get("profile_id")) for u in users[:6])
                more = f" 等 {len(users)} 个" if len(users) > 6 else ""
                extra = (f"\n\n还有 {len(users)} 个环境在用：{names}{more}。"
                         "\n正在运行的不受影响，但以后新建环境就带不上这个包了。")
            ov = ConfirmOverlay(
                self.card, self.theme, self.fonts, "删除扩展包",
                (f"确定删除「{name}」吗？\n\n"
                 "已经应用到环境里的扩展不会被删掉（包只是出厂模板），"
                 "但这份包本身会从库里消失。" + extra),
                "仍然删除" if users else "删除",
                on_confirm=lambda: self._delete(pack_id, name, force=bool(users)),
            )
            ov.open()

        def got_usage(res: tuple[bool, Any, dict[str, Any] | None]) -> None:
            ok, data, _err = res
            # 查占用失败也别把删包这条路堵死，只是少了提示
            users = ((data or {}).get("profiles") if ok else None) or []
            open_confirm(list(users))

        call_async(self.card, lambda: self.api.pack_usage(pack_id), got_usage)

    def _delete(self, pack_id: str, name: str, *, force: bool = False) -> None:
        self.set_status(f"正在删除扩展包「{name}」…")
        call_async(self.card, lambda: self.api.delete_pack(pack_id, force=force),
                   lambda res: self._after_delete(res, name))

    def _after_delete(self, res: tuple[bool, Any, dict[str, Any] | None], name: str) -> None:
        ok, data, err = res
        if ok:
            self.set_status(f"已删除扩展包「{name}」")
            self.on_log(f"已删除扩展包「{name}」", "ok")
            self.on_changed()
            self.reload(force=True)
            return
        code = str((err or {}).get("code") or "")
        msg = str((err or {}).get("message") or "")
        self.set_status(f"删除失败：[{code}] {msg}", bad=True)
        self.on_log(f"删除扩展包失败：{name} [{code}] {msg}", "bad")


class PackApplyOverlay(ChoiceOverlay):
    """给某个环境挑要应用的扩展包（多选）。

    多选是有意的：用户经常"一次把这几个包都装上"，单选就得来回点好几次。
    """

    def __init__(self, master: tk.Misc, theme: Theme, fonts: dict[str, Any],
                 *, profile_name: str, packs: list[dict[str, Any]],
                 on_apply: Callable[[list[str]], None]) -> None:
        options = []
        for pack in packs:
            sub_bits = [
                f"{int(pack.get('extension_count') or 0)} 个扩展",
                str(pack.get("size_human") or ""),
            ]
            if pack.get("is_default"):
                sub_bits.append("默认")
            options.append({
                "value": str(pack.get("id") or ""),
                "label": str(pack.get("name") or pack.get("id") or ""),
                "sub": " · ".join([b for b in sub_bits if b]),
                # 默认包预先勾上：那本来就是"新环境该有的样子"
                "checked": bool(pack.get("is_default")),
            })
        super().__init__(
            master, theme, fonts, "应用扩展包",
            f"选要装进「{profile_name}」的包（插件代码 + 本地数据一起装）。\n"
            "环境正在运行时，扩展要等下次启动才加载。",
            options, on_pick=lambda picked: on_apply(picked), multi=True,
            empty_hint="还没有扩展包，先去「扩展包」里从环境导入一份。",
            confirm_text="应用",
        )


# ── 环境面板 ──────────────────────────────────────────────
class ProfilePanel(tk.Frame):
    """本机环境列表：列出 / 新建 / 启动 / 停止 / 删除 / 改代理 / 应用扩展包。

    数据只走 `GET /api/v1/profiles`（外加一次 `GET /api/v1/proxies` 用来把
    `proxy_id` 还原成脱敏地址）；操作结果（成功或失败原因）都会写进
    客户端底部的事件流，并同时显示在自己那行状态文字里 —— 用户不该为了
    知道"为什么没建成"去别处找。
    """

    ROW_LIMIT_NAME = 18
    ROW_LIMIT_GROUP = 8
    ROW_LIMIT_PROXY = ROW_LIMIT_PROXY   # 模块级那一个（见 proxy_upstream_of 上方）

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
        # 代理库（id → 一条代理）。环境行只存 `proxy_id`，地址要按 id 关联出来，
        # 否则界面上永远显示"未设代理"（见 services/proxylib.py 顶部说明）。
        self._library: dict[int, dict[str, Any]] = {}
        self._signature: tuple[Any, ...] | None = None
        self._inflight = False
        self._busy = 0                     # 正在飞的操作数（启停/删除/新建/改代理）
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
        head.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(head, text="本机环境", bg=theme.card, fg=theme.ink, font=fonts["bold"],
                 anchor="w").pack(side="left")
        tk.Label(head, textvariable=self.count_var, bg=theme.card, fg=theme.faint,
                 font=fonts["tiny"], anchor="w").pack(side="left", padx=(8, 0))
        ttk.Button(head, text="新建环境", style="TinyAccent.TButton",
                   command=self.ask_create).pack(side="right")
        ttk.Button(head, text="刷新", style="Tiny.TButton",
                   command=lambda: self.refresh(force=True)).pack(side="right", padx=(0, 6))

        # 第二行放"库"类入口。为什么不跟「新建环境 / 刷新」挤同一行：640 宽的
        # tab 里塞 5 个按钮，pack 会把最后那个压成十几像素 —— 实测「扩展包」
        # 只剩 13px，字整个消失（点也点不准）。另起一行反而更好找。
        bar2 = tk.Frame(self, bg=theme.card)
        bar2.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Button(bar2, text="导入代理", style="Tiny.TButton",
                   command=self.ask_import_proxies).pack(side="left")
        ttk.Button(bar2, text="代理库", style="Tiny.TButton",
                   command=self.ask_proxy_library).pack(side="left", padx=(6, 0))
        ttk.Button(bar2, text="扩展包", style="Tiny.TButton",
                   command=self.ask_packs).pack(side="left", padx=(6, 0))
        tk.Label(bar2, text="扩展包 = 把某个环境已装好的插件连数据打包",
                 bg=theme.card, fg=theme.faint, font=fonts["tiny"],
                 anchor="e").pack(side="right")

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
        """拉一次环境列表（顺带拉代理库，用来把 proxy_id 还原成地址）。

        `force=True` 用于"用户主动点了刷新/刚做完一个操作"，
        这时忽略浮层与忙碌状态（否则点完按钮列表不动，像卡住了）。
        """
        if self._inflight:
            return                                    # 上一次还没回来，别叠加请求
        if not force and (self._typing() or self._busy):
            return                                    # 不打断输入 / 不在操作中途重排
        self._inflight = True
        call_async(self, self._fetch_snapshot, self._on_list)

    def _fetch_snapshot(self) -> tuple[bool, Any, dict[str, Any] | None]:
        """后台线程里一次性取回「环境 + 代理库」。

        为什么合到一个请求里：两者要拼起来才能算出"这一行用的哪个代理"，
        分两次回来会有"环境是新的、代理库是旧的"那一帧，行上的代理串会闪。
        代理库读失败不算致命：环境列表照常显示，只是不显示代理地址。
        """
        ok, data, err = self.api.list_profiles()
        if not ok:
            return ok, data, err
        ok2, data2, _err2 = self.api.list_proxies()
        proxies = list((data2 or {}).get("proxies") or []) if ok2 else []
        return True, {
            "profiles": list((data or {}).get("profiles") or []),
            "proxies": proxies,
        }, None

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
        library: dict[int, dict[str, Any]] = {}
        for row in (data or {}).get("proxies") or []:
            try:
                library[int(row.get("id"))] = dict(row)
            except (TypeError, ValueError):
                continue
        self._profiles = profiles
        self._library = library
        running = sum(1 for p in profiles if str(p.get("state") or "") == "running")
        self.count_var.set(f"{len(profiles)} 个 · 运行中 {running}")
        signature = tuple(
            (str(p.get("id")), str(p.get("name")), str(p.get("state") or ""),
             str(p.get("group_name") or ""), str(p.get("last_error") or ""),
             str(p.get("proxy_id") or ""), str(p.get("proxy") or ""))
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
        # 两行布局。为什么不能挤一行：570px 里要塞"名字 + 徽章 + 分组 + 代理串
        # + 4 个按钮"，pack 会按需压缩，实测「代理」只剩 25px、「应用包」被压成
        # 1px —— 按钮还在，但点不着。信息一行、按钮一行是唯一稳的排法。
        # 注意 Frame 的 padx/pady 只收单个整数（收元组是 TclError：
        # `expected screen distance but got "4 1"`），上下间距交给 pack 给。
        line = tk.Frame(row, bg=theme.card2, padx=9, pady=4)
        line.pack(fill="x")
        acts = tk.Frame(row, bg=theme.card2, padx=9, pady=0)
        acts.pack(fill="x", pady=(0, 5))

        tk.Label(line, text=_clip(name, self.ROW_LIMIT_NAME), bg=theme.card2, fg=theme.ink,
                 font=fonts["bold"], anchor="w").pack(side="left")
        tk.Label(line, text=label, bg=badge_bg, fg=badge_fg, font=fonts["tiny"],
                 padx=5, pady=1).pack(side="left", padx=(7, 0))
        group = str(p.get("group_name") or "").strip()
        if group:
            tk.Label(line, text=_clip(group, self.ROW_LIMIT_GROUP), bg=theme.card2,
                     fg=theme.faint, font=fonts["tiny"], anchor="w").pack(side="left", padx=(7, 0))
        # 代理串直接摆在行上：用户改完代理最爱问的就是"这个环境到底走哪个出口"，
        # 藏在浮层里等于每次都要多点一下。没有就写"未设代理"，别留空白让人猜。
        proxy_text = proxy_display_for(p, self._library)
        tk.Label(line, text=_clip(proxy_text, self.ROW_LIMIT_PROXY) if proxy_text else "未设代理",
                 bg=theme.card2, fg=theme.dim if proxy_text else theme.faint,
                 font=fonts["tiny"], anchor="w").pack(side="left", padx=(7, 0))

        ttk.Button(acts, text="删除", style="Danger.TButton",
                   command=lambda: self.ask_delete(pid, name)).pack(side="right")
        stop_like = state in ("running", "starting")
        ttk.Button(acts, text="停止" if stop_like else "启动", style="Tiny.TButton",
                   command=lambda: self.toggle(pid, name, state)).pack(side="right", padx=(0, 6))
        ttk.Button(acts, text="代理", style="Tiny.TButton",
                   command=lambda: self.ask_proxy(pid, name)).pack(side="right", padx=(0, 6))
        ttk.Button(acts, text="应用包", style="Tiny.TButton",
                   command=lambda: self.ask_apply_packs(pid, name)).pack(side="right", padx=(0, 6))
        ttk.Button(acts, text="管理插件", style="Tiny.TButton",
                   command=lambda: self.ask_manage_plugins(pid, name)).pack(side="right", padx=(0, 6))

    # -- 浮层统一入口 --
    def _open_overlay(self, ov: _Overlay) -> None:
        """打开一个新的自绘浮层（同一时刻只留一个）。

        为什么必须走这一个入口：自动刷新的"别打断用户"判断只看 `self._typing()`
        （= `self._overlay` 是否打开）。任何绕过这里直接 `ov.open()` 的浮层，
        都会让 5 秒 tick 在用户打字时重建控件、把光标踢走。
        """
        old = self._overlay
        if old is not None and old.is_open():
            old.close()
        self._overlay = ov
        ov.open()

    # -- 操作：新建 --
    def ask_create(self) -> None:
        ov = CreateProfileOverlay(self, self.theme, self.fonts, api=self.api,
                                  on_submit=self._do_create)
        self._open_overlay(ov)

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
        proxy_text = (values.get("proxy") or "").strip()
        if not name:
            return fail("名称不能为空")
        if len(name) > 64:
            return fail("名称不要超过 64 个字")
        if self._busy:
            return fail("上一个操作还没完成，稍等一下")
        self._busy += 1
        self.set_status(f"正在创建「{name}」…")
        self.on_log(f"正在创建环境「{name}」", "info")
        # 「当然也可以选择关闭」：勾选框的值是 "1"/""，这里转成布尔
        with_defaults = (values.get("with_default_packs") or "") == "1"
        # 勾了具体包就只带那些（后端 pack_ids 优先于 with_default_packs）
        picked = [x for x in (values.get("pack_ids") or "").split(",") if x.strip()]
        # **只有界面真的提供了勾选器时才传 pack_ids**（空列表=干净环境）；
        # 没勾选器时传 None，保持"按 with_default_packs"的老语义。
        has_picker = "pack_ids" in (values or {})
        call_async(self, lambda: self.api.create_profile(
            name, group, with_default_packs=with_defaults,
            pack_ids=picked if has_picker else None),
            lambda res: self._after_create(res, name, ov, proxy_text))
        return True

    def _after_create(self, res: tuple[bool, Any, dict[str, Any] | None],
                      name: str, ov: Any = None, proxy_text: str = "") -> None:
        ok, data, err = res
        if ok:
            pid = str((data or {}).get("id") or name)
            if proxy_text:
                # 第二步：绑代理。忙碌标记**不在这里减**，等第二步回来再减 ——
                # 否则这两步之间的空隙里，用户能点出第二个启动/删除，
                # 而新建流程还没结束，状态机会乱。
                self.on_log(f"已创建环境「{name}」，正在设置代理 …", "ok")
                self.set_status(f"已创建「{name}」，正在设置代理 …")
                self._apply_new_proxy(pid, name, proxy_text, ov)
                self.on_changed()
                self.refresh(force=True)
                return
            self._busy = max(0, self._busy - 1)
            self.on_log(f"已创建环境「{name}」", "ok")
            self.set_status(f"已创建「{name}」")
            # **插件没带上必须让用户看见**（2026-11-20 审查指出）：
            # 后端 `warnings` 字段早就有，但 GUI 从没读过它 —— 用户在桌面端
            # 建完环境看到的是绿色成功，直到启动才发现插件没带上。
            _warns = list((data or {}).get("warnings") or [])
            for _w in _warns:
                self.on_log(f"「{name}」的插件告警：{_w}", "warn")
            if _warns:
                self.set_status(f"已创建「{name}」，但插件没完全带上（{len(_warns)} 条告警）",
                                bad=True)
            if isinstance(ov, TextOverlay) and ov.is_open():
                ov.close()          # 只有成功了才收起浮层
            if _warns:
                NoticeOverlay(
                    self, self.theme, self.fonts,
                    title="环境已创建，但插件没完全带上",
                    message="\n".join(f"· {w}" for w in _warns)
                            + "\n\n环境本身可以正常用；去「管理插件」里可以补装。",
                ).open()
        else:
            self._busy = max(0, self._busy - 1)
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

    def _apply_new_proxy(self, pid: str, name: str, proxy_text: str, ov: Any) -> None:
        """新建流程的第二步：把代理绑上去。

        为什么失败要单独弹一个提示：环境已经建好了，这一步静默失败的话用户会
        以为"代理填了、也在用"，直到发现出口 IP 不对（可能在花的钱上才看出来）。
        所以宁可打断一次，并把"去哪里补"直接写在提示里。
        """
        self.on_log(f"正在给「{name}」设置代理 {mask_proxy_text(proxy_text)}", "info")
        call_async(self, lambda: self.api.set_profile_proxy(pid, raw=proxy_text),
                   lambda res: self._after_new_proxy(res, pid, name, ov))

    def _after_new_proxy(self, res: tuple[bool, Any, dict[str, Any] | None],
                         pid: str, name: str, ov: Any) -> None:
        self._busy = max(0, self._busy - 1)
        ok, data, err = res
        if ok:
            display = proxy_display_from_assign(data or {}) or "未设置"
            self.on_log(f"已给「{name}」设置代理：{display}", "ok")
            self.set_status(f"已创建「{name}」，代理 {display}")
            if isinstance(ov, TextOverlay) and ov.is_open():
                ov.close()
        else:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            hint = str((err or {}).get("hint") or "")
            extra = f"（{hint}）" if hint else ""
            self.on_log(f"环境「{name}」已建好，但代理没设上：[{code}] {msg}{extra}", "bad")
            self.set_status(f"环境已建好，但代理没设上：[{code}] {msg}", bad=True)
            if isinstance(ov, TextOverlay) and ov.is_open():
                ov.close()
            self._open_overlay(NoticeOverlay(
                self, self.theme, self.fonts,
                title="环境已建好，但代理没设上",
                message=(f"「{name}」已经创建成功。\n\n"
                         f"代理没设上：[{code}] {msg}{extra}\n\n"
                         "可以在环境列表里点这一行的「代理」重新设置。"),
                danger=True,
            ))
        self.on_changed()
        self.refresh(force=True)

    # -- 操作：代理（行上改 / 批量导入 / 代理库）--
    def ask_proxy(self, pid: str, name: str) -> None:
        profile: dict[str, Any] = {}
        for p in self._profiles:
            if str(p.get("id")) == pid:
                profile = p
                break
        if not profile:
            profile = {"id": pid, "name": name}
        ov = ProxyOverlay(self, self.theme, self.fonts, api=self.api,
                          profile=profile, library=self._library)
        idx = str(pid)
        ov.on_save = lambda raw: self._save_proxy(idx, name, raw=raw, ov=ov)
        ov.on_clear = lambda: self._save_proxy(idx, name, raw="", ov=ov)
        self._open_overlay(ov)

    def _save_proxy(self, pid: str, name: str, *, raw: str, ov: ProxyOverlay | None) -> None:
        if self._busy:
            msg = "上一个操作还没完成，稍等一下"
            if ov is not None and ov.is_open():
                ov.mark_failed(msg)
            self.set_status(msg, bad=True)
            self.on_log(f"没给「{name}」改代理：上一个操作还没完成", "warn")
            return
        self._busy += 1
        verb = "清除代理" if not raw else "保存代理"
        self.set_status(f"正在{verb}：{name} …")
        self.on_log(f"正在{verb}：「{name}」", "info")
        call_async(self, lambda: self.api.set_profile_proxy(pid, raw=raw),
                   lambda res: self._after_save_proxy(res, name, raw, ov))

    def _after_save_proxy(self, res: tuple[bool, Any, dict[str, Any] | None],
                          name: str, raw: str, ov: ProxyOverlay | None) -> None:
        self._busy = max(0, self._busy - 1)
        ok, data, err = res
        d = data if isinstance(data, dict) else {}
        if ok:
            cleared = bool(d.get("cleared"))
            display = proxy_display_from_assign(d) or "未设置"
            restart = bool(d.get("restart_required"))
            hint = str(d.get("restart_hint") or "")
            if cleared:
                self.on_log(f"已清除环境「{name}」的代理", "ok")
                self.set_status(f"已清除「{name}」的代理")
            else:
                self.on_log(f"环境「{name}」代理改为 {display}"
                            + ("（正在运行，需要重启后生效）" if restart else ""),
                            "warn" if restart else "ok")
                self.set_status(f"「{name}」代理：{display}"
                                + ("，重启这个环境后生效" if restart else ""),
                                bad=False)
            if ov is not None and ov.is_open():
                ov.mark_saved(display, restart=restart, hint=hint)
        else:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            hint = str((err or {}).get("hint") or "")
            extra = f"（{hint}）" if hint else ""
            self.on_log(f"设置代理失败：{name} [{code}] {msg}{extra}", "bad")
            self.set_status(f"设置代理失败：[{code}] {msg}", bad=True)
            if ov is not None and ov.is_open():
                ov.mark_failed(f"[{code}] {msg}{extra}")
        self.on_changed()
        self.refresh(force=True)

    def ask_import_proxies(self) -> None:
        # 回调要拿到浮层实例本身（用来把结果写在按钮下面），所以先造再绑：
        # 构造函数里那个占位回调只是满足签名，下一行就被替换掉。
        ov = ImportProxiesOverlay(self, self.theme, self.fonts, on_import=lambda _t: None)
        ov.on_import = lambda text: self._do_import_proxies(text, ov)
        self._open_overlay(ov)

    def _do_import_proxies(self, text: str, ov: ImportProxiesOverlay | None) -> None:
        payload = (text or "").strip()
        if not payload:
            if ov is not None and ov.is_open():
                ov.note("粘贴框是空的：一行一个代理，直接整段粘进来", bad=True)
            self.set_status("导入代理：粘贴框是空的", bad=True)
            self.on_log("导入代理没有执行：粘贴框是空的", "warn")
            return
        self._busy += 1
        self.set_status("正在导入代理 …")
        self.on_log("正在批量导入代理 …", "info")
        call_async(self, lambda: self.api.import_proxies(payload),
                   lambda res: self._after_import_proxies(res, ov))

    def _after_import_proxies(self, res: tuple[bool, Any, dict[str, Any] | None],
                              ov: ImportProxiesOverlay | None) -> None:
        self._busy = max(0, self._busy - 1)
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            hint = str((err or {}).get("hint") or "")
            extra = f"（{hint}）" if hint else ""
            text = f"导入失败：[{code}] {msg}{extra}"
            if ov is not None and ov.is_open():
                ov.note(text, bad=True)
            self.set_status(text, bad=True)
            self.on_log(f"批量导入代理失败：[{code}] {msg}{extra}", "bad")
            return
        d = data if isinstance(data, dict) else {}
        imported = int(d.get("imported") or 0)
        skipped = int(d.get("skipped") or 0)
        ignored = int(d.get("ignored") or 0)
        failed = list(d.get("failed") or [])
        summary = f"导入 {imported} 条 / 跳过 {skipped} 条"
        if ignored:
            summary += f" / 忽略空行与注释 {ignored} 行"
        if failed:
            summary += f" / 失败 {len(failed)} 条"
            head = failed[:3]
            detail = "；".join(
                f"{_clip(mask_proxy_text(str(f.get('line') or '')), 30)} → "
                f"{str(f.get('error') or '格式不对')}"
                for f in head
            )
            summary += f"\n失败明细（前 {len(head)} 条）：{detail}"
        if ov is not None and ov.is_open():
            ov.note(summary, bad=bool(failed))
        self.set_status(f"导入代理：{imported} 条成功，{skipped} 条重复跳过"
                        + (f"，{len(failed)} 条失败" if failed else ""),
                        bad=bool(failed and not imported))
        self.on_log(f"批量导入代理：新增 {imported} 条，重复跳过 {skipped} 条，"
                    f"失败 {len(failed)} 条", "warn" if failed else "ok")
        if failed:
            first = failed[0]
            self.on_log(f"导入失败样例：[{str(first.get('error') or '')}] "
                        f"{_clip(mask_proxy_text(str(first.get('line') or '')), 60)}", "bad")
        self.on_changed()
        self.refresh(force=True)

    def ask_proxy_library(self) -> None:
        ov = ProxyLibraryOverlay(self, self.theme, self.fonts, api=self.api,
                                 on_log=self.on_log, on_changed=self.on_changed)
        self._open_overlay(ov)
        ov.reload()

    # -- 操作：扩展包 --
    def ask_packs(self) -> None:
        ov = PacksOverlay(self, self.theme, self.fonts, api=self.api,
                          on_log=self.on_log, on_changed=self.on_changed,
                          profiles_provider=lambda: list(self._profiles))
        self._open_overlay(ov)
        ov.reload()

    def ask_apply_packs(self, pid: str, name: str) -> None:
        """给这个环境应用扩展包：先拉包列表，再让他挑。"""
        self.set_status("正在读取扩展包列表 …")

        def open_picker(res: tuple[bool, Any, dict[str, Any] | None]) -> None:
            ok, data, err = res
            if not ok:
                code = str((err or {}).get("code") or "")
                msg = str((err or {}).get("message") or "")
                self.set_status(f"读不到扩展包：[{code}] {msg}", bad=True)
                self.on_log(f"读取扩展包失败：[{code}] {msg}", "bad")
                return
            packs = list((data or {}).get("packs") or [])
            if not packs:
                self._open_overlay(NoticeOverlay(
                    self, self.theme, self.fonts,
                    title="还没有扩展包",
                    message=("先在「扩展包」里点「从环境导入」，"
                             "选那个已经装好插件的环境（例如 RXYCDP），"
                             "把插件连数据一起打包，再回来应用。"),
                ))
                self.set_status("还没有扩展包，先导入一份")
                return
            ov = PackApplyOverlay(
                self, self.theme, self.fonts, profile_name=name, packs=packs,
                on_apply=lambda ids: self._do_apply_packs(pid, name, ids),
            )
            self._open_overlay(ov)

        call_async(self, self.api.list_packs, open_picker)

    def ask_manage_plugins(self, pid: str, name: str) -> None:
        """看这个环境装了哪些插件，并能逐个摘掉。

        ## 为什么（2026-11-20 用户反馈）

        用户要「每个浏览器的插件」这个视角：不只是"给它装"，还要能看见
        "它现在有哪些、哪些是测试用的、把不需要的去掉"。

        以前只有「应用扩展包」（只加不减）和一个裸文本框（要手输 slug），
        管理起来很别扭。
        """
        def open_it(res: Any) -> None:
            ok, data, err = res
            if not ok:
                self.set_status(f"读取环境失败：{(err or {}).get('message', '')}", bad=True)
                return
            d = data or {}
            exts = [str(x) for x in (d.get("extensions") or [])]
            running = bool(d.get("running"))
            # `_Overlay` 要 5 个参数（master/theme/fonts/title/subtitle），
            # 控件往 `ov.body` 里塞 —— 别再自造 `_make_card`（那函数根本不存在，
            # 第一版调用它，按钮一点就 AttributeError，被 call_async 吞成"没反应"）。
            ov = _Overlay(self, self.theme, self.fonts,
                          f"「{name}」的插件（{len(exts)} 个）")
            card = ov.body
            if not exts:
                tk.Label(card, text="这个环境还没有装插件。点「应用扩展包」给它装。",
                         bg=self.theme.card2, fg=self.theme.faint, font=self.fonts["tiny"],
                         anchor="w", justify="left", wraplength=380).pack(fill="x", pady=(6, 0))
            else:
                box = tk.Frame(card, bg=self.theme.card2)
                box.pack(fill="x", pady=(6, 0))
                for slug in exts:
                    r = tk.Frame(box, bg=self.theme.card2)
                    r.pack(fill="x", pady=1)
                    tk.Label(r, text=slug, bg=self.theme.card2, fg=self.theme.ink,
                             font=self.fonts["tiny"], anchor="w").pack(side="left")
                    ttk.Button(r, text="移除", style="Tiny.TButton",
                               command=lambda s_=slug: self._do_remove_ext(pid, name, s_, ov)
                               ).pack(side="right")
            if running:
                tk.Label(card, text="环境正在运行：改完插件要重启才生效"
                                    "（--load-extension 只在启动时读一次）。",
                         bg=self.theme.card2, fg=self.theme.warn, font=self.fonts["tiny"],
                         anchor="w", justify="left", wraplength=380).pack(fill="x", pady=(6, 0))
            ttk.Button(card, text="关闭", style="Tiny.TButton",
                       command=ov.close).pack(anchor="e", pady=(8, 0))
            ov.open()

        call_async(self, lambda: self.api.get_profile(pid), open_it)

    def _do_remove_ext(self, pid: str, name: str, slug: str, ov: Any) -> None:
        def done(res: Any) -> None:
            ok, data, err = res
            if not ok:
                self.set_status(f"移除失败：{(err or {}).get('message', '')}", bad=True)
                return
            left = (data or {}).get("extensions") or []
            self.on_log(f"已从「{name}」移除 {slug}，剩余 {len(left)} 个", "ok")
            self.set_status(f"已移除 {slug}")
            try:
                ov.close()
            except Exception:
                pass
            self.ask_manage_plugins(pid, name)      # 重开，看到最新列表
            self.refresh(force=True)

        call_async(self, lambda: self.api.remove_extensions(pid, [slug]), done)

    def _do_apply_packs(self, pid: str, name: str, pack_ids: list[str]) -> None:
        if self._busy:
            self.set_status("上一个操作还没完成，稍等一下", bad=True)
            self.on_log(f"没给「{name}」应用扩展包：上一个操作还没完成", "warn")
            return
        self._busy += 1
        self.set_status(f"正在把 {len(pack_ids)} 个扩展包应用到「{name}」…")
        self.on_log(f"正在给环境「{name}」应用扩展包：{pack_ids}", "info")
        call_async(self, lambda: self.api.apply_packs(pid, pack_ids),
                   lambda res: self._after_apply_packs(res, name))

    def _after_apply_packs(self, res: tuple[bool, Any, dict[str, Any] | None],
                           name: str) -> None:
        self._busy = max(0, self._busy - 1)
        ok, data, err = res
        if not ok:
            code = str((err or {}).get("code") or "")
            msg = str((err or {}).get("message") or "")
            hint = str((err or {}).get("hint") or "")
            extra = f"（{hint}）" if hint else ""
            self.on_log(f"应用扩展包失败：{name} [{code}] {msg}{extra}", "bad")
            self.set_status(f"应用扩展包失败：[{code}] {msg}", bad=True)
            self._open_overlay(NoticeOverlay(
                self, self.theme, self.fonts, title="应用扩展包失败",
                message=f"「{name}」[{code}] {msg}{extra}", danger=True,
            ))
            self.refresh(force=True)
            return
        d = data if isinstance(data, dict) else {}
        applied = list(d.get("applied") or [])
        warnings = list(d.get("warnings") or [])
        needs_restart = list(d.get("needs_restart") or [])
        names = "、".join(str(a.get("name") or a.get("slug") or "") for a in applied[:6])
        if len(applied) > 6:
            names += f" 等 {len(applied)} 个"
        if not applied:
            self.on_log(f"扩展包没有写入任何扩展：{name}", "warn")
            self.set_status("扩展包没有写入任何扩展（包里可能是空的）", bad=True)
        else:
            self.on_log(f"已给「{name}」应用 {len(applied)} 个扩展：{names}", "ok")
            self.set_status(f"已给「{name}」应用 {len(applied)} 个扩展")
        lines = []
        if applied:
            lines.append(f"已应用 {len(applied)} 个扩展到「{name}」：\n{names}")
        else:
            lines.append(f"包里没有可用的扩展，「{name}」没有被改动。")
        if needs_restart:
            lines.append("环境正在运行：扩展要到下次启动才会加载。")
        # 内核 ≥137 已移除命令行加载扩展的能力：数据与代码照搬，但插件不会出现。
        # 不在这里说清，用户会以为是自己操作错了（这一条是实测结论，见 docs/16 §五）。
        if d.get("extensions_active") is False:
            lines.append("⚠ " + str(d.get("extensions_note") or
                                      "当前内核不加载外部扩展：代码与数据已带入，但插件不会出现。"))
            self.on_log(f"「{name}」的扩展已带入，但当前内核不会加载它们（≥137 已移除该能力）", "warn")
        if warnings:
            lines.append("注意：" + "；".join(str(w) for w in warnings[:3]))
        self._open_overlay(NoticeOverlay(
            self, self.theme, self.fonts, title="应用扩展包",
            message="\n\n".join(lines), danger=not applied,
        ))
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
        ov = ConfirmOverlay(
            self, self.theme, self.fonts,
            title="删除环境",
            message=(f"确定删除「{name}」吗？\n\n"
                     "它的浏览器数据（cookie、指纹配置、本地存储）会一起被清掉，"
                     "这一步不能撤销。"),
            confirm_text="删除",
            on_confirm=lambda: self._do_delete(pid, name),
        )
        self._open_overlay(ov)

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

"""统一的"不要弹黑框"子进程入口。

## 这个模块解决什么

知纹客户端是 `console=False` 打包出来的 Windows GUI 程序 —— **它自己没有控制台**。
而它运行中要拉起一堆控制台程序：`tasklist`、`taskkill`、`netstat`、
`powershell`、`icacls`，以及远程命令行用的 `cmd.exe` / `/bin/sh`。

Windows 的规则是：一个没有控制台的进程去启动**控制台程序**时，系统会为子进程
**新建**一个控制台窗口。于是每探活一次、每杀一个浏览器，用户桌面上就闪一次黑框。
实测「开环境 → 探活 → 关环境」一轮能闪十几次，用户的原话是
"客户端一运行，桌面上老是跳 CMD 黑框"。

所以：`core/src` 里**任何**起子进程的地方都走这两个函数，不许直接调
`subprocess.run` / `subprocess.Popen`（`tests/test_proc_hidden.py` 里有一条
审计测试盯着这件事，别再手写裸调用）。

## 为什么是两个 flag 一起给（双保险）

1. `creationflags |= CREATE_NO_WINDOW`（`0x08000000`）：
   告诉系统"这个控制台程序不要控制台窗口"。这是最有效的一条。
2. `startupinfo.dwFlags |= STARTF_USESHOWWINDOW` 且 `wShowWindow = SW_HIDE`：
   在 `STARTUPINFO` 里声明"按隐藏状态启动"。

两条都写，是因为生效路径不一样：从打包后的 GUI 主程序**直接**拉子进程时，
`CREATE_NO_WINDOW` 稳定生效；而经由中间层（`cmd.exe` 再往下、某些终端会话、
部分 Windows 版本）时，最后兜住的常常是 `STARTUPINFO`。只写一条，用户在某些
机器上仍然能看到闪窗 —— 这个坑现场复现过，所以别"简化"成一条。

## 合并规则

`creationflags` 用**位或**合并，绝不覆盖。调用方可能已经传了
`CREATE_NEW_PROCESS_GROUP`（`0x00000200`）——它和 `CREATE_NO_WINDOW`
是**不同的位**，两个都必须留着。覆盖掉进程组那一位，`shell.py` 里
"Ctrl+C / taskkill /T 杀整棵进程树"的行为就变了（`CREATE_NEW_PROCESS_GROUP`
是 shell 侧唯一的进程组锚点）。

调用方自己传了 `startupinfo` 时同理：**原地补位**，保留它已有的字段。

## 哪些调用点不能用（或要特殊处理）

- **自己是 GUI 程序、要显示自己窗口的子进程**：传 `gui_child=True`。
  典型例子是 `engines.spawn()` 拉起的 Chrome / Chromium。它仍然加
  `CREATE_NO_WINDOW`（对 GUI 子系统程序这个位会被系统忽略，无副作用），
  但**不设** `SW_HIDE` —— `wShowWindow` 会被 GUI 程序的 `WinMain`
  当作 `nCmdShow`，Chrome 会按它决定首个窗口怎么显示，设成 `SW_HIDE`
  等于"启动了一个看不见的浏览器"，那比闪黑框更糟。
- **就是要给用户看的控制台窗口**（比如将来做"开一个命令窗口让用户自己敲"）：
  `CREATE_NEW_CONSOLE` 与 `CREATE_NO_WINDOW` 互斥，那种场景不能走这里。
  目前仓库里没有这种调用点。
- **非 Windows 平台**：不需要（也没有）这个概念。这两个函数在 POSIX 上
  一个参数都不加、一个参数都不改，纯粹是透传 —— 这样同一份调用代码
  在两个平台上是同一个形状，不会出现"Windows 改了、Linux 忘了"的分叉。
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

# Windows 创建标志。这里写数值而不是 `subprocess.CREATE_NO_WINDOW`：
# 那些常量**只在 Windows 版 Python 里存在**，Linux/macOS 上 import 期就会
# AttributeError。而这些代码要在 Linux CI 上跑（Windows 分支由测试
# monkeypatch `sys.platform` 覆盖），所以必须自己持有数值。
CREATE_NO_WINDOW = 0x08000000
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0


class _StartupInfoShim:
    """`subprocess.STARTUPINFO` 的替身（非 Windows 平台用）。

    真 Windows 上永远走 `subprocess.STARTUPINFO`；Linux 上根本没有这个类，
    但 Windows 分支必须能在 Linux CI 里被测到 —— 否则"flag 到底加没加上"
    这件事永远没有证据，下次谁把 `creationflags` 删了也没人发现。
    替身只需要同名的两个字段。
    """

    __slots__ = ("dwFlags", "wShowWindow")

    def __init__(self) -> None:
        self.dwFlags = 0
        self.wShowWindow = 0


def _is_windows() -> bool:
    """每次调用都现读，不缓存成模块常量。

    测试要能 monkeypatch `sys.platform` 把 Windows 分支在 Linux 上跑一遍；
    缓存成 `_IS_WIN = sys.platform == "win32"` 就再也测不动了。
    """
    return sys.platform == "win32"


def _new_startupinfo() -> Any:
    cls = getattr(subprocess, "STARTUPINFO", None) or _StartupInfoShim
    return cls()


def _hidden(kwargs: dict[str, Any], gui_child: bool) -> dict[str, Any]:
    """把调用方给的 kwargs 补成"不弹窗"版本，其余参数原样保留。"""
    out = dict(kwargs)
    if not _is_windows():
        # POSIX 没有控制台窗口这回事：不塞 creationflags、不塞 startupinfo，
        # start_new_session 之类调用方自己给的参数也一并原样带回。
        return out

    # 位或，不是赋值：调用方可能已经给了 CREATE_NEW_PROCESS_GROUP 或别的标志，
    # 覆盖会丢掉进程组语义（见模块注释）。
    out["creationflags"] = int(out.get("creationflags") or 0) | CREATE_NO_WINDOW

    if not gui_child:
        # 调用方自己传了 startupinfo 就原地补位，别把它已有的字段扔掉。
        si = out.get("startupinfo") or _new_startupinfo()
        si.dwFlags = int(getattr(si, "dwFlags", 0) or 0) | STARTF_USESHOWWINDOW
        si.wShowWindow = SW_HIDE
        out["startupinfo"] = si
    return out


def run_hidden(
    argv: Any,
    *,
    gui_child: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """跑一个子进程并等它结束，Windows 上不弹控制台窗口。

    `argv` 与 `**kwargs` 全部透传给 `subprocess.run`：`stdin` / `stdout` /
    `stderr` / `timeout` / `text` / `encoding` / `errors` / `env` / `cwd` /
    `check` / `input` 都能照常用，行为与直接调 `subprocess.run` 一致
    （包括 `check=True` 抛 `CalledProcessError`、超时抛 `TimeoutExpired`）。

    `gui_child=True` 见模块注释：子进程自己是要显示窗口的 GUI 程序时用。
    """
    return subprocess.run(argv, **_hidden(kwargs, gui_child))


def popen_hidden(
    argv: Any,
    *,
    gui_child: bool = False,
    **kwargs: Any,
) -> subprocess.Popen:
    """启动子进程**不等待**，Windows 上不弹控制台窗口。

    参数透传规则与 `run_hidden` 相同。加 `CREATE_NO_WINDOW` 不影响
    `stdin=PIPE` / `stdout=PIPE` 的读写 —— 隐藏的是窗口，不是管道，
    所以远程命令行那种"起 cmd.exe 再把输出读回来"的用法照旧。

    `shell.py` 那种"要控制进程组、稍后 taskkill /T"的调用点也照常工作：
    调用方传的 `CREATE_NEW_PROCESS_GROUP` 会被位或保留。
    """
    return subprocess.Popen(argv, **_hidden(kwargs, gui_child))

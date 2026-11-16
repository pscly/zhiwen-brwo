"""本地命令执行器（远程命令行的设备侧）。

## 这个模块负责什么

1. **真的执行一条命令**，拿回 rc / stdout / stderr / 耗时，带超时与输出上限。
2. **审计**：每次执行都留痕，但日志里只记命令与结果码，不记输出内容
   （输出里很可能有密码、token）。
3. 不做鉴权、不做网络 —— 那两层分别在 `api.py`（本地接口）与
   `services/shellagent.py`（远程拉取）里。

## 安全上的几个刻意的决定

- **杀进程用进程组**：`subprocess` 直接 kill 只杀父进程，
  `ping -t`、`tail -f` 这类子进程会活下来继续占端口。所以
  POSIX 用 `start_new_session=True` + `killpg`，
  Windows 用 `CREATE_NEW_PROCESS_GROUP` + `taskkill /T /F`。
- **输出按字节截断**，并且**按 UTF-8 安全边界切**：直接 `[:N]` 会把
  一个汉字切成半个，前端显示成乱码（这个坑很好踩）。
- **stdout/stderr 分开收集**：合并会让错误信息淹没在正常输出里，
  面板要分色显示。
- **工作目录默认不落到系统根**：`ZW_SHELL_CWD` 为空时用用户主目录，
  而不是 `C:\\` 或 `/` —— 在根目录跑 `rm`/`del` 太容易出事。

## 为什么不支持交互式命令

`vim`、`python`（无参数）、`ssh` 这类需要 TTY。远程执行拿不到终端，
它们会立刻退出或挂住。这是设计取舍，不是缺陷；需要交互就用 SSH 或
Windows 远程桌面。`zw shell` 会提示这一点。
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("zw.shell")

# 单条命令的硬上限：再怎么配也不让它无限长
_HARD_MAX_TIMEOUT = 600
_HARD_MAX_OUTPUT = 4 * 1024 * 1024      # 4MB，超过就截断


class ShellError(Exception):
    """执行相关的可预期错误。"""

    def __init__(self, code: str, message: str, status: int = 400, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint


@dataclass
class ExecResult:
    """一次执行的结果。字段与 hub 的 job 形状对齐，别随意改名。"""

    command: str
    rc: int = -1
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    truncated: bool = False
    error: str = ""
    cwd: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_cwd() -> str:
    """默认工作目录。空配置时用用户主目录，不落到系统根。"""
    from zw_core.config import get_settings

    s = get_settings()
    configured = (getattr(s, "shell_cwd", "") or "").strip()
    if configured:
        return configured
    return str(Path.home())


def _decode(data: bytes) -> str:
    """把子进程输出解成文本。

    Windows 上原生命令（`ipconfig`、`chcp` 等）输出的是 GBK/CP936，
    而管道里又没有编码信息。策略：
    先按 UTF-8 严格解，失败再按系统首选编码解，最后兜 latin-1 保证不抛。
    """
    if not data:
        return ""
    preferred = ""
    try:
        import locale

        preferred = locale.getpreferredencoding(False) or ""
    except Exception:
        preferred = ""
    for enc in ("utf-8", "gbk", "cp1252", preferred or "utf-8"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """按字节上限截断，且不切断 UTF-8 字符。返回 (文本, 是否截断)。"""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text, False
    cut = raw[:limit]
    # 回退到最后一个完整字符的边界（UTF-8 续字节是 10xxxxxx）
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    # 如果最后剩下的是一个被截断的多字节首字节，也去掉
    if cut:
        try:
            cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return cut.decode("utf-8", errors="replace"), True


def _kill_tree(proc: subprocess.Popen) -> None:
    """连同子孙进程一起杀掉。只 kill 父进程会留下孤儿占着端口/文件。"""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _build_argv(command: str) -> list[str]:
    """把命令交给系统 shell 执行。

    用 `cmd /c` 与 `/bin/sh -c` 而不是 `shell=True`：
    后者在某些平台上会多包一层，且不利于我们自己控制进程组。
    """
    if sys.platform == "win32":
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/s", "/c", command]
    shell = "/bin/sh"
    if not Path(shell).exists():
        shell = shutil.which("sh") or "/bin/bash"
    return [shell, "-c", command]


def execute(
    command: str,
    cwd: str = "",
    timeout: int | float | None = None,
    max_output: int | None = None,
    env_extra: dict[str, str] | None = None,
) -> ExecResult:
    """执行一条命令，永不抛异常（失败都进 `result.error`）。

    为什么永不抛：调用方有三个（本地 API、远程代理、CLI），
    每个都要 try/except 一遍太容易漏；统一在这里收口，
    返回值里带 `error` 让上层决定怎么呈现。
    """
    from zw_core.config import get_settings

    s = get_settings()
    limit_t = int(getattr(s, "shell_timeout", 60) or 60)
    limit_o = int(getattr(s, "shell_max_output", 65536) or 65536)

    try:
        tmo = float(timeout if timeout is not None else limit_t)
    except (TypeError, ValueError):
        tmo = float(limit_t)
    tmo = max(1.0, min(tmo, float(_HARD_MAX_TIMEOUT)))

    cap = int(max_output if max_output is not None else limit_o)
    cap = max(256, min(cap, _HARD_MAX_OUTPUT))

    work = (cwd or "").strip() or default_cwd()
    result = ExecResult(command=command, cwd=work)

    if not command or not command.strip():
        result.error = "EMPTY_COMMAND"
        return result
    if len(command) > 4000:
        result.error = "COMMAND_TOO_LONG"
        return result
    if not Path(work).exists() or not Path(work).is_dir():
        result.error = f"CWD_NOT_FOUND: {work}"
        return result

    env = dict(os.environ)
    # 让子进程知道自己在被谁调用（脚本可以据此改变行为，例如少用分页器）
    env.setdefault("ZW_SHELL", "1")
    env.setdefault("ZW_DEVICE_ID", os.environ.get("ZW_DEVICE_ID", ""))
    env.setdefault("PYTHONUNBUFFERED", "1")
    # 关掉分页器与彩色输出，否则 `git log` 之类会卡在 less 里
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    env["TERM"] = "dumb"
    if env_extra:
        env.update({k: str(v) for k, v in env_extra.items()})

    argv = _build_argv(command)
    kwargs: dict[str, Any] = {
        "cwd": work,
        "env": env,
        "stdin": subprocess.DEVNULL,   # 交互式命令立刻拿到 EOF，不会挂住
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True

    started = time.time()
    result.started_at = started
    logger.info("执行命令 device=%s cwd=%s cmd=%r", env.get("ZW_DEVICE_ID", ""), work, command[:200])

    proc: subprocess.Popen | None = None
    try:
        proc = subprocess.Popen(argv, **kwargs)  # noqa: S603  这是本模块的核心职责
        try:
            out_b, err_b = proc.communicate(timeout=tmo)
            result.rc = proc.returncode if proc.returncode is not None else -1
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                out_b, err_b = proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, ValueError):
                out_b, err_b = b"", b""
            result.rc = -1
            result.error = f"TIMEOUT: 超过 {int(tmo)} 秒被终止"
    except FileNotFoundError as exc:
        result.error = f"SHELL_NOT_FOUND: {exc}"
        out_b, err_b = b"", b""
    except OSError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        out_b, err_b = b"", b""
    finally:
        result.finished_at = time.time()
        result.duration_ms = int((result.finished_at - started) * 1000)

    out_text = _decode(out_b or b"")
    err_text = _decode(err_b or b"")
    out_text, t1 = _clip(out_text, cap)
    err_text, t2 = _clip(err_text, cap)
    result.stdout = out_text
    result.stderr = err_text
    result.truncated = bool(t1 or t2)

    # 审计：只记命令与结果，**不记输出**（输出里可能有凭据）
    logger.info(
        "命令结束 cmd=%r rc=%s %dms truncated=%s error=%s",
        command[:200], result.rc, result.duration_ms, result.truncated, result.error or "-",
    )
    return result


def shell_hint() -> str:
    """给用户看的一句话说明当前平台的 shell。"""
    if sys.platform == "win32":
        return os.environ.get("COMSPEC") or "cmd.exe"
    return "/bin/sh"

"""持久命令行会话（实时终端的引擎）。

## 为什么不能继续用"一条命令一次 subprocess"

`services/shell.py` 的执行模型是「起进程 → 等它结束 → 一次性拿输出」。
对"跑一条 `ipconfig` 看结果"够用，但对实时终端不行：

- 用户看不到过程（`ping -n 10` 要等 10 秒才出字，`gradle build` 要等几分钟）
- AI 无法先看一半输出再决定下一步
- `cd`、`export`、`set` 都不保持（每条命令一个新进程）
- 长驻程序（`python`、`node`、日志 `tail`）根本跑不了

所以这里做**持久会话**：一个 shell 进程从头活到尾，命令往里写，
输出边产生边推给调用方。

## 怎么知道一条命令跑完了

管道拿不到 TTY，所以没有提示符可等。做法是**在命令后追加一个唯一标记**，
标记里带上退出码与当前目录：

    <用户命令> ; printf '\\n__ZW<tok>_%s__%s__\\n' "$?" "$PWD"      # POSIX
    <用户命令> & echo __ZW<tok>_!errorlevel!__!CD!__                # Windows(cmd /v:on)

读到标记就说明这条命令结束了，同时拿到 `rc` 与 `cwd`（所以 `cd` 能保持）。
标记用每次会话随机的 token，避免用户输出里恰好出现同样字符串。

Windows 上必须开 `cmd /v:on`（延迟展开）—— 否则 `%errorlevel%` 会在
**整行解析时**就被替换成上一条命令的码，拿到的永远是错的值。这是 cmd 的经典坑。

## 交互支持

`write()` 直接把字节送进 stdin，所以运行中的程序（`python`、`node`、`mysql`）
能接收输入。但要接受一个限制：**这不是真正的 PTY**。
`vim`、`htop`、`top` 这类需要终端控制的程序不会正常工作（它们要么立刻退出，
要么输出一堆控制字符）。这是刻意的取舍：真实 PTY 在 Windows 上要 ConPTY、
在 POSIX 上要 pty 模块，两边行为还不一致，代价远大于收益 ——
需要全屏交互时用 SSH 或远程桌面。

## 编码

Windows 上会话启动时先 `chcp 65001`，之后整个会话的字节流都是 UTF-8，
所以解码只需要一个增量 UTF-8 解码器（能正确处理"一个汉字被切在两个读块之间"）。
不这么做的话，英文版 Windows 的 cp437 会把中文变成 `?`，且不可逆。

## 输出上限

单条命令的输出按 `max_output` 截断（只影响"保留历史"，**不影响实时流**：
流是边收边发的）。这样做是为了防止 `yes` 之类把内存吃满后拖垮整个客户端。
"""

from __future__ import annotations

import codecs
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from .proc import popen_hidden

# 单条命令输出保留上限（字节）。超出后继续流式转发，但不再累积。
_HARD_MAX_OUTPUT = 4 * 1024 * 1024
# 会话闲置多久自动回收（秒）。设备侧代理会定期清理。
IDLE_TTL = 1800.0


class SessionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _shell_argv() -> list[str]:
    """会话用的 shell。"""
    if sys.platform == "win32":
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        # /v:on 延迟展开（拿正确 errorlevel）；/q 关回显（不然命令会被原样打出来）
        return [comspec, "/v:on", "/q", "/k"]
    shell = "/bin/sh"
    if not os.path.exists(shell):
        shell = "/bin/bash" if os.path.exists("/bin/bash") else "sh"
    # 不要 -i：交互模式会打提示符、还会因为拿不到 TTY 而报警告，污染输出
    return [shell]


class ShellSession:
    """一个持久 shell 会话。

    回调（都在读线程里执行，调用方自己负责切回自己的线程/事件循环）：
      on_output(data: str)                  —— 有新的输出
      on_done(rc: int, cwd: str, *, timed_out: bool = False) —— 一条命令结束
      on_exit(rc: int)                      —— 会话进程退出
    """

    def __init__(
        self,
        sid: str,
        *,
        cwd: str = "",
        timeout: float = 0.0,
        max_output: int = 65536,
        on_output: Callable[[str], None] | None = None,
        on_done: Callable[..., None] | None = None,
        on_exit: Callable[[int], None] | None = None,
    ) -> None:
        self.sid = sid
        self.cwd = (cwd or "").strip() or str(_home())
        self.default_timeout = float(timeout or 0)
        self.max_output = max(1024, min(int(max_output or 65536), _HARD_MAX_OUTPUT))
        self.on_output = on_output or (lambda _d: None)
        self.on_done = on_done or (lambda *a, **k: None)
        self.on_exit = on_exit or (lambda _rc: None)

        self.token = secrets.token_hex(6)
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = ""                  # 未消费的已解码文本（用于找标记）
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._busy = False              # 有命令在跑
        self._started_at = 0.0
        self._last_active = time.time()
        self._exited = False
        self._lines: list[str] = []     # 当前命令已积累的输出（供非流式调用方取用）
        self._kept = 0
        self.truncated = False
        self._timer: threading.Timer | None = None
        self._pending_timeout = 0.0
        self.last_rc: int | None = None
        self.commands = 0

    # ── 生命周期 ────────────────────────────────────────
    def start(self) -> None:
        # 上一次的 shell 退出了（用户敲了 exit、或崩了）：先收尾再重开一个新的。
        # 不这样做的话，终端会永久卡在"已断开"，只能关掉重连。
        if self._proc is not None:
            if self._proc.poll() is None and not self._exited:
                return
            self._cleanup_proc()
        self._exited = False
        if not os.path.isdir(self.cwd):
            self.cwd = str(_home())
        argv = _shell_argv()
        kwargs: dict[str, Any] = {
            "cwd": self.cwd,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,   # 合并：终端里本来就该看到同一条流
            "bufsize": 0,
            "env": self._env(),
        }
        if sys.platform == "win32":
            # 独立进程组：关闭会话时能整棵树杀掉（cmd 会派生子进程）
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            self._proc = popen_hidden(argv, **kwargs)
        except OSError as exc:
            raise SessionError("SHELL_SPAWN_FAILED", f"起不了 shell：{exc}") from exc
        self._started_at = time.time()
        self._reader = threading.Thread(target=self._read_loop, name=f"zw-sess-{self.sid}", daemon=True)
        self._reader.start()
        if sys.platform == "win32":
            # 整个会话统一用 UTF-8，否则英文版 Windows 的 cp437 会把中文变成 ?
            self._raw_write(b"chcp 65001>nul\r\n")

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["ZW_SESSION"] = self.sid
        # 关掉分页器与彩色：不然 `git log` 会卡在 less 里等按键，
        # `ls --color` 会往管道里塞 ANSI 转义（前端得自己剥）
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"
        env["TERM"] = "dumb"
        env["NO_COLOR"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def close(self) -> None:
        if self._exited and self._proc is None:
            return
        self._exited = True
        self._cancel_timer()
        self._cleanup_proc()
        self.on_exit(-1)

    def _cleanup_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                # exit 让 shell 自己走；对 POSIX 与 cmd 都有效
                proc.stdin.write(b"exit\r\n" if sys.platform == "win32" else b"exit\n")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream and not stream.closed:
                    stream.close()
            except (OSError, ValueError):
                pass

    # ── 状态 ────────────────────────────────────────────
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._exited

    def status(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "cwd": self.cwd,
            "alive": self.alive(),
            "busy": self._busy,
            "commands": self.commands,
            "last_rc": self.last_rc,
            "started_at": self._started_at,
            "idle_seconds": int(time.time() - self._last_active),
            "truncated": self.truncated,
        }

    # ── 执行 ────────────────────────────────────────────
    def run(self, command: str, timeout: float | None = None) -> None:
        """提交一条命令。**立即返回**，输出与结束都走回调。"""
        text = (command or "").strip()
        if not text:
            raise SessionError("EMPTY_COMMAND", "命令不能为空")
        if len(text) > 4000:
            raise SessionError("COMMAND_TOO_LONG", "命令太长（上限 4000 字符）")
        if not self.alive():
            self.start()
        with self._lock:
            self._busy = True
            self._lines = []
            self._buf = ""
            self._kept = 0
            self.truncated = False
            self.commands += 1
        self._arm_timeout(timeout)
        self._raw_write(self._wrap(text))

    def write(self, data: str) -> None:
        """把原始输入送进 stdin（给运行中的交互式程序用）。"""
        if not data:
            return
        if not self.alive():
            raise SessionError("SESSION_DEAD", "会话已结束")
        self._last_active = time.time()
        self._raw_write(data)

    def interrupt(self) -> None:
        """发 Ctrl+C。"""
        if self.alive():
            self._raw_write("\x03")

    def _wrap(self, command: str) -> bytes:
        tok = self.token
        if sys.platform == "win32":
            # !errorlevel! 需要 /v:on；!CD! 同理。用 & 而不是 && ：前一条失败也要打标记
            line = f"{command} & echo __ZW{tok}_!errorlevel!__!CD!__\r\n"
        else:
            # 用分号而不是 &&：失败也要打标记，否则界面上永远卡在"执行中"
            line = f"{command}; printf '__ZW{tok}_%s__%s__\\n' \"$?\" \"$PWD\"\n"
        return line.encode("utf-8", errors="replace")

    def _raw_write(self, data: bytes | str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.closed:
            raise SessionError("SESSION_DEAD", "会话已结束")
        raw = data.encode("utf-8") if isinstance(data, str) else data
        with self._write_lock:
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise SessionError("SESSION_DEAD", f"写入失败：{exc}") from exc
        self._last_active = time.time()

    # ── 超时 ────────────────────────────────────────────
    def _arm_timeout(self, timeout: float | None) -> None:
        self._cancel_timer()
        tmo = float(timeout if timeout is not None else self.default_timeout or 0)
        if tmo <= 0:
            return
        self._pending_timeout = min(tmo, 3600.0)
        timer = threading.Timer(self._pending_timeout + 1.0, self._on_timeout)
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _cancel_timer(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.cancel()

    def _on_timeout(self) -> None:
        if not self._busy:
            return
        # 先发 Ctrl+C 让它有机会收尾；再下一轮由调用方决定是否强杀
        try:
            self.interrupt()
        except SessionError:
            pass
        self.on_output(f"\n[超时] 命令超过 {int(self._pending_timeout)} 秒，已发送中断\n")

    # ── 读线程 ──────────────────────────────────────────
    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        while True:
            try:
                chunk = stream.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            text = self._decoder.decode(chunk)
            if text:
                self._consume(text)
        tail = self._decoder.decode(b"", final=True)
        if tail:
            self._consume(tail)
        # 进程结束：把残留缓冲当作输出吐出去，然后通知
        if self._buf:
            leftover, self._buf = self._buf, ""
            if leftover:
                self.on_output(leftover)
        rc = -1
        try:
            rc = proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            rc = -1
        was_busy = self._busy
        self._busy = False
        self._cancel_timer()
        self._exited = True
        if was_busy:
            self.last_rc = rc
            self.on_done(rc, self.cwd, timed_out=False)
        self.on_exit(rc)

    def _consume(self, text: str) -> None:
        """把新解码出的文本切分成"输出"与"结束标记"。"""
        self._buf += text
        marker = f"__ZW{self.token}_"
        while True:
            idx = self._buf.find(marker)
            if idx < 0:
                # 没有标记：整段都是输出。但要保留可能被切断的标记前缀，
                # 否则一个标记被切成两次读块就永远认不出来。
                keep = _partial_suffix(self._buf, marker)
                if keep:
                    emit, self._buf = self._buf[: len(self._buf) - keep], self._buf[len(self._buf) - keep :]
                else:
                    emit, self._buf = self._buf, ""
                if emit:
                    self._emit(emit)
                return
            # 标记前的部分是输出
            emit = self._buf[:idx]
            if emit:
                self._emit(emit)
            rest = self._buf[idx + len(marker) :]
            parsed = _parse_marker(rest)
            if parsed is None:
                # 标记还没读全，等下一块
                self._buf = self._buf[idx:]
                return
            rc, cwd, consumed = parsed
            after = rest[consumed:]
            # 标记行本身的换行（printf/echo 带的）不属于命令输出，吃掉它，
            # 否则每条命令后面都会多出一个空行。
            if after.startswith("\r\n"):
                after = after[2:]
            elif after.startswith("\n"):
                after = after[1:]
            self._buf = after
            self._finish(rc, cwd)

    def _emit(self, text: str) -> None:
        if not text:
            return
        self._last_active = time.time()
        if self._kept < self.max_output:
            room = self.max_output - self._kept
            piece = text if len(text) <= room else text[:room]
            self._lines.append(piece)
            self._kept += len(piece)
            if len(piece) < len(text):
                self.truncated = True
        else:
            self.truncated = True
        self.on_output(text)

    def _finish(self, rc: int, cwd: str) -> None:
        self._cancel_timer()
        if cwd:
            self.cwd = cwd
        self.last_rc = rc
        with self._lock:
            self._busy = False
        self._last_active = time.time()
        self.on_done(rc, self.cwd, timed_out=False)

    def take_output(self) -> str:
        """取走当前命令已积累的输出（非流式调用方用）。"""
        with self._lock:
            out = "".join(self._lines)
            self._lines = []
            return out


# ── 工具 ──────────────────────────────────────────────
def _parse_marker(rest: str) -> tuple[int, str, int] | None:
    """从标记后面解析 `rc__cwd__`。

    期望形状（`_` 与 `__` 之间）：`0__/root__`
    返回 (rc, cwd, 本段消耗的字符数)；还没读全返回 None。
    """
    split = rest.find("__")
    if split < 0:
        return None
    rc_text = rest[:split]
    if not rc_text or not rc_text.strip().lstrip("-").isdigit():
        # 标记后面不是数字：说明这不是我们的标记（可能是用户输出里恰好撞了前缀）
        return None
    tail = rest[split + 2 :]
    end = tail.find("__")
    if end < 0:
        return None
    cwd = tail[:end]
    return int(rc_text), cwd, split + 2 + end + 2


def _partial_suffix(buf: str, marker: str) -> int:
    """buf 末尾有多少字符是 marker 的前缀（可能被读块切断）。"""
    limit = min(len(buf), len(marker) - 1)
    for n in range(limit, 0, -1):
        if buf.endswith(marker[:n]):
            return n
    return 0


def _home() -> str:
    try:
        from ..config import get_settings

        configured = (getattr(get_settings(), "shell_cwd", "") or "").strip()
        if configured and os.path.isdir(configured):
            return configured
    except Exception:
        pass
    return os.path.expanduser("~")


def _kill_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            from .proc import run_hidden

            run_hidden(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, timeout=10)
            return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


# ── 会话仓库 ──────────────────────────────────────────
class SessionRegistry:
    """按 (device, sid) 管理会话，并回收闲置的。"""

    def __init__(self) -> None:
        self._items: dict[str, ShellSession] = {}
        self._lock = threading.Lock()

    def create(self, sid: str, **kwargs: Any) -> ShellSession:
        with self._lock:
            existing = self._items.get(sid)
            if existing is not None and existing.alive():
                return existing
            sess = ShellSession(sid, **kwargs)
            self._items[sid] = sess
        sess.start()
        return sess

    def get(self, sid: str) -> ShellSession | None:
        with self._lock:
            return self._items.get(sid)

    def drop(self, sid: str) -> None:
        with self._lock:
            sess = self._items.pop(sid, None)
        if sess is not None:
            sess.close()

    def close_all(self) -> None:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        for sess in items:
            sess.close()

    def reap_idle(self, ttl: float = IDLE_TTL) -> int:
        now = time.time()
        dead: list[str] = []
        with self._lock:
            for sid, sess in self._items.items():
                if not sess.busy and (now - sess._last_active) > ttl:
                    dead.append(sid)
        for sid in dead:
            self.drop(sid)
        return len(dead)

    def listing(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.status() for s in self._items.values()]

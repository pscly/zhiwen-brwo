"""远程命令行队列（hub 侧）。

协议契约见 `docs/13-远程命令行协议.md`，这里只实现"队列 + 状态机"，不执行命令 ——
命令由设备侧 core 拉走并在本机跑，hub 全程不连设备。

## 为什么是队列 + 长轮询

客户端可能跑在云电脑 / NAT 后面，服务器**连不进去**它的 127.0.0.1。
所以：管理员投递 → hub 排队 → 设备出站长轮询取走 → 设备回传结果。
设备侧不新开任何监听端口，这是这套方案安全上最重要的一条。

## 几条刻意的设计

1. **内存为主，SQLite 只存终态历史**。
   排队中的命令重启就丢是有意为之：重启后把一条十分钟前的命令补发给设备，
   比丢掉它更危险（用户以为没执行，其实执行了）。终态历史留最近若干条，
   重启后面板还能看到"刚才发生了什么"。

2. **长轮询用 `threading.Condition.wait()`，不是 sleep 忙轮询**。
   忙轮询在几十台设备下会白烧 CPU，而且唤醒延迟还是随机的（取决于轮询间隔）。
   这里投递时 `notify_all()`，设备那一侧几乎是立刻返回。

3. **所有状态变更走同一把锁**（`self._cond` 包着同一把）。
   状态机跨 submit / next_job / complete / cancel / expire 五处修改，
   两把锁一定会漏一条路径。

4. **输出按 UTF-8 安全边界截断**。
   `raw[:limit].decode("utf-8", "ignore")` —— 直接切字节会把汉字/emoji 切成
   半个，前端 JSON 解析直接报错。这是必须守住的（列表里已经有中文输出）。

5. **job 归属校验在 `ShellQueue` 里做，不只靠路由**。
   不同设备的 job 互不可见：拿别人的 job_id 来查/来回传，一律 404
   `SHELL_JOB_NOT_FOUND`（而不是 403 —— 403 会泄漏"这个 id 存在"）。
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger("zw.hub.shell")

# ── 状态机 ────────────────────────────────────────────────
QUEUED = "queued"      # 已投递，等设备取
RUNNING = "running"    # 设备已取走，正在跑
DONE = "done"          # 跑完了（rc 可能是非 0，那是命令自己的事）
ERROR = "error"        # 没跑起来（设备侧开关关了 / cwd 不存在 / 拉进程失败）
TIMEOUT = "timeout"    # 跑超时被杀
CANCELED = "canceled"  # 还没下发就被管理员取消
EXPIRED = "expired"    # 超过 15 分钟没设备来取，自动作废

TERMINAL = frozenset({DONE, ERROR, TIMEOUT, CANCELED, EXPIRED})

# ── 上限 ──────────────────────────────────────────────────
MAX_COMMAND_CHARS = 4000   # 单条命令长度（字符，不是字节）
DEFAULT_TIMEOUT = 60       # 没传 timeout 时的秒数
MAX_TIMEOUT = 600          # 协议上限
MAX_WAIT = 30.0            # 长轮询一次最多挂 30 秒
DEFAULT_DEVICE_WAIT = 25.0 # 设备侧默认等待
DEFAULT_ADMIN_WAIT = 30.0  # 管理员查结果默认等待
STALE_AFTER = 900          # 15 分钟没人取 → expired
KEEP_HISTORY = 100         # 重启后仍保留的终态条数（协议要求的"最近 100 条"）
MAX_DB_ROWS = 200          # 表里最多留多少条终态（比内存多一点，历史查询够用）


class ShellError(Exception):
    """带 HTTP 语义的远程命令行错误。与 `DeviceError` 同风格。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ── 小工具 ────────────────────────────────────────────────
def truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    """按字节截断，但不切出半个字符。返回 (文本, 是否截断)。"""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if limit is None or limit <= 0:
        return text, False          # 配 0/负数当成"不截断"，不静默清空输出
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


def _as_int(value: Any) -> int | None:
    """转 int；转不了返回 None。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _new_id() -> str:
    return "sh_" + secrets.token_hex(6)   # sh_ + 12 位小写 hex


_DDL = """
CREATE TABLE IF NOT EXISTS shell_jobs (
  id          TEXT PRIMARY KEY,
  device_id   TEXT NOT NULL,
  command     TEXT NOT NULL,
  cwd         TEXT NOT NULL DEFAULT '',
  timeout     INTEGER,
  status      TEXT NOT NULL,
  rc          INTEGER,
  stdout      TEXT NOT NULL DEFAULT '',
  stderr      TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER NOT NULL DEFAULT 0,
  truncated   INTEGER NOT NULL DEFAULT 0,
  error       TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL,
  started_at  REAL,
  finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_shell_jobs_device ON shell_jobs(device_id, created_at);
CREATE INDEX IF NOT EXISTS idx_shell_jobs_created ON shell_jobs(created_at);
"""

_ROW_FIELDS = (
    "id", "device_id", "command", "cwd", "timeout", "status", "rc", "stdout", "stderr",
    "duration_ms", "truncated", "error", "created_at", "started_at", "finished_at",
)


class ShellQueue:
    """远程命令队列：内存状态 + SQLite 终态历史。线程安全。"""

    def __init__(self, path: Path, max_output: int = 65536, queue_limit: int = 200) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_output = int(max_output)
        self.queue_limit = max(1, int(queue_limit))
        self._cond = threading.Condition()          # 一把锁管住全部状态
        self._jobs: dict[str, dict[str, Any]] = {}  # id -> job（内存主状态）
        self._pending: dict[str, deque[str]] = {}   # device_id -> 待被取走的 job id（FIFO）
        self._db = self._connect()
        self._init_db()
        self._load_recent()

    # ── 存储 ──────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        """**一条**连接贯穿全生命周期，由 `self._cond` 串行化。

        为什么不像设备表那样每次开一条：`complete()` 是热路径（每条命令都走），
        而每次 `sqlite3.connect` + `PRAGMA journal_mode=WAL` 都要重新协商日志模式，
        实测单次就要几十毫秒 —— 调用方会以为 hub 卡了。
        `synchronous=NORMAL` 是 WAL 下的推荐值：只在 checkpoint 时 fsync。
        代价是断电可能丢最后几条**历史记录**（命令本身早已执行完），可以接受；
        换来的是一条 complete 从 13ms 降到 0.2ms。
        """
        c = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def close(self) -> None:
        """关掉底层连接（测试收尾用；生产进程退出时由操作系统回收）。"""
        with self._cond:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    def _init_db(self) -> None:
        self._db.executescript(_DDL)
        self._db.commit()

    def _load_recent(self) -> None:
        """把最近 KEEP_HISTORY 条终态读回内存（排队中的本来就不落库）。"""
        rows = self._db.execute(
            "SELECT * FROM shell_jobs ORDER BY created_at DESC LIMIT ?", (KEEP_HISTORY,)
        ).fetchall()
        with self._cond:
            for row in reversed(rows):          # 旧 → 新，插入顺序即时间顺序
                job = self._row_to_job(row)
                self._jobs[job["id"]] = job

    def _persist_locked(self, job: dict[str, Any]) -> None:
        """落一条终态。调用方必须已持有 `self._cond`。"""
        try:
            self._db.execute(
                f"INSERT OR REPLACE INTO shell_jobs({','.join(_ROW_FIELDS)}) "
                f"VALUES({','.join('?' * len(_ROW_FIELDS))})",
                tuple(
                    (1 if job["truncated"] else 0) if k == "truncated" else job.get(k)
                    for k in _ROW_FIELDS
                ),
            )
            # 表只留最近 MAX_DB_ROWS 条，别让它无限长
            self._db.execute(
                "DELETE FROM shell_jobs WHERE id NOT IN "
                "(SELECT id FROM shell_jobs ORDER BY created_at DESC LIMIT ?)",
                (MAX_DB_ROWS,),
            )
            self._db.commit()
        except sqlite3.Error:
            self._db.rollback()
            raise

    def _fetch(self, job_id: str) -> dict[str, Any] | None:
        """从 SQLite 读一条（内存里已被裁剪的老历史）。调用方必须已持有锁。"""
        row = self._db.execute("SELECT * FROM shell_jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def _fetch_device_history(self, device_id: str, limit: int) -> list[dict[str, Any]]:
        """调用方必须已持有锁。"""
        rows = self._db.execute(
            "SELECT * FROM shell_jobs WHERE device_id=? ORDER BY created_at DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    @staticmethod
    def _row_to_job(row: Any) -> dict[str, Any]:
        job = {k: row[k] for k in _ROW_FIELDS}
        job["truncated"] = bool(job.get("truncated"))
        if job.get("timeout") is not None:
            job["timeout"] = int(job["timeout"])
        return job

    # ── 内存裁剪 ──────────────────────────────────────────
    def _prune_locked(self) -> None:
        """只裁终态，且只留最近 KEEP_HISTORY 条。排队中/运行中的一条都不动。"""
        terminal = [j for j in self._jobs.values() if j["status"] in TERMINAL]
        if len(terminal) <= KEEP_HISTORY:
            return
        terminal.sort(key=lambda j: float(j.get("created_at") or 0))
        for job in terminal[: len(terminal) - KEEP_HISTORY]:
            self._jobs.pop(job["id"], None)

    # ── 投递 ──────────────────────────────────────────────
    def submit(self, device_id: str, command: str, cwd: str = "", timeout: int | None = None) -> dict[str, Any]:
        """管理员投递一条命令。设备离线也允许投（排队等它回来）。"""
        dev = (device_id or "").strip()
        text = command if isinstance(command, str) else ""
        if not text.strip():
            raise ShellError(400, "SHELL_COMMAND_REQUIRED", "command 不能为空")
        if len(text) > MAX_COMMAND_CHARS:
            raise ShellError(
                400, "SHELL_COMMAND_TOO_LONG",
                f"命令长度 {len(text)} 超过上限 {MAX_COMMAND_CHARS} 字符",
            )
        tmo = DEFAULT_TIMEOUT if timeout in (None, "") else _as_int(timeout)
        if tmo is None or not (1 <= tmo <= MAX_TIMEOUT):
            raise ShellError(400, "SHELL_TIMEOUT_INVALID", f"timeout 必须是 1..{MAX_TIMEOUT} 之间的整数秒")
        cwd_text = (cwd if isinstance(cwd, str) else "").strip()

        self.expire_stale()   # 顺手把 15 分钟没人取的老命令作废（hub 没有独立巡检线程）

        now = time.time()
        with self._cond:
            pending = self._pending.setdefault(dev, deque())
            if len(pending) >= self.queue_limit:
                raise ShellError(
                    429, "SHELL_QUEUE_FULL",
                    f"该设备排队中的命令已有 {len(pending)} 条（上限 {self.queue_limit}），等它执行完再投",
                )
            job: dict[str, Any] = {
                "id": _new_id(),
                "device_id": dev,
                "command": text,
                "cwd": cwd_text,
                "timeout": tmo,
                "status": QUEUED,
                "rc": None,
                "stdout": "",
                "stderr": "",
                "duration_ms": 0,
                "truncated": False,
                "error": "",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
            }
            self._jobs[job["id"]] = job
            pending.append(job["id"])
            self._cond.notify_all()       # 唤醒挂着的长轮询
            logger.info("远程命令已投递 device=%s job=%s timeout=%s chars=%s",
                        dev, job["id"], tmo, len(text))
            return dict(job)

    # ── 设备取命令（长轮询） ──────────────────────────────
    def next_job(self, device_id: str, wait: float = DEFAULT_DEVICE_WAIT) -> dict[str, Any] | None:
        """在 `wait` 秒内等到一条命令就返回，超时返回 None。

        等待用 `Condition.wait()`，不是循环 sleep —— 投递方 `notify_all()` 之后
        这里几乎立刻返回，既不忙轮询，也没有轮询间隔带来的延迟。
        """
        dev = (device_id or "").strip()
        wait_s = DEFAULT_DEVICE_WAIT if wait is None else _as_float(wait, DEFAULT_DEVICE_WAIT)
        wait_s = max(0.0, min(wait_s, MAX_WAIT))
        deadline = time.monotonic() + wait_s
        stale_before = time.time() - STALE_AFTER
        with self._cond:
            while True:
                pending = self._pending.get(dev)
                if pending:
                    job_id = pending.popleft()
                    job = self._jobs.get(job_id)
                    if job is None or job["status"] != QUEUED:
                        continue                      # 已被取消/作废，取下一条
                    if float(job["created_at"]) < stale_before:
                        # 太老了：不给设备执行（十几分钟前的命令现在跑，语义已经变了）
                        self._finish_locked(job, EXPIRED, error="SHELL_EXPIRED")
                        continue
                    job["status"] = RUNNING
                    job["started_at"] = time.time()
                    job["error"] = ""
                    self._cond.notify_all()
                    logger.info("远程命令已下发 device=%s job=%s", dev, job["id"])
                    return {
                        "id": job["id"],
                        "command": job["command"],
                        "cwd": job["cwd"],
                        "timeout": job["timeout"],
                        "created_at": job["created_at"],
                    }
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    # ── 设备回传结果 ──────────────────────────────────────
    def complete(
        self,
        job_id: str,
        device_id: str,
        rc: Any = 0,
        stdout: str = "",
        stderr: str = "",
        duration_ms: Any = 0,
        truncated: bool = False,
        error: str = "",
    ) -> dict[str, Any]:
        """落终态。`error` 非空表示没跑起来（此时 rc 一般是 -1）。"""
        rc_i = _as_int(rc)
        if rc_i is None:
            raise ShellError(400, "SHELL_BAD_RESULT", f"rc 必须是整数，收到 {rc!r}")
        out, cut_out = truncate_utf8(stdout or "", self.max_output)
        err, cut_err = truncate_utf8(stderr or "", self.max_output)
        err_msg = (error or "").strip()
        if len(err_msg) > 500:
            err_msg = err_msg[:500]
        status = DONE
        if err_msg:
            # 设备侧超时用 SHELL_TIMEOUT 回传，面板据此显示"超时"而不是"失败"
            status = TIMEOUT if "TIMEOUT" in err_msg.upper() else ERROR

        with self._cond:
            job = self._jobs.get(job_id) or self._fetch(job_id)
            if job is None or job["device_id"] != (device_id or "").strip():
                # 不区分"不存在"和"是别人的"：403 会泄漏这个 id 存在
                raise ShellError(404, "SHELL_JOB_NOT_FOUND", f"没有这条命令：{job_id}")
            if job["status"] in TERMINAL:
                raise ShellError(
                    409, "SHELL_JOB_TERMINAL",
                    f"这条命令已经是终态（{job['status']}），不再接受结果",
                )
            # 终态字段一次性写全：rc / 输出 / 耗时 / 截断标记 / 错误原因
            self._finish_locked(
                job,
                status,
                rc=rc_i,
                stdout=out,
                stderr=err,
                duration_ms=max(0, _as_int(duration_ms) or 0),
                truncated=bool(truncated) or cut_out or cut_err,
                error=err_msg,
            )
            logger.info("远程命令结束 device=%s job=%s status=%s rc=%s duration_ms=%s truncated=%s",
                        job["device_id"], job_id, status, rc_i, job["duration_ms"], job["truncated"])
            return dict(job)

    # ── 查询 ──────────────────────────────────────────────
    def get(self, job_id: str, device_id: str | None = None) -> dict[str, Any]:
        """取一条。`device_id` 给了就校验归属（不同设备的 job 互不可见）。"""
        jid = (job_id or "").strip()
        with self._cond:
            job = self._jobs.get(jid) or self._fetch(jid)
            if job is None:
                raise ShellError(404, "SHELL_JOB_NOT_FOUND", f"没有这条命令：{jid}")
            if device_id is not None and job["device_id"] != (device_id or "").strip():
                raise ShellError(404, "SHELL_JOB_NOT_FOUND", f"没有这条命令：{jid}")
            return dict(job)

    def wait_terminal(self, job_id: str, device_id: str, wait: float = DEFAULT_ADMIN_WAIT) -> dict[str, Any]:
        """等到终态，或 `wait` 秒后把当前状态原样返回。"""
        jid = (job_id or "").strip()
        wait_s = max(0.0, min(_as_float(wait, DEFAULT_ADMIN_WAIT), MAX_WAIT))
        deadline = time.monotonic() + wait_s
        with self._cond:
            job = self._jobs.get(jid) or self._fetch(jid)
            if job is None or job["device_id"] != (device_id or "").strip():
                raise ShellError(404, "SHELL_JOB_NOT_FOUND", f"没有这条命令：{jid}")
            # 从这里开始只认本地引用：内存裁剪只删终态，运行中的一定还在；
            # 即使刚好被裁掉，这个引用也仍然指向同一个 dict，状态不会读丢。
            while job["status"] not in TERMINAL:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            return dict(job)

    def history(self, device_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """按 created_at 倒序。内存与 SQLite 合并（重启前后的都能看到）。"""
        dev = (device_id or "").strip()
        lim = max(1, min(int(limit or 50), 200))
        with self._cond:
            merged: dict[str, dict[str, Any]] = {}
            for job in self._jobs.values():
                if job["device_id"] == dev:
                    merged[job["id"]] = dict(job)
            for job in self._fetch_device_history(dev, lim):
                merged.setdefault(job["id"], job)
        rows = sorted(merged.values(), key=lambda j: float(j.get("created_at") or 0), reverse=True)
        return rows[:lim]

    # ── 取消 / 过期 ───────────────────────────────────────
    def cancel(self, job_id: str, device_id: str) -> bool:
        """只有还没被设备取走的命令能取消。返回是否真的取消了。"""
        jid = (job_id or "").strip()
        dev = (device_id or "").strip()
        with self._cond:
            job = self._jobs.get(jid) or self._fetch(jid)
            if job is None or job["device_id"] != dev:
                raise ShellError(404, "SHELL_JOB_NOT_FOUND", f"没有这条命令：{jid}")
            if job["status"] != QUEUED:
                return False
            self._finish_locked(job, CANCELED, error="SHELL_CANCELED")
            logger.info("远程命令已取消 device=%s job=%s", dev, jid)
            return True

    def expire_stale(self, max_age: float = STALE_AFTER) -> int:
        """把超过 max_age 秒还没被取走的 queued 标成 expired。返回处理条数。"""
        try:
            cutoff = time.time() - max(0.0, float(max_age))
        except (TypeError, ValueError):
            cutoff = time.time() - STALE_AFTER
        expired = 0
        with self._cond:
            for job in list(self._jobs.values()):
                if job["status"] == QUEUED and float(job["created_at"]) < cutoff:
                    self._finish_locked(job, EXPIRED, error="SHELL_EXPIRED")
                    expired += 1
            if expired:
                logger.info("远程命令过期作废 %s 条", expired)
        return expired

    # ── 统计 ──────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        """面板 KPI。区分"排队的 / 在跑的 / 跑完的 / 失败的"。"""
        with self._cond:
            counts: dict[str, int] = {}
            devices_with_pending: set[str] = set()
            for job in self._jobs.values():
                status = job["status"]
                counts[status] = counts.get(status, 0) + 1
                if status in (QUEUED, RUNNING):
                    devices_with_pending.add(job["device_id"])
            return {
                "queued": counts.get(QUEUED, 0),
                "running": counts.get(RUNNING, 0),
                "done": counts.get(DONE, 0),
                "error": counts.get(ERROR, 0) + counts.get(TIMEOUT, 0),
                "devices_with_pending": len(devices_with_pending),
            }

    # ── 内部（调用方必须已持有锁） ────────────────────────
    def _finish_locked(self, job: dict[str, Any], status: str, **fields: Any) -> None:
        job.update(fields)
        job["status"] = status
        job["finished_at"] = time.time()
        # 不论从哪条路径进入终态（取消 / 过期 / 回传 / 设备取走时发现太老），
        # 都要从"待取队列"里摘掉，否则队列长度会被幽灵条目撑大、误报 429。
        self._drop_pending_locked(job["device_id"], job["id"])
        self._persist_locked(job)
        self._prune_locked()
        self._cond.notify_all()

    def _drop_pending_locked(self, device_id: str, job_id: str) -> None:
        pending = self._pending.get(device_id)
        if not pending:
            return
        try:
            pending.remove(job_id)
        except ValueError:
            pass


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

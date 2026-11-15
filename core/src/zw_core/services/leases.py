"""本机租约：防止同一台机器上对一个环境并发 start / sync。

跨机互斥由 hub 的租约负责；这里是本机的第一道闸。
两边都过，才允许动手 —— 因为大部分人踩的坑都是"自己两台机器同时开同一个号"。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True      # 存在但不属于我，算活着


class LeaseBusy(Exception):
    def __init__(self, owner: str, kind: str, remain: int) -> None:
        super().__init__(f"环境被 {owner} 持有（{kind}，剩余 {remain} 秒）")
        self.owner = owner
        self.kind = kind
        self.remain = remain


# 进程内重入计数：同一个进程可能对同一个环境嵌套加锁
# （例如 sync.pull 持着 sync 锁，内部又调 manager.start 去拿 run 锁）。
# 不做重入的话，后者会把前者判成"别人持有"，从而自己把自己挡在门外。
_INPROC: dict[str, int] = {}
_INPROC_LOCK = threading.Lock()

# 进程级稳定的 owner 标识。
# 坑：以前 owner 是每个 LocalLease 实例随机生成的，导致 stop() 里新建一个实例去 release 时
# "owner 对不上"，租约文件永远删不掉 —— 下次 start 就会被自己的旧租约挡住 90 秒。
# 现在同一进程内所有实例共享同一个 owner，release 才能真正生效。
_PROC_OWNER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def process_owner() -> str:
    return _PROC_OWNER


def owner_pid(owner: str) -> int:
    """从 owner 串里取出 pid（格式 host:pid:rand）。取不到返回 0。"""
    parts = (owner or "").split(":")
    if len(parts) >= 2:
        try:
            return int(parts[-2])
        except ValueError:
            return 0
    return 0


class LocalLease:
    """基于文件 + 心跳的租约，跨进程也有效（同一台机器上跑两个 core 也能互斥）。"""

    def __init__(self, profile_dir: Path, ttl: int = 90) -> None:
        self.path = Path(profile_dir) / "lease.json"
        self.ttl = ttl
        self.owner = _PROC_OWNER
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def status(self) -> dict[str, Any] | None:
        data = self._read()
        if not data:
            return None
        remain = int(float(data.get("ttl", 0)) - (time.time() - float(data.get("heartbeat", 0))))
        data["alive"] = remain > 0
        data["remain_seconds"] = max(0, remain)
        data["is_mine"] = data.get("owner") == self.owner
        return data

    def acquire(self, kind: str = "run") -> dict[str, Any]:
        with self._lock:
            key = str(self.path.parent)
            with _INPROC_LOCK:
                if _INPROC.get(key, 0) > 0:
                    # 本进程已持有 → 重入，直接成功
                    _INPROC[key] += 1
                    cur = self._read() or {}
                    return {"owner": cur.get("owner", self.owner), "kind": cur.get("kind", kind), "reentrant": True}
            cur = self._read()
            if cur and cur.get("owner") == self.owner:
                # 本进程持有 → 重入（不依赖计数器，双保险）
                with _INPROC_LOCK:
                    _INPROC[key] = _INPROC.get(key, 0) + 1
                self._write({**cur, "heartbeat": time.time()})
                return {**cur, "reentrant": True}
            if cur:
                remain = int(float(cur.get("ttl", 0)) - (time.time() - float(cur.get("heartbeat", 0))))
                if remain > 0 and cur.get("owner") != self.owner:
                    raise LeaseBusy(cur.get("owner", "?"), cur.get("kind", "?"), remain)
            payload = {
                "owner": self.owner,
                "kind": kind,
                "ttl": self.ttl,
                "acquired_at": time.time(),
                "heartbeat": time.time(),
            }
            self._write(payload)
            with _INPROC_LOCK:
                _INPROC[key] = _INPROC.get(key, 0) + 1
            return payload

    def heartbeat(self) -> None:
        with self._lock:
            cur = self._read()
            if cur and cur.get("owner") == self.owner:
                cur["heartbeat"] = time.time()
                self._write(cur)

    def release(self) -> None:
        key = str(self.path.parent)
        with self._lock:
            with _INPROC_LOCK:
                depth = _INPROC.get(key, 0)
                if depth > 1:
                    _INPROC[key] = depth - 1
                    return                      # 还有外层持有，先不删文件
                _INPROC.pop(key, None)
            cur = self._read()
            if cur and cur.get("owner") == self.owner:
                try:
                    self.path.unlink()
                except OSError:
                    pass

    def force_release(self) -> bool:
        """无条件删除租约文件。给 stop / unlock 这类"明确要释放"的操作用。

        它们本来就该释放，不该因为 owner 不匹配而失败。
        """
        with self._lock:
            with _INPROC_LOCK:
                _INPROC.pop(str(self.path.parent), None)
            if self.path.exists():
                try:
                    self.path.unlink()
                    return True
                except OSError:
                    return False
            return False

    def reap_if_stale(self) -> bool:
        """如果租约的持有进程已经不在了，就把它清掉。返回是否清理过。"""
        cur = self._read()
        if not cur:
            return False
        if cur.get("owner") == self.owner:
            return False
        pid = owner_pid(cur.get("owner", ""))
        host = (cur.get("owner", "").split(":") or [""])[0]
        if host != socket.gethostname():
            return False          # 别的机器上的租约，这里不管
        if pid and _pid_alive(pid):
            return False          # 进程还活着，尊重它
        return self.force_release()

    def _write(self, payload: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

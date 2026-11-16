"""本机租约。

这里有一条踩过的真实缺陷：以前 owner 是每个实例随机生成的，
于是 stop() 里新建实例去 release 时"对不上 owner"，租约文件永远删不掉，
下次 start 会被自己的旧租约挡住 90 秒。下面专门锁死这个行为。
"""

from __future__ import annotations

import json
import os
import socket
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from zw_core.services import leases
from zw_core.services.leases import LeaseBusy, LocalLease


@pytest.fixture()
def pdir() -> Path:
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_release_works_from_another_instance(pdir: Path):
    """核心回归：一个实例 acquire，另一个实例 release 也必须成功。"""
    LocalLease(pdir).acquire("run")
    assert (pdir / "lease.json").exists()
    LocalLease(pdir).release()          # 不同实例
    assert not (pdir / "lease.json").exists(), "release 必须能删掉别的实例写的租约（同进程）"

    # 释放后可以再次获取
    LocalLease(pdir).acquire("run")


def test_nested_reentrant(pdir: Path):
    """同一进程嵌套加锁必须放行（sync.pull 里会嵌套调 start）。"""
    a = LocalLease(pdir)
    a.acquire("sync")
    b = LocalLease(pdir)                # 另一个实例，同进程
    b.acquire("run")                    # 不应抛
    b.release()
    assert (pdir / "lease.json").exists(), "内层释放不应该删掉外层的租约"
    a.release()
    assert not (pdir / "lease.json").exists()


def test_other_process_blocks(pdir: Path):
    """别人持有且未过期 → 必须挡住。"""
    path = pdir / "lease.json"
    path.write_text(json.dumps({
        "owner": f"{socket.gethostname()}:999999:deadbeef", "kind": "run",
        "ttl": 90, "acquired_at": time.time(), "heartbeat": time.time(),
    }))
    with pytest.raises(LeaseBusy) as exc:
        LocalLease(pdir).acquire("run")
    assert exc.value.remain > 0


def test_force_release_ignores_owner(pdir: Path):
    path = pdir / "lease.json"
    path.write_text(json.dumps({"owner": "别的主机:1:aaa", "kind": "sync",
                                "ttl": 90, "acquired_at": time.time(), "heartbeat": time.time()}))
    assert LocalLease(pdir).force_release() is True
    assert not path.exists()


def test_reap_if_stale_clears_dead_owner(pdir: Path):
    """持有者进程已经不在了 → 启动时应该自动清掉。"""
    path = pdir / "lease.json"
    path.write_text(json.dumps({
        "owner": f"{socket.gethostname()}:999999:dead",   # 这个 pid 基本不可能存在
        "kind": "run", "ttl": 90,
        "acquired_at": time.time(), "heartbeat": time.time(),
    }))
    assert LocalLease(pdir).reap_if_stale() is True
    assert not path.exists()


def test_reap_if_stale_respects_live_owner(pdir: Path):
    """持有者是活着的进程 → 不动它。"""
    path = pdir / "lease.json"
    path.write_text(json.dumps({
        "owner": f"{socket.gethostname()}:{os.getpid()}:x",  # 就是当前进程，但 owner 串不同
        "kind": "run", "ttl": 90,
        "acquired_at": time.time(), "heartbeat": time.time(),
    }))
    # 当前 pid 活着 → 不该清理
    assert LocalLease(pdir).reap_if_stale() is False
    assert path.exists()


def test_reap_if_stale_ignores_other_host(pdir: Path):
    path = pdir / "lease.json"
    path.write_text(json.dumps({"owner": "另一台机器:12345:x", "kind": "sync",
                                "ttl": 90, "acquired_at": time.time(), "heartbeat": time.time()}))
    assert LocalLease(pdir).reap_if_stale() is False
    assert path.exists()


def test_owner_is_stable_within_process():
    assert leases.process_owner() == leases.process_owner()
    assert leases.process_owner().count(":") == 2


def test_pid_alive_is_safe_on_all_platforms(monkeypatch):
    """`_pid_alive` 必须跨平台安全。

    Windows 上 `os.kill(pid, 0)` 会被转成 TerminateProcess，对任意 pid
    抛 `OSError: [WinError 87]`。以前没捕获，`reap_if_stale` 直接崩，
    租约逻辑在 Windows 上等于不可用（CI 在真 Windows 上抓到的）。
    """
    from zw_core.services import leases as L

    assert L._pid_alive(0) is False
    assert L._pid_alive(-1) is False
    # 当前进程一定活着
    assert L._pid_alive(os.getpid()) is True
    # 一个几乎不可能存在的 pid：必须返回 False 而不是抛异常
    assert L._pid_alive(999999999) is False


def test_pid_alive_swallows_oserror(monkeypatch):
    """即使 os.kill 抛奇怪的 OSError，也不能把调用方带崩。"""
    from zw_core.services import leases as L

    monkeypatch.setattr(L, "_IS_WIN", False)

    def boom(pid, sig):
        raise OSError(22, "模拟平台差异")

    monkeypatch.setattr(L.os, "kill", boom)
    # 保守策略：不确定就按"活着"处理，宁可不清租约
    assert L._pid_alive(12345) is True

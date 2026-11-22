"""并发启动时端口不能撞，撞了要自动换。

## 为什么（2026-11-22 用户反馈）

> 我同时启动好几个窗口的时候居然会报错，说我端口冲突什么的
> 端口这个如果被占用就换个端口就行

根因是 `PortPool.allocate()` 的"先查再占"没加锁。`start_many()` 用线程池
真并发（默认 3 路），于是：

    T1: 21001 不在 _used，_port_free(21001)=True
    T2: 21001 不在 _used，_port_free(21001)=True      ← 两个都通过
    T1: _used[21001]=A，返回 21001
    T2: _used[21001]=B，返回 21001                    ← 撞了

一个浏览器抢到端口，另一个在 `wait_ready` 里看到"端口被别的进程占用"，
报 `PORT_MISMATCH`。
"""

from __future__ import annotations

import concurrent.futures


def test_allocate_is_atomic_under_concurrency():
    """32 个线程同时要端口，拿到的必须**互不相同**。"""
    from zw_core.services.engines import PortPool

    pool = PortPool(31000, 31099)
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        ports = list(ex.map(lambda i: pool.allocate(f"p{i}"), range(32)))

    assert len(ports) == 32
    assert len(set(ports)) == 32, f"有重复端口：{sorted(ports)}"
    # 每一个都要真的被记账，别只发不记
    assert set(pool.used()) == set(ports)


def test_release_frees_port_for_reuse():
    from zw_core.services.engines import PortPool

    pool = PortPool(31100, 31101)
    a = pool.allocate("a")
    b = pool.allocate("b")
    assert a != b
    pool.release(a)
    c = pool.allocate("c")
    assert c == a, "释放过的端口应该能被再次分配"


def test_falls_back_to_next_port_when_launch_hits_port_mismatch(tmp_path, monkeypatch):
    """启动时撞到 `PORT_MISMATCH`（端口被别人占了），要**自动换端口重试**。

    这是用户明确要的行为：端口被占就换一个，而不是把错误甩给他。
    对外不用改协议 —— `/start` 返回的 `cdp` 里就是最终连得上的端口。
    """
    from zw_core import config

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_DEVICE_ID", "zwd_portretry_0001")
    monkeypatch.setenv("ZW_SHELL_ENABLED", "false")
    monkeypatch.setenv("ZW_PORT_POOL_START", "31200")
    monkeypatch.setenv("ZW_PORT_POOL_END", "31240")
    config.reset_settings()
    try:
        from zw_core.services import engines, manager

        p = manager.create_profile({"name": "换端口", "with_default_packs": False})

        calls: list[int] = []

        def flaky_wait(port, **kw):
            """第一次报端口冲突，之后成功。

            不能在这里调真实 `wait_ready` —— 假进程不会真去开浏览器，
            那样会干等 45 秒超时（第一版就白跑了 46 秒）。
            """
            calls.append(port)
            if len(calls) == 1:
                raise engines.EngineError(
                    "PORT_MISMATCH", f"端口 {port} 被别的进程占用，不是刚启动的浏览器。")
            return (f"ws://127.0.0.1:{port}/devtools/browser/fake",
                    f"http://127.0.0.1:{port}")

        # 不真的拉内核：spawn 返回一个假进程对象，terminate 变空操作
        class _FakeProc:
            """够用的假进程：`wait_ready` 会调 `poll()` 判断进程死没死。"""

            pid = 999999

            def poll(self):
                return None          # None = 还活着

        from pathlib import Path as _P

        monkeypatch.setattr(engines, "find_chrome", lambda *a, **k: _P("/bin/true"))
        monkeypatch.setattr(engines, "build_cmdline", lambda *a, **k: ["true"])
        monkeypatch.setattr(engines, "spawn", lambda *a, **k: _FakeProc())
        monkeypatch.setattr(engines, "terminate", lambda *a, **k: None)
        monkeypatch.setattr(engines, "wait_ready", flaky_wait)

        manager.start(p["id"])

        assert len(calls) >= 2, f"应该换端口重试，实际只试了 {calls}"
        assert calls[0] != calls[1], f"两次用的是同一个端口：{calls}"
        prof = manager.get_profile(p["id"])
        assert prof["state"] == "running"
        # 记账的端口必须是**最终成功**的那个
        assert prof["cdp_port"] == calls[-1], (prof["cdp_port"], calls)
    finally:
        from zw_core import config
        config.reset_settings()

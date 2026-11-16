"""实时终端：会话引擎 + WS 协议 + hub 中继。

这三层都容易在重构时坏，而且坏了以后"看起来能用"（命令能发出去），
只是没输出/没流式/退出码不对 —— 所以每层都要有断言。

用真 socket / 真 WebSocket 验，不 mock 协议本身。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from zw_core.services.session import ShellSession, _parse_marker, _partial_suffix


@pytest.fixture()
def sess(tmp_path, monkeypatch):
    from zw_core import config, db

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    db.reset_db()
    s = ShellSession("t", cwd=str(tmp_path), timeout=20)
    s.start()
    time.sleep(0.3)
    yield s
    try:
        s.close()
    except Exception:
        pass
    config.reset_settings()
    db.reset_db()


def _run(s, cmd, timeout=8.0):
    out, done = [], []
    s.on_output = lambda d: out.append(d)
    s.on_done = lambda rc, cwd, timed_out=False: done.append((rc, cwd, timed_out))
    s.run(cmd)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if done:
            return "".join(out), done[0]
        time.sleep(0.02)
    raise AssertionError(f"命令没有结束：{cmd!r}")


# ── 会话引擎 ──────────────────────────────────────────
def test_session_runs_and_reports_rc(sess):
    out, (rc, cwd, timed_out) = _run(sess, "echo hello")
    assert out.strip() == "hello"
    assert rc == 0 and not timed_out


def test_session_keeps_cwd(sess, tmp_path):
    """`cd` 必须保持 —— 这是"实时终端"和"一问一答"最本质的区别。"""
    _run(sess, "cd /tmp")
    out, (rc, _cwd, _) = _run(sess, "pwd")
    assert out.strip() == "/tmp"


def test_session_reports_nonzero_rc(sess):
    _out, (rc, _cwd, _) = _run(sess, "exit_code_probe 2>/dev/null; false")
    assert rc != 0


def test_session_streams_progressively(sess):
    """输出必须**边产生边到**，不是等命令结束才一次性给。"""
    chunks, done = [], []
    sess.on_output = lambda d: chunks.append((time.time(), d))
    sess.on_done = lambda rc, cwd, timed_out=False: done.append(rc)
    sess.run("echo FIRST; sleep 1.2; echo SECOND")
    time.sleep(0.6)
    early = "".join(d for _t, d in chunks)
    assert "FIRST" in early, f"0.6 秒时应该已经收到第一行：{early!r}"
    assert "SECOND" not in early, "第二行不该这么早出现"
    deadline = time.time() + 8
    while time.time() < deadline and not done:
        time.sleep(0.05)
    assert done, "命令最终应该结束"
    assert "SECOND" in "".join(d for _t, d in chunks)


def test_session_survives_shell_exit(sess):
    """用户手滑敲 `exit` 不该让终端永久废掉。"""
    _out, (rc, _cwd, _) = _run(sess, "exit 3")
    assert rc == 3
    out, (rc2, _c, _) = _run(sess, "echo back", timeout=12)
    assert "back" in out and rc2 == 0


def test_marker_parsing_helpers():
    # consumed = len(rc) + 2 + len(cwd) + 2，不要手算死数字（我第一版就算错了）
    rc, cwd, used = _parse_marker("0__/root__rest")
    assert (rc, cwd) == (0, "/root")
    assert used == len("0__/root__")
    assert "0__/root__rest"[used:] == "rest"
    assert _parse_marker("abc__") is None, "标记后面不是数字就不是我们的标记"
    assert _parse_marker("12__") is None, "还没读全要返回 None 等下一块"
    # 被读块切断的前缀要保住
    assert _partial_suffix("xx__ZWabc", "__ZWabcdef_") == len("__ZWabc")
    assert _partial_suffix("nothing", "__ZWabcdef_") == 0


# ── hub 中继 ──────────────────────────────────────────
def test_relay_attach_and_fanout():
    from zw_hub.termrelay import TermRelay

    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send_text(self, text):
            self.sent.append(text)

        async def close(self, code=1000, reason=""):
            pass

    async def go():
        rel = TermRelay()
        ws = FakeWS()
        link = await rel.attach("zwd_aaaaaaaaaaaaaaaaaaaa", ws)
        assert rel.online("zwd_aaaaaaaaaaaaaaaaaaaa")
        q = link.subscribe()
        link.fanout({"t": "out", "data": "hi"})
        got = q.get_nowait()
        assert got["data"] == "hi"
        link.unsubscribe(q)
        await rel.detach(link)
        assert not rel.online("zwd_aaaaaaaaaaaaaaaaaaaa")

    asyncio.run(go())


def test_relay_replaces_old_connection():
    """设备重连时新连接顶掉旧的，但会话不丢（设备侧保留）。"""
    from zw_hub.termrelay import TermRelay

    class FakeWS:
        def __init__(self):
            self.closed = None

        async def send_text(self, text):
            pass

        async def close(self, code=1000, reason=""):
            self.closed = reason

    async def go():
        rel = TermRelay()
        old_ws = FakeWS()
        old = await rel.attach("zwd_bbbbbbbbbbbbbbbbbbbb", old_ws)
        new = await rel.attach("zwd_bbbbbbbbbbbbbbbbbbbb", FakeWS())
        assert rel.get("zwd_bbbbbbbbbbbbbbbbbbbb") is new
        assert old_ws.closed == "replaced-by-new-connection"

    asyncio.run(go())


def test_relay_queue_overflow_drops_oldest():
    """一个卡住的管理员不能把整台设备的转发堵死。"""
    from zw_hub import termrelay as tr

    class FakeWS:
        async def send_text(self, text):
            pass

        async def close(self, code=1000, reason=""):
            pass

    async def go():
        rel = tr.TermRelay()
        link = await rel.attach("zwd_cccccccccccccccccccc", FakeWS())
        q = link.subscribe()
        for i in range(tr._ADMIN_QUEUE_MAX + 50):
            link.fanout({"t": "out", "data": str(i)})
        # 队列没爆，且最新的一定在里面
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert len(items) <= tr._ADMIN_QUEUE_MAX
        assert any(i.get("data") == str(tr._ADMIN_QUEUE_MAX + 49) for i in items), "最新内容必须还在"

    asyncio.run(go())

"""设备侧实时终端必须**主动发 ping**。

## 为什么（2026-11-19 云电脑真机）

hub 的 `term_relay().online(device_id)` 判据是"90 秒内收到过这条设备的任何消息"。
而两端都不主动发消息：设备只在收到 hub 的 ping 时回 pong，hub 也从不发 ping。

于是设备安静挂着 90 秒就被判"实时通道未连"：
- 控制台 `realtime` 显示 False
- `/v1/console/term/{device_id}` 直接回 `device_offline` 并关闭

**常驻终端在正常空闲状态下永远建不起来** —— 用户要"全程留个终端连接"就卡在这。

`_PING_EVERY = 20.0` 这个常量早就定义好了，却没有任何地方引用它：写了一半。
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "core" / "src" / "zw_core" / "services" / "termhub.py"


def _tree() -> ast.Module:
    return ast.parse(SRC.read_text(encoding="utf-8"))


def test_ping_loop_is_scheduled():
    """`_ping_loop` 必须真的被 create_task 起来，否则等于没写。"""
    src = SRC.read_text(encoding="utf-8")
    assert "_ping_loop" in src, "找不到 _ping_loop"
    assert "create_task(self._ping_loop())" in src, (
        "_ping_loop 定义了却没被调度 —— 正是这次 bug 的形态"
    )


def test_ping_interval_is_below_hub_idle_timeout():
    """ping 间隔必须明显小于 hub 的空闲判定窗口（90 秒）。"""
    tree = _tree()
    value = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_PING_EVERY":
                    value = ast.literal_eval(node.value)
    assert value is not None, "找不到 _PING_EVERY"
    assert value <= 30, f"ping 间隔 {value}s 太接近 90s 窗口，容易抖成掉线"
    assert value > 0

    # hub 侧的窗口常量也要能读到，确认两边关系
    hub_src = (ROOT / "hub" / "src" / "zw_hub" / "termrelay.py").read_text(encoding="utf-8")
    import re

    m = re.search(r"DEVICE_IDLE_TIMEOUT\s*=\s*([0-9.]+)", hub_src)
    assert m, "hub 侧找不到 DEVICE_IDLE_TIMEOUT"
    idle = float(m.group(1))
    assert value * 2 < idle, (
        f"ping 间隔 {value}s 相对空闲窗口 {idle}s 太稀疏：中间断一次就会被判掉线"
    )


def test_hub_also_sends_keepalive_ping():
    """hub 侧也要主动 ping —— 这是**不需要设备升级**就能修好的那一半。

    设备侧 ping（`core/services/termhub._ping_loop`）只有新客户端才有；
    手里还是 0.7.95 及更早的机器不会发，于是安静 90 秒照样被判掉线。
    而设备本来就会 `ping → pong`（`_handle` 里实现过），所以 hub 主动发
    ping 能立刻覆盖老客户端。

    这个修复不用等用户升级安装包，改完 hub 就能验证。
    """
    import re

    relay = (ROOT / "hub/src/zw_hub/termrelay.py").read_text(encoding="utf-8")
    assert "async def keepalive" in relay, "DeviceLink 上没有 keepalive"
    assert '"t": "ping"' in relay, "keepalive 没真的发 ping"

    m = re.search(r"_SERVER_PING_EVERY\s*=\s*([0-9.]+)", relay)
    assert m, "找不到服务端 ping 间隔常量"
    every = float(m.group(1))

    hub_src = (ROOT / "hub/src/zw_hub/main.py").read_text(encoding="utf-8")
    assert "create_task(link.keepalive())" in hub_src, "keepalive 定义了却没被调度"
    assert "keepalive.cancel()" in hub_src, "断开时没取消，任务会泄漏"

    idle = float(re.search(r"DEVICE_IDLE_TIMEOUT\s*=\s*([0-9.]+)", relay).group(1))
    assert every * 2 < idle, f"服务端 ping {every}s 相对 {idle}s 窗口太稀疏"


def test_hub_main_imports_asyncio():
    """`hub/main.py` 原来没导入 asyncio，`create_task` 会 NameError。"""
    import ast

    tree = ast.parse((ROOT / "hub/src/zw_hub/main.py").read_text(encoding="utf-8"))
    imported = {a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    assert "asyncio" in imported, "hub/main.py 没导入 asyncio"


def test_ping_loop_actually_sends_pings(monkeypatch):
    """**真跑** `_ping_loop`，验证它真的每隔一段时间发一次 ping。

    上面那条只看源码字符串（"`create_task(self._ping_loop())` 在不在"），
    能防手滑删除，但证明不了循环体真的在工作。

    这里把 `_PING_EVERY` 调到 10 毫秒，让它跑几轮，看 `_send` 收到了什么。
    """
    import asyncio

    from zw_core.services import termhub

    class FakeClient:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.connected = True

        async def _send(self, msg):
            self.sent.append(msg)

    monkeypatch.setattr(termhub, "_PING_EVERY", 0.01)

    async def run() -> list[dict]:
        c = FakeClient()
        # 绑定真实方法
        task = asyncio.create_task(termhub.TermClient._ping_loop(c))  # type: ignore[arg-type]
        await asyncio.sleep(0.08)          # 约 8 轮
        c.connected = False
        await asyncio.sleep(0.03)          # 让它看到 connected=False 后退出
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return c.sent

    sent = asyncio.run(run())
    assert sent, "ping 循环跑了但一条都没发"
    assert all(m.get("t") == "ping" for m in sent), sent[:3]
    assert len(sent) >= 2, f"只发了 {len(sent)} 条，循环可能没真的在跑"

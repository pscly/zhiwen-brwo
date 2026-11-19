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

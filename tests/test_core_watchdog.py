"""GUI 必须看护 core：掉了自动拉起来。

## 为什么（2026-11-19 云电脑真机，我把自己锁在外面）

心跳由 **GUI 进程**发，core 是 `LocalCore` 起的子服务。core 挂了之后：

- 心跳照发 → 服务器上显示"在线"
- 但远程命令（走 core 的 HTTP 队列）拉不到
- 实时终端（走 core 的 WS 隧道）连不上
- core 自己的 `/api/v1/*` 全不通

于是**整台机器对外完全够不着**，只能跑到机器前点一下。我这次清旧进程时
顺手把 core 带走，就这么失去通道的；用户也反复遇到过。

修法：GUI 的 5 秒 tick 里核 `/healthz`，**连续两次**不通才重启（单次抖动不算），
恢复后立刻重建心跳与面板。
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUI = ROOT / "core" / "src" / "zw_core" / "gui.py"


def _src() -> str:
    return GUI.read_text(encoding="utf-8")


def test_watchdog_exists_and_is_called_in_tick():
    s = _src()
    assert "def watch_core" in s, "没有 core 看护"
    assert "watch_core()" in s, "看护没被 tick 调用（等于没写）"


def test_watchdog_needs_two_consecutive_failures():
    """单次不通不重启 —— 一次抖动就把好的服务杀掉更糟。"""
    s = _src()
    seg = s[s.index("def watch_core"):]
    seg = seg[: seg.index("\n    def tick")] if "\n    def tick" in seg else seg
    assert "core_miss" in seg
    assert "core_miss\"] < 2" in seg or "core_miss'] < 2" in seg, "没有『连续两次』的门槛"


def test_watchdog_restarts_core_and_rebuilds_heartbeat():
    s = _src()
    seg = s[s.index("def watch_core"):]
    seg = seg[: seg.index("\n    def tick")] if "\n    def tick" in seg else seg
    assert "core.start(" in seg, "没有真的重启 core"
    assert "start_heartbeat(" in seg, "恢复后没有重建心跳"
    assert "run_bg(" in seg, "重启是阻塞操作，必须放后台（否则界面又卡住）"


def test_heartbeat_interval_is_remembered():
    """重启后要用同一个心跳间隔，不能退回写死的 60。"""
    s = _src()
    assert 'state["hb_interval"]' in s
    assert 'int(state.get("hb_interval") or 60)' in s


def test_watchdog_does_not_restart_before_first_connect():
    """还没连过服务器时不要去"重启"——那时 core 本来就还没起。"""
    s = _src()
    seg = s[s.index("def watch_core"):]
    seg = seg[: seg.index("\n    def tick")] if "\n    def tick" in seg else seg
    assert 'state.get("core_on")' in seg, "没判断是否已连接过"

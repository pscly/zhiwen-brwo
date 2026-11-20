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


def test_watchdog_delegates_to_pure_decision():
    """闭包只负责调判定函数，"连续两次"的门槛由纯函数保证（见下方行为测试）。"""
    s = _src()
    assert "def _watchdog_decision" in s, "判定没抽成可测的纯函数"
    seg = s[s.index("def watch_core"):]
    seg = seg[: seg.index("\n    def tick")] if "\n    def tick" in seg else seg
    assert "_watchdog_decision(" in seg, "闭包没走判定函数"
    assert 'if action in ("skip", "ok", "wait")' in seg, "没按动作分支处理"


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


# ── 行为测试（不是读源码字符串，而是喂状态、验决策）────────────

def test_decision_waits_for_one_miss_then_heals():
    """**核心约束**：单次不通只等，连续两次才重启。

    一次抖动就把好的服务杀掉，比"偶尔恢复得慢"更糟 —— 会把正在
    正常提供服务的 core 打断。
    """
    from zw_core.gui import _watchdog_decision

    a1 = _watchdog_decision(core_on=True, healing=False, alive=False, misses=0)
    assert a1 == ("wait", 1), a1
    a2 = _watchdog_decision(core_on=True, healing=False, alive=False, misses=a1[1])
    assert a2 == ("heal", 2), a2


def test_decision_resets_counter_when_core_recovers():
    """抖一下就恢复 → 计数必须清零，否则两次相隔很远的抖动会累积成"重启"。"""
    from zw_core.gui import _watchdog_decision

    miss = _watchdog_decision(core_on=True, healing=False, alive=False, misses=0)
    assert miss[1] == 1
    ok = _watchdog_decision(core_on=True, healing=False, alive=True, misses=miss[1])
    assert ok == ("ok", 0), ok
    # 再过很久又抖一次，仍然只是 wait
    again = _watchdog_decision(core_on=True, healing=False, alive=False, misses=ok[1])
    assert again == ("wait", 1), again


def test_decision_skips_before_first_connect():
    """还没连过服务器时 core 本来就没起，不该去"重启"。"""
    from zw_core.gui import _watchdog_decision

    assert _watchdog_decision(core_on=False, healing=False, alive=False, misses=0) == ("skip", 0)


def test_decision_skips_while_healing():
    """正在重启时不能再叠加一次重启。"""
    from zw_core.gui import _watchdog_decision

    assert _watchdog_decision(core_on=True, healing=True, alive=False, misses=5) == ("skip", 5)

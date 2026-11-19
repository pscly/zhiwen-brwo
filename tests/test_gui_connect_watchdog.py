"""连接流程必须有超时兜底，不能永远停在「连接中…」。

`connect()` 里三步阻塞（`probe_server` / `enroll` / `core.start`）各有自己的超时，
但一旦哪个真的挂死（DNS 解析卡住、半开连接、uvicorn 卡在 startup），
后台线程不返回 → 界面永远显示「连接中…」→ 用户只能强杀进程。
看起来就是「客户端坏了」，而这其实是可以自愈的。
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUI = ROOT / "core" / "src" / "zw_core" / "gui.py"


def test_connect_has_watchdog():
    s = GUI.read_text(encoding="utf-8")
    assert "_watchdog" in s, "没有超时看护"
    assert "root.after(120_000" in s, "看护没挂到 Tk 的定时器上"
    assert "连接超时" in s, "超时后没有明确的状态"


def test_watchdog_only_fires_for_current_attempt():
    """重连后旧看护理应作废（用递增序号判断），否则会把新的连接误判成超时。"""
    s = GUI.read_text(encoding="utf-8")
    assert "connect_seq" in s
    seg = s[s.index("def _watchdog"):]
    seg = seg[: seg.index("root.after(120_000")] if "root.after(120_000" in seg else seg
    assert "state.get(\"connect_seq\") != seq" in seg or "connect_seq\") != seq" in seg, \
        "看护没有校验序号，重连会被旧的定时器误伤"


def test_watchdog_reenables_button():
    """超时后必须把「连接并开始」按钮恢复，否则用户没法重试。"""
    s = GUI.read_text(encoding="utf-8")
    seg = s[s.index("def _watchdog"):]
    seg = seg[: seg.index("root.after(120_000")] if "root.after(120_000" in seg else seg
    assert "!disabled" in seg, "超时后没恢复按钮"

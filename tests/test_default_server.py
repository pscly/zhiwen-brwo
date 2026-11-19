"""首次使用默认连 3.5，不用先填地址。

用户要求（2026-11-19）：「默认就让他直接用 3.5 的那个，如果要修改的话再手动改，
默认打开直接就尝试连接 3.5。如果连不上，再提醒他是否修改。」

原来的行为：`server_var` 初值是 `cfg.get("server") or ""` —— 首次安装是空串，
必须手填地址才能连，等于"初次使用还要先知道服务器在哪"。
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_default_server_constant_is_3_5():
    from zw_core.client import DEFAULT_SERVER

    assert DEFAULT_SERVER == "192.168.3.5:8700", DEFAULT_SERVER


def test_gui_prefills_default_server():
    """GUI 的地址框初值必须是默认服务器，而不是空串。"""
    src = (ROOT / "core" / "src" / "zw_core" / "gui.py").read_text(encoding="utf-8")
    assert 'cfg.get("server") or DEFAULT_SERVER' in src, "地址框没预填默认值"
    assert "DEFAULT_SERVER" in src.split("from zw_core.client import")[1][:600], "没导入常量"


def test_unreachable_default_gives_actionable_hint():
    """连不上默认服务器时，要明确告诉用户"改哪个框、怎么改"。"""
    src = (ROOT / "core" / "src" / "zw_core" / "gui.py").read_text(encoding="utf-8")
    seg = src[src.index('if phase == "probe_failed"'):]
    seg = seg[: seg.index('if phase == "need_token"')] if 'if phase == "need_token"' in seg else seg
    assert "默认连的是" in seg, "没说明默认连的是哪台"
    assert "输入框" in seg or "地址" in seg, "没告诉用户去哪里改"

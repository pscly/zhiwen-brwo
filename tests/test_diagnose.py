"""`diagnose`：core 起不来时也能体检（**不依赖任何服务在跑**）。

## 为什么需要（2026-11-20 卡了我好几轮）

现有 `doctor` 走的是 `POST /api/v1/maintenance/doctor` —— 那是**正在跑的 core** 在体检。
而用户最需要体检的时刻恰恰是「core 起不来」，那时 doctor 连不上，等于没有。

云电脑上就卡在这：core 不可达，我这边什么都做不了，用户也不知道去哪看。

`diagnose` 查的都是不需要服务的事实：配置能否读出、数据目录能否写、
端口被谁占、内核在不在、日志尾部。
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "core" / "src" / "zw_core" / "cli.py"


def _src() -> str:
    return CLI.read_text(encoding="utf-8")


def test_diagnose_is_registered():
    s = _src()
    assert 'sub.add_parser(\n        "diagnose"' in s or '"diagnose"' in s, "没注册子命令"
    assert "def cmd_diagnose" in s
    assert 'sp.set_defaults(func=cmd_diagnose)' in s, "注册了但没绑定函数"


def test_diagnose_does_not_touch_a_running_service():
    """核心要求：**不许**走 `_call`（那会连 127.0.0.1 的 core）。"""
    tree = ast.parse(_src())
    fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cmd_diagnose":
            fn = node
    assert fn is not None
    calls = {getattr(n.func, "id", None) for n in ast.walk(fn) if isinstance(n, ast.Call)}
    assert "_call" not in calls, "diagnose 不能依赖正在运行的 core"


def test_diagnose_covers_the_key_facts():
    """要查的关键项一个都不能少。"""
    s = _src()
    seg = s[s.index("def cmd_diagnose"):]
    seg = seg[: seg.index("def cmd_doctor")] if "def cmd_doctor" in seg else seg
    for want, why in (
        ("配置文件", "坏 JSON 会让机器码变化"),
        ("数据目录", "不可写则 core 起不来"),
        ("port_owner_pid", "端口可能被旧实例占着"),
        ("find_chrome", "没内核则建环境报错"),
        ("zhiwen.log", "真正的报错通常在日志尾部"),
    ):
        assert want in seg, f"没检查「{want}」（{why}）"


def test_diagnose_accepts_port_override():
    s = _src()
    assert '"--port"' in s, "不能指定端口（排查端口冲突时必需）"

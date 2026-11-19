"""新客户端不能"静默接管"旧版本的 core。

## 这是 2026-11-19 云电脑上折腾了半天的真凶

用户反复反馈「装了新版本，版本号还是旧的」，我一直以为是他没装成功。
查下来是这样：

1. 他之前手动双击过一个**便携版**（在 Downloads 里），它一直占着 8730
2. 之后每次覆盖安装新版本，新客户端启动时**只探了 `/healthz` 就接管了**那个旧 core
3. 于是永远跑旧代码，界面上一切正常 —— 比报错更糟

修法是两件事：
- 接管时**读对方版本**，不一致就在日志里大声说「升级不会生效」
- 给一个 `take_over()`：只杀**监听该端口且进程名含 zhiwen** 的那个进程，然后
  用自己的版本重启（不是本程序一律拒绝，绝不误杀）
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "core" / "src" / "zw_core" / "client.py"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def test_attach_records_remote_version():
    """接管时必须读对方版本并记下来（否则界面无从解释"版本没变"）。"""
    s = _src()
    assert "attached_version" in s
    assert "_remote_version" in s, "没有读对方版本的实现"
    assert "other != __version__" in s, "没有做版本比较"


def test_attach_warns_loudly_on_mismatch():
    """版本不一致要打 WARNING，而不是安静地复用。"""
    s = _src()
    tree = ast.parse(s)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name == "warning":
                seg = ast.get_source_segment(s, node) or ""
                if "复用" in seg and "旧版本" in seg:
                    found = True
    assert found, "没有『复用了一个旧版本』的 warning 日志"


def test_take_over_only_kills_our_own_process():
    """`take_over` 必须校验进程名，非本程序一律拒绝。

    这条是安全边界：自动关进程很容易误伤，只允许关名字里带 `zhiwen` 的。
    """
    s = _src()
    assert "def take_over" in s
    seg = s[s.index("def take_over"):]
    seg = seg[: seg.index("\n    @staticmethod")] if "\n    @staticmethod" in seg else seg
    assert "port_owner_pid" in seg, "没有查端口归属"
    assert 'zhiwen' in seg and "not in name.lower()" in seg, "没有做进程名白名单校验"


def test_take_over_reuses_existing_helpers():
    """复用 `engines.port_owner_pid` —— 别再写一套 netstat 解析。"""
    s = _src()
    assert "from .services import engines as _engines" in s
    assert "_engines.port_owner_pid(port)" in s


def test_gui_surfaces_the_mismatch_and_offers_takeover():
    """桌面 GUI 要当场提示并给一键接管，否则用户永远升不上去。"""
    gui = (ROOT / "core" / "src" / "zw_core" / "gui.py").read_text(encoding="utf-8")
    assert "attached_version" in gui
    assert "take_over" in gui
    assert "升级还没生效" in gui or "升级不会生效" in gui

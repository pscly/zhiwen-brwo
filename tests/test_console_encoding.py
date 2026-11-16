"""Windows 控制台编码兜底。

这不是理论问题：CI 上真挂过两次（测试脚本、安装脚本静态检查），
而用户 Windows 机器上的第一条命令 `zw doctor` 会输出 ✅，
在 cp936/cp1252 控制台上必然 UnicodeEncodeError。
"""

from __future__ import annotations

import io
import sys

from zw_core.console import is_windows, setup_console


class FakeStream:
    """可以记录 reconfigure 调用的假 stdout。

    不能继承 io.StringIO：它的 encoding 是只读属性，赋值会
    AttributeError（这正是"用真流做测试容易踩的坑"，干脆不用真流）。
    """

    def __init__(self, encoding: str = "cp1252") -> None:
        self.encoding = encoding
        self.reconfigured: list[dict] = []

    def reconfigure(self, **kw):
        self.reconfigured.append(kw)
        if "encoding" in kw:
            self.encoding = str(kw["encoding"])

    def write(self, _s): return 0
    def flush(self): pass


def test_setup_console_switches_non_utf8_stream(monkeypatch):
    out = FakeStream("cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    changed = setup_console()
    assert changed is True
    assert out.reconfigured, "非 UTF-8 的流必须被 reconfigure"
    assert out.reconfigured[0]["encoding"] == "utf-8"
    assert out.reconfigured[0]["errors"] == "replace", "编不了的字要降级，不能崩"


def test_setup_console_leaves_utf8_alone(monkeypatch):
    out = FakeStream("utf-8")
    monkeypatch.setattr(sys, "stdout", out)
    assert setup_console() is False
    assert not out.reconfigured


def test_setup_console_force_overrides(monkeypatch):
    out = FakeStream("utf-8")
    monkeypatch.setattr(sys, "stdout", out)
    assert setup_console(force=True) is True
    assert out.reconfigured


def test_setup_console_survives_stream_without_reconfigure(monkeypatch):
    """pytest 的 capture 流、某些包装流没有 reconfigure，不能因此崩。"""

    class NoReconfigure:
        encoding = "cp1252"

    monkeypatch.setattr(sys, "stdout", NoReconfigure())
    monkeypatch.setattr(sys, "stderr", NoReconfigure())
    assert setup_console() is False   # 什么都没改成，但也没抛


def test_setup_console_handles_none_stream(monkeypatch):
    """pythonw / GUI 子系统下 sys.stdout 可能是 None。"""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert setup_console() is False


def test_cli_main_installs_console_fix(monkeypatch):
    """CLI 入口必须先修编码，再做别的事。"""
    import zw_core.cli as cli_mod

    called = {"n": 0}

    def fake() -> bool:
        called["n"] += 1
        return True

    monkeypatch.setattr("zw_core.console.setup_console", fake)
    monkeypatch.setattr(sys, "argv", ["zw", "--version"])
    try:
        cli_mod.main()
    except SystemExit:
        pass                      # --version 会退出，正常
    assert called["n"] == 1, "main() 里没有先调用 setup_console"


def test_is_windows_is_bool():
    assert isinstance(is_windows(), bool)

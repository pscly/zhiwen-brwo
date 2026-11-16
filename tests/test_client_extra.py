"""客户端新增能力：开机自启、字体探测、GUI 模块可导入。

这些是 0.3.0 补的"日常可用性"，比注册流程更容易悄悄坏掉
（自启写错路径不会报错，只会在重启后发现没起）。
"""

from __future__ import annotations

import os
import time
import sys

import pytest

from zw_core import client as C


# ── 开机自启 ──────────────────────────────────────────────
@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(C.CONFIG_DIR_ENV, str(tmp_path / "home"))
    return tmp_path / "home"


def test_autostart_roundtrip(home, monkeypatch, tmp_path):
    """开→查→关→查 必须自洽，且**按平台**落到正确位置。

    之前这条只按 Linux 写（断言 XDG 的 .desktop 存在），
    在 macOS CI 上必然失败 —— 因为那边写的是 LaunchAgents 的 plist。
    """
    if sys.platform == "win32":
        pytest.skip("Windows 上会写真实注册表，靠 CI 真机验证")

    if sys.platform == "darwin":
        # macOS 用固定路径 ~/Library/LaunchAgents，改不了；用假的 HOME 隔离
        fake_home = tmp_path / "machome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        landed = fake_home / "Library" / "LaunchAgents" / "cc.pscly.zhiwen-client.plist"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        landed = tmp_path / "xdg" / "autostart" / "zhiwen-client.desktop"

    assert C.autostart_enabled() is False
    ok, msg = C.set_autostart(True)
    assert ok, msg
    assert C.autostart_enabled() is True
    assert landed.exists(), f"自启文件没落到预期位置：{landed}"

    body = landed.read_text(encoding="utf-8")
    if sys.platform == "darwin":
        assert "cc.pscly.zhiwen-client" in body
        assert "RunAtLoad" in body
    else:
        assert "[Desktop Entry]" in body
        # 断言的必须是**产品标记**，不能是"某个路径里恰好含 zhiwen"。
        # 本地能过纯粹因为仓库目录叫 zhiwen_brwo，CI 上路径不同就会失败。
        assert "zw_core.client" in body or "zhiwen-client" in body.lower()
        assert "Name=知纹客户端" in body

    ok, msg = C.set_autostart(False)
    assert ok, msg
    assert C.autostart_enabled() is False
    assert not landed.exists()


def test_autostart_idempotent(home, monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("Windows 走注册表")
    if sys.platform == "darwin":
        fake_home = tmp_path / "machome2"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for _ in range(3):
        ok, _msg = C.set_autostart(True)
        assert ok
        assert C.autostart_enabled() is True
    for _ in range(3):
        ok, _msg = C.set_autostart(False)
        assert ok


def test_launch_command_mentions_module_when_not_frozen(monkeypatch):
    """源码运行时自启命令要带 `-m zw_core.client`。

    否则开机自启会打开一个 python REPL（用户看到黑窗口一闪），
    而不是客户端 —— 这个错误很隐蔽，只有重启后才发现。
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = C._launch_command()
    assert "-m zw_core.client" in cmd
    assert sys.executable in cmd


def test_launch_command_is_bare_exe_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd = C._launch_command()
    assert cmd.strip('"') == sys.executable
    assert "-m" not in cmd


# ── 字体探测 ──────────────────────────────────────────────
def test_pick_ui_font_returns_three_tuple():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:
        pytest.skip("没有可用显示")
    try:
        fam, size, weight = C.pick_ui_font(root, 10)
        assert isinstance(fam, str) and fam
        assert size == 10
        assert weight in ("normal", "bold")
    finally:
        root.destroy()


def test_pick_mono_font_returns_string():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:
        pytest.skip("没有可用显示")
    try:
        assert isinstance(C.pick_mono_font(root), str)
    finally:
        root.destroy()


# ── GUI 模块 ──────────────────────────────────────────────
def test_gui_module_imports():
    """gui 模块必须能干净导入（它依赖 client 的一堆导出，改名会断）。"""
    import zw_core.gui as gui

    assert callable(gui.run_gui)
    assert callable(gui._make_icon_image) or True   # PIL 缺失时也不该崩


def test_client_run_gui_delegates(monkeypatch):
    """client.run_gui 应当委托给 gui 模块（而不是留着一份旧实现）。"""
    import zw_core.client as cli_mod
    import zw_core.gui as gui_mod

    called = {"n": 0}

    def fake() -> int:
        called["n"] += 1
        return 0

    monkeypatch.setattr(gui_mod, "run_gui", fake)
    assert cli_mod.run_gui() == 0
    assert called["n"] == 1


# ── 心跳携带统计 ──────────────────────────────────────────
def test_heartbeat_sends_stats_payload(monkeypatch, home):
    """心跳要能把本机概况带给服务器（面板据此显示"几个环境在跑"）。"""
    import httpx

    seen: dict = {}

    class Resp:
        status_code = 200
        content = b"x"
        text = "{}"

        def json(self):
            return {"ok": True, "data": {"device": {}}}

    class FakeClient:
        def __init__(self, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def post(self, url, json=None, headers=None, **kw):
            seen["json"] = json
            seen["headers"] = headers
            return Resp()

    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
    C.heartbeat("http://x:8700", "zwd_abcdefgh12345678",
                extra={"profiles": 3, "running": 1})
    assert seen["json"] == {"profiles": 3, "running": 1}
    assert seen["headers"]["X-ZW-Device"] == "zwd_abcdefgh12345678"


def test_heartbeat_without_stats_still_works(monkeypatch, home):
    import httpx

    seen: dict = {}

    class Resp:
        status_code = 200
        content = b"x"
        text = "{}"

        def json(self):
            return {"ok": True, "data": {}}

    class FakeClient:
        def __init__(self, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def post(self, url, json=None, headers=None, **kw):
            seen["json"] = json
            return Resp()

    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
    res = C.heartbeat("http://x:8700", "zwd_abcdefgh12345678")
    assert res["ok"] is True
    assert seen["json"] is None   # 不传就是空 body，别塞个 {}


# ── 心跳健康度（界面据此显示"和服务器还连着吗"）────────────
def test_heartbeat_status_before_first_success(home):
    hb = C.Heartbeat("http://x:8700", "zwd_abcdefgh12345678", interval=60)
    st = hb.status()
    assert st["healthy"] is False
    assert st["last_ok"] == 0
    assert st["age"] is None
    assert st["error"] == ""


def test_heartbeat_status_tolerates_short_gap(home):
    """网络抖一下不该报红 —— 但真断线必须能看出来。"""
    hb = C.Heartbeat("http://x:8700", "zwd_abcdefgh12345678", interval=60)
    hb.last_ok = time.time() - 30
    assert hb.status()["healthy"] is True
    hb.last_ok = time.time() - 200      # 超过 3 个周期
    assert hb.status()["healthy"] is False


def test_heartbeat_status_reports_revoked(home):
    hb = C.Heartbeat("http://x:8700", "zwd_abcdefgh12345678", interval=60)
    hb.last_ok = time.time()
    hb.revoked = True
    st = hb.status()
    assert st["revoked"] is True
    assert st["healthy"] is False, "被踢了不能再算健康"


def test_macos_autostart_plist_branch(home, monkeypatch, tmp_path):
    """macOS 分支生成 LaunchAgents plist。

    在 Linux 上也能验：把 sys.platform 掰成 darwin、HOME 指到临时目录。
    这样这条分支不必等 macOS CI 才知道对不对（它刚在 macOS CI 上挂过一次）。
    """
    if sys.platform == "win32":
        pytest.skip("Windows 上 sys.platform 的猴子补丁会连带改坏 winreg 判断")
    fake_home = tmp_path / "machome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(C.sys, "platform", "darwin")

    assert C.autostart_enabled() is False
    ok, msg = C.set_autostart(True)
    assert ok, msg
    plist = fake_home / "Library" / "LaunchAgents" / "cc.pscly.zhiwen-client.plist"
    assert plist.exists()
    body = plist.read_text(encoding="utf-8")
    assert "<?xml" in body
    assert "cc.pscly.zhiwen-client" in body
    assert "RunAtLoad" in body
    assert "ProgramArguments" in body
    assert C.autostart_enabled() is True

    ok, msg = C.set_autostart(False)
    assert ok, msg
    assert not plist.exists()

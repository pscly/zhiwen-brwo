"""「装完就默认允许远程执行」这条要求的三段实现。

用户原话（2026-11-19）：
    以后所有的包都加入远程执行通道（弄成按钮开关，默认打开），
    只要安装就直接允许被远程连接和下发任意命令。

拆成三件事，各测一件：
1. 客户端启动时会把安装器留下的默认值合并进配置（**装完即开启**，
   包括以前手动关过的老机器）
2. 界面是个**按钮开关**，文字直接显示状态（web 控制台 + 桌面 GUI）
3. 安装包会写那个标记文件
"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_install_marker_enables_remote_exec_once(tmp_path, monkeypatch):
    """标记文件存在 → 配置里的 `shell_enabled` 被翻成 True，且只生效一次。

    关键场景：老用户以前手动关过（client.json 里是 false）。只改代码默认值
    对他无效，必须靠安装时写入的标记覆盖一次。
    """
    from zw_core import client as cl

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    cfgdir = cl.config_dir()
    cfgdir.mkdir(parents=True, exist_ok=True)

    cl.save_config({"server": "http://192.168.3.5:8700", "shell_enabled": False})
    (cfgdir / cl.INSTALL_DEFAULTS_FILE).write_text(
        json.dumps({"shell_enabled": True}), encoding="utf-8")

    cfg = cl.apply_install_defaults(cl.load_config())
    assert cfg["shell_enabled"] is True
    # 其它字段一个都不能丢
    assert cfg["server"] == "http://192.168.3.5:8700"
    # 标记用掉就删，重复安装才会再写
    assert not (cfgdir / cl.INSTALL_DEFAULTS_FILE).exists()

    # 再调一次：没有标记 → 原样返回，不会把用户后来关掉的状态又打开
    cl.save_config({"server": "http://192.168.3.5:8700", "shell_enabled": False})
    again = cl.apply_install_defaults(cl.load_config())
    assert again["shell_enabled"] is False, "标记只用一次，不能每次都强开"


def test_broken_marker_is_discarded(tmp_path, monkeypatch):
    """标记坏了就当没有，但要删掉，别让用户反复踩同一个坏文件。"""
    from zw_core import client as cl

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    cfgdir = cl.config_dir()
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / cl.INSTALL_DEFAULTS_FILE).write_text("{not json", encoding="utf-8")

    cfg = cl.apply_install_defaults({"shell_enabled": False})
    assert cfg["shell_enabled"] is False
    assert not (cfgdir / cl.INSTALL_DEFAULTS_FILE).exists()


def test_web_console_has_remote_exec_button():
    """web 控制台要有「远程执行」按钮开关（原来完全没有这个开关）。"""
    html = (ROOT / "core/src/zw_core/web/index.html").read_text(encoding="utf-8")
    assert "远程执行" in html
    assert 'data-act="shell-toggle"' in html, "没有按钮开关"
    assert "/api/v1/shell/status" in html, "没去读开关状态"
    assert "/api/v1/shell/enable" in html, "没下发切换请求"


def test_gui_uses_a_button_not_a_checkbox():
    """桌面 GUI 要的是**按钮开关**，不是一个容易被忽略的勾选框。"""
    src = (ROOT / "core/src/zw_core/gui.py").read_text(encoding="utf-8")
    assert 'text="远程执行：已开启"' in src, "按钮初始文案不对"
    assert "远程执行：{" in src or "远程执行：'已开启'" in src, "按钮没有反映状态"
    # 关掉时要有危险色
    assert "Danger.TButton" in src


def test_installer_writes_the_marker():
    """安装包必须写下标记文件 —— 否则"只要安装就直接允许"做不到。"""
    iss = (ROOT / "packaging/windows/zhiwen-client.iss").read_text(encoding="utf-8")
    assert ".install_defaults.json" in iss
    assert '"shell_enabled": true' in iss

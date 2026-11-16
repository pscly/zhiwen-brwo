"""安装/运行路径必须是纯英文。

用户明确要求："安装目录到最后居然还有中文路径出现，这个不可以接受，
一定要是纯英文路径才行"。

这条约束容易在以后被无意破坏（比如有人把 DefaultDirName 改回中文，
或者给 macOS 的 .app 起个中文名），所以用测试钉住三件事：

1. Windows 安装脚本里所有会落盘的指令都是 ASCII
2. macOS 的 .app 文件名是 ASCII（显示名可以中文）
3. 客户端自己的配置目录、数据目录名是 ASCII
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ISS = REPO / "packaging" / "windows" / "zhiwen-client.iss"
SPEC = REPO / "packaging" / "zhiwen-client.spec"


def test_installer_script_exists():
    assert ISS.exists(), "Windows 安装脚本不见了"


def test_installer_paths_are_ascii():
    """安装目录/开始菜单组/快捷方式名都必须是 ASCII。

    中文只能出现在显示名（AppName）与注释里。
    """
    text = ISS.read_text(encoding="utf-8")
    offenders: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(";") or not stripped:
            continue
        lowered = stripped.lower()
        # 只检查"值会落到磁盘路径"的指令
        is_path_directive = (
            lowered.startswith("defaultdirname=")
            or lowered.startswith("defaultgroupname=")
            or lowered.startswith("outputbasefilename=")
            or lowered.startswith("name:")
            or "destname:" in lowered
            or "destdir:" in lowered
        )
        if not is_path_directive:
            continue
        # Name: 后面跟 Filename: 时，Filename 是源程序路径（也是我们要管的）
        if not stripped.isascii():
            offenders.append(stripped)
    assert not offenders, "这些指令会产生中文路径：\n" + "\n".join(offenders[:8])


def test_installer_uses_english_dir_name():
    """安装目录必须以 ZhiwenClient 结尾，不能是中文。"""
    text = ISS.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("DefaultDirName="))
    assert "{#MyAppDirName}" in line, f"应该用 ASCII 的目录名常量，实际：{line}"
    define = next(ln for ln in text.splitlines() if ln.strip().startswith("#define MyAppDirName"))
    assert define.split('"')[1].isascii(), f"目录名常量必须 ASCII：{define}"


def test_display_name_may_be_chinese():
    """显示名保留中文 —— 路径英文不等于界面英文。"""
    text = ISS.read_text(encoding="utf-8")
    define = next(ln for ln in text.splitlines() if ln.strip().startswith("#define MyAppName"))
    value = define.split('"')[1]
    assert not value.isascii(), "AppName 应该保留中文显示名（用户看到的是它）"


def test_macos_bundle_path_is_ascii():
    """.app 的文件名是路径，必须 ASCII；显示名放 CFBundleDisplayName。"""
    spec = SPEC.read_text(encoding="utf-8")
    assert 'name="ZhiwenClient.app"' in spec, "macOS .app 的路径名必须是 ASCII"
    assert "CFBundleDisplayName" in spec, "中文名应该放在 CFBundleDisplayName 里"
    # 确认没有残留的中文 .app 名
    assert "知纹客户端.app" not in spec


def test_client_config_dir_is_ascii():
    """配置目录名（%LOCALAPPDATA%\\zhiwen-client）不能有中文。"""
    from zw_core import client as C

    name = C.config_dir().name
    assert name.isascii(), f"配置目录名含非 ASCII：{name!r}"


def test_default_data_dir_is_ascii():
    """默认数据目录名（...\\zhiwen）不能有中文。"""
    from zw_core import config as cfg

    p = cfg._default_root()
    assert p.name.isascii(), f"数据目录名含非 ASCII：{p.name!r}"


def test_autostart_artifacts_are_ascii():
    """开机自启生成的文件名/注册表值名都必须是 ASCII。"""
    from zw_core import client as C

    assert C._AUTOSTART_NAME.isascii()
    assert C._AUTOSTART_WIN_NAME.isascii()


def test_selftest_warns_on_chinese_path(tmp_path, monkeypatch):
    """路径里有中文时：**只告警，不判失败**。

    为什么不能判失败：Windows 用户名是中文时，%LOCALAPPDATA% 那一段
    用户改不了。判死只会让人以为程序坏了，实际问题在系统账户名。
    """
    from zw_core import client as C

    home = tmp_path / "中文目录" / "client"
    monkeypatch.setenv(C.CONFIG_DIR_ENV, str(home))
    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "中文目录" / "root"))
    from zw_core import config as cfg

    cfg.reset_settings()
    try:
        code = C.selftest(show_dialog=False)
        report = (home / "selftest.txt").read_text(encoding="utf-8")
        assert code == 0, "只该告警，不该返回非 0"
        assert "WARN" in report
        assert "纯英文" in report
    finally:
        cfg.reset_settings()

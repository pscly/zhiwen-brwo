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


def test_upgrade_does_not_reuse_previous_dir():
    """升级不能沿用上次的安装目录。

    这是"改了 DefaultDirName 但路径还是中文"的真正原因：Inno 默认
    UsePreviousAppDir=yes，会从注册表读上次装在哪，然后原样装回去。
    只改默认目录名对**升级用户**完全无效。
    """
    text = ISS.read_text(encoding="utf-8")
    lower = text.lower()
    assert "usepreviousappdir=no" in lower, "必须显式关掉，否则升级会沿用旧目录"
    assert "usepreviousgroup=no" in lower, "开始菜单组同理"


def test_installer_cleans_legacy_chinese_dir():
    """安装时要清掉老版本留在中文目录里的文件。

    不清理的话，升级后会有两份文件：卸载项指向新目录，
    旧目录里的文件再也删不掉（用户只会看到磁盘里莫名其妙多一个中文文件夹）。
    """
    text = ISS.read_text(encoding="utf-8")
    assert "[InstallDelete]" in text
    assert r'Type: filesandordirs; Name: "{autopf}\知纹客户端"' in text
    assert r'Type: filesandordirs; Name: "{group}\知纹客户端"' in text


def test_install_delete_only_targets_known_legacy_paths():
    """[InstallDelete] 里的中文路径必须是已知的历史遗留，不能随手加。

    这一段是 C36（纯 ASCII）的**精确例外**：它只做删除、不创建路径。
    这里把例外限制成"只能是那几个旧名字"，防止以后有人往里塞新中文路径。
    """
    text = ISS.read_text(encoding="utf-8")
    block = text.split("[InstallDelete]", 1)[1].split("[", 1)[0]
    allowed = {
        r'"{autopf}\知纹客户端"',
        r'"{group}\知纹客户端"',
        r'"{autodesktop}\知纹客户端.lnk"',
    }
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("Type:"):
            continue
        value = line.split("Name:", 1)[1].strip()
        assert value in allowed, f"InstallDelete 出现了未预期的路径：{line}"

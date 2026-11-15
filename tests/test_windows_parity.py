"""Windows 上的行为必须能离线验到。

这台开发机是 Linux，但用户明天在 Windows 上用。所以凡是"平台相关但可以纯计算"
的部分，都用 monkeypatch 把 IS_WIN 掰成 True 来验形状；凡是"必须真机"的部分
（DPAPI、taskkill、Get-CimInstance 的实际输出）在文档里明确标为未验证，不在这里假装测过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zw_core.services import engines


def _profile(**kw):
    base = {
        "id": "p", "name": "n", "fp_seed": 999, "fp_platform": "host",
        "fp_platform_version": "", "fp_brand": "", "fp_brand_version": "",
        "fp_hardware": None, "fp_timezone": "", "fp_lang": "", "fp_accept_lang": "",
        "fp_disable_spoofing": "", "fp_ua": "",
        "proxy": "", "window_w": 1280, "window_h": 800,
        "extensions": [], "start_urls": [], "headless": 0, "cdp_enabled": 1,
    }
    base.update(kw)
    return base


def test_windows_paths_in_cmdline(monkeypatch, tmp_path):
    """Windows 路径里带反斜杠与盘符，命令行必须原样保留。"""
    monkeypatch.setattr(engines, "IS_WIN", True)
    monkeypatch.setattr(engines, "_is_root", lambda: False)
    udd = Path(r"C:\Users\me\AppData\Local\zhiwen\profiles\abc\udd")
    cmd = engines.build_cmdline(Path(r"C:\chrome\chrome.exe"), udd, 21001, _profile())
    assert cmd[0] == r"C:\chrome\chrome.exe"
    assert f"--user-data-dir={udd}" in cmd


def test_windows_no_sandbox_not_added_for_normal_user(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "IS_WIN", True)
    monkeypatch.setattr(engines, "_is_root", lambda: False)
    cmd = engines.build_cmdline(Path("chrome.exe"), tmp_path, 21001, _profile())
    assert "--no-sandbox" not in cmd
    assert "--disable-dev-shm-usage" not in cmd, "这个参数是 Linux 容器用的，Windows 上没意义"


def test_windows_headless_only_when_requested(monkeypatch, tmp_path):
    monkeypatch.setattr(engines, "IS_WIN", True)
    monkeypatch.setattr(engines, "_needs_headless", lambda: False)
    cmd = engines.build_cmdline(Path("chrome.exe"), tmp_path, 21001, _profile())
    assert "--headless=new" not in cmd, "Windows 有桌面，不该自动 headless"

    cmd2 = engines.build_cmdline(Path("chrome.exe"), tmp_path, 21001, _profile(headless=1))
    assert "--headless=new" in cmd2


def test_windows_child_env_does_not_set_tz(monkeypatch):
    """Windows 不认 TZ 环境变量，时区只能靠 --timezone 参数。"""
    monkeypatch.setattr(engines, "IS_WIN", True)
    env = engines.child_env({"fp_timezone": "Asia/Shanghai", "fp_lang": "zh-CN"})
    assert "TZ" not in env or env.get("TZ") != "Asia/Shanghai"


def test_extract_udd_windows_style():
    cmd = r'C:\chrome\chrome.exe --user-data-dir=C:\Users\a\zd\profiles\x\udd --remote-debugging-port=1'
    assert engines._extract_udd(cmd) == r"C:\Users\a\zd\profiles\x\udd"


def test_extract_udd_quoted_windows_path():
    cmd = r'chrome.exe "--user-data-dir=C:\Program Files\zd\udd" --type=gpu'
    # 引号包裹时 strip('"') 会去掉，但 --type= 说明这是子进程
    assert engines._extract_udd(cmd) == r"C:\Program Files\zd\udd"
    assert engines._is_child_process(cmd) is True


def test_port_free_probe_works():
    """端口探测在 Windows 上走同一段 socket 代码，这里只是确认它不抛异常。"""
    assert engines._port_free(1) in (True, False)


def test_extension_list_serialization(monkeypatch, tmp_path):
    """extensions 字段可能是 JSON 字符串（数据库里）或 list（内存里），两边都要能解析。"""
    ext = tmp_path / "e"
    ext.mkdir()
    p = _profile(extensions=json.dumps([str(ext)]))
    cmd = engines.build_cmdline(Path("chrome.exe"), tmp_path, 21001, p)
    load = [a for a in cmd if a.startswith("--load-extension=")]
    assert load and str(ext) in load[0]

    p2 = _profile(extensions="不是 JSON")
    cmd2 = engines.build_cmdline(Path("chrome.exe"), tmp_path, 21001, p2)
    assert not [a for a in cmd2 if a.startswith("--load-extension=")], "坏数据要静默跳过，不能崩"


def test_start_urls_bad_json(monkeypatch, tmp_path):
    cmd = engines.build_cmdline(Path("chrome.exe"), tmp_path, 21001, _profile(start_urls="{bad"))
    assert cmd[-1] == "about:blank", "坏 URL 数据要退回 about:blank，而不是把程序搞崩"

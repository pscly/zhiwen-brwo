"""跨平台命令行拼装与端口池。

重点验证 Windows 分支 —— 这台开发机是 Linux，但代码必须两边都对。
所以用 monkeypatch 把 IS_WIN 掰成 True 来验命令行形状。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zw_core.services import engines


@pytest.fixture()
def fake_profile() -> dict:
    return {
        "id": "p1", "name": "测试", "fp_seed": 123456,
        "fp_platform": "host", "fp_brand": "", "fp_brand_version": "",
        "fp_hardware": None, "fp_lang": "zh-CN", "fp_ua": "",
        "proxy": "http://127.0.0.1:7890", "window_w": 1280, "window_h": 800,
        "headless": 0, "cdp_enabled": 1, "extensions": [], "start_urls": [],
    }


BASE_ARGS_MUST_HAVE = ["--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*"]


def test_cmdline_has_fingerprint_and_gateway_origin(fake_profile, tmp_path):
    # 用 tmp_path 而不是写死 "/tmp/udd"：在 Windows 上 Path("/tmp/udd")
    # 会被 str() 成 "\tmp\udd"，写死的断言必然失败（CI 上真的因此挂过）。
    udd = tmp_path / "udd"
    cmd = engines.build_cmdline(Path("/usr/bin/chrome"), udd, 21001, fake_profile)
    joined = " ".join(cmd)
    assert "--fingerprint=123456" in joined
    assert "--remote-debugging-port=21001" in joined
    assert f"--user-data-dir={udd}" in joined
    assert "--proxy-server=http://127.0.0.1:7890" in joined
    assert "--lang=zh-CN" in joined
    assert "--window-size=1280,800" in joined
    for must in BASE_ARGS_MUST_HAVE:
        assert must in cmd, f"缺少基础参数 {must}"


def test_cross_os_platform_flag(fake_profile):
    fake_profile["fp_platform"] = "windows"
    cmd = engines.build_cmdline(Path("chrome"), Path("/tmp/u"), 21002, fake_profile)
    assert "--fingerprint-platform=windows" in cmd
    # host 时不应该出现这个参数
    fake_profile["fp_platform"] = "host"
    cmd = engines.build_cmdline(Path("chrome"), Path("/tmp/u"), 21002, fake_profile)
    assert not any(a.startswith("--fingerprint-platform") for a in cmd)


def test_cdp_disabled_drops_port(fake_profile):
    fake_profile["cdp_enabled"] = 0
    cmd = engines.build_cmdline(Path("chrome"), Path("/tmp/u"), 21003, fake_profile)
    assert not any(a.startswith("--remote-debugging-port") for a in cmd)


def test_missing_extension_paths_are_dropped(fake_profile, tmp_path: Path):
    real = tmp_path / "ext"
    real.mkdir()
    fake_profile["extensions"] = [
        {"path": str(real)},
        {"path": "/不存在的/扩展"},
    ]
    cmd = engines.build_cmdline(Path("chrome"), Path("/tmp/u"), 21004, fake_profile)
    load = [a for a in cmd if a.startswith("--load-extension=")]
    assert load and str(real) in load[0]
    assert "不存在的" not in load[0]


def test_windows_branch_uses_no_sandbox_only_when_root(monkeypatch, fake_profile):
    """Windows 上不应该因为 _is_root 而加 --no-sandbox。"""
    monkeypatch.setattr(engines, "IS_WIN", True)
    monkeypatch.setattr(engines, "_is_root", lambda: False)
    cmd = engines.build_cmdline(Path("chrome.exe"), Path("C:/u"), 21005, fake_profile)
    assert "--no-sandbox" not in cmd

    # Linux + root 时必须加
    monkeypatch.setattr(engines, "IS_WIN", False)
    monkeypatch.setattr(engines, "_is_root", lambda: True)
    cmd = engines.build_cmdline(Path("chrome"), Path("/u"), 21006, fake_profile)
    assert "--no-sandbox" in cmd
    assert "--disable-dev-shm-usage" in cmd


def test_needs_headless(monkeypatch):
    monkeypatch.setattr(engines, "IS_WIN", True)
    assert engines._needs_headless() is False, "Windows 永远不需要 headless"

    monkeypatch.setattr(engines, "IS_WIN", False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert engines._needs_headless() is True, "无图形会话应降级 headless"

    monkeypatch.setenv("DISPLAY", ":0")
    assert engines._needs_headless() is False


def test_headless_profile_adds_flag(fake_profile):
    fake_profile["headless"] = 1
    cmd = engines.build_cmdline(Path("chrome"), Path("/tmp/u"), 21007, fake_profile)
    assert "--headless=new" in cmd


def test_port_pool_allocate_release():
    pool = engines.PortPool(31000, 31002)   # 只有 3 个
    a = pool.allocate("p1")
    b = pool.allocate("p2")
    c = pool.allocate("p3")
    assert len({a, b, c}) == 3
    with pytest.raises(engines.EngineError) as exc:
        pool.allocate("p4")
    assert exc.value.code == "PORT_EXHAUSTED"
    pool.release(a)
    assert pool.allocate("p4") == a


def test_find_chrome_explicit_path(tmp_path: Path, monkeypatch):
    fake = tmp_path / ("chrome.exe" if engines.IS_WIN else "chrome")
    fake.write_bytes(b"#!/bin/sh\n")
    os.chmod(fake, 0o755)

    from zw_core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "chrome_bin", str(fake))
    assert engines.find_chrome(s) == fake

    monkeypatch.setattr(s, "chrome_bin", str(tmp_path / "不存在"))
    with pytest.raises(engines.EngineError) as exc:
        engines.find_chrome(s)
    assert exc.value.code == "ENGINE_NOT_FOUND"

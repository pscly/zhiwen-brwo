"""引擎层补充：端口归属校验、时区/语言环境、扩展解析、锁文件清理。

这一组对应 0.2.0 修掉的几个"会毁账号"的问题：
- 端口被别的 chrome 占用时，绝不能把别人的 ws 登记成自己的路由（串号）；
- 异常退出留下的 SingletonLock 会让下次启动直接失败；
- fp_timezone / fp_accept_lang 以前只躺在数据库里，从来没进过命令行或环境变量。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from zw_core.services import engines


def _profile(**kw):
    base = {
        "id": "p", "name": "n", "fp_seed": 12345, "fp_platform": "host",
        "fp_platform_version": "", "fp_brand": "", "fp_brand_version": "",
        "fp_hardware": None, "fp_timezone": "", "fp_lang": "", "fp_accept_lang": "",
        "fp_disable_spoofing": "", "fp_ua": "",
        "proxy": "", "window_w": 1280, "window_h": 800,
        "extensions": [], "start_urls": [], "headless": 0, "cdp_enabled": 1,
    }
    base.update(kw)
    return base


# ── 命令行 ────────────────────────────────────────────────
def test_timezone_accept_lang_platform_version_flags(tmp_path):
    cmd = engines.build_cmdline(
        Path("/usr/bin/chrome"), tmp_path, 21001,
        _profile(fp_timezone="Asia/Shanghai", fp_lang="zh-CN", fp_accept_lang="zh-CN,en-US",
                 fp_platform="windows", fp_platform_version="15.2.0"),
    )
    joined = " ".join(cmd)
    assert "--timezone=Asia/Shanghai" in joined
    assert "--accept-lang=zh-CN,en-US" in joined
    assert "--lang=zh-CN" in joined
    assert "--fingerprint-platform=windows" in joined
    assert "--fingerprint-platform-version=15.2.0" in joined


def test_accept_lang_falls_back_to_lang(tmp_path):
    """只给 lang 时也要补 accept-lang，否则 navigator.languages 与 UA 不一致。"""
    cmd = engines.build_cmdline(Path("/usr/bin/chrome"), tmp_path, 21001, _profile(fp_lang="ja-JP"))
    assert "--accept-lang=ja-JP" in cmd


def test_disable_spoofing_flag(tmp_path):
    cmd = engines.build_cmdline(
        Path("/usr/bin/chrome"), tmp_path, 21001, _profile(fp_disable_spoofing="font,gpu")
    )
    assert "--disable-spoofing=font,gpu" in cmd


def test_cdp_always_binds_loopback(tmp_path):
    """必须显式绑回环，否则某些平台会绑 0.0.0.0 把 CDP 暴露给整个内网。"""
    cmd = engines.build_cmdline(Path("/usr/bin/chrome"), tmp_path, 21001, _profile())
    assert "--remote-debugging-address=127.0.0.1" in cmd


def test_resolved_extensions_used_when_present(tmp_path):
    ext = tmp_path / "ext"
    ext.mkdir()
    cmd = engines.build_cmdline(
        Path("/usr/bin/chrome"), tmp_path, 21001,
        _profile(_resolved_extensions=[str(ext)]),
    )
    load = [a for a in cmd if a.startswith("--load-extension=")]
    assert load and str(ext) in load[0]


# ── 子进程环境 ────────────────────────────────────────────
def test_child_env_sets_tz(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    env = engines.child_env({"fp_timezone": "America/New_York", "fp_lang": "en-US"})
    if engines.IS_WIN:
        assert "TZ" not in env          # Windows 靠 --timezone 参数，不认 TZ
    else:
        assert env["TZ"] == "America/New_York"
        assert env["LANG"].startswith("en_US")


def test_child_env_no_timezone_leaves_untouched(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Berlin")
    env = engines.child_env({"fp_timezone": ""})
    assert env.get("TZ") == "Europe/Berlin"


# ── 端口归属 ──────────────────────────────────────────────
def test_port_owner_pid_for_listening_socket():
    """起一个真监听，验证能查出 pid（Linux 上走 ss/lsof）。"""
    import socket

    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sk.bind(("127.0.0.1", 0))
    sk.listen(1)
    port = sk.getsockname()[1]
    try:
        pid = engines.port_owner_pid(port)
        # 容器里 ss 可能不可用，查不到不算失败；查到就必须是我们自己
        assert pid in (0, __import__("os").getpid())
    finally:
        sk.close()


def test_port_owner_pid_free_port():
    import socket

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    assert engines.port_owner_pid(port) == 0


def test_is_our_browser_rejects_foreign(tmp_path):
    """别人（或别的目录）的进程不能被认成我们的浏览器。"""
    root = tmp_path / "profiles"
    root.mkdir()
    assert engines.is_our_browser(1, root) is False      # pid 1 一定是别人的
    assert engines.is_our_browser(0, root) is False


def test_is_our_browser_accepts_own(tmp_path):
    """本进程命令行里没有 --user-data-dir，所以不该被认成浏览器。"""
    import os

    root = tmp_path / "profiles"
    root.mkdir()
    assert engines.is_our_browser(os.getpid(), root) is False


@pytest.mark.skipif(engines.IS_WIN, reason="Windows 上 DevToolsActivePort 语义不同")
def test_read_devtools_port(tmp_path):
    udd = tmp_path / "udd"
    udd.mkdir()
    assert engines.read_devtools_port(udd) is None
    (udd / "DevToolsActivePort").write_text("21001\n/devtools/browser/abc\n", encoding="utf-8")
    got = engines.read_devtools_port(udd)
    assert got == (21001, "/devtools/browser/abc")
    (udd / "DevToolsActivePort").write_text("garbage\n", encoding="utf-8")
    assert engines.read_devtools_port(udd) is None


# ── 锁文件 ────────────────────────────────────────────────
def test_clear_stale_locks():
    from zw_core.services import manager as mgr

    import tempfile

    udd = Path(tempfile.mkdtemp())
    for name in ("SingletonLock", "DevToolsActivePort", "lockfile"):
        (udd / name).write_text("x")
    removed = mgr._clear_stale_locks(udd)
    assert set(removed) == {"SingletonLock", "DevToolsActivePort", "lockfile"}
    assert not any((udd / n).exists() for n in removed)


def test_clear_stale_locks_does_not_touch_other_files():
    from zw_core.services import manager as mgr

    import tempfile

    udd = Path(tempfile.mkdtemp())
    (udd / "Preferences").write_text("{}")
    (udd / "SingletonLock").write_text("x")
    mgr._clear_stale_locks(udd)
    assert (udd / "Preferences").exists()


# ── 端口池 ────────────────────────────────────────────────
def test_port_pool_skips_busy_ports():
    import socket

    with socket.socket() as sk:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sk.bind(("127.0.0.1", 0))
        sk.listen(1)
        busy = sk.getsockname()[1]
        pool = engines.PortPool(busy, busy + 5)
        got = pool.allocate("p1")
        assert got != busy, "被系统占用的端口不能被分配出去"


# ── 崩溃退避与空闲回收 ────────────────────────────────────
def test_manual_start_resets_restart_count(tmp_path, monkeypatch):
    """人主动 start 必须清零崩溃计数，否则修好问题的环境永远不会再被自动重启。"""
    from zw_core import config, db
    from zw_core.services import manager as mgr

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_ADMIN_TOKEN", "")
    monkeypatch.setenv("ZW_ALLOWED_CIDRS", "")
    # 必须固定 host 为回环：外部环境可能带着 ZW_HOST=0.0.0.0，
    # 而"公开监听必须配令牌"是刻意的安全保护，不该被测试环境绕过或误伤。
    monkeypatch.setenv("ZW_HOST", "127.0.0.1")
    config.reset_settings()
    db.reset_db()

    p = mgr.create_profile({"name": "backoff-test"})
    mgr._update(p["id"], restart_count=3, state="crashed")

    # 让 start 在拿引擎之前就失败（没有内核），但计数重置必须已经发生
    monkeypatch.setattr(engines, "find_chrome", lambda s: (_ for _ in ()).throw(
        engines.EngineError("ENGINE_NOT_FOUND", "测试里故意不提供内核")))
    with pytest.raises(mgr.ProfileError):
        mgr.start(p["id"])
    assert mgr.get_profile(p["id"])["restart_count"] == 0

    config.reset_settings()
    db.reset_db()


def test_health_tick_skips_auto_restart_after_limit(tmp_path, monkeypatch):
    """连续崩溃到达上限后，巡检不能再重启（否则无限循环把 CPU 打满）。"""
    from zw_core import config, db
    from zw_core.services import manager as mgr

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root2"))
    monkeypatch.setenv("ZW_ADMIN_TOKEN", "")
    monkeypatch.setenv("ZW_ALLOWED_CIDRS", "")
    monkeypatch.setenv("ZW_HOST", "127.0.0.1")
    config.reset_settings()
    db.reset_db()

    p = mgr.create_profile({"name": "crash-loop"})
    mgr._update(p["id"], state="running", cdp_port=29999, restart_count=3, restart_policy="always")
    monkeypatch.setattr(engines, "probe", lambda port: False)   # 端口永远不通 = 崩了
    started = {"n": 0}
    monkeypatch.setattr(mgr, "start", lambda *a, **k: started.__setitem__("n", started["n"] + 1))

    events = mgr.health_tick()
    assert started["n"] == 0, "已达上限不该再自动重启"
    assert any(e.get("restart_skipped") for e in events), events
    assert mgr.get_profile(p["id"])["state"] == "crashed"

    config.reset_settings()
    db.reset_db()


def test_idle_reclaim_never_touches_connected_profiles(tmp_path, monkeypatch):
    """有活 CDP 连接的环境，无论空闲多久都不能被回收。"""
    from zw_core import config, db, gateway
    from zw_core.services import manager as mgr

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root3"))
    monkeypatch.setenv("ZW_ADMIN_TOKEN", "")
    monkeypatch.setenv("ZW_ALLOWED_CIDRS", "")
    monkeypatch.setenv("ZW_HOST", "127.0.0.1")
    config.reset_settings()
    db.reset_db()

    s = config.get_settings()
    monkeypatch.setattr(s, "idle_timeout", 1)

    p = mgr.create_profile({"name": "idle-test"})
    mgr._update(p["id"], state="running", cdp_port=29998, last_active_at=1.0)
    monkeypatch.setattr(engines, "probe", lambda port: True)     # 端口健康
    stopped = {"n": 0}
    monkeypatch.setattr(mgr, "stop", lambda *a, **k: stopped.__setitem__("n", stopped["n"] + 1))

    # 有连接 → 不回收
    gateway.ROUTER.add_conn(p["id"], 8)
    try:
        events = mgr.health_tick()
        assert stopped["n"] == 0, "有活连接时不该回收"
        assert not any(e.get("type") == "profile.idle_stopped" for e in events)
    finally:
        gateway.ROUTER.drop_conn(p["id"])

    # 没连接 → 该回收
    mgr.health_tick()
    assert stopped["n"] == 1, "没有连接且空闲超时应该回收"

    config.reset_settings()
    db.reset_db()

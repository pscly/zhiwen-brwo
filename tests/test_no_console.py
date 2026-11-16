"""无控制台环境（Windows GUI 子程序）下的启动路径。

云电脑上真踩到的报错：
    本机服务启动失败：ValueError: Unable to configure formatter 'default'

根因：`console=False` 打包的 Windows 程序里 `sys.stdout` / `sys.stderr`
都是 None。uvicorn 默认会用 dictConfig 配日志，它的 "default" formatter
构造时要调 `sys.stdout.isatty()` → AttributeError，
dictConfig 把它包成那句 ValueError。

两条修复：
1. `uvicorn.Config(..., log_config=None)` —— 不让它重配日志；
2. `setup_logging()` 拿不到流就不装 StreamHandler（只留文件日志）。

这组测试把"两个流都是 None"作为常态来验，以后改回去会立刻挂。
"""

from __future__ import annotations

import socket
import sys
import time

import pytest


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


@pytest.fixture()
def no_streams(monkeypatch):
    """把 stdout/stderr 置 None，模拟 GUI 子程序。"""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    yield


@pytest.fixture()
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_ADMIN_TOKEN", "")
    monkeypatch.setenv("ZW_HOST", "127.0.0.1")
    monkeypatch.setenv("ZW_ALLOWED_CIDRS", "")
    # LocalCore.start(device_id=...) 会写 os.environ["ZW_DEVICE_ID"]，
    # 而 core 调 hub 时会带这个头。不在这里清干净的话，后面的
    # test_sync_integration 会带着一个"未注册设备"去请求 hub，
    # 报 `设备未注册：zwd_testabcdefgh` —— 测试之间互相污染（真发生过）。
    monkeypatch.setenv("ZW_DEVICE_ID", "")
    monkeypatch.setenv("ZW_HUB_URL", "")
    monkeypatch.setenv("ZW_HUB_TOKEN", "")
    from zw_core import config, db

    config.reset_settings()
    db.reset_db()
    yield tmp_path / "root"
    config.reset_settings()
    db.reset_db()


def test_setup_logging_without_streams(isolated_root, no_streams):
    """没有 stdout/stderr 时 setup_logging 不能抛，且仍然写文件。"""
    from zw_core import logging_conf

    logging_conf._CONFIGURED = False
    logging_conf.setup_logging(force=True)      # 不抛就算过
    logs = list((isolated_root / "logs").glob("zhiwen.log"))
    assert logs, "文件日志必须还在（那是无控制台环境下唯一的排错入口）"


def test_localcore_passes_log_config_none(isolated_root, no_streams, monkeypatch):
    """LocalCore 起 uvicorn 时必须传 log_config=None。

    直接盯修复点：把它去掉，无控制台环境就会再次
    `ValueError: Unable to configure formatter 'default'`（云电脑上真踩到过）。
    这里用包装过的 Config 记录实参，再真跑一次 start。
    """
    import uvicorn

    from zw_core import client as C

    captured: dict = {}
    RealConfig = uvicorn.Config

    class RecordingConfig(RealConfig):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **kw):
            captured.update(kw)
            super().__init__(*a, **kw)

    monkeypatch.setattr(uvicorn, "Config", RecordingConfig)
    port = _free_port()
    core = C.LocalCore(port=port)
    try:
        assert core.start() is True, core.error
        assert captured.get("log_config", "缺失") is None, captured
    finally:
        core.stop()


def test_localcore_starts_without_streams(isolated_root, no_streams):
    """真正的端到端：没有 stdout 也要能把本机服务拉起来并响应 /healthz。"""
    import httpx

    from zw_core import client as C

    port = _free_port()
    core = C.LocalCore(port=port)
    try:
        assert core.start(device_id="zwd_testabcdefgh", hub_url="") is True, core.error
        r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=5)
        assert r.status_code == 200
        assert r.json()["ok"] is True
    finally:
        core.stop()


def test_localcore_stats_without_streams(isolated_root, no_streams):
    """统计接口也要能在无控制台环境下工作（GUI 依赖它刷面板）。"""
    from zw_core import client as C

    port = _free_port()
    core = C.LocalCore(port=port)
    try:
        assert core.start() is True, core.error
        st = core.stats()
        assert st is not None
        assert "profiles" in st
    finally:
        core.stop()

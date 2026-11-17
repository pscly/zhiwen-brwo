"""桌面客户端：机器码、地址规范化、探测与注册。

客户端是用户唯一会直接接触的东西，所以"填错时怎么表现"比"填对能跑"更重要。
这里重点验：地址各种写法都认、错误能落到人话、机器码稳定且不重复。
"""

from __future__ import annotations

import os

import pytest

from zw_core import client as cli_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """给每个用例一个独立的客户端配置目录。"""
    d = tmp_path / "client-home"
    monkeypatch.setenv(cli_mod.CONFIG_DIR_ENV, str(d))
    return d


# ── 地址规范化 ────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expect",
    [
        ("192.168.3.5", "http://192.168.3.5:8700"),
        ("192.168.3.5:8700", "http://192.168.3.5:8700"),
        ("192.168.3.5:9999", "http://192.168.3.5:9999"),
        ("http://192.168.3.5:8700", "http://192.168.3.5:8700"),
        ("http://192.168.3.5:8700/", "http://192.168.3.5:8700"),
        ("HTTPS://example.com", "https://example.com:8700"),
        ("  10.0.0.1  ", "http://10.0.0.1:8700"),
        ("hz.example.com:1234", "http://hz.example.com:1234"),
    ],
)
def test_normalize_server(raw, expect):
    assert cli_mod.normalize_server(raw) == expect


@pytest.mark.parametrize("raw", ["", "   ", "http://", "://x"])
def test_normalize_server_rejects_garbage(raw):
    assert cli_mod.normalize_server(raw) == ""


# ── 机器码 ────────────────────────────────────────────────
def test_device_id_is_stable(home):
    a = cli_mod.ensure_device_id()
    b = cli_mod.ensure_device_id()
    assert a == b
    assert a.startswith("zwd_")
    assert len(a) >= 20


def test_device_id_persisted_to_disk(home):
    did = cli_mod.ensure_device_id()
    cfg = json_load(cli_mod.config_path())
    assert cfg["device_id"] == did


def test_device_id_changes_after_deleting_config(home):
    a = cli_mod.ensure_device_id()
    cli_mod.config_path().unlink()
    b = cli_mod.ensure_device_id()
    assert a != b, "删掉配置应该变成一台'新机器'（这是踢下线的绕行方式，服务器会看到新设备）"


def test_config_file_permissions(home):
    """配置文件里有对接码，不该让同机其他用户读到。

    Windows 上不能断言 POSIX 位：`os.chmod` 只影响只读标志，文件仍是 0o666，
    真实权限由 ACL 决定（client._restrict_file 用 icacls 处理）。
    所以在 Windows 上只验"文件存在且可读写"，POSIX 上才验 0600。
    """
    cli_mod.ensure_device_id()
    p = cli_mod.config_path()
    assert p.exists()
    if os.name == "posix":
        mode = p.stat().st_mode & 0o777
        assert mode == 0o600, f"配置里有凭据，权限应为 0600，实际 {oct(mode)}"
    else:
        # Windows：至少要能读回来（icacls 授权当前用户完全控制）
        import json

        assert json.loads(p.read_text(encoding="utf-8"))["device_id"]


# ── 探测与注册（用一个假服务器，不依赖网络）──────────────
class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.content = b"x"
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    """替换 httpx.Client，用来验客户端对服务器各种响应的处理。"""

    def __init__(self, handler):
        self._handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        return self._handler("GET", url, None)

    def post(self, url, json=None, headers=None, **kw):
        return self._handler("POST", url, json)


def patch_httpx(monkeypatch, handler):
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient(handler))


def test_probe_identifies_zhiwen_hub(monkeypatch, home):
    def handler(method, url, body):
        return FakeResp(200, {"ok": True, "data": {"service": "zhiwen-hub", "token_required": False,
                                                   "auto_approve": True, "version": "0.3.0"}})

    patch_httpx(monkeypatch, handler)
    info = cli_mod.probe_server("http://x:8700")
    assert info["ok"] is True and info["service"] == "zhiwen-hub"


def test_probe_rejects_non_zhiwen(monkeypatch, home):
    patch_httpx(monkeypatch, lambda m, u, b: FakeResp(200, {"ok": True, "data": {"service": "nginx"}}))
    info = cli_mod.probe_server("http://x:8700")
    assert info["ok"] is False and "不是知纹" in info["error"]


def test_probe_handles_unreachable(monkeypatch, home):
    def boom(*a, **k):
        raise ConnectionError("refused")

    import httpx

    def handler(method, url, body):
        boom()

    patch_httpx(monkeypatch, handler)
    info = cli_mod.probe_server("http://x:8700")
    assert info["ok"] is False and "连不上" in info["error"]


def test_enroll_success(monkeypatch, home):
    def handler(method, url, body):
        assert url.endswith("/v1/devices/enroll")
        assert body["device_id"].startswith("zwd_")
        return FakeResp(200, {"ok": True, "data": {"device": {"status": "active"}, "auto_approve": True}})

    patch_httpx(monkeypatch, handler)
    res = cli_mod.enroll("http://x:8700", cli_mod.ensure_device_id())
    assert res["ok"] is True


def test_enroll_reports_revoked(monkeypatch, home):
    patch_httpx(monkeypatch, lambda m, u, b: FakeResp(403, {
        "ok": False, "code": "DEVICE_REVOKED", "message": "这台设备已被服务器端踢下线"}))
    res = cli_mod.enroll("http://x:8700", cli_mod.ensure_device_id())
    assert res["ok"] is False
    assert res["code"] == "DEVICE_REVOKED"
    assert "踢下线" in res["error"]


def test_heartbeat_sends_device_header(monkeypatch, home):
    seen = {}

    class CapClient(FakeClient):
        def post(self, url, json=None, headers=None, **kw):
            seen.update(headers or {})
            return FakeResp(200, {"ok": True, "data": {"device": {}}})

    import httpx

    monkeypatch.setattr(httpx, "Client", lambda **kw: CapClient(None))
    res = cli_mod.heartbeat("http://x:8700", "zwd_abcdefgh12345678")
    assert res["ok"] is True
    assert seen.get("X-ZW-Device") == "zwd_abcdefgh12345678"


def test_fingerprint_has_human_fields(home):
    fp = cli_mod.device_fingerprint()
    for k in ("hostname", "platform", "arch"):
        assert fp[k], f"缺少 {k}（面板上要显示给人看）"


# ── 自检入口 ──────────────────────────────────────────────
def test_selftest_passes(capsys, home):
    """--selftest 是给"下载后跑不起来"用的，它自己必须过。

    注意 show_dialog=False：Windows 上 selftest 默认会弹 MessageBox，
    测试里一弹就永远等人点确定 —— CI 在 Windows 上因此卡了 10 分钟
    （pytest-timeout 抓到的栈就在 MessageBoxW 上）。
    """
    code = cli_mod.selftest(show_dialog=False)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "全部" in out and "通过" in out


def json_load(p):
    import json

    return json.loads(p.read_text(encoding="utf-8"))


def test_selftest_never_blocks_in_ci(home, monkeypatch):
    """CI 环境下 selftest **绝不能**弹需要人点的窗口。

    Windows 上默认会弹 MessageBox；CI 里没人点，进程就永远卡住 ——
    pytest-timeout 抓到过这个栈（CI 白等 10 分钟）。
    现在两道保险：显式 show_dialog=False，以及检测 CI 环境变量。
    """
    monkeypatch.setenv("CI", "true")
    called = {"msgbox": False}

    import ctypes

    class FakeUser32:
        def MessageBoxW(self, *a, **k):
            called["msgbox"] = True
            return 1

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"user32": FakeUser32()})(), raising=False)
    cli_mod.selftest()          # 不传参数，靠 CI 检测
    assert called["msgbox"] is False, "CI 里不该弹窗"


def test_selftest_reports_to_file(home):
    """结果要落盘，因为 Windows GUI 程序没有 stdout。"""
    cli_mod.selftest(show_dialog=False)
    report = home / "selftest.txt"
    assert report.exists(), "selftest 结果必须写文件（GUI 程序看不到 stdout）"
    text = report.read_text(encoding="utf-8")
    assert "全部" in text and "通过" in text


def test_localcore_binds_all_interfaces_by_default(home, tmp_path, monkeypatch):
    """客户端启动的本机服务要听 0.0.0.0（用户要求远程能用机器码打开控制台）。

    这里只断言"环境变量被设成 0.0.0.0"，不真起服务（起服务由 test_no_console 覆盖）。
    之前 LocalCore.start 里 `setdefault("ZW_HOST", "127.0.0.1")` 把 config 的新默认值
    覆盖掉了，表现是"改了 config 还是只听回环"——实测发布包才发现。
    """
    import os as _os

    from zw_core.client import LocalCore

    # LocalCore.start() 会直接写 os.environ（ZW_HOST/ZW_PORT/ZW_DEVICE_ID），
    # 这是它的实现方式。测试必须自己快照+恢复，否则会污染后面的用例
    # （表现是 test_sync_integration 报"设备未注册"）。
    snapshot = {k: _os.environ.get(k) for k in ("ZW_HOST", "ZW_PORT", "ZW_DEVICE_ID", "ZW_HUB_URL")}
    for key in ("ZW_HOST", "ZW_PORT"):
        monkeypatch.delenv(key, raising=False)
    core = LocalCore(port=8799)
    # 只跑到设置环境变量那一步就停下：用 is_running 提前返回把它短路掉
    monkeypatch.setattr(core, "is_running", lambda: False)
    monkeypatch.setattr(core, "start", core.__class__.start.__get__(core))
    try:
        core.start(device_id="zwd_hostprobe123456789012", hub_url="")
    except Exception:
        pass          # 起 uvicorn 可能失败，不影响下面的断言
    finally:
        core.stop()
    # 断言发生在恢复之前
    bound = _os.environ.get("ZW_HOST")
    for k, v in snapshot.items():
        if v is None:
            _os.environ.pop(k, None)
        else:
            _os.environ[k] = v
    from zw_core import config as _cfg

    _cfg.reset_settings()
    assert bound == "0.0.0.0", f"ZW_HOST 应该是 0.0.0.0，实际 {bound!r}"
    # 只断言环境变量是不够的：uvicorn.Config 那一层曾经把 host 写死 127.0.0.1，
    # 于是"配置改了、环境变量也对、实际还是只听回环"。这里直接读源码断言，
    # 三层（默认值 / 环境变量 / uvicorn 参数）任何一层写死都会被抓到。
    import inspect as _inspect

    src = _inspect.getsource(LocalCore.start)
    assert 'host="127.0.0.1"' not in src, "uvicorn 的 host 不能写死回环"
    assert "get_settings().host" in src, "uvicorn 的 host 必须取 settings.host"
    # 清理后必须回到快照值（全量跑时前面的用例可能已经留下值，所以不能假设是 None）
    assert _os.environ.get("ZW_HOST") == snapshot["ZW_HOST"]


# ── 端口被占时的自愈（云电脑上真踩到过）────────────────────────────
def test_localcore_falls_back_when_port_busy(tmp_path, monkeypatch):
    """首选端口被占时要自动换端口，而不是等 45 秒后报"启动失败"。

    现场：用户手动跑了一个便携版占了 8730，新装的客户端起不来。
    心跳还在（服务器上看着"在线"），但 core 没了 ——
    远程命令和远程控制台**一起失联**，只能跑到机器前处理。
    """
    import socket as _socket

    from zw_core import config as _cfg
    from zw_core.client import LocalCore

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_ADMIN_TOKEN", "")
    monkeypatch.setenv("ZW_SHELL_ENABLED", "false")
    _cfg.reset_settings()

    blocker = _socket.socket()
    # 不要设 SO_REUSEADDR：Windows 上它会允许重复绑定，探测就测不出"被占用"
    if __import__("sys").platform == "win32":
        blocker.setsockopt(_socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(8)
    busy = int(blocker.getsockname()[1])
    snapshot = {k: __import__("os").environ.get(k) for k in ("ZW_HOST", "ZW_PORT", "ZW_DEVICE_ID")}
    core = LocalCore(port=busy)
    try:
        ok = core.start(device_id="zwd_portsfallback1234567", hub_url="")
        assert ok is True, core.error
        assert core.actual_port and core.actual_port != busy
        assert core.is_running()
        # 端口要报给服务器，否则远程还按旧端口连
        st = core.stats() or {}
        assert int(st.get("port") or 0) == core.actual_port
    finally:
        core.stop()
        blocker.close()
        import os as _os

        for k, v in snapshot.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        _cfg.reset_settings()


def test_localcore_attaches_to_running_instance(tmp_path, monkeypatch):
    """已经有健康的 core 在跑时，要附上去，而不是起第二个、也不是报失败。"""
    from zw_core import config as _cfg
    from zw_core.client import LocalCore

    import os as _os

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root2"))
    monkeypatch.setenv("ZW_ADMIN_TOKEN", "")
    monkeypatch.setenv("ZW_SHELL_ENABLED", "false")
    _cfg.reset_settings()
    # LocalCore.start 会直接写 os.environ（ZW_HOST/ZW_PORT/ZW_DEVICE_ID），
    # 不快照恢复会污染后面的用例（表现是 test_sync_integration 报"设备未注册"）。
    snapshot = {k: _os.environ.get(k) for k in ("ZW_HOST", "ZW_PORT", "ZW_DEVICE_ID", "ZW_HUB_URL")}
    a = LocalCore(port=8755)
    b = None
    try:
        assert a.start(device_id="zwd_attachcase12345678", hub_url="") is True, a.error
        b = LocalCore(port=8755)
        assert b.start(device_id="zwd_attachcase12345678", hub_url="") is True
        assert b.attached is True
        # B 停止不能把 A 的服务关掉
        b.stop()
        assert a.is_running(), "第二个实例停止时误伤了第一个"
    finally:
        a.stop()
        if b is not None:
            b.stop()
        for k, v in snapshot.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        _cfg.reset_settings()


def test_public_bind_without_credential_warns_explicitly(home, monkeypatch, caplog):
    """绑 0.0.0.0 但没有凭据 → 降级为回环，**必须给出可见提示**。

    真踩过：配置层会静默降级（安全上是对的），并把原因写进 `s.host_notice`，
    但**全项目没有任何地方读它** —— 用户设了 ZW_HOST=0.0.0.0，实际只能本机访问，
    日志一个字都不说，`/api/v1/meta` 里也查不到，只能自己猜为什么局域网连不上。
    现在两处都补上了出口，这条测试钉住它们。
    """
    from zw_core import config

    monkeypatch.setenv("ZW_HOST", "0.0.0.0")
    monkeypatch.setenv("ZW_ADMIN_TOKEN", "")
    monkeypatch.delenv("ZW_DEVICE_ID", raising=False)
    config.reset_settings()
    s = config.get_settings()

    assert s.host == "127.0.0.1", "对外监听无凭据时必须降级"
    assert s.host_notice, "降级必须留下可显示的说明"
    assert "ZW_ADMIN_TOKEN" in s.host_notice and "127.0.0.1" in s.host_notice

    # ① meta 里有出口
    from fastapi.testclient import TestClient

    from zw_core.main import create_app

    # TestClient 默认来源是 "testclient"（非回环）→ 会被鉴权挡住。
    # 显式指定来源为本机回环，才是"本机打开控制台"的真实情形。
    with TestClient(create_app(), client=("127.0.0.1", 50000)) as c:
        d = c.get("/api/v1/meta").json()["data"]
    assert d["host"] == "127.0.0.1"
    assert d.get("host_notice"), "meta 必须带出降级原因，否则用户无处可查"
    assert "ZW_ADMIN_TOKEN" in d["host_notice"]


def test_uvicorn_does_not_rewrite_client_ip_from_forwarded_header(monkeypatch):
    """uvicorn 不许替我们信 `X-Forwarded-For` —— 那是一条免令牌通道。

    uvicorn 默认 `proxy_headers=True` 且 `forwarded_allow_ips="127.0.0.1,::1"`：
    本机反代（nginx / Lucky）连到 127.0.0.1:8730 时，它会把 `request.client.host`
    换成请求头里的 XFF。而"来自回环 = 免令牌"正是我们的鉴权规则 —— 于是远程
    攻击者只要经反代发一个 `X-Forwarded-For: 127.0.0.1`，就会被当成"本机"，
    整个控制台不需要令牌。用户的实际部署就是这个拓扑。

    我们有自己的显式策略（`ZW_TRUST_PROXY_HEADERS` + `api._client_ip()`，
    默认关闭），所以必须让 uvicorn 别插手。
    """
    from zw_core import main as mainmod

    captured = {}

    def fake_run(app, **kw):
        captured.update(kw)

    monkeypatch.setattr("uvicorn.run", fake_run)
    mainmod.run()

    assert captured.get("proxy_headers") is False, (
        f"proxy_headers 必须显式关掉，实际 {captured.get('proxy_headers')!r}")
    assert str(captured.get("forwarded_allow_ips") or "") == "", (
        f"forwarded_allow_ips 必须为空，实际 {captured.get('forwarded_allow_ips')!r}")

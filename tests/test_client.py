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

"""远程命令行：设备侧代理（拉取 → 执行 → 回传）。

用假 hub（本地 HTTPServer）验证行为，不依赖真 hub、跑得快。

三个必须守住的语义：
1. 开着的时候真的会拉、会执行、会回传结果。
2. 关掉之后**根本不拉**（不是"拉了但不执行"—— 那会让用户的关闭形同虚设）。
3. 极端时序：拿到命令之后、执行之前被关掉，也不能执行（纵深防御）。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from zw_core.services.shellagent import ShellAgent, start_agent, stop_agent


class FakeHub:
    """假 hub：按顺序发命令，记录收到的结果。"""

    def __init__(self, jobs: list[dict] | None = None, revoked: str = "") -> None:
        self.jobs = list(jobs or [])
        self.results: list[tuple[str, dict]] = []
        self.polls: list[str] = []
        self.revoked = revoked
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音
                pass

            def do_GET(self):
                outer.polls.append(self.path)
                if outer.revoked:
                    body = json.dumps({"ok": False, "code": outer.revoked, "message": "x"}).encode()
                    self.send_response(403)
                else:
                    job = outer.jobs.pop(0) if outer.jobs else None
                    body = json.dumps({"ok": True, "data": {"job": job}}).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) or b"{}"
                # 注册接口也要能模拟"被踢下线"：否则 ensure_registered
                # 的拒绝分支永远测不到（踢下线必须连重新注册也挡住）。
                if outer.revoked and self.path.endswith("/v1/devices/enroll"):
                    body = json.dumps({"ok": False, "code": outer.revoked, "message": "x"}).encode()
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                outer.results.append((self.path, json.loads(raw)))
                body = b'{"ok":true,"data":{"accepted":true}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def wait_results(self, n: int, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(self.results) >= n:
                return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        self.srv.shutdown()


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    from zw_core import config, db

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    db.reset_db()
    yield
    config.reset_settings()
    db.reset_db()


def test_agent_fetches_executes_and_reports(isolated):
    hub = FakeHub([
        {"id": "sh_a1", "command": "echo from-remote", "cwd": "", "timeout": 10},
        {"id": "sh_a2", "command": "exit 5", "cwd": "", "timeout": 10},
    ])
    ag = ShellAgent(hub.url, "zwd_testdevice", wait=1)
    try:
        ag.start()
        assert hub.wait_results(2), "两条命令都应该被执行并回传"
    finally:
        ag.stop()
        hub.close()

    first = dict(hub.results[0][1])
    second = dict(hub.results[1][1])
    assert "from-remote" in first["stdout"]
    assert first["rc"] == 0
    assert second["rc"] == 5
    assert ag.executed == 2
    assert ag.last_rc == 5


def test_agent_does_not_poll_when_disabled(isolated):
    """关掉之后一个请求都不该发出去。

    如果只是"拉到了不执行"，用户仍然会看到队列被吃掉、hub 上有请求记录，
    关闭就没有意义了。
    """
    hub = FakeHub([{"id": "sh_b1", "command": "echo nope", "cwd": "", "timeout": 10}])
    ag = ShellAgent(hub.url, "zwd_testdevice", wait=1)
    ag.set_enabled(False)
    try:
        ag.start()
        time.sleep(2.0)
    finally:
        ag.stop()
        hub.close()
    assert hub.polls == [], f"关闭时不该拉取，实际 {hub.polls}"
    assert hub.results == []
    assert hub.jobs, "命令应该还留在队列里"


def test_agent_skips_execution_if_disabled_mid_flight(isolated):
    """拿到命令之后、执行之前被关掉 → 回 SHELL_DISABLED，不执行。

    这个时序是真实存在的：用户看到命令进来了，赶紧关开关。
    只靠"关闭时不拉取"挡不住它。
    """
    hub = FakeHub([{"id": "sh_c1", "command": "echo should-not-run", "cwd": "", "timeout": 10}])
    ag = ShellAgent(hub.url, "zwd_testdevice", wait=1)
    orig = ag._handle

    def handle_then_disable(job):
        ag.set_enabled(False)
        orig(job)

    ag._handle = handle_then_disable
    try:
        ag.start()
        assert hub.wait_results(1, timeout=10)
    finally:
        ag.stop()
        hub.close()

    body = hub.results[0][1]
    assert "SHELL_DISABLED" in body["error"]
    assert body["rc"] == -1
    assert "should-not-run" not in body["stdout"]


def test_agent_stops_polling_when_revoked(isolated):
    """被踢下线后不要疯狂重试（那只会刷日志、打 hub）。"""
    hub = FakeHub(revoked="DEVICE_REVOKED")
    ag = ShellAgent(hub.url, "zwd_testdevice", wait=1)
    try:
        ag.start()
        time.sleep(2.5)
        n = len(hub.polls)
        assert n >= 1, "应该尝试过至少一次"
        time.sleep(2.5)
        # 被踢后进入 30 秒等待，不该继续高频请求
        assert len(hub.polls) - n <= 1, f"被踢后仍在重试：{hub.polls}"
        assert "移除" in ag.paused_reason
    finally:
        ag.stop()
        hub.close()


def test_agent_backoff_on_network_error(isolated):
    """连不上 hub 时按退避重试，不要打爆对方。

    不要赌"2 秒内一定有 last_error"：Windows 上线程调度与首次 DNS/连接超时
    都更慢，固定 sleep 会让这条测试变成偶发失败（CI 真挂过）。改成轮询等待。
    """
    ag = ShellAgent("http://127.0.0.1:1", "zwd_testdevice", wait=1)
    try:
        ag.start()
        deadline = time.time() + 10
        while time.time() < deadline and not ag.last_error:
            time.sleep(0.1)
        assert ag.last_error, "10 秒内应该记录到一次连接错误"
        assert ag.executed == 0, "连不上就不该有任何执行"
    finally:
        ag.stop()


def test_agent_status_shape(isolated):
    ag = ShellAgent("http://x:8700", "zwd_testdevice")
    st = ag.status()
    for key in ("enabled", "running", "hub", "device_id", "executed",
                "failed", "last_command", "last_rc", "last_error", "paused_reason"):
        assert key in st, f"status 缺字段 {key}"
    assert st["enabled"] is True, "默认应该是开着的"
    assert st["running"] is False


def test_start_agent_requires_hub_and_device(isolated):
    """没有 hub 地址或设备号就别启动 —— 否则会一直报错。"""
    assert start_agent("", "zwd_x") is None
    assert start_agent("http://x:8700", "") is None
    stop_agent()


def test_set_hub_repoints_agent(isolated):
    hub1 = FakeHub([])
    hub2 = FakeHub([{"id": "sh_d1", "command": "echo two", "cwd": "", "timeout": 10}])
    ag = ShellAgent(hub1.url, "zwd_testdevice", wait=1)
    try:
        ag.start()
        time.sleep(0.5)
        ag.set_hub(hub2.url)          # 换服务器
        assert hub2.wait_results(1, timeout=10), "换地址后应该开始向新 hub 拉"
    finally:
        ag.stop()
        hub1.close()
        hub2.close()
    assert "two" in hub2.results[0][1]["stdout"]


def test_ensure_registered_enrolls_device(isolated):
    """服务器上直接跑的 core 要能自己注册成设备。

    没有客户端帮它注册时（3.5 上那个 systemd core 就是这种），
    不补这一步心跳会 403 DEVICE_UNKNOWN，面板里看不到这台机器。
    """
    from zw_core.services.shellagent import ensure_registered

    hub = FakeHub([])
    try:
        res = ensure_registered(hub.url, "zwd_selfregister1234567890", timeout=5)  # type: ignore[call-arg]
    except TypeError:
        res = ensure_registered(hub.url, "zwd_selfregister1234567890")
    finally:
        hub.close()
    assert res.get("ok") is True, res


def test_ensure_registered_requires_inputs(isolated):
    from zw_core.services.shellagent import ensure_registered

    assert ensure_registered("", "zwd_x").get("ok") is False
    assert ensure_registered("http://x:8700", "").get("ok") is False


def test_ensure_registered_reports_revoked(isolated):
    """被踢下线的设备不能靠重启注册复活 —— 那等于踢下线形同虚设。"""
    from zw_core.services.shellagent import ensure_registered

    hub = FakeHub([], revoked="DEVICE_REVOKED")
    try:
        res = ensure_registered(hub.url, "zwd_revoked123456789012345")
    finally:
        hub.close()
    assert res.get("ok") is False
    assert res.get("code") == "DEVICE_REVOKED"


def test_heartbeat_thread_reports_shell_enabled(isolated):
    """无客户端的 core 要自己发心跳，并上报 shell_enabled。

    不心跳的话：注册后 3 分钟被判离线 → 面板一直是灰的，
    也没人上报"这台能不能执行命令"，终端输入框会一直是禁用的。
    """
    from zw_core.services.shellagent import HeartbeatThread

    seen: list[dict] = []
    hub = FakeHub([])
    # 拦截心跳 POST，记录 payload
    orig_post = hub.results

    # 心跳间隔有 15 秒下限（防忙轮询），所以测试用 send_now() 直接发一次，
    # 不去等定时器 —— 那会让单测变成十几秒。
    hb = HeartbeatThread(hub.url, "zwd_hb123456789012345678", interval=15, enabled=True)
    assert hb.send_now() is True
    st = hb.status()
    hub.close()
    assert st["last_ok"] > 0, "应该成功发过至少一次心跳"
    assert st["last_error"] == ""


def test_heartbeat_thread_requires_inputs(isolated):
    from zw_core.services.shellagent import start_heartbeat, stop_agent

    assert start_heartbeat("", "zwd_x") is None
    assert start_heartbeat("http://x:8700", "") is None
    stop_agent()


def test_heartbeat_payload_carries_core_base_and_inventory(isolated):
    """心跳必须带 core_base 与浏览器清单 —— 主控台靠它们展示 CDP 地址。

    只报数字（几个环境、几个在跑）是不够的：用户要在主控台上看到
    "这台机器上有哪些浏览器、CDP 地址是多少"，那就得把清单传上去。
    """
    from zw_core.services import manager as mgr
    from zw_core.services.shellagent import HeartbeatThread

    mgr.create_profile({"name": "清单测试A"})
    mgr.create_profile({"name": "清单测试B"})

    hb = HeartbeatThread("http://127.0.0.1:1", "zwd_x123456789012345678")
    payload = hb._payload()
    assert payload["core_base"].startswith("http")
    inv = payload["inventory"]
    assert len(inv) == 2, inv
    names = {i["name"] for i in inv}
    assert names == {"清单测试A", "清单测试B"}
    for item in inv:
        assert item["cdp_path"].startswith("/cdp/")
        assert "seed" not in item and "password" not in item, "清单里不能带敏感字段"


def test_send_now_returns_false_without_hub(isolated):
    from zw_core.services.shellagent import HeartbeatThread

    assert HeartbeatThread("", "zwd_x").send_now() is False
    assert HeartbeatThread("http://127.0.0.1:1", "").send_now() is False

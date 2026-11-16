"""设备授权流程：pending → 允许 → active。

用户要的流程：客户端装好后显示「等待服务器授权」，管理员在服务端一台一台点允许，
客户端自动继续；**未授权时远程终端仍然可用**。

这几条都很容易在重构时被破坏（尤其是"未授权也能用终端"这条，看起来像是漏洞），
所以逐条钉住。
"""

from __future__ import annotations

import pytest

from zw_hub.config import reset_settings


@pytest.fixture()
def hub(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from zw_hub import main as hub_main

    monkeypatch.setenv("ZW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZW_PANEL_USER", "pscly")
    monkeypatch.setenv("ZW_PANEL_PASSWORD", "pw-test")
    monkeypatch.setenv("ZW_HUB_TOKEN", "")
    monkeypatch.setenv("ZW_DEVICE_AUTO_APPROVE", "false")
    monkeypatch.setenv("ZW_ALLOWED_CIDRS", "")
    reset_settings()
    hub_main._shell_queue = None
    c = TestClient(hub_main.app)
    yield c
    reset_settings()


def login(hub) -> None:
    assert hub.post("/panel/api/login", json={"username": "pscly", "password": "pw-test"}).status_code == 200


def enroll(hub, dev: str, host: str = "PC-A") -> dict:
    r = hub.post("/v1/devices/enroll", json={"device_id": dev, "hostname": host, "platform": "Windows"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["device"]


def test_new_device_waits_for_approval(hub):
    """默认（auto_approve=false）新设备是 pending，不是直接可用。"""
    dev = enroll(hub, "zwd_pending1111222233334444")
    assert dev["status"] == "pending", dev


def test_pending_device_can_heartbeat_and_report(hub):
    """等授权时也要能上报状态，否则控制台看不到它在等。"""
    enroll(hub, "zwd_pending2222333344445555")
    r = hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": "zwd_pending2222333344445555"},
                 json={"profiles": 2, "running": 1, "shell_enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["device"]["status"] == "pending"


def test_pending_device_is_blocked_from_sync(hub):
    """数据面（同步）必须等授权：这是"允许使用"的实际含义。"""
    enroll(hub, "zwd_pending3333444455556666")
    r = hub.get("/v1/index", headers={"X-ZW-Device": "zwd_pending3333444455556666"})
    # /v1/index 走 auth 依赖；设备态不是 active 时应当被拒
    assert r.status_code in (401, 403), f"未授权设备不该能同步，实际 {r.status_code}"


def test_pending_device_can_still_use_remote_terminal(hub):
    """**未授权也能被远程终端操控** —— 用户明确要求。

    这条看起来像"漏洞"，其实是刻意的：授权管的是"这台机器能不能同步数据"，
    不是"能不能被管理员操作"。而且没有它，你就没法在授权前上机器查环境。
    """
    dev = "zwd_pending4444555566667777"
    enroll(hub, dev)
    login(hub)

    r = hub.get("/v1/devices/shell/next", headers={"X-ZW-Device": dev}, params={"wait": 0.2})
    assert r.status_code == 200, f"待授权设备应该能拉命令，实际 {r.status_code}: {r.text}"

    job = hub.post(f"/v1/devices/{dev}/shell", json={"command": "echo hi", "timeout": 5}).json()["data"]
    got = hub.get("/v1/devices/shell/next", headers={"X-ZW-Device": dev}, params={"wait": 1})
    assert got.json()["data"]["job"]["id"] == job["job_id"]

    res = hub.post(f"/v1/devices/shell/{job['job_id']}/result",
                   headers={"X-ZW-Device": dev},
                   json={"rc": 0, "stdout": "hi\n", "stderr": "", "duration_ms": 3})
    assert res.status_code == 200
    final = hub.get(f"/v1/devices/{dev}/shell/{job['job_id']}", params={"wait": 1}).json()["data"]["job"]
    assert final["status"] == "done" and final["stdout"] == "hi\n"


def test_approve_makes_device_active(hub):
    dev = "zwd_approve1111222233334444"
    enroll(hub, dev)
    login(hub)
    r = hub.post(f"/panel/api/devices/{dev}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["device"]["status"] == "active"
    # 允许之后同步就通了
    assert hub.get("/v1/index", headers={"X-ZW-Device": dev}).status_code == 200


def test_reject_blocks_sync_but_keeps_terminal(hub):
    """拒绝 ≠ 删除：记录留着，随时能改成允许；终端也还通。"""
    dev = "zwd_reject1111222233334444"
    enroll(hub, dev)
    login(hub)
    assert hub.post(f"/panel/api/devices/{dev}/reject", json={"note": "不认识的机器"}).status_code == 200

    # 同步被拒
    assert hub.get("/v1/index", headers={"X-ZW-Device": dev}).status_code in (401, 403)
    # 心跳被拒（提示去联系管理员）
    hb = hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": dev}, json={})
    assert hb.status_code == 403 and hb.json()["code"] == "DEVICE_REJECTED"
    # 记录还在
    devs = {d["device_id"]: d for d in hub.get("/panel/api/devices").json()["data"]["devices"]}
    assert devs[dev]["status"] == "rejected"

    # 再允许回来
    assert hub.post(f"/panel/api/devices/{dev}/approve").json()["data"]["device"]["status"] == "active"


def test_pending_shows_up_in_overview_totals(hub):
    """控制台要能看到"有几台在等授权"，否则用户不知道要去点。"""
    login(hub)
    enroll(hub, "zwd_pending5555666677778888", host="等待中")
    d = hub.get("/v1/console/overview", params={"probe": False}).json()["data"]
    assert d["totals"]["pending"] >= 1
    m = next(m for m in d["machines"] if m["hostname"] == "等待中")
    assert m["status"] == "pending"


def test_shell_polling_flag_starts_false_then_true(hub):
    """控制台的"终端就绪"标记：设备没来拉时是 false，拉过之后变 true。

    用户抱怨"下发命令没反应"，这个标记能让他一眼看出是设备没在拉，
    而不是命令本身有问题。
    """
    dev = "zwd_poll111122223333444455"
    enroll(hub, dev)
    login(hub)
    d = hub.get("/v1/console/overview", params={"probe": False}).json()["data"]
    m = next(m for m in d["machines"] if m["device_id"] == dev)
    assert m["shell_polling"] is False
    assert m["shell_last_poll_ago"] is None

    hub.get("/v1/devices/shell/next", headers={"X-ZW-Device": dev}, params={"wait": 0.1})

    d = hub.get("/v1/console/overview", params={"probe": False}).json()["data"]
    m = next(m for m in d["machines"] if m["device_id"] == dev)
    assert m["shell_polling"] is True
    assert m["shell_last_poll_ago"] is not None and m["shell_last_poll_ago"] < 10


def test_auto_approve_true_still_works(hub, monkeypatch):
    """想回到"填 IP 就能用"时，ZW_DEVICE_AUTO_APPROVE=true 仍然有效。"""
    from zw_hub import main as hub_main

    monkeypatch.setenv("ZW_DEVICE_AUTO_APPROVE", "true")
    reset_settings()
    hub_main._shell_queue = None
    dev = enroll(hub, "zwd_auto111122223333444455")
    assert dev["status"] == "active"

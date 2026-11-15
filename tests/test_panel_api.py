"""管理面板 HTTP 层：登录、鉴权、设备操作。

这里最要紧的是两条安全性质：
1. 没登录时**所有** /panel/api/* 都必须拒绝（除了 whoami 明确返回 logged_in=false）；
2. 会话 cookie 必须 httponly，且不能靠猜（它是带密钥的 HMAC，不是用户名）。
"""

from __future__ import annotations

import os
import shutil

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def hub(tmp_path):
    os.environ["ZW_DATA_DIR"] = str(tmp_path)
    os.environ["ZW_PANEL_USER"] = "pscly"
    os.environ["ZW_PANEL_PASSWORD"] = "REDACTED_PANEL_PASSWORD"
    os.environ["ZW_HUB_TOKEN"] = ""
    os.environ["ZW_ALLOWED_CIDRS"] = ""
    from zw_hub import config

    config.reset_settings()
    from zw_hub import main as hub_main

    hub_main._store = None
    hub_main._devices = None
    client = TestClient(hub_main.app)
    yield client
    shutil.rmtree(tmp_path, ignore_errors=True)


def login(c: TestClient) -> None:
    r = c.post("/panel/api/login", json={"username": "pscly", "password": "REDACTED_PANEL_PASSWORD"})
    assert r.status_code == 200, r.text


def test_panel_page_served(hub):
    r = hub.get("/panel")
    assert r.status_code == 200
    assert "服务器端管理" in r.text
    # 不许引用外部 CDN（内网离线环境）
    for bad in ("https://cdn", "unpkg.com", "jsdelivr"):
        assert bad not in r.text


def test_favicon_works(hub):
    assert hub.get("/favicon.ico").status_code == 200


def test_whoami_when_logged_out(hub):
    r = hub.get("/panel/api/whoami")
    assert r.status_code == 200, "未登录是正常状态，不该返回 4xx（会在 console 里刷错误）"
    assert r.json()["data"]["logged_in"] is False


def test_protected_endpoints_require_login(hub):
    for path in ("/panel/api/stats", "/panel/api/devices", "/panel/api/events"):
        r = hub.get(path)
        assert r.status_code == 401, f"{path} 未登录时应拒绝，实际 {r.status_code}"
        assert r.json()["code"] == "PANEL_UNAUTHENTICATED"


def test_login_rejects_bad_credentials(hub):
    for body in (
        {"username": "pscly", "password": "nope"},
        {"username": "nope", "password": "REDACTED_PANEL_PASSWORD"},
        {"username": "", "password": ""},
    ):
        r = hub.post("/panel/api/login", json=body)
        assert r.status_code == 401
        # 不泄露是用户名错还是密码错
        assert r.json()["message"] == "登录名或密码不正确"


def test_login_sets_httponly_cookie(hub):
    r = hub.post("/panel/api/login", json={"username": "pscly", "password": "REDACTED_PANEL_PASSWORD"})
    cookie_header = r.headers.get("set-cookie", "")
    assert "httponly" in cookie_header.lower(), cookie_header
    assert "samesite=lax" in cookie_header.lower(), cookie_header


def test_cookie_is_not_guessable(hub):
    """会话值必须是带密钥的 HMAC，不能是用户名之类可以直接构造的东西。"""
    login(hub)
    value = hub.cookies.get("zw_panel")
    assert value and value != "pscly"
    assert len(value) >= 40
    # 伪造一个同长度的随机串必须无效
    hub.cookies.set("zw_panel", "0" * len(value))
    assert hub.get("/panel/api/stats").status_code == 401


def test_logout(hub):
    login(hub)
    assert hub.get("/panel/api/stats").status_code == 200
    assert hub.post("/panel/api/logout").status_code == 200
    assert hub.get("/panel/api/stats").status_code == 401


def test_device_flow_through_panel(hub):
    # 客户端注册（不带令牌也能注册 —— 这就是"只填 IP 就能用"）
    r = hub.post("/v1/devices/enroll", json={"device_id": "zwd_abcdefgh12345678", "hostname": "PC-A"})
    assert r.status_code == 200, r.text
    did = "zwd_abcdefgh12345678"

    login(hub)
    devices = hub.get("/panel/api/devices").json()["data"]["devices"]
    assert any(d["device_id"] == did for d in devices)

    # 踢下线
    assert hub.post(f"/panel/api/devices/{did}/revoke", json={"note": "测试"}).status_code == 200
    # 该设备之后所有带 X-ZW-Device 的请求都被拒
    r = hub.get("/v1/index", headers={"X-ZW-Device": did})
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_REVOKED"
    # 心跳也被拒
    r = hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": did})
    assert r.status_code == 403
    # 重新注册也不能自愈
    r = hub.post("/v1/devices/enroll", json={"device_id": did, "hostname": "PC-A"})
    assert r.status_code == 403

    # 恢复
    assert hub.post(f"/panel/api/devices/{did}/restore").status_code == 200
    assert hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": did}).status_code == 200


def test_panel_rename_and_delete(hub):
    hub.post("/v1/devices/enroll", json={"device_id": "zwd_abcdefgh12345678", "hostname": "PC-A"})
    login(hub)
    r = hub.patch("/panel/api/devices/zwd_abcdefgh12345678", json={"label": "客厅"})
    assert r.json()["data"]["label"] == "客厅"
    assert hub.delete("/panel/api/devices/zwd_abcdefgh12345678").json()["data"]["deleted"] is True
    assert hub.get("/panel/api/devices").json()["data"]["devices"] == []


def test_enroll_info_reports_no_token_required(hub):
    d = hub.get("/v1/enroll-info").json()["data"]
    assert d["token_required"] is False, "默认形态是不要对接码"
    assert d["auto_approve"] is True
    assert d["enroll_endpoint"] == "/v1/devices/enroll"


def test_enroll_requires_token_when_configured(hub, monkeypatch):
    from zw_hub.config import get_settings

    monkeypatch.setattr(get_settings(), "token", "secret-token")
    r = hub.post("/v1/devices/enroll", json={"device_id": "zwd_abcdefgh12345678"})
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
    r = hub.post("/v1/devices/enroll", json={"device_id": "zwd_abcdefgh12345678"},
                 headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200


def test_old_api_still_works_without_device_header(hub):
    """兼容：不带设备头的老用法（curl/脚本）仍能访问，因为它们本来就要过令牌。"""
    assert hub.get("/v1/index").status_code == 200

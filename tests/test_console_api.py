"""主控台接口（`/v1/console/*`）。

这是"打开 :8700 就能看到机器、浏览器、令牌"的后端。测试重点不在字段齐全，
而在几条容易做错的地方：

1. 浏览器清单随心跳上报后能被读回，且**空心跳不清空**它；
2. NAT 后面探测不到 core 时是 `false` 而不是报错；
3. `secrets` 必须有鉴权（它是明文密钥）；
4. 令牌/密码**绝不出现在** overview 里（overview 是给 AI 随便调的）。
"""

from __future__ import annotations

import pytest

from zw_hub.config import reset_settings


@pytest.fixture()
def hub(monkeypatch, tmp_path):
    """起一个真 hub 的 TestClient（复用项目里已有的 fixture 风格）。"""
    from fastapi.testclient import TestClient

    from zw_hub import main as hub_main

    monkeypatch.setenv("ZW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZW_PANEL_USER", "pscly")
    monkeypatch.setenv("ZW_PANEL_PASSWORD", "pw-test")
    monkeypatch.setenv("ZW_HUB_TOKEN", "")
    monkeypatch.setenv("ZW_ALLOWED_CIDRS", "")
    reset_settings()
    hub_main._shell_queue = None
    hub_main._devices = None  # type: ignore[attr-defined]
    c = TestClient(hub_main.app)
    yield c
    reset_settings()


def login(hub) -> None:
    r = hub.post("/panel/api/login", json={"username": "pscly", "password": "pw-test"})
    assert r.status_code == 200, r.text


def _enroll(hub, dev: str, host: str = "PC-A", platform: str = "Windows") -> None:
    r = hub.post("/v1/devices/enroll", json={"device_id": dev, "hostname": host, "platform": platform})
    assert r.status_code == 200, r.text


def test_overview_requires_auth(hub):
    """overview 会暴露机器与 IP，必须鉴权。"""
    assert hub.get("/v1/console/overview").status_code == 401


def test_overview_lists_machines_and_browsers(hub):
    login(hub)
    _enroll(hub, "zwd_aaaa1111222233334444")
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": "zwd_aaaa1111222233334444"},
             json={"profiles": 2, "running": 1, "shell_enabled": True,
                   "core_base": "http://192.168.3.6:8730",
                   "inventory": [
                       {"id": "p1", "name": "环境A", "state": "running", "cdp_path": "/cdp/p1"},
                       {"id": "p2", "name": "环境B", "state": "stopped", "cdp_path": "/cdp/p2"},
                   ]})

    d = hub.get("/v1/console/overview", params={"probe": False}).json()["data"]
    assert d["totals"]["machines"] == 1
    assert d["totals"]["browsers"] == 2
    m = d["machines"][0]
    assert m["hostname"] == "PC-A"
    assert m["core_base"] == "http://192.168.3.6:8730"
    assert [b["name"] for b in m["browsers"]] == ["环境A", "环境B"]
    assert m["browsers"][0]["cdp_http"] == "http://192.168.3.6:8730/cdp/p1"
    assert m["browsers"][0]["cdp_ws"].startswith("ws://192.168.3.6:8730/cdp/p1")


def test_overview_never_leaks_secrets(hub):
    """overview 是给 AI 随便调的，**不能**带令牌/密码原文。"""
    login(hub)
    _enroll(hub, "zwd_bbbb1111222233334444")
    raw = hub.get("/v1/console/overview", params={"probe": False}).text
    assert "pw-test" not in raw
    assert "1234" not in raw or "default_credentials" in raw


def test_empty_heartbeat_keeps_inventory(hub):
    """空心跳不能把已上报的浏览器清单清空。

    客户端升级过程中、或某次心跳字段缺失，都不该让主控台突然"浏览器全没了"。
    """
    login(hub)
    _enroll(hub, "zwd_cccc1111222233334444")
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": "zwd_cccc1111222233334444"},
             json={"inventory": [{"id": "x", "name": "保留我", "state": "running", "cdp_path": "/cdp/x"}]})
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": "zwd_cccc1111222233334444"},
             json={"profiles": 1})
    d = hub.get("/v1/console/overview", params={"probe": False}).json()["data"]
    m = d["machines"][0]
    assert [b["name"] for b in m["browsers"]] == ["保留我"]


def test_unreachable_core_is_false_not_error(hub):
    """core 探测不到就是 false —— NAT 后面的机器本来就连不上，不是错误。"""
    login(hub)
    _enroll(hub, "zwd_dddd1111222233334444")
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": "zwd_dddd1111222233334444"},
             json={"core_base": "http://127.0.0.1:1"})
    d = hub.get("/v1/console/overview", params={"probe": True}).json()["data"]
    assert d["machines"][0]["core_reachable"] is False


def test_machine_without_core_base_has_null_reachable(hub):
    login(hub)
    _enroll(hub, "zwd_eeee1111222233334444")
    d = hub.get("/v1/console/overview", params={"probe": True}).json()["data"]
    assert d["machines"][0]["core_reachable"] is None


def test_inventory_is_sanitized(hub):
    """设备上报的字段要裁长度、限条数，别让一台设备把主控台撑爆。"""
    login(hub)
    _enroll(hub, "zwd_ffff1111222233334444")
    big = [{"id": "x" * 500, "name": "名" * 500, "state": "running", "cdp_path": "p" * 500}
           for _ in range(300)]
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": "zwd_ffff1111222233334444"},
             json={"inventory": big})
    d = hub.get("/v1/console/overview", params={"probe": False}).json()["data"]
    inv = d["machines"][0]["browsers"]
    assert len(inv) <= 200, f"没限条数：{len(inv)}"
    assert len(inv[0]["id"]) <= 64
    assert len(inv[0]["name"]) <= 120


def test_secrets_requires_auth(hub):
    """secrets 是明文密钥，没鉴权必须 401。"""
    assert hub.get("/v1/console/secrets").status_code == 401


def test_secrets_returns_values_after_login(hub):
    login(hub)
    d = hub.get("/v1/console/secrets").json()["data"]
    assert d["panel_user"] == "pscly"
    assert d["panel_password"] == "pw-test"
    assert d["hub_token_set"] is False
    assert "hint" in d


def test_secrets_with_bearer_token(hub, monkeypatch):
    """没 cookie 时用 Bearer 令牌也该能调（给脚本/AI 用）。"""
    from zw_hub import main as hub_main
    from zw_hub.config import get_settings

    monkeypatch.setenv("ZW_HUB_TOKEN", "tok-console")
    reset_settings()
    hub_main._shell_queue = None
    r = hub.get("/v1/console/secrets", headers={"Authorization": "Bearer tok-console"})
    assert r.status_code == 200
    assert r.json()["data"]["hub_token_set"] is True


def test_head_root_returns_200(hub):
    """HEAD `/` 必须是 200。

    3.5 上的服务导航页用 `HEAD /__probe/http/8700` 判断服务在不在线，
    而 FastAPI 的 `@app.get` 不会自动支持 HEAD —— 少这条，导航页上的
    知纹入口状态点会永远是灰的，看起来像服务挂了。
    """
    r = hub.head("/")
    assert r.status_code == 200
    assert r.content == b"", "HEAD 不该带 body"


def test_panel_redirects_to_root(hub):
    """旧地址 /panel 永久跳到 /，老书签不能断。"""
    r = hub.get("/panel", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_compose_passes_allowlist_into_container():
    """compose 必须把 ZW_ALLOWED_CIDRS 透传进容器。

    这个坑真踩过：`.env` 里写了 `ZW_ALLOWED_CIDRS=192.168.3.0/24`，
    但 `docker-compose.yml` 的 `environment:` 是一份**显式清单**，
    没列出来的变量根本不会进容器 —— 白名单等于没开，而界面/接口上
    看不出任何异常（`allowed_cidrs` 显示为空数组）。

    这类"配置存在但从不生效"的问题最难发现，所以用测试钉住。
    """
    from pathlib import Path

    compose = (Path(__file__).resolve().parent.parent / "hub" / "docker-compose.yml").read_text(encoding="utf-8")
    for key in ("ZW_ALLOWED_CIDRS", "ZW_PANEL_USER", "ZW_PANEL_PASSWORD",
                "ZW_HUB_TOKEN", "ZW_DEVICE_AUTO_APPROVE", "ZW_SHELL_ENABLED"):
        assert key in compose, f"compose 没有透传 {key}，设了也不会生效"


def test_panel_secret_persists_across_restarts(tmp_path):
    """面板会话密钥必须落盘，重启后旧 cookie 仍然有效。

    之前是"每次启动随机生成"：部署脚本一 `docker compose up -d`，
    所有人的登录就失效，表现为"刚登录过又让我登"，而且很难联想到是重启。
    """
    from zw_hub.config import Settings

    data = tmp_path / "data"
    data.mkdir()
    a = Settings()
    a.data_dir = data
    a.resolve_paths()
    assert a.panel_secret, "应该生成密钥"
    keyfile = data / "panel-secret"
    assert keyfile.exists(), "密钥必须落盘"

    b = Settings()
    b.data_dir = data
    b.resolve_paths()
    assert b.panel_secret == a.panel_secret, "重启后必须沿用同一个密钥"


def test_inventory_carries_masked_proxy(hub):
    """主控台的「代理」列要能看到每个环境走哪个代理 —— 但不能看到密码。

    密码只留在客户端那台机器上：hub 的库会被备份、也会被别的机器同步读，
    代理密码不该躺在那里。所以客户端上报前就把密码换成 ***，
    hub 侧再按白名单重建一次（不盲信上报内容）。
    """
    login(hub)
    dev = "zwd_proxycol111122223333"
    hub.post("/v1/devices/enroll", json={"device_id": dev, "hostname": "px-pc"})
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": dev}, json={
        "inventory": [
            {"id": "a", "name": "带代理", "state": "stopped",
             "proxy": {"set": True, "url": "socks5://u:***@1.2.3.4:20000",
                       "has_auth": True, "username": "u", "host": "1.2.3.4",
                       "port": 20000, "scheme": "socks5"}},
            {"id": "b", "name": "直连", "state": "running", "proxy": {"set": False}},
        ]})
    d = hub.get("/v1/console/overview", params={"probe": False}).json()["data"]
    m = next(x for x in d["machines"] if x["device_id"] == dev)
    by_name = {b["name"]: b for b in m["browsers"]}
    assert by_name["带代理"]["proxy"]["set"] is True
    assert by_name["带代理"]["proxy"]["host"] == "1.2.3.4"
    assert by_name["带代理"]["proxy"]["port"] == 20000
    assert by_name["带代理"]["proxy"]["has_auth"] is True
    assert by_name["直连"]["proxy"]["set"] is False


def test_hub_strips_password_from_inventory_proxy(hub):
    """即使客户端把明文密码塞进 payload，hub 也不能把它存进库。"""
    login(hub)
    dev = "zwd_proxyleak11112222333"
    hub.post("/v1/devices/enroll", json={"device_id": dev})
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": dev}, json={
        "inventory": [{"id": "x", "name": "泄漏测试", "state": "stopped",
                       "proxy": {"set": True, "url": "socks5://user:REALPASSWORD@9.9.9.9:1080",
                                 "has_auth": True, "host": "9.9.9.9", "port": 1080,
                                 "scheme": "socks5", "password": "REALPASSWORD"}}]})
    raw = hub.get("/v1/console/overview", params={"probe": False}).text
    assert "REALPASSWORD" not in raw, "hub 把代理密码存下来/返回了"


def test_overview_exposes_core_alive(hub):
    """进程活着 ≠ 本机服务活着，主控台必须能区分。

    现场（2026-11-17 云电脑）：core 起不来，但心跳照常发，
    服务器上一直显示"在线"，而远程命令和远程控制台全废 ——
    看的人完全不知道发生了什么，只能跑到机器前处理。
    """
    login(hub)
    dev = "zwd_corealive1112223334"
    hub.post("/v1/devices/enroll", json={"device_id": dev, "hostname": "cx-pc"})
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": dev},
             json={"core_alive": False, "core_error": "端口被占", "core_port": 8730})

    d = hub.get("/v1/console/overview", params={"probe": False}).json()["data"]
    m = next(x for x in d["machines"] if x["device_id"] == dev)
    # 重点：core_alive 独立于 online 反映真实情况。
    # （online 还受审批状态影响，那条由审批用例覆盖，这里不混进来。）
    assert m["core_alive"] is False

    # 恢复上报后要能翻回来
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": dev}, json={"core_alive": True})
    d = hub.get("/v1/console/overview", params={"probe": False}).json()["data"]
    m = next(x for x in d["machines"] if x["device_id"] == dev)
    assert m["core_alive"] is True


def test_overview_core_alive_unknown_for_old_clients(hub):
    """旧客户端不上报 core_alive → 必须是 None（未知），不能默认成"正常"。"""
    login(hub)
    dev = "zwd_corealiveunknown5566"
    hub.post("/v1/devices/enroll", json={"device_id": dev, "hostname": "old-pc"})
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": dev}, json={"profiles": 1})
    d = hub.get("/v1/console/overview", params={"probe": False}).json()["data"]
    m = next(x for x in d["machines"] if x["device_id"] == dev)
    assert m["core_alive"] is None


def test_client_console_page_skips_login_when_auth_not_required():
    """本机打开客户端控制台时，页面**不应该**弹登录层。

    用户原话：「客户端打开控制台的话，就再也不需要输入什么访问令牌，直接就可以进入」。

    真出过这个 bug：页面启动时是
        `if (!S.token) { showLogin(); return; }`
    —— 完全没看服务端返回的 `auth_required`。于是即使 core 在回环上已经放行，
    打开页面仍然被要求输令牌，看起来那个改动从没生效过。
    这里直接读页面源码钉住修复后的形状（JS 行为由 scripts/ui-check.cjs 覆盖）。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    html = (root / "core/src/zw_core/web/index.html").read_text(encoding="utf-8")

    # 必须存在"先 loadMeta、再看 auth_required"的分支
    assert "auth_required === false" in html, "页面没有按 auth_required 决定是否弹登录框"
    # `if (!S.token)` 分支里不能"直接 showLogin 就 return"
    m = re.search(r"if \(!S\.token\) \{(.{0,400}?)\n  \}", html, re.S)
    assert m, "找不到 !S.token 的启动分支"
    body = m.group(1)
    assert "loadMeta()" in body, f"没先问 meta 就弹登录框：{body[:200]}"
    assert "hideLogin()" in body, "auth_required=false 时没有隐藏登录层"


def test_meta_auth_required_is_per_request_not_per_config():
    """`auth_required` 必须是"**这次请求**要不要令牌"，不是"配没配令牌"。

    真踩过：原来是 `bool(s.admin_token)`。于是配了令牌的机器，即使从本机回环访问
    （接口已经放行、返回 200），页面也会因为 `auth_required=true` 弹出登录框 ——
    正是用户抱怨的「打开控制台还要输访问密码」。之前只在没配令牌的机器上验证过，
    所以漏了这条常见配置。
    """
    import os
    import shutil
    import tempfile

    from fastapi.testclient import TestClient

    from zw_core import db

    tmp = tempfile.mkdtemp()
    # **不要**设 ZW_DEVICE_ID：它会被后续测试继承（host 心跳/同步按设备号注册），
    # 第一版设了没还原，导致 test_sync_integration 里 8 项报
    # 「设备未注册：zwd_metapertest…」。这里只需要"配了 admin_token"这一个条件。
    # 顺手把改动过的环境变量快照下来，结束时还原。
    keys = ("ZW_ROOT", "ZW_ADMIN_TOKEN", "ZW_DEVICE_ID")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["ZW_ROOT"] = tmp
        os.environ["ZW_ADMIN_TOKEN"] = "secret123"   # 关键：**配了**令牌
        os.environ.pop("ZW_DEVICE_ID", None)

        from zw_core import config

        config.reset_settings()
        db.reset_db()
        from zw_core.main import create_app

        H = {"Authorization": "Bearer secret123"}
        with TestClient(create_app(), client=("127.0.0.1", 50001)) as c:
            d = c.get("/api/v1/meta", headers=H).json()["data"]
        assert d["auth_required"] is False, "本机回环不该要求令牌（否则页面会弹登录框）"

        with TestClient(create_app(), client=("192.168.3.99", 50002)) as c:
            d2 = c.get("/api/v1/meta", headers=H).json()["data"]
        assert d2["auth_required"] is True, "远程必须要求令牌"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def test_client_console_does_not_claim_auth_extension_works():
    """客户端控制台不能再说"选代理会注入凭据扩展"。

    实测结论（多次真机验证）：Chrome 137 起**忽略 `--load-extension`**，
    官方 Chrome 149/150/157 全都不加载扩展。带用户名密码的代理真正靠的是
    **本机中继**（`services/proxyrelay.py`：中继代填 `Proxy-Authorization` /
    做 SOCKS5 RFC1929 握手）。

    界面文案里留着"会自动注入凭据扩展"，用户遇到代理连不上时就会以为是扩展
    没生效，往错的方向查 —— 这类"文案比现实旧"的问题比代码 bug 更难发现。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    html = (root / "core/src/zw_core/web/index.html").read_text(encoding="utf-8")

    assert "注入凭据扩展" not in html, "客户端控制台仍在宣称注入认证扩展"
    assert "本机中继" in html, "应当说明带认证代理走的是本机中继"

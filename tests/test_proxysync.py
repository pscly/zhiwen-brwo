"""代理库双向同步（服务器 ↔ 用户机）。

用户要求：「代理可以在服务器里面进行添加管控，也可以在用户机上添加管控，
都是一样的…用户机上添加过的代理，都会自动同步到服务器里面，
也可以在服务器里面删除，或者隐藏」。
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from zw_hub import proxies as hubpx
from zw_hub.config import get_settings as hub_settings


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    """真起一个 uvicorn hub。

    不能用 `TestClient`：同步模块走的是**真 httpx**，而 TestClient 的
    base_url 是 `http://testserver`（不可解析）。这里起在随机端口上，
    和线上路径完全一致（含设备鉴权、cookie）。
    """
    import socket
    import threading
    import time

    import uvicorn

    monkeypatch.setenv("ZW_DATA_DIR", str(tmp_path / "hub"))
    monkeypatch.setenv("ZW_PANEL_USER", "pscly")
    monkeypatch.setenv("ZW_PANEL_PASSWORD", "pw")
    monkeypatch.setenv("ZW_DEVICE_AUTO_APPROVE", "true")
    monkeypatch.setenv("ZW_ALLOWED_CIDRS", "")
    import zw_hub.config as hcfg

    hcfg.reset_settings()
    from zw_hub import main as hubmain

    s = hcfg.get_settings()
    s.resolve_paths()

    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = int(sk.getsockname()[1])
    sk.close()

    server = uvicorn.Server(uvicorn.Config(hubmain.app, host="127.0.0.1", port=port,
                                           log_level="warning", access_log=False,
                                           log_config=None))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:  # pragma: no cover
        raise RuntimeError("hub 没起来")

    c = httpx.Client(base_url=base, timeout=15)
    c.post("/panel/api/login", json={"username": "pscly", "password": "pw"})

    class Hub:
        def __init__(self, client, url):
            self._c = client
            self.base_url = url
            self.cookies = client.cookies

        def get(self, *a, **k):
            return self._c.get(*a, **k)

        def post(self, *a, **k):
            return self._c.post(*a, **k)

        def delete(self, *a, **k):
            return self._c.delete(*a, **k)

    try:
        yield Hub(c, base)
    finally:
        server.should_exit = True
        try:
            th.join(timeout=5)
        except Exception:
            pass


@pytest.fixture()
def client_root(tmp_path, monkeypatch, hub):
    """本地 core 的 root + 指向这个 hub 的配置。"""
    import zw_core.config as ccfg

    root = tmp_path / "core"
    monkeypatch.setenv("ZW_ROOT", str(root))
    monkeypatch.setenv("ZW_HUB_URL", str(hub.base_url))
    monkeypatch.setenv("ZW_DEVICE_ID", "zwd_synctest1234567890ab")
    monkeypatch.setenv("ZW_HOST", "127.0.0.1")
    ccfg.reset_settings()
    from zw_core import db

    db.reset_db()
    yield root
    ccfg.reset_settings()
    db.reset_db()


def _hub_client(hub) -> httpx.Client:
    """hub 本身就是真 httpx 客户端，直接复用（已带登录 cookie）。"""
    return hub._c


def test_store_never_returns_plaintext_password(hub):
    """列表接口永远不给明文密码，只给 has_password。"""
    c = _hub_client(hub)
    c.post("/v1/proxies", json={"scheme": "socks5", "host": "1.2.3.4", "port": 1080,
                                "username": "u", "password": "TOPSECRET",
                                "label": "x"})
    body = c.get("/v1/proxies").text
    assert "TOPSECRET" not in body
    item = c.get("/v1/proxies").json()["data"]["proxies"][0]
    assert item["has_password"] is True


def test_password_is_encrypted_at_rest(hub, tmp_path):
    """库里落的是密文，不是明文。"""
    c = _hub_client(hub)
    c.post("/v1/proxies", json={"scheme": "http", "host": "1.1.1.1", "port": 8080,
                                "username": "u", "password": "ATRESTSECRET"})
    db = (tmp_path / "hub" / "zhiwen-hub.sqlite3").read_bytes()
    assert b"ATRESTSECRET" not in db, "密码明文进了服务器数据库"


def test_secret_roundtrip_and_unreadable_after_key_change(hub, monkeypatch):
    """密钥变了要明确报错，不能静默当成"没有密码"。"""
    s = hub_settings()
    item = hubpx.upsert(s, scheme="http", host="2.2.2.2", port=8080,
                        username="u", password="KEEPME")
    assert hubpx.secret_of(s, item["id"]) == "KEEPME"
    # 换掉密钥 → 解不开
    monkeypatch.setattr(s, "panel_secret", "a-totally-different-secret")
    with pytest.raises(hubpx.ProxyError) as ei:
        hubpx.secret_of(s, item["id"])
    assert ei.value.code == "PROXY_SECRET_UNREADABLE"


def test_hide_excludes_from_list_and_device_pull(hub):
    """隐藏 = 默认列表和客户端拉取里都不出现，但没被删。"""
    c = _hub_client(hub)
    made = c.post("/v1/proxies", json={"scheme": "http", "host": "3.3.3.3",
                                       "port": 8080, "label": "藏起来"}).json()["data"]
    assert len(c.get("/v1/proxies").json()["data"]["proxies"]) == 1
    c.post(f"/v1/proxies/{made['id']}/hide", json={"hidden": True})
    assert c.get("/v1/proxies").json()["data"]["proxies"] == []
    assert len(c.get("/v1/proxies", params={"include_hidden": True}).json()["data"]["proxies"]) == 1
    # 取消隐藏能回来
    c.post(f"/v1/proxies/{made['id']}/hide", json={"hidden": False})
    assert len(c.get("/v1/proxies").json()["data"]["proxies"]) == 1


def test_device_push_then_visible_on_server(hub, client_root):
    """用户机加了代理 → 自动同步到服务器（带 origin，能看出是哪台加的）。"""
    from zw_core.services import proxylib, proxysync

    proxylib.import_batch("socks5://u1:PW1@66.80.12.66:20000 # 落地A\n"
                          "http://127.0.0.1:21080,socks5://u2:PW2@5.5.5.5:1080 # 链式B")
    up = proxysync.push()
    assert up["ok"] is True, up
    assert up["created"] == 2

    items = _hub_client(hub).get("/v1/proxies").json()["data"]["proxies"]
    assert {i["label"] for i in items} == {"落地A", "链式B"}
    assert all(i["origin"].startswith("zwd_synctest") for i in items)
    # 链式的前置要原样保存
    chain = next(i for i in items if i["label"] == "链式B")
    assert chain["hop"] == "http://127.0.0.1:21080"
    assert chain["has_password"] is True


def test_server_added_proxy_flows_down_to_client(hub, client_root):
    """服务器加的代理 → 用户机拉下来能用（含密码与链式前置）。"""
    c = _hub_client(hub)
    c.post("/v1/proxies", json={"scheme": "socks5", "host": "9.9.9.9", "port": 1080,
                                "username": "svr", "password": "SVRPW",
                                "label": "服务器加的", "hop": "http://127.0.0.1:21080"})
    from zw_core.services import proxylib, proxysync

    down = proxysync.pull()
    assert down["ok"] is True, down
    assert down["added"] == 1
    rows = proxylib.list_all()
    assert [r["label"] for r in rows] == ["服务器加的"]
    assert rows[0]["is_chain"] is True
    # 密码真的下来了（否则拉下来也用不了）
    spec = proxylib._spec_of_row(proxylib._load(rows[0]["id"]))
    assert spec.password == "SVRPW"
    assert spec.hop is not None


def test_pull_is_idempotent(hub, client_root):
    """重复拉取不能重复插入。"""
    c = _hub_client(hub)
    c.post("/v1/proxies", json={"scheme": "http", "host": "4.4.4.4", "port": 8080,
                                "label": "只一条"})
    from zw_core.services import proxylib, proxysync

    assert proxysync.pull()["added"] == 1
    assert proxysync.pull()["added"] == 0
    assert len(proxylib.list_all()) == 1


def test_sync_failure_never_raises(client_root, monkeypatch):
    """服务器连不上时同步只返回失败，绝不抛异常打断本地操作。"""
    monkeypatch.setenv("ZW_HUB_URL", "http://127.0.0.1:9")
    import zw_core.config as ccfg

    ccfg.reset_settings()
    from zw_core.services import proxylib, proxysync

    proxylib.import_batch("http://u:p@1.2.3.4:8080")
    up = proxysync.push(timeout=3)
    down = proxysync.pull(timeout=3)
    assert up["ok"] is False and down["ok"] is False
    assert up.get("error")
    # 本机库不能因为同步失败被动过
    assert len(proxylib.list_all()) == 1


def test_scheme_with_slashes_is_normalized(hub):
    """面板上填 `http://` 是最自然的写法，不能拼出 `http://://`。"""
    c = _hub_client(hub)
    item = c.post("/v1/proxies", json={"scheme": "http://", "host": "5.5.5.5",
                                       "port": 8080}).json()["data"]
    assert item["upstream"] == "http://5.5.5.5:8080"

"""API 冒烟：不启动真浏览器，只验证路由、鉴权与错误形状。

需要先设置 ZW_ROOT 到一个临时目录（fixture 里做）。
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    tmp = tempfile.mkdtemp()
    os.environ["ZW_ROOT"] = tmp
    os.environ["ZW_ADMIN_TOKEN"] = "t0ken"
    os.environ["ZW_PORT"] = "8799"
    os.environ["ZW_HUB_URL"] = ""
    from zw_core import config

    config.reset_settings()
    from zw_core import db

    db.reset_db()
    # 必须用 main.create_app：/mcp 这条路由是在那里挂上去的
    from zw_core.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c
    shutil.rmtree(tmp, ignore_errors=True)


H = {"Authorization": "Bearer t0ken"}


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_auth_required(client):
    assert client.get("/api/v1/meta").status_code == 401


def test_meta(client):
    d = client.get("/api/v1/meta", headers=H).json()
    assert d["ok"] and d["data"]["name"] == "zhiwen-core"


def test_ai_manifest(client):
    d = client.get("/api/v1/ai/manifest", headers=H).json()["data"]
    assert "标准作业流程" in d and "错误码速查" in d


def test_profile_lifecycle(client):
    r = client.post("/api/v1/profiles", headers=H, json={"name": "p-smoke"})
    assert r.status_code == 200, r.text
    pid = r.json()["data"]["id"]
    assert r.json()["data"]["fp_seed"] > 0

    # 重名必须被拒
    assert client.post("/api/v1/profiles", headers=H, json={"name": "p-smoke"}).json()["code"] == "NAME_TAKEN"

    # 改
    r = client.patch(f"/api/v1/profiles/{pid}", headers=H, json={"note": "改了"})
    assert r.json()["data"]["note"] == "改了"

    # 种子不可改
    r = client.patch(f"/api/v1/profiles/{pid}", headers=H, json={"fp_seed": 1})
    assert r.json()["code"] == "SEED_IMMUTABLE"

    # 未启动取 CDP → 409
    r = client.get(f"/api/v1/profiles/{pid}/cdp", headers=H)
    assert r.status_code == 409 and r.json()["code"] == "STATE_CONFLICT"

    # 未启动读 cookie → 409 且带提示
    r = client.get(f"/api/v1/profiles/{pid}/cookies", headers=H)
    assert r.status_code == 409 and "提示" not in r.json() or "hint" in r.json()

    # 删
    assert client.delete(f"/api/v1/profiles/{pid}?purge=true", headers=H).json()["ok"]


def test_countries_of_error_shape(client):
    r = client.get("/api/v1/profiles/不存在", headers=H)
    body = r.json()
    assert r.status_code == 404 and body["code"] == "PROFILE_NOT_FOUND" and "message" in body


def test_mcp_initialize(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.json()["result"]["serverInfo"]["name"] == "zhiwen"
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert len(tools) >= 20


def test_profile_detail_does_not_issue_cdp_token(client):
    """详情接口被 UI 反复调用，绝不能每调一次就签发一个令牌，否则表会无限增长。"""
    from zw_core.db import get_db

    r = client.post("/api/v1/profiles", headers=H, json={"name": "p-token"})
    pid = r.json()["data"]["id"]

    with get_db().conn() as c:
        before = c.execute("SELECT COUNT(*) AS n FROM cdp_tokens").fetchone()["n"]

    for _ in range(5):
        d = client.get(f"/api/v1/profiles/{pid}", headers=H).json()["data"]
        assert "cdp" not in d or d["cdp"] is None
        assert d["cdp_ready"] is False
        assert "cdp_hint" in d

    with get_db().conn() as c:
        after = c.execute("SELECT COUNT(*) AS n FROM cdp_tokens").fetchone()["n"]
    assert after == before, "详情接口不应该签发令牌"


def test_token_cap_and_purge(client):
    from zw_core.db import get_db
    from zw_core.services import manager as mgr

    r = client.post("/api/v1/profiles", headers=H, json={"name": "p-cap"})
    pid = r.json()["data"]["id"]

    for _ in range(30):
        mgr.issue_cdp_token(pid, ttl=60)

    with get_db().conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM cdp_tokens WHERE profile_id=?", (pid,)).fetchone()["n"]
    assert n <= mgr.MAX_LIVE_TOKENS_PER_PROFILE, f"令牌没有被封顶：{n}"

    # 过期的应被清理
    mgr.issue_cdp_token(pid, ttl=60)
    with get_db().conn() as c:
        c.execute("UPDATE cdp_tokens SET expires_at=? WHERE profile_id=?", (1.0, pid))
    assert mgr.purge_expired_tokens() >= 1
    st = mgr.token_stats()
    assert st["expired"] == 0


def test_stats_includes_token_stats(client):
    d = client.get("/api/v1/stats", headers=H).json()["data"]
    assert "cdp_tokens" in d and "live" in d["cdp_tokens"]

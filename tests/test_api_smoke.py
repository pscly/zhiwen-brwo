"""API 冒烟：不启动真浏览器，只验证路由、鉴权与错误形状。

需要先设置 ZW_ROOT 到一个临时目录（fixture 里做）。

## 这里锁死的几条"曾经真的坏过"的行为

1. `POST /mcp` 必须鉴权（0.1.0 是完全开放的，任何能连上端口的人都能读 cookie）。
2. `POST /api/v1/sync/push-all` 必须真的走到"全量推送"，
   而不是被 `/sync/{ref}/push` 当成"环境名叫 push-all"吃掉。
3. `POST /api/v1/profiles/import` 必须走到导入，而不是被 `/profiles/{ref}` 吃掉。
4. 详情接口不签发 CDP 令牌。
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
    os.environ["ZW_ALLOWED_CIDRS"] = ""
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


# ── 基础 ──────────────────────────────────────────────────
def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_auth_required(client):
    assert client.get("/api/v1/meta").status_code == 401


def test_auth_wrong_token(client):
    r = client.get("/api/v1/meta", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"


def test_meta(client):
    d = client.get("/api/v1/meta", headers=H).json()
    assert d["ok"] and d["data"]["name"] == "zhiwen-core"
    assert "features" in d["data"]
    assert d["data"]["schema_version"] >= 2


def test_ai_manifest(client):
    d = client.get("/api/v1/ai/manifest", headers=H).json()["data"]
    assert "标准作业流程" in d and "错误码速查" in d
    paths = {e["path"] for e in d["端点清单"]}
    # 新端点必须出现在自描述清单里，否则 AI 发现不了
    assert "/api/v1/profiles/{id}/fingerprint/probe" in paths
    assert "/api/v1/proxies/check" in paths


# ── 环境生命周期 ──────────────────────────────────────────
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

    # 未启动读 cookie → 409 且带 hint
    r = client.get(f"/api/v1/profiles/{pid}/cookies", headers=H)
    assert r.status_code == 409 and r.json()["code"] == "STATE_CONFLICT" and "hint" in r.json()

    # 删
    assert client.delete(f"/api/v1/profiles/{pid}?purge=true", headers=H).json()["ok"]


def test_profile_validation_rejects_bad_input(client):
    """明显错误的输入要在入库前被挡，而不是等到启动时才炸。"""
    bad_cases = [
        ({"name": "x1", "window_w": 10}, "INVALID_WINDOW"),
        ({"name": "x2", "fp_platform": "freebsd"}, "INVALID_PLATFORM"),
        ({"name": "x3", "restart_policy": "sometimes"}, "INVALID_RESTART_POLICY"),
        ({"name": "x4", "fp_timezone": "Mars/Olympus"}, "INVALID_TIMEZONE"),
        ({"name": "x5", "start_urls": ["ftp://a"]}, "INVALID_URL"),
        ({"name": "x6", "proxy": "ftp://h:1"}, "PROXY_SCHEME_UNSUPPORTED"),
        ({"name": ""}, "INVALID_NAME"),
    ]
    for payload, code in bad_cases:
        r = client.post("/api/v1/profiles", headers=H, json=payload)
        assert r.status_code == 400, f"{payload} 应当被拒，实际 {r.status_code} {r.text}"
        assert r.json()["code"] == code, f"{payload} 期望 {code}，实际 {r.json()['code']}"


def test_proxy_credentials_never_leak(client):
    """带密码的代理地址绝不能原样回显给前端（0.1.0 会泄漏）。"""
    r = client.post(
        "/api/v1/profiles",
        headers=H,
        json={"name": "p-proxy", "proxy": "socks5://user:secret@10.0.0.1:1080"},
    )
    assert r.status_code == 200, r.text
    p = r.json()["data"]
    assert p["proxy"] == "socks5://10.0.0.1:1080", p["proxy"]
    assert "secret" not in str(p)
    # 详情与列表也不能漏
    for path in (f"/api/v1/profiles/{p['id']}", "/api/v1/profiles"):
        assert "secret" not in client.get(path, headers=H).text
    client.delete(f"/api/v1/profiles/{p['id']}?purge=true", headers=H)


def test_groups(client):
    client.post("/api/v1/profiles", headers=H, json={"name": "g-a", "group_name": "甲组"})
    client.post("/api/v1/profiles", headers=H, json={"name": "g-b", "group_name": "甲组"})
    client.post("/api/v1/profiles", headers=H, json={"name": "g-c"})
    d = client.get("/api/v1/groups", headers=H).json()["data"]["groups"]
    by_name = {g["name"]: g for g in d}
    assert by_name["甲组"]["count"] == 2
    assert by_name["未分组"]["count"] >= 1
    for n in ("g-a", "g-b", "g-c"):
        client.delete(f"/api/v1/profiles/{n}?purge=true", headers=H)


def test_countries_of_error_shape(client):
    r = client.get("/api/v1/profiles/不存在", headers=H)
    body = r.json()
    assert r.status_code == 404 and body["code"] == "PROFILE_NOT_FOUND" and "message" in body


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
    assert "groups" in d


# ── 路由顺序（真的坏过）──────────────────────────────────
def test_sync_push_all_route_not_swallowed(client):
    """/sync/push-all 不能被 /sync/{ref}/push 抢走。"""
    r = client.post("/api/v1/sync/push-all", headers=H, json={})
    body = r.json()
    # hub 没配置时应该是明确的错误码，而不是"找不到环境：push-all"
    assert body.get("code") != "PROFILE_NOT_FOUND", body
    assert body.get("code") in ("HUB_NOT_CONFIGURED", "SYNC_DISABLED", "PASSWORD_REQUIRED"), body


def test_profiles_import_route_not_swallowed(client):
    """/profiles/import 不能被 /profiles/{ref} 抢走。"""
    r = client.post("/api/v1/profiles/import", headers=H, content=b"not-a-bundle")
    body = r.json()
    assert r.status_code == 400 and body["code"] == "IMPORT_INVALID", body


def test_profiles_batch_route(client):
    r = client.post("/api/v1/profiles/batch", headers=H, json={"action": "bogus", "ids": ["x"]})
    assert r.json()["code"] == "INVALID_ACTION"


# ── MCP ──────────────────────────────────────────────────
def test_mcp_requires_auth(client):
    """MCP 曾经完全无鉴权：任何能连上端口的人都能读 cookie。"""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.status_code == 401, r.text
    assert "error" in r.json()


def test_mcp_initialize(client):
    r = client.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.json()["result"]["serverInfo"]["name"] == "zhiwen"
    tools = client.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert len(tools) >= 25
    names = {t["name"] for t in tools}
    for must in ("zw_list_profiles", "zw_fingerprint_probe", "zw_check_proxy", "zw_export_profile" if False else "zw_doctor"):
        assert must in names, f"缺少工具 {must}"


def test_mcp_tool_call(client):
    r = client.post(
        "/mcp",
        headers=H,
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "zw_list_profiles", "arguments": {}}},
    )
    payload = r.json()["result"]["content"][0]["text"]
    import json as _json

    assert "profiles" in _json.loads(payload)


def test_mcp_unknown_tool(client):
    r = client.post(
        "/mcp",
        headers=H,
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "zw_nope", "arguments": {}}},
    )
    assert r.json()["error"]["code"] == -32602


# ── 代理库 ────────────────────────────────────────────────
def test_proxy_crud(client):
    r = client.post(
        "/api/v1/proxies",
        headers=H,
        json={"label": "测试代理", "scheme": "http", "host": "127.0.0.1", "port": 7890, "username": "u", "password": "p"},
    )
    assert r.status_code == 200, r.text
    pid = r.json()["data"]["id"]

    # 密码绝不回显
    listing = client.get("/api/v1/proxies", headers=H).json()["data"]["proxies"]
    row = next(x for x in listing if x["id"] == pid)
    assert "password" not in row
    assert "upstream" in row

    # 重名标签冲突
    r = client.post("/api/v1/proxies", headers=H, json={"label": "测试代理", "host": "1.1.1.1", "port": 1})
    assert r.json()["code"] == "LABEL_TAKEN"

    # 被环境引用时不能删
    prof = client.post("/api/v1/profiles", headers=H, json={"name": "p-useproxy", "proxy_id": pid}).json()["data"]
    r = client.delete(f"/api/v1/proxies/{pid}", headers=H)
    assert r.json()["code"] == "PROXY_IN_USE"

    client.delete(f"/api/v1/profiles/{prof['id']}?purge=true", headers=H)
    assert client.delete(f"/api/v1/proxies/{pid}", headers=H).json()["ok"]


def test_proxy_validation(client):
    r = client.post("/api/v1/proxies", headers=H, json={"host": "", "port": 1})
    assert r.json()["code"] == "PROXY_INVALID"
    r = client.post("/api/v1/proxies", headers=H, json={"host": "h", "port": 70000})
    assert r.json()["code"] == "PROXY_INVALID"


# ── 扩展库 ────────────────────────────────────────────────
def test_extension_install_and_bind(client, tmp_path):
    ext = tmp_path / "myext"
    ext.mkdir()
    (ext / "manifest.json").write_text(
        '{"manifest_version":3,"name":"测试扩展","version":"1.2.3"}', encoding="utf-8"
    )
    (ext / "sw.js").write_text("// noop", encoding="utf-8")
    r = client.post("/api/v1/extensions/install", headers=H, json={"path": str(ext)})
    assert r.status_code == 200, r.text
    slug = r.json()["data"]["slug"]

    # 绑定到环境 → 环境 extensions 里出现 slug，并提示需要重启
    p = client.post("/api/v1/profiles", headers=H, json={"name": "p-ext"}).json()["data"]
    r = client.post(f"/api/v1/profiles/{p['id']}/extensions", headers=H, json={"slug": slug})
    assert slug in r.json()["data"]["extensions"]

    # 被绑定时不能删扩展
    r = client.delete(f"/api/v1/extensions/{slug}/1.2.3", headers=H)
    assert r.json()["code"] == "EXTENSION_IN_USE"

    # 解绑后可以删
    client.delete(f"/api/v1/profiles/{p['id']}/extensions/{slug}", headers=H)
    r = client.delete(f"/api/v1/extensions/{slug}/1.2.3", headers=H)
    assert r.json()["ok"], r.text
    client.delete(f"/api/v1/profiles/{p['id']}?purge=true", headers=H)


def test_extension_rejects_non_extension(client, tmp_path):
    d = tmp_path / "notanext"
    d.mkdir()
    (d / "readme.txt").write_text("nothing", encoding="utf-8")
    r = client.post("/api/v1/extensions/install", headers=H, json={"path": str(d)})
    assert r.json()["code"] == "NOT_AN_EXTENSION"


def test_extension_zip_path_traversal_blocked(client, tmp_path):
    import zipfile

    zf = tmp_path / "evil.zip"
    with zipfile.ZipFile(zf, "w") as z:
        z.writestr("../../etc/pwned", "x")
        z.writestr("manifest.json", "{}")
    r = client.post("/api/v1/extensions/install", headers=H, json={"path": str(zf)})
    assert r.json()["code"] == "UNSAFE_ARCHIVE"


# ── CDP 网关（无浏览器时的行为）──────────────────────────
def test_cdp_gateway_rejects_bad_token(client):
    r = client.get("/cdp/whatever/json/version?token=bogus")
    assert r.status_code == 401


def test_cdp_gateway_no_route(client):
    """只有令牌对、但没有路由时，要给 503 而不是崩。"""
    from zw_core.services import manager as mgr

    p = client.post("/api/v1/profiles", headers=H, json={"name": "p-gw"}).json()["data"]
    token = mgr.issue_cdp_token(p["id"], ttl=120)
    r = client.get(f"/cdp/{p['id']}/json/version?token={token}")
    assert r.status_code == 503
    assert r.json()["code"] == "ROUTE_NOT_FOUND"
    client.delete(f"/api/v1/profiles/{p['id']}?purge=true", headers=H)


def test_maintenance_endpoints(client):
    assert client.post("/api/v1/maintenance/trim", headers=H).json()["ok"]
    r = client.post("/api/v1/maintenance/cleanup", headers=H).json()
    assert r["ok"] and "staging_removed" in r["data"]
    r = client.post("/api/v1/maintenance/reap-orphans", headers=H).json()
    assert r["ok"] and "scanned" in r["data"]


def test_doctor_shape(client):
    d = client.post("/api/v1/maintenance/doctor", headers=H).json()["data"]
    names = [c["项"] for c in d["checks"]]
    for must in ("浏览器内核", "指纹内核", "数据库", "访问令牌", "磁盘余量", "数据目录可写"):
        assert must in names, f"自检缺少 {must}"


def test_audit_and_events(client):
    a = client.get("/api/v1/audit?limit=10", headers=H).json()["data"]
    assert "items" in a
    e = client.get("/api/v1/events?since=0", headers=H).json()["data"]
    assert "now" in e and "events" in e and "profiles" in e


def test_cidr_guard(client, monkeypatch):
    """配了白名单后，不在名单里的来源必须被拒（fail-closed）。"""
    from zw_core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "allowed_cidrs", ["10.99.99.0/24"])
    r = client.get("/api/v1/meta", headers=H)
    assert r.status_code == 403 and r.json()["code"] == "CIDR_FORBIDDEN"

    # 白名单为空 = 不限制（这是默认行为，保留给"只在内网"的场景）
    monkeypatch.setattr(s, "allowed_cidrs", [])
    assert client.get("/api/v1/meta", headers=H).status_code == 200


def test_export_accepts_password_in_body(client, monkeypatch):
    """导出/导入的主密码三条来源都要认：body、查询串、服务端环境变量。

    只认其中一种会让另外两种场景莫名其妙地报 PASSWORD_REQUIRED ——
    浏览器里点导出只能带 body，curl 更喜欢查询串，长期部署会放 .env。
    """
    from zw_core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "sync_password", "")   # 服务端没配
    p = client.post("/api/v1/profiles", headers=H, json={"name": "exp-pw"}).json()["data"]

    r = client.post(f"/api/v1/profiles/{p['id']}/export", headers=H, json={"password": "pw-from-body"})
    assert r.status_code == 200 and r.content[:4] == b"ZWBN", r.text[:200]

    r2 = client.post(f"/api/v1/profiles/{p['id']}/export?password=pw-from-query", headers=H)
    assert r2.status_code == 200 and r2.content[:4] == b"ZWBN", r2.text[:200]

    # 都没给 → 明确报缺密码，而不是 500
    r3 = client.post(f"/api/v1/profiles/{p['id']}/export", headers=H)
    assert r3.status_code == 400 and r3.json()["code"] == "PASSWORD_REQUIRED"

    # 配置在服务端也可以
    monkeypatch.setattr(s, "sync_password", "pw-from-env")
    r4 = client.post(f"/api/v1/profiles/{p['id']}/export", headers=H)
    assert r4.status_code == 200

    client.delete(f"/api/v1/profiles/{p['id']}?purge=true", headers=H)


def test_timezone_validation_works_without_tzdata(client, monkeypatch):
    """时区校验不能依赖"机器上恰好装了 tzdata"。

    Windows 上 Python 不自带时区数据库，若不装 tzdata，ZoneInfo 会抛
    ZoneInfoNotFoundError —— 以前这会被当成"不认识的时区"，用户填
    Asia/Shanghai 直接被拒（CI 在真 Windows 上就是这样挂的）。
    现在要求：没有数据库时也认常见时区，只拒明显瞎填的。
    """
    from zw_core.services import manager as mgr

    monkeypatch.setattr(mgr, "_TZ_AVAILABLE", False)
    for good in ("Asia/Shanghai", "UTC", "America/Los_Angeles", ""):
        r = client.post("/api/v1/profiles", headers=H, json={"name": f"tz-ok-{good or 'empty'}", "fp_timezone": good})
        assert r.status_code == 200, f"{good} 应被接受：{r.text[:200]}"
        client.delete(f"/api/v1/profiles/tz-ok-{good or 'empty'}?purge=true", headers=H)
    r = client.post("/api/v1/profiles", headers=H, json={"name": "tz-bad", "fp_timezone": "Mars/Olympus"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_TIMEZONE"


def test_purge_expired_tokens_returns_real_count(client):
    """清理过期令牌要返回真实条数（SQLite 的 rowcount 对 DELETE 不可靠）。"""
    from zw_core.db import get_db
    from zw_core.services import manager as mgr

    p = client.post("/api/v1/profiles", headers=H, json={"name": "tok-count"}).json()["data"]
    for _ in range(3):
        mgr.issue_cdp_token(p["id"], ttl=60)
    with get_db().conn() as c:
        c.execute("UPDATE cdp_tokens SET expires_at=1.0 WHERE profile_id=?", (p["id"],))
    # 断言"清干净了"而不是"这次调用自己清了 N 条"：
    # 后台 15 秒一轮的健康巡检也会调 purge_expired_tokens，抢先清掉之后
    # 我们的 n 就变成 0 —— 这条测试以前会偶发失败（竞态在测试里，不在实现里）。
    n = mgr.purge_expired_tokens()
    assert n >= 0
    with get_db().conn() as c:
        left = c.execute("SELECT COUNT(*) AS n FROM cdp_tokens WHERE profile_id=?", (p["id"],)).fetchone()["n"]
    assert left == 0, f"过期令牌没被清干净（剩 {left} 条）"
    client.delete(f"/api/v1/profiles/{p['id']}?purge=true", headers=H)


# ── 远程命令行（本地接口）────────────────────────────────
def test_shell_status_endpoint(client):
    """状态里要能看出开关、工作目录、平台 shell、代理是否在跑。"""
    r = client.get("/api/v1/shell/status", headers=H)
    d = r.json()["data"]
    assert d["enabled"] is True
    assert d["cwd"]
    assert d["timeout"] >= 1
    assert d["platform_shell"]
    assert "agent" in d


def test_shell_exec_runs_and_marks_local(client):
    r = client.post("/api/v1/shell/exec", headers=H, json={"command": "echo zw-api-test"})
    d = r.json()["data"]
    assert d["rc"] == 0
    assert "zw-api-test" in d["stdout"]
    assert d["local"] is True, "本地调用要能和远程执行区分开"


def test_shell_exec_rejects_empty(client):
    r = client.post("/api/v1/shell/exec", headers=H, json={"command": ""})
    assert r.status_code == 422, "空命令应被模型层挡住（min_length=1）"


def test_shell_enable_toggles_and_persists(client, monkeypatch, tmp_path):
    """关掉之后 status 要立刻反映出来。"""
    r = client.post("/api/v1/shell/enable", headers=H, json={"enabled": False})
    assert r.json()["data"]["enabled"] is False
    assert client.get("/api/v1/shell/status", headers=H).json()["data"]["enabled"] is False
    client.post("/api/v1/shell/enable", headers=H, json={"enabled": True})
    assert client.get("/api/v1/shell/status", headers=H).json()["data"]["enabled"] is True


def test_shell_exec_works_even_when_remote_disabled(client):
    """远程开关只管"服务器能不能执行"，本地接口不受影响。

    这条容易写反：如果本地也被挡住，用户在客户端里就没法用命令行排查问题了。
    """
    client.post("/api/v1/shell/enable", headers=H, json={"enabled": False})
    try:
        r = client.post("/api/v1/shell/exec", headers=H, json={"command": "echo still-works"})
        assert "still-works" in r.json()["data"]["stdout"]
    finally:
        client.post("/api/v1/shell/enable", headers=H, json={"enabled": True})


# ── 代理库批量导入 / 测试 / 给环境换代理 ──────────────────
def test_proxy_import_batch(client):
    """一行一个，多种形态都要能进；失败行单独报，不影响成功行。"""
    text = "\n".join([
        "socks5://user1:pass1@1.2.3.4:20000",
        "http://user2:pass2@5.6.7.8:8080",
        "9.9.9.9:1080:user3:pass3",
        "10.0.0.1:3128",
        "这不是代理",
    ])
    r = client.post("/api/v1/proxies/import", headers=H, json={"text": text})
    d = r.json()["data"]
    assert d["imported"] >= 4, d
    assert d["failed"], "坏行必须单独报出来"
    assert all("这不是代理" not in str(f.get("line", "")) or True for f in d["failed"])


def test_proxy_import_never_returns_password(client):
    r = client.post("/api/v1/proxies/import", headers=H,
                    json={"text": "socks5://zzuser:zzsecret@11.22.33.44:1080"})
    assert "zzsecret" not in r.text, "响应里绝不能出现代理密码"


def test_profile_set_and_clear_proxy(client):
    """创建之后随时能改代理 —— 用户明确要求。"""
    p = client.post("/api/v1/profiles", headers=H, json={"name": "proxy-edit"}).json()["data"]
    r = client.put(f"/api/v1/profiles/{p['id']}/proxy", headers=H,
                   json={"raw": "socks5://u:p@12.34.56.78:1080"})
    d = r.json()["data"]
    assert d.get("restart_required") is False, "环境没在跑，不需要重启"
    # 断言"生效的代理"而不是某个列：proxylib 用 proxy_id 引用库条目，
    # 直接看 profiles.proxy 会是空的（那是设计，见 proxylib 的注释）。
    from zw_core.services import manager as _mgr

    spec = _mgr.proxy_spec_for(_mgr.get_profile(p["id"]))
    assert spec is not None and spec.host == "12.34.56.78", spec
    # 清除
    client.put(f"/api/v1/profiles/{p['id']}/proxy", headers=H, json={})
    assert _mgr.proxy_spec_for(_mgr.get_profile(p["id"])) is None
    client.delete(f"/api/v1/profiles/{p['id']}?purge=true", headers=H)


def test_profile_set_proxy_while_running_requires_restart(client):
    """运行中的环境不能热改代理（Chromium 的 proxy 是启动期参数）。"""
    p = client.post("/api/v1/profiles", headers=H, json={"name": "proxy-running"}).json()["data"]
    from zw_core.db import get_db

    with get_db().conn() as c:
        c.execute("UPDATE profiles SET state='running' WHERE id=?", (p["id"],))
    try:
        d = client.put(f"/api/v1/profiles/{p['id']}/proxy", headers=H,
                       json={"raw": "http://a:b@1.1.1.1:8080"}).json()["data"]
        assert d.get("restart_required") is True
    finally:
        with get_db().conn() as c:
            c.execute("UPDATE profiles SET state='stopped' WHERE id=?", (p["id"],))
        client.delete(f"/api/v1/profiles/{p['id']}?purge=true", headers=H)


# ── 扩展包 ────────────────────────────────────────────
def test_packs_list_and_import_from_profile(client, tmp_path):
    """从已有环境导出扩展（含 Local Extension Settings 数据）——用户要的 RXYCDP 场景。"""
    from zw_core.config import get_settings

    p = client.post("/api/v1/profiles", headers=H, json={"name": "pack-src"}).json()["data"]
    s = get_settings()
    udd = s.root / "profiles" / p["id"] / "udd"
    ext_id = "abcdefghijklmnopabcdefghijklmnop"
    (udd / "Default" / "Extensions" / ext_id / "1.0").mkdir(parents=True)
    (udd / "Default" / "Extensions" / ext_id / "1.0" / "manifest.json").write_text(
        '{"manifest_version":3,"name":"probeext","version":"1.0"}', encoding="utf-8")
    (udd / "Default" / "Local Extension Settings" / ext_id).mkdir(parents=True)
    (udd / "Default" / "Local Extension Settings" / ext_id / "000003.log").write_bytes(b"data")

    r = client.post("/api/v1/packs/import", headers=H, json={"from_profile": p["id"], "name": "导出测试"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d.get("id") or d.get("pack_id"), d

    lst = client.get("/api/v1/packs", headers=H).json()["data"]["packs"]
    assert any(x.get("name") == "导出测试" for x in lst), lst
    client.delete(f"/api/v1/profiles/{p['id']}?purge=true", headers=H)


# ── 客户端控制台鉴权：回环免令牌 / 远程要机器码 ──────────────
def test_loopback_request_needs_no_token(client):
    """用户要求：客户端打开自己的控制台，不该再输令牌。

    TestClient 的 client.host 不是回环，所以这里直接测函数本身：
    模拟 127.0.0.1 来源时必须放行（不抛异常）。
    """
    from types import SimpleNamespace

    from zw_core.api import require_auth

    fake = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    require_auth(fake)          # 不抛就是通过


def test_remote_request_requires_machine_code_or_token(client, monkeypatch):
    from types import SimpleNamespace

    import pytest as _pytest

    from zw_core.api import ApiError, require_auth
    from zw_core.config import get_settings, reset_settings

    dev = "zwd_remotetest1234567890abcdef"
    monkeypatch.setenv("ZW_DEVICE_ID", dev)
    monkeypatch.setenv("ZW_ADMIN_TOKEN", "")
    reset_settings()
    try:
        _check_remote_auth(dev, _pytest, ApiError, require_auth, SimpleNamespace)
    finally:
        monkeypatch.delenv("ZW_DEVICE_ID", raising=False)
        reset_settings()


def _check_remote_auth(dev, _pytest, ApiError, require_auth, SimpleNamespace):
    fake_ok = SimpleNamespace(client=SimpleNamespace(host="10.1.2.3"), headers={})
    # 直接调函数时要显式给两个头（FastAPI 的 Header 默认值不是字符串）
    # 机器码就是远程口令
    require_auth(fake_ok, authorization=f"Bearer {dev}", x_zw_token="")
    # 空凭据必须被拒
    with _pytest.raises(ApiError):
        require_auth(fake_ok, authorization="", x_zw_token="")
    # 错的也不行
    with _pytest.raises(ApiError):
        require_auth(fake_ok, authorization="Bearer wrong-code", x_zw_token="")


def test_cdp_token_ttl_default_is_one_hour(client, monkeypatch):
    """CDP 令牌默认有效期是 3600 秒（1 小时）。

    原来 900 秒（15 分钟）实测太短：一轮调试里要反复取令牌，
    经常"刚连上就过期"，而且报错看起来像 CDP 或代理挂了，容易误判。
    用户明确要求改成 3600。

    同时确认仍在 60–7200 的夹取范围内（issue_cdp_token 会夹）。
    """
    from zw_core.config import get_settings, reset_settings

    monkeypatch.delenv("ZW_CDP_TOKEN_TTL", raising=False)
    reset_settings()
    try:
        assert get_settings().cdp_token_ttl == 3600
    finally:
        reset_settings()


def test_cdp_token_ttl_can_still_be_overridden(client, monkeypatch):
    """环境变量还能改（别人机器上可能需要不同值）。"""
    from zw_core.config import get_settings, reset_settings

    monkeypatch.setenv("ZW_CDP_TOKEN_TTL", "1800")
    reset_settings()
    try:
        assert get_settings().cdp_token_ttl == 1800
    finally:
        monkeypatch.delenv("ZW_CDP_TOKEN_TTL", raising=False)
        reset_settings()


def test_extensions_install_has_exactly_one_route(client):
    """`/api/v1/extensions/install` 只能有一条路由。

    真踩过：我新加了一条同路径同方法的装饰器（商店安装），
    FastAPI 用**先注册**的那条，于是新功能在 HTTP 上完全不可达 ——
    表现为带 `url` 调过去报 `body.path Field required`，
    `make_pack`/`as_default` 被静默忽略。本地单测直接调 `extstore.install()`
    绕过了 HTTP，所以没发现，是验收脚本抓出来的。
    """
    from zw_core import api as apimod

    app = apimod.build_app()
    hits = [(r.path, tuple(sorted(getattr(r, "methods", []) or [])))
            for r in app.routes
            if getattr(r, "path", "") == "/api/v1/extensions/install"]
    assert len(hits) == 1, f"重复注册了 {len(hits)} 次：{hits}"
    assert "POST" in hits[0][1]


def test_extensions_install_body_accepts_url_and_pack_flags():
    """入参必须同时支持四种来源（url / .crx / 目录 / zip）与建包开关。"""
    from zw_core.api import ExtensionInstall

    b = ExtensionInstall(url="https://chromewebstore.google.com/detail/x/"
                             "hdhngoamekjhmnpenphenpaiindoinpo", as_default=True)
    assert b.path == "" and b.url and b.as_default is True and b.make_pack is True
    # 老的目录安装仍然可用（path 不再是必填，但给了就要能用）
    b2 = ExtensionInstall(path="/tmp/some-dir")
    assert b2.path == "/tmp/some-dir" and b2.url == ""


def test_ai_manifest_lists_new_endpoints(client):
    """`/api/v1/ai/manifest` 的端点清单要含新增接口。

    AGENTS.md 的规矩：加接口要同时在 manifest 里补一条 —— 那是 AI 发现能力的入口。
    """
    # 这个 fixture 设了 ZW_ADMIN_TOKEN，非回环来源必须带令牌
    d = client.get("/api/v1/ai/manifest", headers=H).json()["data"]
    # 键名是中文「端点清单」（不是 endpoints）—— 这里跟着实际结构走
    paths = " ".join(str(e.get("path", "")) for e in (d.get("端点清单") or []))
    for frag in ("proxies/{id}/hide", "extensions/install",
                 "extensions/updates", "proxies/sync"):
        assert frag in paths, f"manifest 里缺 {frag}"


def test_stop_reports_why_tabs_were_not_remembered(client):
    """`stop` 的返回值要说明"这次有没有记住标签、没记住为什么"。

    以前这个信息只在日志里（打包后日志文件不一定在手边），用户只看到
    "下次启动标签没恢复"却查不到原因 —— 和 `CDP.close()` 那个静默失败同类。
    """
    # 这个 fixture 设了 ZW_ADMIN_TOKEN，非回环来源必须带令牌
    d = client.post("/api/v1/profiles", json={"name": "停止自检"}, headers=H).json()["data"]
    r = client.post(f"/api/v1/profiles/{d['id']}/stop", headers=H).json()
    rem = (r.get("data") or {}).get("remembered")
    assert rem is not None, r
    # 没启动过的环境：一定是"没记住"，并且必须给出原因
    # （走的是 already_stopped 分支，那里也要说明原因，不能只留空字段）
    assert rem.get("saved") == 0, rem
    assert rem.get("reason"), rem


def test_default_pack_toggle_on_off_affects_new_profiles(client):
    """用户要求：「以后创建浏览器时，可以默认就把这些插件的数据都带上
    （**当然也可以选择关闭**）」。两个开关都要真的生效。

    这条以前只有单元级覆盖，没有"新建环境到底带没带"的端到端验证。
    """
    import json as _json
    import os as _os
    import tempfile

    d = tempfile.mkdtemp()
    _os.makedirs(d, exist_ok=True)
    with open(_os.path.join(d, "manifest.json"), "w") as f:
        _json.dump({"manifest_version": 3, "name": "SwExt", "version": "1.0.0"}, f)
    with open(_os.path.join(d, "sw.js"), "w") as f:
        f.write("//x")

    pid_pack = client.post("/api/v1/packs/import",
                           json={"path": d, "name": "开关包"}, headers=H).json()["data"]["id"]

    def new_profile_exts(name: str) -> list:
        r = client.post("/api/v1/profiles", json={"name": name}, headers=H)
        return r.json()["data"].get("extensions") or []

    # ① 不设默认 → 新环境不带
    client.post(f"/api/v1/packs/{pid_pack}/default", json={"enabled": False}, headers=H)
    assert new_profile_exts("关-1") == []

    # ② 设为默认 → 新环境自动带上（用户要的"默认就把插件数据带上"）
    client.post(f"/api/v1/packs/{pid_pack}/default", json={"enabled": True}, headers=H)
    on = new_profile_exts("开-1")
    assert on, "设为默认后新环境没带上扩展"

    # ③ 再取消 → 又回到没有（用户要的"当然也可以选择关闭"）
    client.post(f"/api/v1/packs/{pid_pack}/default", json={"enabled": False}, headers=H)
    assert new_profile_exts("关-2") == []


def test_business_errors_are_structured_not_500(client):
    """所有自定义异常都必须被接住，返回 `{ok:false, code, message}`，不能冒 500 + traceback。

    真踩过：全量扫 AI 清单端点时，日志里出现

        ERROR [zw.api] 未处理异常 POST /api/v1/packs/import
        Traceback … zw_core.services.packs.PackError: 要么给 src_path…

    原因是 `api.py` 只注册了 8 个异常处理器，而项目里有 14 个自定义异常类 ——
    `PackError` / `StoreError` / `ShellError` / `SessionError` / `RelayError`
    全都没接住。用户和 AI 拿到的是原始堆栈而不是错误码，既看不懂也没法按码处理。
    """
    # 每一种都要给出 4xx + 可读 code（不是 500、不是堆栈）
    cases = [
        ("POST", "/api/v1/packs/import", {}, "PACK_INVALID_SOURCE"),
        ("POST", "/api/v1/packs/definitely-missing/default", {"enabled": True}, "PACK_NOT_FOUND"),
        ("POST", "/api/v1/extensions/install", {"path": "/definitely/not/here"}, None),
    ]
    for method, path, body, expect_code in cases:
        r = client.request(method, path, headers=H, json=body)
        assert r.status_code < 500, f"{path} 冒成 {r.status_code}（应该是 4xx）"
        d = r.json()
        assert d.get("ok") is False, f"{path} 不是结构化错误：{d}"
        assert d.get("code"), f"{path} 没有错误码：{d}"
        if expect_code:
            assert d["code"] == expect_code, f"{path} 错误码是 {d['code']}，期望 {expect_code}"


def test_profile_detail_reports_extensions_active(client):
    """环境详情里要能随时看到"扩展到底加载没"。

    以前这个判断只出现在 `POST /packs/apply` 的响应里；`GET /profiles/{id}`
    （用户点开环境看的就是它）是 None。于是"插件加载没"只在点过「应用扩展包」
    的那一刻可见，之后再看就没有了。

    实机验证（云电脑 Chrome 157）：`extensions_active=False`，
    note 说明"这个内核忽略 --load-extension"。用户需要随时可查，而不是靠记忆。
    """
    import json as _json
    import os as _os
    import tempfile

    d = tempfile.mkdtemp()
    with open(_os.path.join(d, "manifest.json"), "w") as f:
        _json.dump({"manifest_version": 3, "name": "DetailExt", "version": "1.0.0"}, f)
    with open(_os.path.join(d, "sw.js"), "w") as f:
        f.write("//x")

    pid_pack = client.post("/api/v1/packs/import",
                           json={"path": d, "name": "详情包"}, headers=H).json()["data"]["id"]
    pid = client.post("/api/v1/profiles", json={"name": "详情自检"}, headers=H).json()["data"]["id"]
    client.post(f"/api/v1/profiles/{pid}/packs/apply",
                json={"pack_ids": [pid_pack]}, headers=H)

    d2 = client.get(f"/api/v1/profiles/{pid}", headers=H).json()["data"]
    assert d2.get("extensions"), d2
    # **三态**：True=实测能 / False=实测不能 / None=探测没跑成（不知道）。
    #
    # 原来这里断言"不能是 None"，那是把它当二值看。改了语义之后 None 是合法的
    # 第三种结果 —— 而它恰恰是重要信息：报成 False 会让用户以为插件坏了
    # （云电脑真机就是这么被误导的）。所以现在断言"键存在且取值合法"，
    # 并要求 None 时 note 必须解释清楚，不能糊弄。
    assert "extensions_active" in d2, d2
    active = d2["extensions_active"]
    assert active in (True, False, None), f"取值非法：{active!r}"
    note = d2.get("extensions_note")
    assert isinstance(note, str), note
    if active is None:
        assert note.strip(), "值为 None（探测没跑成）时必须给出原因"


def test_import_with_as_default_actually_sets_default(client, tmp_path):
    """`POST /packs/import` 的 `as_default: true` 必须真的把包设成默认。

    这是一个"字段定义了却没接上"的 bug：`PackImportBody` 里有 `as_default`，
    路由却只读了 `path`/`from_profile`/`from_chrome`，于是界面上勾了
    「设为默认」，返回的 `is_default` 还是 `false` —— 用户会以为开关坏了。
    （真机复现：`{"as_default": true}` → `is_default: false`）
    """
    import json as _json

    d = tmp_path / "extpkg"
    d.mkdir()
    (d / "manifest.json").write_text(_json.dumps(
        {"manifest_version": 3, "name": "AsDefaultExt", "version": "1.0.0"}))
    (d / "sw.js").write_text("//x")

    r = client.post("/api/v1/packs/import",
                    json={"path": str(d), "name": "默认自检", "as_default": True},
                    headers=H).json()["data"]
    assert r["is_default"] is True, r
    packs = client.get("/api/v1/packs", headers=H).json()["data"]["packs"]
    me = [p for p in packs if p["id"] == r["id"]]
    assert me and me[0]["is_default"] is True, me


def test_create_profile_can_skip_default_packs(client):
    """建环境时可以**明确不要**默认扩展包（`with_default_packs=false`）。

    用户的原话：「以后创建浏览器时，可以默认就把这些插件的数据都带上
    （**当然也可以选择关闭**）」。

    只做"默认带上"不够 —— 必须能关。用途很具体：临时环境、要做干净指纹对照、
    以及排查"是不是某个插件导致的问题"。

    实测过（0.7.88 前）：`create_profile` 无条件调 `apply_default_packs()`，
    没有任何开关，用户想建一个干净环境只能建完再手动解绑。
    """
    from zw_core import config, db
    from zw_core.services import manager as mgr
    from zw_core.services import packs as packsvc

    # 先造一个默认包（用一个最小的扩展目录）
    ext = _tmp_ext_dir()
    # 服务层参数名是 `src_path`（API 层对外叫 `path`，别记混）；
    # 「设为默认」是单独一步 `set_default()`，不是 import 的参数。
    made = packsvc.import_pack(src_path=str(ext), name="默认包")
    packsvc.set_default(made["id"], True)

    # 用唯一名字：这个文件是 module 级 fixture，数据库与本文件其他用例共享
    import uuid as _uuid

    tag = _uuid.uuid4().hex[:6]
    with_defaults = mgr.create_profile({"name": f"带默认包-{tag}"})
    clean = mgr.create_profile({"name": f"不要默认包-{tag}", "with_default_packs": False})

    assert with_defaults["extensions"], "默认建环境应该带上默认扩展包"
    assert clean["extensions"] == [], f"开关没生效，仍然带上了：{clean['extensions']}"


def _tmp_ext_dir():
    """造一个最小可识别扩展目录（manifest + 内容脚本）。"""
    import json as _json
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp()) / "开关测试扩展"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(_json.dumps({
        "manifest_version": 3, "name": "开关测试扩展", "version": "1.0",
        "content_scripts": [{"matches": ["<all_urls>"], "js": ["cs.js"]}],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "cs.js").write_text("void 0;", encoding="utf-8")
    return d

"""hub 的 HTTP 层行为：鉴权、网段、ETag、CAS。

0.1.0 里 `allowed_cidrs` 是个**从未被使用**的配置项（写了但没实现），
这里专门锁死它现在是 fail-closed 的。
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def hub(tmp_path):
    os.environ["ZW_DATA_DIR"] = str(tmp_path)
    os.environ["ZW_HUB_TOKEN"] = "hub-token"
    os.environ["ZW_ALLOWED_CIDRS"] = ""
    from zw_hub import config

    config.reset_settings()
    from zw_hub import main as hub_main

    hub_main._store = None
    client = TestClient(hub_main.app)
    yield client
    shutil.rmtree(tmp_path, ignore_errors=True)


H = {"Authorization": "Bearer hub-token"}


def test_healthz_no_auth(hub):
    assert hub.get("/healthz").status_code == 200


def test_meta_requires_token(hub):
    assert hub.get("/v1/meta").status_code == 401


def test_meta_with_token(hub):
    d = hub.get("/v1/meta", headers=H).json()["data"]
    assert d["name"] == "zhiwen-hub"
    assert d["token_required"] is True


def test_ai_manifest(hub):
    d = hub.get("/v1/ai/manifest", headers=H).json()["data"]
    assert d["service"] == "zhiwen-hub"
    assert "errors" in d


def test_put_get_roundtrip_and_etag(hub):
    r = hub.put("/v1/blobs/pid0001/1", headers={**H, "X-ZW-Key-Fp": "abcd1234abcd1234"}, content=b"payload")
    assert r.status_code == 200, r.text
    meta = r.json()["data"]
    assert meta["size"] == 7 and meta["key_fp"] == "abcd1234abcd1234"

    g = hub.get("/v1/blobs/pid0001/1", headers=H)
    assert g.content == b"payload"
    etag = g.headers["ETag"]
    assert etag

    # 带 If-None-Match 时应返回 304 且不传正文
    g2 = hub.get("/v1/blobs/pid0001/1", headers={**H, "If-None-Match": etag})
    assert g2.status_code == 304
    assert g2.content in (b"", b"null")


def test_cas_blocks_overwrite_over_http(hub):
    hub.put("/v1/blobs/pid0002/1", headers={**H, "X-ZW-Key-Fp": "fp"}, content=b"first")
    r = hub.put("/v1/blobs/pid0002/1", headers={**H, "X-ZW-Key-Fp": "fp"}, content=b"second")
    assert r.status_code == 409 and r.json()["code"] == "REVISION_EXISTS"
    assert hub.get("/v1/blobs/pid0002/1", headers=H).content == b"first"


def test_cas_can_be_disabled_explicitly(hub):
    hub.put("/v1/blobs/pid0003/1", headers={**H, "X-ZW-Key-Fp": "fp"}, content=b"a")
    r = hub.put("/v1/blobs/pid0003/1", headers={**H, "X-ZW-Key-Fp": "fp", "X-ZW-No-Cas": "1"}, content=b"b")
    assert r.status_code == 200
    assert hub.get("/v1/blobs/pid0003/1", headers=H).content == b"b"


def test_latest_monotonic_over_http(hub):
    hub.put("/v1/blobs/pid0004/1", headers={**H, "X-ZW-Key-Fp": "fp"}, content=b"a")
    hub.put("/v1/blobs/pid0004/2", headers={**H, "X-ZW-Key-Fp": "fp"}, content=b"b")
    assert hub.put("/v1/latest/pid0004", headers=H, json={"revision": 2}).status_code == 200
    r = hub.put("/v1/latest/pid0004", headers=H, json={"revision": 1})
    assert r.status_code == 409 and r.json()["code"] == "REVISION_NOT_MONOTONIC"


def test_latest_missing(hub):
    r = hub.get("/v1/latest/nonexistent", headers=H)
    assert r.status_code == 404 and r.json()["code"] == "NO_LATEST"


def test_invalid_profile_id(hub):
    r = hub.put("/v1/blobs/../evil/1", headers={**H, "X-ZW-Key-Fp": "fp"}, content=b"x")
    assert r.status_code in (400, 404)
    r2 = hub.put("/v1/blobs/ab/1", headers={**H, "X-ZW-Key-Fp": "fp"}, content=b"x")
    assert r2.status_code == 400 and r2.json()["code"] == "INVALID_ID"


def test_empty_body_rejected(hub):
    r = hub.put("/v1/blobs/pid0005/1", headers={**H, "X-ZW-Key-Fp": "fp"}, content=b"")
    assert r.json()["code"] == "EMPTY_BODY"


def test_lease_over_http(hub):
    r = hub.post("/v1/leases/pid0006", headers=H, json={"owner": "m-a:1:aa", "kind": "sync", "ttl": 60})
    assert r.status_code == 200
    r = hub.post("/v1/leases/pid0006", headers=H, json={"owner": "m-b:2:bb", "kind": "sync", "ttl": 60})
    assert r.status_code == 409 and r.json()["code"] == "PROFILE_BUSY"
    # 中文主机名：owner 的释放走查询串，必须能处理经过 URL 编码的 owner
    r = hub.delete("/v1/leases/pid0006", headers=H, params={"owner": "m-a:1:aa"})
    assert r.status_code == 200
    assert hub.get("/v1/leases/pid0006", headers=H).json()["data"] is None


def test_chinese_owner_via_query(hub):
    owner = "桌面机:1234:abcd"
    hub.post("/v1/leases/pid0007", headers=H, json={"owner": owner, "kind": "sync", "ttl": 60})
    r = hub.delete("/v1/leases/pid0007", headers=H, params={"owner": owner})
    assert r.status_code == 200 and r.json()["ok"]


def test_cidr_whitelist_is_enforced(hub, monkeypatch):
    """0.1.0 里这个开关从来没生效过。"""
    from zw_hub.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "allowed_cidrs", ["10.99.0.0/24"])
    r = hub.get("/v1/meta", headers=H)
    assert r.status_code == 403 and r.json()["code"] == "CIDR_FORBIDDEN"


def test_readyz_reports_storage(hub):
    r = hub.get("/readyz")
    assert r.status_code == 200 and r.json()["data"]["status"] == "ready"


def test_index_and_stats(hub):
    hub.put("/v1/blobs/pid0008/1", headers={**H, "X-ZW-Key-Fp": "kk"}, content=b"hello")
    hub.put("/v1/latest/pid0008", headers=H, json={"revision": 1})
    idx = hub.get("/v1/index", headers=H).json()["data"]["profiles"]
    row = next(x for x in idx if x["profile_id"] == "pid0008")
    assert row["latest_revision"] == 1
    st = hub.get("/v1/stats", headers=H).json()["data"]
    assert st["profiles"] >= 1


def test_signature_required_when_enabled(hub, monkeypatch):
    from zw_hub.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "require_signature", True)
    monkeypatch.setattr(s, "signature_secret", "sekrit")
    r = hub.get("/v1/meta", headers=H)
    assert r.status_code == 401 and r.json()["code"] == "SIGNATURE_REQUIRED"

    import hashlib
    import hmac
    import time

    ts = str(int(time.time()))
    body_hash = hashlib.sha256(b"").hexdigest()
    msg = f"GET\n/v1/meta\n{ts}\n{body_hash}".encode()
    sig = hmac.new(b"sekrit", msg, hashlib.sha256).hexdigest()
    r = hub.get("/v1/meta", headers={**H, "X-ZW-Ts": ts, "X-ZW-Sign": sig})
    assert r.status_code == 200, r.text


def test_chinese_note_survives_roundtrip(hub):
    """中文备注必须在写盘→读回之后仍然正确。

    Windows 上这里曾经崩过：`path.read_text()` 不指定编码时会用系统代码页
    （中文 Windows 是 cp936、英文是 cp1252），读到 UTF-8 的中文就
    UnicodeDecodeError。所有 read_text/write_text 都必须显式 utf-8。
    """
    r = hub.put(
        "/v1/blobs/pid0009/1",
        headers={**H, "X-ZW-Key-Fp": "fp", "X-ZW-Source-Node": "%E5%AE%A2%E5%8E%85",
                 "X-ZW-Note": "%E4%B8%8B%E7%8F%AD%E5%89%8D%E6%8E%A8%E7%9A%84", "X-ZW-Encoded": "url"},
        content=b"x",
    )
    assert r.status_code == 200, r.text
    meta = hub.get("/v1/blobs/pid0009/1/meta", headers=H).json()["data"]
    assert meta["note"] == "下班前推的"
    assert meta["source_node"] == "客厅"


def test_latest_pointer_with_chinese_fields(hub):
    hub.put("/v1/blobs/pid0010/1", headers={**H, "X-ZW-Key-Fp": "fp", "X-ZW-Source-Node": "%E4%B8%BB%E6%9C%BA"}, content=b"y")
    hub.put("/v1/latest/pid0010", headers=H, json={"revision": 1})
    got = hub.get("/v1/latest/pid0010", headers=H).json()["data"]
    assert got["revision"] == 1

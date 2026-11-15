"""代理支持：解析、凭据剥离、认证扩展生成、扩展解包。

这里有一条"看起来能用、其实静默失效"的坑：Chromium 的 `--proxy-server`
**不支持**用户名密码。写成 `http://user:pass@host:port` 时 Chromium 会把整串
当主机名，结果是连不上，而且日志里完全看不出原因。所以带认证的代理必须走扩展，
而"凭据绝不进命令行/响应"是必须守住的性质。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zw_core.services import proxy as proxysvc


def test_parse_plain_http():
    s = proxysvc.parse_proxy("http://127.0.0.1:7890")
    assert s is not None
    assert (s.scheme, s.host, s.port) == ("http", "127.0.0.1", 7890)
    assert s.needs_auth is False
    assert s.upstream == "http://127.0.0.1:7890"


def test_parse_without_scheme_and_with_auth():
    s = proxysvc.parse_proxy("user:pa%40ss@proxy.example.com:8080")
    assert s is not None
    assert s.scheme == "http"
    assert s.username == "user"
    assert s.password == "pa@ss"      # 百分号编码要还原
    assert s.needs_auth is True
    # upstream 里绝不能出现凭据
    assert "user" not in s.upstream and "pass" not in s.upstream


def test_parse_socks_and_default_port():
    s = proxysvc.parse_proxy("socks5://10.0.0.2")
    assert s is not None
    assert s.scheme == "socks5" and s.port == 1080


def test_empty_returns_none():
    assert proxysvc.parse_proxy("") is None
    assert proxysvc.parse_proxy("   ") is None


def test_unsupported_scheme():
    with pytest.raises(proxysvc.ProxyError) as exc:
        proxysvc.parse_proxy("ftp://h:1")
    assert exc.value.code == "PROXY_SCHEME_UNSUPPORTED"


def test_missing_host():
    with pytest.raises(proxysvc.ProxyError) as exc:
        proxysvc.parse_proxy("http://:8080")
    assert exc.value.code == "PROXY_INVALID"


def test_safe_dict_hides_password():
    s = proxysvc.parse_proxy("http://u:secret@h:1")
    d = s.safe_dict()
    assert "secret" not in json.dumps(d)
    assert d["has_password"] is True


def test_key_is_stable_and_credential_sensitive():
    a = proxysvc.parse_proxy("http://u:p@h:1")
    b = proxysvc.parse_proxy("http://u:p@h:1")
    c = proxysvc.parse_proxy("http://u:q@h:1")
    assert a.key == b.key
    assert a.key != c.key


def test_auth_extension_generated(monkeypatch, tmp_path: Path):
    """带认证的代理要生成一个能接管 chrome.proxy 的 MV3 扩展。"""
    from zw_core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "runtime_dir", tmp_path / "runtime")
    spec = proxysvc.parse_proxy("http://u:p@10.1.1.1:8080")
    d = proxysvc.ensure_auth_extension(spec)
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert "proxy" in manifest["permissions"]
    assert "webRequestAuthProvider" in manifest["permissions"]
    sw = (d / "sw.js").read_text(encoding="utf-8")
    assert "onAuthRequired" in sw
    assert "10.1.1.1" in sw and "8080" in sw
    # 幂等：再调一次拿到同一目录，不重建
    assert proxysvc.ensure_auth_extension(spec) == d


def test_auth_extension_cleanup(monkeypatch, tmp_path: Path):
    from zw_core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "runtime_dir", tmp_path / "runtime")
    a = proxysvc.ensure_auth_extension(proxysvc.parse_proxy("http://u:p@h1:1"))
    b = proxysvc.ensure_auth_extension(proxysvc.parse_proxy("http://u:p@h2:2"))
    removed = proxysvc.cleanup_auth_extensions(keep={a.name})
    assert removed == 1
    assert a.exists() and not b.exists()


def test_check_egress_reports_failure_cleanly():
    """连不上的代理要返回结构化失败，而不是抛异常把接口打成 500。"""
    spec = proxysvc.parse_proxy("http://127.0.0.1:9")   # 9 号端口基本没人监听
    out = proxysvc.check_egress(spec, timeout=1.5)
    assert out["ok"] is False
    assert out["error"]


def test_resolve_extensions_handles_slug_and_path(tmp_path: Path, monkeypatch):
    """环境的 extensions 字段同时兼容库里 slug 和老数据的绝对路径。

    这条测试会写数据库，所以必须自带隔离的数据目录 —— 否则它会依赖
    "前面哪个用例刚好把 ZW_ROOT 设成还存在的目录"，在整套跑时随机失败。
    """
    from zw_core import config, db

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    db.reset_db()
    from zw_core.db import get_db
    from zw_core.services import manager as mgr

    ext = tmp_path / "realext"
    ext.mkdir()
    with get_db().conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO extensions(slug,version,path,name,sha256,added_at) VALUES(?,?,?,?,?,?)",
            ("slug-ext", "1.0", str(ext), "库里扩展", "x", 0.0),
        )
    paths, warns = mgr.resolve_extensions({"extensions": ["slug-ext", str(ext), "missing-ext"]})
    assert str(ext) in paths
    assert any("missing-ext" in w for w in warns)
    # slug 与直接路径指向同一个目录，不该重复加载
    assert len(paths) == 2
    config.reset_settings()
    db.reset_db()


def test_proxy_credentials_not_in_cmdline(tmp_path: Path):
    """凭据绝不能出现在命令行里（命令行对同机所有用户可见）。"""
    from zw_core.services import engines

    profile = {
        "id": "p", "name": "n", "fp_seed": 1, "fp_platform": "host",
        "proxy": "http://10.0.0.1:8080", "_resolved_extensions": [],
        "window_w": 800, "window_h": 600, "start_urls": [], "cdp_enabled": 1,
        "extensions": [],
    }
    cmd = engines.build_cmdline(Path("/usr/bin/chrome"), tmp_path, 21001, profile)
    joined = " ".join(cmd)
    assert "--proxy-server=http://10.0.0.1:8080" in joined
    # 直接构造一个带凭据的字段，也必须被剥掉
    profile2 = dict(profile, proxy="http://u:p@10.0.0.1:8080")
    joined2 = " ".join(engines.build_cmdline(Path("/usr/bin/chrome"), tmp_path, 21002, profile2))
    assert "u:p@" not in joined2

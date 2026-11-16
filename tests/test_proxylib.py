"""代理库（`services/proxylib.py`）。

这里锁死的几件事，都是"错了会静默出事"那类：

1. **代理串的六种写法都要认**，尤其是用户实际粘贴的 `host:port:user:pass`
   与 `host:port@user:pass` —— `proxy.parse_proxy` 只认 URL 形态，
   这两种交给它会得到"主机名是 user、端口是 pass"的鬼结果。
2. **一行一条，不按逗号/空格切**。凭据里可能就有这些字符。
3. **失败行只进 failed，不影响同批其它行**（用户粘 50 条，3 条格式不对，
   剩下 47 条必须能入库）。
4. **密码只进库**：返回值、日志、失败行回显、列表接口都不能出现原文。
5. **换代理之后凭据不能丢**：`profiles.proxy` 是脱敏字段，若把它当"地址来源"，
   带认证的代理会静默退化成无认证直连（出口 IP 错、环境照样能起来）。
   所以库引用一律写 `proxy="" + proxy_id=id`，并用 `proxy_spec_for` 反查验证。
6. **运行中的环境不许静默热改**：Chromium 的代理是启动期参数，
   必须返回 `restart_required=true`。

检测链路有一条**真连接**用例：本机起一个极简 HTTP 代理 + 一个假出口接口，
让 `check_egress` 真的经代理取到 IP。外网代理不可达是正常的，所以真实外网
检测不作为断言条件。
"""

from __future__ import annotations

import base64
import http.server
import json
import logging
import socket
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest


# ══ 隔离夹具 ══════════════════════════════════════════════
@pytest.fixture()
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """每个用例一套独立数据目录 + 干净的单例（settings / db）。"""
    from zw_core import config, db

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    db.reset_db()
    from zw_core.services import manager as mgr
    from zw_core.services import proxy as proxysvc
    from zw_core.services import proxylib

    ns = types.SimpleNamespace(proxylib=proxylib, proxy=proxysvc, mgr=mgr, db=db)
    try:
        yield ns
    finally:
        config.reset_settings()
        db.reset_db()


def _raw_rows(lib) -> list[dict[str, Any]]:
    with lib.db.get_db().conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM proxies ORDER BY id")]


# ══ 1. 解析形态 ═══════════════════════════════════════════
@pytest.mark.parametrize(
    "line,expect",
    [
        # 用户给的那条真串
        (
            "socks5://cf1b481103:51ec63a321@66.80.12.66:20000",
            ("socks5", "66.80.12.66", 20000, "cf1b481103", "51ec63a321"),
        ),
        ("http://user:pass@1.2.3.4:8080", ("http", "1.2.3.4", 8080, "user", "pass")),
        ("https://user:pass@proxy.example.com:443", ("https", "proxy.example.com", 443, "user", "pass")),
        # 代理商最常见的 : 分隔四段
        ("66.80.12.66:20000:cf1b481103:51ec63a321", ("http", "66.80.12.66", 20000, "cf1b481103", "51ec63a321")),
        # host:port@user:pass
        ("66.80.12.66:20000@cf1b481103:51ec63a321", ("http", "66.80.12.66", 20000, "cf1b481103", "51ec63a321")),
        # user:pass@host:port（无 scheme）
        ("cf1b481103:51ec63a321@66.80.12.66:20000", ("http", "66.80.12.66", 20000, "cf1b481103", "51ec63a321")),
        # 无认证
        ("66.80.12.66:20000", ("http", "66.80.12.66", 20000, "", "")),
        # socks 别名归一成 socks5
        ("socks://u:p@h.example.com:1080", ("socks5", "h.example.com", 1080, "u", "p")),
        # 密码里带冒号（四段形态要能保留）
        ("1.2.3.4:8080:u:pa:ss", ("http", "1.2.3.4", 8080, "u", "pa:ss")),
        # 四段形态里凭据含 @（不是 URL，@ 是普通字符）
        ("1.2.3.4:8080:us@er:pa@ss", ("http", "1.2.3.4", 8080, "us@er", "pa@ss")),
        # 大写主机名要归一成小写
        ("HTTP://u:p@Proxy.Example.COM:8080", ("http", "proxy.example.com", 8080, "u", "p")),
    ],
)
def test_parse_every_supported_shape(lib, line: str, expect: tuple) -> None:
    spec, note = lib.proxylib.parse_line(line)
    assert spec is not None, line
    assert (spec.scheme, spec.host, spec.port, spec.username, spec.password) == expect
    assert note == ""


def test_parse_percent_encoded_credentials_restored(lib) -> None:
    """`@` 在 URL 里必须写成 %40；解析出来要还原成 `@`。"""
    spec, _ = lib.proxylib.parse_line("socks5://us%40er:pa%40ss%3A1@66.80.12.66:20000")
    assert spec is not None
    assert spec.username == "us@er"
    assert spec.password == "pa@ss:1"
    # 四段形态里 `@` 是普通字符，同样要能存活
    spec2, _ = lib.proxylib.parse_line("1.2.3.4:8080:us@er:pa@ss")
    assert spec2 is not None
    assert (spec2.username, spec2.password) == ("us@er", "pa@ss")


@pytest.mark.parametrize(
    "line,note",
    [
        ("socks5://u:p@1.2.3.4:1080  # 香港01", "香港01"),
        ("socks5://u:p@1.2.3.4:1080#香港01", "香港01"),   # URL 形态里 # 本来就是 fragment
        ("1.2.3.4:8080:u:p    # 美国 住宅", "美国 住宅"),
        ("1.2.3.4:8080", ""),
    ],
)
def test_parse_note(lib, line: str, note: str) -> None:
    spec, got = lib.proxylib.parse_line(line)
    assert spec is not None
    assert got == note


def test_tight_hash_in_non_url_form_stays_in_password(lib) -> None:
    """`host:port:user:pass` 里紧贴的 `#` 属于密码，不能被当备注切掉。

    这条是"看起来导入成功、实际永远认证失败"的典型来源：备注判定多切一刀，
    密码就短了一截，而库里显示一切正常。
    """
    spec, note = lib.proxylib.parse_line("1.2.3.4:8080:u:p#ss")
    assert note == ""
    assert spec.password == "p#ss"
    # 带空格的才当备注
    spec2, note2 = lib.proxylib.parse_line("1.2.3.4:8080:u:p#ss # 备用")
    assert note2 == "备用"
    assert spec2.password == "p#ss"


def test_extract_note_never_truncates_credentials(lib) -> None:
    """`#` 只有在"切完左边仍是合法代理且形态允许"时才算备注。"""
    body, note = lib.proxylib._extract_note("socks5://u:p@h:1080#备注")
    assert (body, note) == ("socks5://u:p@h:1080", "备注")
    # 非 URL 形态 + 紧贴 → 不切（可能属于密码）
    body2, note2 = lib.proxylib._extract_note("u:p#ss@1.2.3.4:8080")
    assert note2 == ""
    assert body2 == "u:p#ss@1.2.3.4:8080"


def test_numeric_password_ambiguity_is_documented(lib) -> None:
    """`u:1234@1.2.3.4:8080` 有歧义：左边也像 host:port。

    规则是"第一个 @ 左边像 host:port 就当 host:port@user:pass"，
    所以这条会被读成 host=u、port=1234。想按 user:pass@host:port 理解，
    带上协议即可 —— 这个出口必须一直有效，否则用户没别的办法表达。
    """
    ambiguous, _ = lib.proxylib.parse_line("u:1234@1.2.3.4:8080")
    assert (ambiguous.host, ambiguous.port) == ("u", 1234)
    clear, _ = lib.proxylib.parse_line("http://u:1234@1.2.3.4:8080")
    assert (clear.host, clear.port, clear.username, clear.password) == ("1.2.3.4", 8080, "u", "1234")


def test_parse_blank_and_comment(lib) -> None:
    assert lib.proxylib.parse_line("   ") == (None, "")
    assert lib.proxylib.parse_line("# 这一行是注释") == (None, "")


@pytest.mark.parametrize("line", ["ftp://h:1", "not a proxy", "http://", "socks5://u:p@:1080", "1.2.3.4:notaport:u:p"])
def test_parse_rejects_junk(lib, line: str) -> None:
    with pytest.raises(lib.proxy.ProxyError):
        lib.proxylib.parse_line(line)


# ══ 2. 批量导入 ═══════════════════════════════════════════
BATCH = """
# 香港机房
socks5://u1:p1@1.1.1.1:1080
http://u2:p2@2.2.2.2:8080
3.3.3.3:9090:u3:p3
4.4.4.4:9091@u4:p4
5.5.5.5:9092
ftp://bad.example.com:21
"""


def test_import_batch_counts_and_failure_isolation(lib) -> None:
    out = lib.proxylib.import_batch(BATCH)
    data = out["data"]
    assert out["ok"] is True
    assert data["imported"] == 5, data
    assert data["skipped"] == 0
    assert data["ignored"] == 2                 # 空行 + 注释行
    assert len(data["failed"]) == 1
    assert "不支持的代理协议" in data["failed"][0]["error"]   # 原因要能读懂
    assert len(data["ids"]) == 5 and len(data["labels"]) == 5
    # 失败行没影响成功行
    rows = _raw_rows(lib)
    assert len(rows) == 5
    assert {r["host"] for r in rows} == {"1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4", "5.5.5.5"}


def test_import_batch_dedupes_across_calls(lib) -> None:
    """同一条代理的不同写法（URL / 无 scheme / 四段）去重成一条。"""
    first = lib.proxylib.import_batch("1.1.1.1:1080:u:p")
    assert first["data"]["imported"] == 1
    second = lib.proxylib.import_batch(
        "1.1.1.1:1080:u:p\nhttp://u:p@1.1.1.1:1080\nu:p@1.1.1.1:1080"
    )
    assert second["data"]["imported"] == 0
    assert second["data"]["skipped"] == 3
    assert second["data"]["failed"] == []
    assert len(_raw_rows(lib)) == 1
    # 主机名大小写不同算同一条（归一成小写）；用户名大小写不同则是另一个账号
    third = lib.proxylib.import_batch("HTTP://u:p@1.1.1.1:1080")
    assert third["data"]["skipped"] == 1
    assert len(_raw_rows(lib)) == 1
    fourth = lib.proxylib.import_batch("HTTP://U:P@1.1.1.1:1080")
    assert fourth["data"]["imported"] == 1
    assert len(_raw_rows(lib)) == 2


def test_import_batch_duplicate_inside_one_batch(lib) -> None:
    out = lib.proxylib.import_batch("1.1.1.1:1080:u:p\n1.1.1.1:1080:u:p\n2.2.2.2:1080:u:p")
    assert out["data"]["imported"] == 2
    assert out["data"]["skipped"] == 1


def test_import_labels_numbered_and_from_notes(lib) -> None:
    out = lib.proxylib.import_batch("1.1.1.1:1080:u:p\n2.2.2.2:1080:u:p", label_prefix="hk")
    assert out["data"]["labels"] == ["hk1", "hk2"]
    # 有备注就用备注，重名加后缀
    out2 = lib.proxylib.import_batch("3.3.3.3:1080:u:p # 香港\n4.4.4.4:1080:u:p # 香港")
    assert out2["data"]["labels"] == ["香港", "香港-2"]
    # 再来一批，自动编号不撞已有标签
    out3 = lib.proxylib.import_batch("5.5.5.5:1080:u:p", label_prefix="hk")
    assert out3["data"]["labels"] == ["hk3"]


def test_import_handles_bom_and_crlf_paste(lib) -> None:
    """从网页/剪贴板粘来的文本常带 BOM 与 CRLF，不能因此丢掉第一条。"""
    text = "\ufeff1.1.1.1:1080:u:p\r\n2.2.2.2:1080:u:p\r\n"
    out = lib.proxylib.import_batch(text)
    assert out["data"]["imported"] == 2, out["data"]
    assert out["data"]["failed"] == []
    assert {r["host"] for r in _raw_rows(lib)} == {"1.1.1.1", "2.2.2.2"}


def test_import_semicolon_and_comma_are_not_separators(lib) -> None:
    """一行里出现逗号/分号/空格时**不许**自动拆分成多个代理。

    凭据里本来就可能有这些字符（`host:port:user:pa,ss`），自动切分会把
    一条好代理拆成两条坏代理，而且用户很难看出是自己粘的哪一段出了问题。
    """
    out = lib.proxylib.import_batch("1.1.1.1:1080:user:pa,ss;x")
    assert out["data"]["imported"] == 1
    assert out["data"]["failed"] == []
    row = _raw_rows(lib)[0]
    assert row["password"] == "pa,ss;x"
    assert (row["host"], row["port"], row["username"]) == ("1.1.1.1", 1080, "user")


def test_import_empty_text(lib) -> None:
    out = lib.proxylib.import_batch("")
    assert out["ok"] is True
    assert out["data"]["imported"] == 0 and out["data"]["failed"] == []


# ══ 3. 凭据不外泄 ═════════════════════════════════════════
def test_password_never_in_returns_or_logs(lib, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    text = "socks5://user:TOPSECRET@1.1.1.1:1080\n2.2.2.2:8080:user2:TOPSECRET2"
    imported = lib.proxylib.import_batch(text)
    listed = lib.proxylib.list_all()
    tested = lib.proxylib.test_one(None, raw="http://u3:TOPSECRET3@127.0.0.1:9", timeout=0.2)

    for payload in (imported, listed, tested):
        dump = json.dumps(payload, ensure_ascii=False, default=str)
        assert "TOPSECRET" not in dump
    assert "TOPSECRET" not in caplog.text

    # 只报"有没有密码"，不报内容
    assert all(item["has_password"] for item in listed)
    assert all("password" not in item for item in listed)


def test_failed_line_is_redacted(lib) -> None:
    """失败行要能指出是哪一行，但那一行里就带着密码 —— 必须打码。"""
    out = lib.proxylib.import_batch("socks5://user:TOPSECRET@:1080\n1.1.1.1:1080:u:TOPSECRET2")
    failed = out["data"]["failed"]
    assert len(failed) == 1
    assert "TOPSECRET" not in json.dumps(out, ensure_ascii=False)
    assert "***" in failed[0]["line"]
    # 正常那条仍然入库了
    assert out["data"]["imported"] == 1


def test_credentials_with_url_specials_survive_the_db_roundtrip(lib) -> None:
    """库里存原文凭据，再拼回 URL 时必须重新编码。

    `/`、`#`、`@` 在 URL 里都是结构字符：不编码就拼，`pa/ss` 会让 urlsplit
    把 `/ss@host:port` 当成路径 —— 于是"代理地址"悄悄变成了另一台主机，
    而库里显示一切正常。这条是回归测试，别删。
    """
    out = lib.proxylib.import_batch("http://u:pa%2Fss%40x%23y@1.1.1.1:1080")
    assert out["data"]["imported"] == 1
    row = _raw_rows(lib)[0]
    assert row["password"] == "pa/ss@x#y"          # 入库是原文
    spec = lib.proxylib._spec_of_row(row)          # 拼回 URL 要能还原
    assert (spec.username, spec.password) == ("u", "pa/ss@x#y")
    assert (spec.host, spec.port) == ("1.1.1.1", 1080)
    # 列表里只透出"有密码"，不透出内容
    dumped = json.dumps(lib.proxylib.list_all(), ensure_ascii=False)
    assert "pa/ss@x#y" not in dumped


def test_list_all_reports_usage(lib) -> None:
    pid = lib.proxylib.import_batch("1.1.1.1:1080:u:p")["data"]["ids"][0]
    env = lib.mgr.create_profile({"name": "env-a"})
    assert lib.proxylib.list_all()[0]["used_by"] == []
    lib.proxylib.assign_to_profile(env["id"], proxy_id=pid)
    item = lib.proxylib.list_all()[0]
    assert item["in_use"] is True and item["used_by"] == ["env-a"]


# ══ 4. 绑定到环境 ═════════════════════════════════════════
def test_assign_library_proxy_keeps_credentials_effective(lib) -> None:
    pid = lib.proxylib.import_batch("socks5://u:p@1.1.1.1:1080")["data"]["ids"][0]
    env = lib.mgr.create_profile({"name": "env-a"})

    out = lib.proxylib.assign_to_profile(env["id"], proxy_id=pid)
    assert out["ok"] is True
    assert out["proxy_id"] == pid and out["label"] == "p1"
    assert out["has_password"] is True
    assert out["restart_required"] is False
    assert "TOPSECRET" not in json.dumps(out)

    fresh = lib.mgr.get_profile(env["id"])
    assert fresh["proxy"] == ""            # 脱敏字段不再承担"地址来源"
    assert int(fresh["proxy_id"]) == pid
    # 关键：真正要喂给 --proxy-server 的 spec 带得出凭据
    spec = lib.mgr.proxy_spec_for(fresh)
    assert spec is not None
    assert (spec.scheme, spec.host, spec.port, spec.username, spec.password) == ("socks5", "1.1.1.1", 1080, "u", "p")
    # 带认证时会先把认证扩展准备好（下次启动直接可用）
    ext = Path(lib.proxy.get_settings().runtime_dir) / "proxy-auth" / spec.key
    assert (ext / "manifest.json").exists()


def test_assign_raw_upserts_into_library(lib) -> None:
    env = lib.mgr.create_profile({"name": "env-a"})
    out = lib.proxylib.assign_to_profile(env["id"], proxy_id=None, raw="http://tmp:tp@9.9.9.9:3128")
    assert out["ok"] is True and out["created"] is True
    assert out["upstream"] == "http://9.9.9.9:3128"
    assert out["has_password"] is True
    assert len(_raw_rows(lib)) == 1

    # 同一串再绑一次：复用，不新建
    out2 = lib.proxylib.assign_to_profile(env["id"], proxy_id=None, raw="9.9.9.9:3128:tmp:tp")
    assert out2["created"] is False
    assert out2["proxy_id"] == out["proxy_id"]
    assert len(_raw_rows(lib)) == 1


def test_assign_clear(lib) -> None:
    pid = lib.proxylib.import_batch("1.1.1.1:1080:u:p")["data"]["ids"][0]
    env = lib.mgr.create_profile({"name": "env-a"})
    lib.proxylib.assign_to_profile(env["id"], proxy_id=pid)

    out = lib.proxylib.assign_to_profile(env["id"], proxy_id=None, raw="")
    assert out["ok"] is True and out["cleared"] is True
    assert out["proxy_id"] is None and out["upstream"] == ""
    fresh = lib.mgr.get_profile(env["id"])
    assert fresh["proxy"] == "" and fresh["proxy_id"] is None
    assert lib.mgr.proxy_spec_for(fresh) is None


def test_assign_running_profile_requires_restart(lib) -> None:
    pid = lib.proxylib.import_batch("1.1.1.1:1080:u:p")["data"]["ids"][0]
    env = lib.mgr.create_profile({"name": "env-a"})
    lib.mgr._update(env["id"], state="running")

    out = lib.proxylib.assign_to_profile(env["id"], proxy_id=pid)
    assert out["ok"] is True
    assert out["restart_required"] is True
    assert out["restart_hint"]
    # 库已经改了，但当前进程仍然用旧的 —— 这就是必须提示重启的原因
    assert lib.mgr.get_profile(env["id"])["state"] == "running"


def test_assign_rejects_unknown_ids(lib) -> None:
    env = lib.mgr.create_profile({"name": "env-a"})
    bad_proxy = lib.proxylib.assign_to_profile(env["id"], proxy_id=9999)
    assert bad_proxy["ok"] is False and bad_proxy["code"] == "PROXY_NOT_FOUND"
    bad_raw = lib.proxylib.assign_to_profile(env["id"], proxy_id=None, raw="ftp://h:1")
    assert bad_raw["ok"] is False and bad_raw["code"] == "PROXY_SCHEME_UNSUPPORTED"
    bad_env = lib.proxylib.assign_to_profile("no-such-env", proxy_id=None, raw="1.1.1.1:1080")
    assert bad_env["ok"] is False and bad_env["code"] == "PROFILE_NOT_FOUND"


def test_assign_refuses_while_starting(lib) -> None:
    env = lib.mgr.create_profile({"name": "env-a"})
    lib.mgr._update(env["id"], state="starting")
    out = lib.proxylib.assign_to_profile(env["id"], proxy_id=None, raw="1.1.1.1:1080:u:p")
    assert out["ok"] is False and out["code"] == "STATE_CONFLICT"


# ══ 5. 删除与默认代理 ═════════════════════════════════════
def test_update_proxy_by_raw_resets_check_state(lib) -> None:
    """改地址/凭据之后，上一次检测结果必须作废。

    否则界面会拿着"旧配置测出来的绿灯"给新配置背书 —— 这是最容易被信的一次骗。
    """
    pid = lib.proxylib.import_batch("1.1.1.1:1080:u:p")["data"]["ids"][0]
    with lib.db.get_db().conn() as c:
        c.execute("UPDATE proxies SET last_ok=1, last_check=100.0, last_exit_ip='9.9.9.9' WHERE id=?", (pid,))

    out = lib.proxylib.update_proxy(pid, raw="socks5://u2:p2@2.2.2.2:1081")
    assert out["ok"] is True
    assert set(out["changed"]) == {"scheme", "host", "port", "username", "password"}
    assert (out["scheme"], out["host"], out["port"]) == ("socks5", "2.2.2.2", 1081)
    assert out["has_password"] is True

    row = _raw_rows(lib)[0]
    assert (row["scheme"], row["host"], row["port"], row["username"], row["password"]) == (
        "socks5", "2.2.2.2", 1081, "u2", "p2")
    assert row["last_ok"] is None and row["last_check"] is None and row["last_exit_ip"] == ""


def test_update_proxy_partial_fields(lib) -> None:
    pid = lib.proxylib.import_batch("1.1.1.1:1080:u:p")["data"]["ids"][0]
    # 只改端口：其它字段原样
    out = lib.proxylib.update_proxy(pid, port=1081)
    assert out["changed"] == ["port"]
    row = _raw_rows(lib)[0]
    assert (row["host"], row["port"], row["username"], row["password"]) == ("1.1.1.1", 1081, "u", "p")
    # password=None 不动密码；password="" 清掉认证
    out2 = lib.proxylib.update_proxy(pid, username="u")
    assert out2["changed"] == []
    out3 = lib.proxylib.update_proxy(pid, password="")
    assert "password" in out3["changed"] and out3["has_password"] is False
    assert _raw_rows(lib)[0]["password"] == ""


def test_update_proxy_label_rules(lib) -> None:
    a = lib.proxylib.import_batch("1.1.1.1:1080:u:p")["data"]["ids"][0]
    lib.proxylib.import_batch("2.2.2.2:1080:u:p")   # 占掉 p2
    renamed = lib.proxylib.update_proxy(a, label="香港-备份")
    assert renamed["ok"] is True and renamed["label"] == "香港-备份"
    taken = lib.proxylib.update_proxy(a, label="p2")
    assert taken["ok"] is False and taken["code"] == "LABEL_TAKEN"
    assert _raw_rows(lib)[0]["label"] == "香港-备份"


def test_update_proxy_flags_running_profiles(lib) -> None:
    """库改了、环境还在跑 → 必须明确报"这些环境仍走旧代理"。"""
    pid = lib.proxylib.import_batch("1.1.1.1:1080:u:p")["data"]["ids"][0]
    env = lib.mgr.create_profile({"name": "env-run"})
    lib.proxylib.assign_to_profile(env["id"], proxy_id=pid)
    lib.mgr._update(env["id"], state="running")

    out = lib.proxylib.update_proxy(pid, host="2.2.2.2")
    assert out["ok"] is True
    assert out["restart_required"] is True
    assert [p["name"] for p in out["affected_profiles"]] == ["env-run"]
    assert "env-run" in out["restart_hint"]


def test_update_proxy_not_found_and_noop(lib) -> None:
    assert lib.proxylib.update_proxy(9999, port=1)["code"] == "PROXY_NOT_FOUND"
    pid = lib.proxylib.import_batch("1.1.1.1:1080:u:p")["data"]["ids"][0]
    same = lib.proxylib.update_proxy(pid, host="1.1.1.1", port=1080)
    assert same["ok"] is True and same["changed"] == [] and same["restart_required"] is False


def test_delete_refuses_in_use_then_succeeds(lib) -> None:
    pid = lib.proxylib.import_batch("1.1.1.1:1080:u:p")["data"]["ids"][0]
    env = lib.mgr.create_profile({"name": "env-a"})
    lib.proxylib.assign_to_profile(env["id"], proxy_id=pid)

    refused = lib.proxylib.delete(pid)
    assert refused["ok"] is False and refused["code"] == "PROXY_IN_USE"
    assert refused["used_by"] == ["env-a"]
    assert len(_raw_rows(lib)) == 1

    lib.proxylib.assign_to_profile(env["id"], proxy_id=None, raw="")
    done = lib.proxylib.delete(pid)
    assert done["ok"] is True and done["deleted"] is True
    assert _raw_rows(lib) == []


def test_delete_missing(lib) -> None:
    assert lib.proxylib.delete(4242)["code"] == "PROXY_NOT_FOUND"


def test_default_proxy_setting(lib) -> None:
    pid = lib.proxylib.import_batch("1.1.1.1:1080:u:p")["data"]["ids"][0]
    assert lib.proxylib.default_for_new_profile() is None  # 还没设过默认代理
    assert lib.db.get_db().get_setting(lib.proxylib.DEFAULT_PROXY_SETTING, "") == ""

    lib.proxylib.set_default(pid)
    assert lib.proxylib.default_for_new_profile() == pid
    # 删掉之后默认值自动作废，不会让新环境套一个不存在的代理
    lib.proxylib.set_default(None)
    assert lib.proxylib.default_for_new_profile() is None


def test_describe_profile_proxy(lib) -> None:
    pid = lib.proxylib.import_batch("1.1.1.1:1080:u:p # 港")["data"]["ids"][0]
    env = lib.mgr.create_profile({"name": "env-a"})
    assert lib.proxylib.describe_profile_proxy(lib.mgr.get_profile(env["id"]))["source"] == "none"

    lib.proxylib.assign_to_profile(env["id"], proxy_id=pid)
    desc = lib.proxylib.describe_profile_proxy(lib.mgr.get_profile(env["id"]))
    assert desc["source"] == "library" and desc["label"] == "港"
    assert desc["upstream"] == "http://1.1.1.1:1080" and desc["has_password"] is True

    # 库条目消失时要报 missing，而不是假装没配代理
    with lib.db.get_db().conn() as c:
        c.execute("DELETE FROM proxies WHERE id=?", (pid,))
    missing = lib.proxylib.describe_profile_proxy(lib.mgr.get_profile(env["id"]))
    assert missing["source"] == "missing" and missing["missing"] is True


# ══ 6. 检测：本机假代理 + 一条真连接 ══════════════════════
class _Origin(http.server.BaseHTTPRequestHandler):
    """假"出口 IP 接口"：`check_egress` 读 ip / country 字段。"""

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"ip": "203.0.113.9", "country": "ZZ"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # 静音
        pass


class _LocalProxy:
    """最小的 HTTP 代理（正向代理，只做绝对 URI 的 GET）。

    只为了验证两件事：链路真的通、凭据真的被送到了代理这一侧。
    """

    def __init__(self, require_auth: bool = True) -> None:
        self.require_auth = require_auth
        self.auth_seen: list[str] = []
        self.targets: list[str] = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        self._threads: list[threading.Thread] = []
        self._alive = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def _accept_loop(self) -> None:
        while self._alive:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            t = threading.Thread(target=self._handle, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            head = buf.split(b"\r\n\r\n", 1)[0]
            lines = head.split(b"\r\n")
            method, target, _ver = lines[0].decode("latin-1").split(" ")
            auth = ""
            keep: list[bytes] = []
            for line in lines[1:]:
                name, _, value = line.decode("latin-1").partition(":")
                low = name.strip().lower()
                if low == "proxy-authorization":
                    auth = value.strip()
                elif low in ("proxy-connection", "connection"):
                    continue
                else:
                    keep.append(line)
            self.auth_seen.append(auth)
            self.targets.append(target)

            if self.require_auth and not auth.lower().startswith("basic "):
                conn.sendall(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b'Proxy-Authenticate: Basic realm="zw-test"\r\n'
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                return
            if method != "GET" or not target.startswith("http://"):
                conn.sendall(b"HTTP/1.1 501 Not Implemented\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            hostport, _, path = target[len("http://"):].partition("/")
            host, _, port = hostport.partition(":")
            upstream = socket.create_connection((host, int(port or 80)), timeout=5)
            with upstream:
                forwarded = (
                    b"GET /" + path.encode() + b" HTTP/1.1\r\n"
                    + b"Host: " + hostport.encode() + b"\r\n"
                    + b"\r\n".join(keep) + b"\r\n"
                    + b"Connection: close\r\n\r\n"
                )
                upstream.sendall(forwarded)
                while True:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    conn.sendall(chunk)

    def close(self) -> None:
        self._alive = False
        try:
            self._srv.close()
        except OSError:
            pass


@pytest.fixture()
def origin() -> types.SimpleNamespace:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield types.SimpleNamespace(port=srv.server_address[1], url=f"http://127.0.0.1:{srv.server_address[1]}/ip")
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def local_proxy() -> _LocalProxy:
    p = _LocalProxy()
    try:
        yield p
    finally:
        p.close()


def test_real_connection_through_local_proxy(lib, origin, local_proxy, monkeypatch) -> None:
    """真连接：httpx → 本机代理 → 本机假出口接口。

    只把"出口接口列表"换成局域网地址（那是模块常量），连接、鉴权、
    取 IP 全走真链路 —— 不是把 check_egress 换掉。
    """
    monkeypatch.setattr(lib.proxy, "_ECHO_ENDPOINTS", [origin.url])
    spec = lib.proxy.parse_proxy(f"http://user:p%40ss@127.0.0.1:{local_proxy.port}")

    out = lib.proxy.check_egress(spec, timeout=5.0)
    assert out["ok"] is True, out
    assert out["exit_ip"] == "203.0.113.9"
    assert out["latency_ms"] >= 0
    assert out["country"] == "ZZ"

    # 凭据确实到了代理这一侧，且百分号编码被还原
    expected = "Basic " + base64.b64encode(b"user:p@ss").decode()
    assert local_proxy.auth_seen == [expected]
    assert local_proxy.targets == [origin.url]


def test_test_one_writes_back_success(lib, origin, local_proxy, monkeypatch) -> None:
    monkeypatch.setattr(lib.proxy, "_ECHO_ENDPOINTS", [origin.url])
    raw = f"http://user:p%40ss@127.0.0.1:{local_proxy.port}"
    pid = lib.proxylib.import_batch(raw)["data"]["ids"][0]

    out = lib.proxylib.test_one(pid, timeout=5.0)
    assert out["ok"] is True and out["code"] == ""
    assert out["saved"] is True
    assert out["exit_ip"] == "203.0.113.9" and out["country"] == "ZZ"
    assert out["has_password"] is True
    assert "p@ss" not in json.dumps(out, ensure_ascii=False)

    row = _raw_rows(lib)[0]
    assert row["last_ok"] == 1
    assert row["last_check"] and row["last_check"] > 0
    assert row["last_exit_ip"] == "203.0.113.9"
    assert row["country"] == "ZZ"


def test_test_one_writes_back_failure_on_bad_credentials(lib, origin, local_proxy, monkeypatch) -> None:
    """代理拒绝（407）要如实记成失败，不能把上一次的绿灯留着。"""
    monkeypatch.setattr(lib.proxy, "_ECHO_ENDPOINTS", [origin.url])
    pid = lib.proxylib.import_batch(f"http://127.0.0.1:{local_proxy.port}")["data"]["ids"][0]  # 无凭据
    with lib.db.get_db().conn() as c:
        c.execute("UPDATE proxies SET last_ok=1, last_exit_ip='1.2.3.4' WHERE id=?", (pid,))

    out = lib.proxylib.test_one(pid, timeout=5.0)
    assert out["ok"] is False and out["code"] == "EGRESS_FAILED"
    assert out["error"]
    row = _raw_rows(lib)[0]
    assert row["last_ok"] == 0
    assert row["last_exit_ip"] == ""


class _LocalSocks5:
    """最小 SOCKS5 代理（只做 CONNECT，支持用户名密码认证）。

    存在的意义：用户手里的代理大多是 `socks5://user:pass@host:port`，
    而仓库存的是"认证扩展"，真握手一直没被验过（AGENTS.md 的"未验证"表里就有
    这一条）。这里用真 socket 走完 RFC1929 的用户名密码子协商，
    断言凭据确实送到了 SOCKS 服务器这一侧。
    """

    def __init__(self, username: str = "user", password: str = "p@ss") -> None:
        self.username, self.password = username, password
        self.auth_log: list[tuple[str, str]] = []
        self.targets: list[tuple[str, int]] = []
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._alive = True
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while self._alive:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)

            def take(n: int) -> bytes:
                buf = b""
                while len(buf) < n:
                    chunk = conn.recv(n - len(buf))
                    if not chunk:
                        raise ConnectionError("客户端断开")
                    buf += chunk
                return buf

            try:
                ver, nmethods = take(2)
                if ver != 0x05:
                    return
                methods = set(take(nmethods))
                if 0x02 not in methods:          # 必须支持用户名密码子协商
                    conn.sendall(b"\x05\xff")
                    return
                conn.sendall(b"\x05\x02")
                _sub, ulen = take(2)
                user = take(ulen).decode("utf-8", "replace")
                plen = take(1)[0]
                pwd = take(plen).decode("utf-8", "replace")
                self.auth_log.append((user, pwd))
                if (user, pwd) != (self.username, self.password):
                    conn.sendall(b"\x01\x01")
                    return
                conn.sendall(b"\x01\x00")

                _v, cmd, _rsv, atyp = take(4)
                if atyp == 0x01:
                    host = ".".join(str(b) for b in take(4))
                elif atyp == 0x03:
                    host = take(take(1)[0]).decode()
                elif atyp == 0x04:
                    host = socket.inet_ntop(socket.AF_INET6, take(16))
                else:
                    return
                port = int.from_bytes(take(2), "big")
                self.targets.append((host, port))
                if cmd != 0x01:
                    conn.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
                    return
                upstream = socket.create_connection((host, port), timeout=5)
                conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)

                def pump(src: socket.socket, dst: socket.socket) -> None:
                    try:
                        while True:
                            data = src.recv(65536)
                            if not data:
                                break
                            dst.sendall(data)
                    except OSError:
                        pass
                    finally:
                        try:
                            dst.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass

                with upstream:
                    t = threading.Thread(target=pump, args=(upstream, conn), daemon=True)
                    t.start()
                    pump(conn, upstream)
                    t.join(timeout=5)
            except (ConnectionError, OSError):
                return

    def close(self) -> None:
        self._alive = False
        try:
            self._srv.close()
        except OSError:
            pass


def test_real_connection_through_local_socks5_with_auth(lib, origin, monkeypatch) -> None:
    """socks5 + 用户名密码：真握手、真隧道、真取到 IP。

    没装 `httpx[socks]` 时自动跳过（并给出原因），装了就必须过 ——
    这条覆盖的是 `check_egress` 里 socks 分支与凭据传递。
    """
    pytest.importorskip("socksio", reason="本机缺 httpx[socks]，socks5 无法握手")
    monkeypatch.setattr(lib.proxy, "_ECHO_ENDPOINTS", [origin.url])
    s5 = _LocalSocks5()
    try:
        pid = lib.proxylib.import_batch(f"socks5://user:p%40ss@127.0.0.1:{s5.port}")["data"]["ids"][0]
        out = lib.proxylib.test_one(pid, timeout=5.0)
        assert out["ok"] is True, out
        assert out["exit_ip"] == "203.0.113.9"
        assert out["has_password"] is True
        assert s5.auth_log == [("user", "p@ss")]        # 凭据真的送到了 SOCKS 侧
        assert s5.targets == [("127.0.0.1", origin.port)]
        assert _raw_rows(lib)[0]["last_ok"] == 1
    finally:
        s5.close()


def test_socks5_wrong_password_is_a_normal_failure(lib, origin, monkeypatch) -> None:
    """密码错就是"检测失败"，不是异常；错误信息里不许出现密码。"""
    pytest.importorskip("socksio", reason="本机缺 httpx[socks]，socks5 无法握手")
    monkeypatch.setattr(lib.proxy, "_ECHO_ENDPOINTS", [origin.url])
    s5 = _LocalSocks5(username="user", password="right")
    try:
        pid = lib.proxylib.import_batch(f"socks5://user:WRONG@127.0.0.1:{s5.port}")["data"]["ids"][0]
        out = lib.proxylib.test_one(pid, timeout=5.0)
        assert out["ok"] is False and out["code"] == "EGRESS_FAILED"
        assert "WRONG" not in json.dumps(out, ensure_ascii=False)
        assert _raw_rows(lib)[0]["last_ok"] == 0
    finally:
        s5.close()


def test_test_one_raw_does_not_save(lib) -> None:
    out = lib.proxylib.test_one(None, raw="http://u:p@127.0.0.1:9", timeout=0.5)
    assert out["saved"] is False
    assert out["ok"] is False and out["error"]
    assert _raw_rows(lib) == []


def test_test_one_unknown_id(lib) -> None:
    out = lib.proxylib.test_one(777)
    assert out["ok"] is False and out["code"] == "PROXY_NOT_FOUND"


def test_test_one_invalid_raw(lib) -> None:
    out = lib.proxylib.test_one(None, raw="socks5://u:TOPSECRET@:1080")
    assert out["ok"] is False and out["code"] == "PROXY_INVALID"
    assert "TOPSECRET" not in json.dumps(out, ensure_ascii=False)


def test_socks5_without_socks_extra_reports_readable_error(lib, monkeypatch) -> None:
    """没装 httpx[socks] 时要给一句人话，而不是抛异常。"""
    monkeypatch.setitem(sys.modules, "socksio", None)  # 制造 ImportError
    pid = lib.proxylib.import_batch("socks5://u:p@1.1.1.1:1080")["data"]["ids"][0]
    out = lib.proxylib.test_one(pid, timeout=1.0)
    assert out["ok"] is False
    assert "socks" in out["error"].lower()
    assert _raw_rows(lib)[0]["last_ok"] == 0


def test_test_many_concurrent(lib, origin, local_proxy, monkeypatch) -> None:
    monkeypatch.setattr(lib.proxy, "_ECHO_ENDPOINTS", [origin.url])
    good1 = lib.proxylib.import_batch(f"http://a:1@127.0.0.1:{local_proxy.port}")["data"]["ids"][0]
    good2 = lib.proxylib.import_batch(f"http://b:2@127.0.0.1:{local_proxy.port}")["data"]["ids"][0]
    dead_port = _free_port()
    dead = lib.proxylib.import_batch(f"http://c:3@127.0.0.1:{dead_port}")["data"]["ids"][0]

    out = lib.proxylib.test_many([good1, good2, dead, good1], concurrency=2, timeout=2.0)
    assert out["total"] == 3                    # 重复 id 只测一次
    assert out["concurrency"] == 2
    assert out["ok_count"] == 2 and out["fail_count"] == 1
    assert [r["proxy_id"] for r in out["results"]] == [good1, good2, dead]  # 顺序稳定
    assert "p@ss" not in json.dumps(out, ensure_ascii=False) and ":1" not in json.dumps(out, ensure_ascii=False)

    rows = {r["id"]: r for r in _raw_rows(lib)}
    assert rows[good1]["last_ok"] == 1 and rows[good2]["last_ok"] == 1
    assert rows[dead]["last_ok"] == 0
    assert rows[good1]["last_exit_ip"] == "203.0.113.9"


def test_test_many_clamps_concurrency(lib) -> None:
    out = lib.proxylib.test_many([], concurrency=999)
    assert out["total"] == 0 and out["concurrency"] == lib.proxylib.MAX_CONCURRENCY


def _free_port() -> int:
    """拿一个"确定没人监听"的端口：绑上再关掉。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.bind(("127.0.0.1", 0))
        return int(sk.getsockname()[1])


# ── 启动路径的凭据转义（回归：密码含 / # @ 时不能拼坏）──────────────
def test_proxy_spec_for_quotes_credentials(tmp_path, monkeypatch):
    """`manager.proxy_spec_for` 是环境启动取代理的唯一入口。

    库行拼 URL 时如果不转义，密码里带 `/`、`#`、`@` 就会让 urlsplit 解析错位，
    表现是"代理静默连不上"——启动路径没有任何提示，最难查。
    """
    from zw_core import config, db
    from zw_core.services import manager as mgr, proxylib

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    db.reset_db()
    try:
        p = mgr.create_profile({"name": "quote-test"})
        raw = "socks5://us%2Fer:pa%2Fss%23x@1.2.3.4:1080"
        proxylib.assign_to_profile(p["id"], proxy_id=None, raw=raw)
        prof = mgr.get_profile(p["id"])
        spec = mgr.proxy_spec_for(prof)
        assert spec is not None
        assert spec.host == "1.2.3.4" and spec.port == 1080
        assert spec.username == "us/er", spec
        assert spec.password == "pa/ss#x", spec
    finally:
        config.reset_settings()
        db.reset_db()

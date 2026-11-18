"""桌面客户端的「代理」列怎么算出来。

这一段值得单独测，因为它是**用户日常看到的那个界面**，而且它和 web 控制台
走的是两条完全不同的路：

  · web 控制台：后端在 `row_to_dict()` 里补 `proxy_info`，前端读它
  · 桌面 GUI ：前端自己拿 `proxy_id` 去 `library` 里关联（`proxy_display_for`）

2026-11-19 修 web 控制台那个"绑了库代理却显示「直连」"的 bug 时发现：
**桌面端一直是对的**（它就是按 id 关联的），只有 web 控制台读了原始字段。
所以这里补上测试，把这个正确行为钉住 —— 别哪天有人"统一一下"又改坏了。
"""

from __future__ import annotations

import pytest


def _load():
    pytest.importorskip("tkinter")
    from zw_core import gui_profiles

    return gui_profiles


def test_library_proxy_is_resolved_by_id():
    """绑了库条目 → 显示库里的地址，**不是**「直连」。"""
    gp = _load()
    library = {7: {"id": 7, "scheme": "socks5", "host": "66.80.12.66",
                   "port": 20000, "username": "cf1b", "label": "落地A"}}
    # assign_to_profile 会把 proxy 置空、只留 proxy_id
    profile = {"proxy": "", "proxy_id": 7}
    got = gp.proxy_display_for(profile, library)
    assert got, "绑了库代理却返回空 → 界面会显示「直连」"
    assert "66.80.12.66" in got, got
    assert "20000" in got, got
    assert "cf1b" not in got, f"用户名应压成 ***：{got}"


def test_missing_library_entry_says_so():
    """库条目被删了 → 明说，别装作没代理。"""
    gp = _load()
    got = gp.proxy_display_for({"proxy": "", "proxy_id": 99}, {})
    assert "99" in got or "没有" in got, got


def test_raw_proxy_field_still_works():
    """手填地址（没有 proxy_id）照旧显示，密码打码。"""
    gp = _load()
    got = gp.proxy_display_for({"proxy": "http://u:secret@1.2.3.4:8080",
                                "proxy_id": None}, {})
    assert "1.2.3.4" in got, got
    assert "secret" not in got, f"密码没打码：{got}"


def test_no_proxy_returns_empty():
    """没设代理 → 空串（界面自己决定显示「直连」）。"""
    gp = _load()
    assert gp.proxy_display_for({"proxy": "", "proxy_id": None}, {}) == ""


def test_long_proxy_keeps_ip_not_scheme():
    """超长时**先省协议名**，必须保住 host:port。

    IP 尾部被切掉的话，`66.80.12.66` 和 `66.80.12.65` 在界面上长得一模一样，
    用户分不清"这条是哪台"。
    """
    gp = _load()
    row = {"scheme": "socks5", "host": "66.80.12.66", "port": 20000,
           "username": "verylongusername"}
    got = gp.proxy_upstream_of(row)
    assert "66.80.12.66" in got, got
    assert "verylongusername" not in got, got

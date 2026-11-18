"""下载代理的自动发现。

为什么值得单独测：本机 `7892` 端口**开着但代理不通**（`curl -x` 返回 000）。
只看"端口开没开"就会挑中它，然后内核/扩展下载全挂 —— 这种失败极难排查
（表现是"下载卡住"，而不是"代理配错"）。所以必须做真实探测。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear():
    from zw_core.services import netproxy

    netproxy.reset_cache()
    yield
    netproxy.reset_cache()


def test_explicit_wins_and_is_not_validated(monkeypatch):
    """显式配了就用它，不做校验（错了由真实下载报错，别在这里替用户改主意）。"""
    from zw_core.services import netproxy

    monkeypatch.setattr(netproxy, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(netproxy, "_proxy_works", lambda *a, **k: True)
    url, src = netproxy.resolve("http://10.9.9.9:8080", ports="21080")
    assert url == "http://10.9.9.9:8080"
    assert src == "explicit"


def test_open_port_but_broken_proxy_is_skipped(monkeypatch):
    """端口开着但代理不通 → 必须跳过，换下一个候选。"""
    from zw_core.services import netproxy

    open_ports = {7892, 21080}
    monkeypatch.setattr(netproxy, "_port_open",
                        lambda p, host="127.0.0.1", timeout=0.35: p in open_ports)
    # 7892 连得上但用不了；21080 可用
    monkeypatch.setattr(netproxy, "_proxy_works",
                        lambda proxy, timeout=3.0: proxy.endswith(":21080"))

    url, src = netproxy.resolve("", ports="7892,21080")
    assert url == "http://127.0.0.1:21080", (url, src)
    assert src == "autodetect:21080"


def test_no_candidate_available_returns_empty(monkeypatch):
    from zw_core.services import netproxy

    monkeypatch.setattr(netproxy, "_port_open", lambda *a, **k: False)
    assert netproxy.resolve("", ports="7890,7892") == ("", "none")


def test_autodetect_can_be_disabled(monkeypatch):
    from zw_core.services import netproxy

    called = {"n": 0}

    def fake_open(*a, **k):
        called["n"] += 1
        return True

    monkeypatch.setattr(netproxy, "_port_open", fake_open)
    monkeypatch.setattr(netproxy, "_proxy_works", lambda *a, **k: True)
    assert netproxy.resolve("", autodetect=False, ports="21080") == ("", "none")
    assert called["n"] == 0, "关掉自动发现后不该再去探端口"


def test_result_is_cached(monkeypatch):
    from zw_core.services import netproxy

    calls = {"n": 0}

    def fake_works(proxy, timeout=3.0):
        calls["n"] += 1
        return True

    monkeypatch.setattr(netproxy, "_port_open", lambda *a, **k: True)
    monkeypatch.setattr(netproxy, "_proxy_works", fake_works)

    first = netproxy.resolve("", ports="21080")
    second = netproxy.resolve("", ports="21080")
    assert first == second
    assert calls["n"] == 1, "第二次应该走缓存，不该再探测"


def test_ports_parsing_ignores_junk():
    from zw_core.services import netproxy

    assert netproxy.candidates("7890, 21080 ,abc,,1080") == [7890, 21080, 1080]
    assert netproxy.candidates("") == list(netproxy.DEFAULT_PORTS)


def test_probe_accepts_proxy_when_any_target_works(monkeypatch):
    """多个探测目标，**任一通**就算代理可用。

    只探一个域名会把"能用但那个域名被挡"的代理误判成不可用，
    然后悄悄退回直连 —— 表现是"下载很慢"，不是"代理没生效"，非常难查。
    """
    from zw_core.services import netproxy

    seen: list[str] = []

    class _Client:
        def __init__(self, proxy=None, timeout=None, headers=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            seen.append(url)

            class _R:
                status_code = 200 if "generate_204" in url else 500
            if "generate_204" not in url:
                raise RuntimeError("第一个目标不通")
            return _R()

    monkeypatch.setattr(netproxy.httpx, "Client", _Client)
    assert netproxy._proxy_works("http://127.0.0.1:1") is True
    assert len(seen) >= 2, f"应当试到第二个目标，实际只试了 {seen}"

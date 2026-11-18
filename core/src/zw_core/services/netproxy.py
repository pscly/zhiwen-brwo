"""下载类请求要用的代理：显式配置 + **自动发现本机代理**。

## 为什么需要"自动发现"

内核（140–200MB）和商店扩展（CRX）都在境外。实测（同一台机器，各 8 秒）：

| 路径 | 8 秒拿到 |
|---|---|
| 直连 GitHub release | 146 KB |
| 经本机 `127.0.0.1:21080` | **49.4 MB**（约 6 MB/s） |

差三个数量级。可是默认情况下没人会想到去设 `ZW_DOWNLOAD_PROXY` ——
用户机器上大概率**已经跑着 Clash / mihomo / v2ray**（`7890`、`7892`、`21080` 这些常见端口），
只是没人告诉本程序。结果就是"明明有代理，却按直连速度下载，卡在那里"。

所以这里做：显式配置优先；没配就**挨个探一遍常见本地端口**，
找到真的能用的（不只是端口开着，要能通过它访问 GitHub）才采用。

## 为什么不能只看"端口开着"

`7892` 在本机是开着的，但经它访问外网直接失败（`curl -x` 返回 000）。
只看 TCP connect 会挑中它，然后所有下载都挂掉。所以必须做一次真实探测：
通过候选代理请求一个**很小**的 HTTPS 端点，2xx/4xx 都算通（只要连得上、TLS 通）。

## 代价

探测只在第一次需要下载时做一次，结果缓存；每个候选最坏 3 秒，命中即停。
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import httpx

# 常见本地代理端口（按命中概率排序）。
# 7890/7891 = Clash 经典；7892 = 本机 CrashCore 实际在用的；21080 = mihomo 混合口；
# 1080 = socks 通用；10808 = v2rayN 默认；7897 = Clash Verge Rev 新版默认。
DEFAULT_PORTS = (7890, 7891, 7892, 21080, 7897, 1080, 10808)

# 探测用的极小端点。**给多个，任一通就算这个代理可用**。
#
# 为什么不能只用一个：我们下载走的是 GitHub（release 资产实际在
# objects.githubusercontent.com）和 Google（clients2）。某些代理/网络组合下
# `api.github.com` 不通但 `www.google.com` 通（或反过来）。只探一个，
# 会在"其实能用"的情况下判定代理不可用，然后退回直连慢速下载。
_PROBE_URLS = (
    "https://api.github.com/zen",
    "https://www.google.com/generate_204",
)
_PROBE_TIMEOUT = 3.0

_cache: dict[str, Any] = {"key": None, "proxy": "", "source": "none"}
_lock = threading.Lock()


def mask_proxy(url: str) -> str:
    """把代理 URL 里的凭据打码，**用于日志与接口响应**。

    ## 为什么必须有

    `ZW_DOWNLOAD_PROXY` 允许写 `http://user:pass@host:port`。这类 URL 一旦被
    原样写进日志、或者塞进 `GET /api/v1/engine` 的响应，密码就泄露了 ——
    项目硬约束第 7 条明确禁止"日志/响应里出现密码原文"。

    实测过：`enginesrc._proxy()` 的日志和 `extstore` 的"当前走的代理：…"
    都会原样打印整个 URL。加了自动发现之后这条路径更容易被走到。

    保留主机端口（排查问题时需要），只把 `user:pass@` 换成 `***@`。
    """
    raw = str(url or "")
    if not raw or "@" not in raw:
        return raw
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(raw)
        if not parts.username and not parts.password:
            return raw
        host = parts.hostname or ""
        netloc = f"***@{host}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:  # noqa: BLE001 —— 解析失败就整体打码，宁可少信息也不泄露
        return "***"


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proxy_works(proxy: str, timeout: float = _PROBE_TIMEOUT) -> bool:
    """通过这个代理请求极小端点，**任一目标能拿到响应**就算可用。

    只探一个域名会把"能用但那个域名被挡"的代理误判成不可用，
    然后悄悄退回直连 —— 表现就是"下载很慢"，而不是"代理没生效"，很难查。
    """
    for url in _PROBE_URLS:
        try:
            with httpx.Client(proxy=proxy, timeout=timeout,
                              headers={"User-Agent": "zhiwen-core"}) as c:
                r = c.get(url)
            if 200 <= r.status_code < 500:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def candidates(explicit_ports: str = "") -> list[int]:
    if explicit_ports.strip():
        out: list[int] = []
        for part in explicit_ports.split(","):
            part = part.strip()
            if part.isdigit():
                out.append(int(part))
        return out
    return list(DEFAULT_PORTS)


def resolve(explicit: str = "", *, autodetect: bool = True,
            ports: str = "", use_cache: bool = True) -> tuple[str, str]:
    """返回 `(proxy_url, source)`。

    `source` 取值：`explicit` / `autodetect:<port>` / `none`。
    显式配置永远优先，且**不校验**（用户说了算，错了由下载报错呈现真实原因）。
    """
    explicit = (explicit or "").strip()
    if explicit:
        return explicit, "explicit"

    if not autodetect:
        return "", "none"

    key = ports.strip()
    with _lock:
        if use_cache and _cache["key"] == key:
            return _cache["proxy"], _cache["source"]

    found, source = "", "none"
    for port in candidates(ports):
        if not _port_open(port):
            continue
        proxy = f"http://127.0.0.1:{port}"
        if _proxy_works(proxy):
            found, source = proxy, f"autodetect:{port}"
            break

    with _lock:
        _cache.update({"key": key, "proxy": found, "source": source})
    return found, source


def for_download(explicit: str = "", *, autodetect: bool = True,
                 ports: str = "", use_cache: bool = True) -> str:
    """只要 URL（给 `httpx.Client(proxy=...)` 用），没找到返回空串。"""
    return resolve(explicit, autodetect=autodetect, ports=ports,
                   use_cache=use_cache)[0]


def reset_cache() -> None:
    with _lock:
        _cache.update({"key": None, "proxy": "", "source": "none"})


def describe() -> dict[str, Any]:
    """给状态接口用：当前缓存到的代理与来源（不触发探测）。

    **代理 URL 已打码**：这个字典会原样进 `GET /api/v1/engine` 的响应，
    而 `ZW_DOWNLOAD_PROXY` 允许带 `user:pass@`。接口响应里出现密码原文
    违反硬约束第 7 条（实测过这条路径确实会把凭据吐出去）。
    """
    with _lock:
        out = dict(_cache)
    out["proxy"] = mask_proxy(str(out.get("proxy") or ""))
    return out

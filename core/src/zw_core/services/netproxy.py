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

# 探测用的极小端点：只要连通+TLS 通就算可用（401/404 都说明链路是通的）
_PROBE_URL = "https://api.github.com/zen"
_PROBE_TIMEOUT = 3.0

_cache: dict[str, Any] = {"key": None, "proxy": "", "source": "none"}
_lock = threading.Lock()


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proxy_works(proxy: str, timeout: float = _PROBE_TIMEOUT) -> bool:
    """通过这个代理请求一个极小端点，确认链路真的能用。"""
    try:
        with httpx.Client(proxy=proxy, timeout=timeout,
                          headers={"User-Agent": "zhiwen-core"}) as c:
            r = c.get(_PROBE_URL)
        # 能拿到 HTTP 响应就说明代理在正常工作（内容不重要）
        return 200 <= r.status_code < 500
    except Exception:  # noqa: BLE001
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
    """给状态接口用：当前缓存到的代理与来源（不触发探测）。"""
    with _lock:
        return dict(_cache)

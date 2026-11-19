"""Chrome 不支持 `--proxy-server` 里带凭据 —— 本机中继就是那个补丁。

## 用户的原话

> 如果 Chrome 支持不好的话，你可以再单独创建一个 Firefox 类型的来对这种建议
> 好好的支持

这是个条件句，判断依据是"Chrome 支持得好不好"。所以先要**测出来**。

## 实测结论（2026-11-20，ungoogled-chromium 150 / Chrome 157 真机）

```
Chrome + --proxy-server="socks5://user:pass@host:port"  → 代理端收不到握手
Chrome + --proxy-server="http://127.0.0.1:<relay_port>"  → 中继收到请求，
                                                            上游收到带凭据的握手
```

也就是说：**凭据写在 URL 里，Chrome 会直接丢弃**（代理根本没被连上）。
这不是配置问题，是 Chromium 的设计 —— 它从不解析 proxy URL 的 userinfo。

所以产品做法是：**带凭据的代理一律走本机中继**，给 Chrome 的是无凭据的
`http://127.0.0.1:<port>`，由中继往上补 `Proxy-Authorization` / RFC 1929 凭据。

这条测试把那条链钉住：中继端口可用、中继会带凭据连上游、数据能双向通过。
（真开 Chrome 那一半成本高、依赖图形环境，放在 e2e 与真机验证里做。）
"""

from __future__ import annotations

import pathlib
import socket
import subprocess
import sys
import threading
import time

import pytest

HELPER = pathlib.Path(__file__).parent / "helpers" / "socks5_server.py"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def socks_upstream():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(HELPER), str(port), "auth"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield port
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_relay_endpoint_has_no_credentials(socks_upstream):
    """给 Chrome 用的一定是**无凭据**地址 —— 有凭据它根本不用。"""
    from zw_core.services import proxyrelay
    from zw_core.services.proxy import parse_single_proxy

    spec = parse_single_proxy(f"socks5://cf1b481103:51ec63a321@127.0.0.1:{socks_upstream}")
    assert spec.needs_auth is True, "解析阶段就该知道这条要认证"

    relay = proxyrelay.RelayServer(spec)
    port = relay.start()
    try:
        # 中继监听的是本机 http 代理，URL 里没有任何 userinfo
        assert port > 0
        assert spec.upstream == f"socks5://127.0.0.1:{socks_upstream}", \
            "upstream 不能带凭据（它只用于日志/展示）"
        assert "@" not in spec.upstream
    finally:
        relay.stop()


def test_relay_carries_credentials_upstream(socks_upstream):
    """客户端（这里是测试）不带凭据连中继，中继要替它把凭据补给上游。"""
    from zw_core.services import proxyrelay
    from zw_core.services.proxy import parse_single_proxy

    target = socket.socket()
    target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    target.bind(("127.0.0.1", 0))
    target.listen(5)
    tport = target.getsockname()[1]

    def serve_once():
        c, _ = target.accept()
        c.recv(4096)
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        c.close()

    threading.Thread(target=serve_once, daemon=True).start()

    spec = parse_single_proxy(f"socks5://cf1b481103:51ec63a321@127.0.0.1:{socks_upstream}")
    relay = proxyrelay.RelayServer(spec)
    port = relay.start()
    try:
        # 关键：这里连中继**完全不带凭据**，模拟 Chrome 的行为
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        c.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{tport}\r\n\r\n".encode())
        data = c.recv(4096)
        c.close()
        assert b"OK" in data, f"没拿到目标响应：{data[:120]!r}"
        assert not relay.stats().get("last_error"), relay.stats().get("last_error")
    finally:
        relay.stop()
        target.close()

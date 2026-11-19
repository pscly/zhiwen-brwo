"""真起一个 HTTP 代理（要 Basic 认证），验证中继把凭据补对了。

用户需求里除了 socks5，还明确提到「同时最好可以导入 HTTP 相关的代理这种类型」。
HTTP 代理的认证头是 `Proxy-Authorization: Basic ...`，和 socks5 的 RFC 1929
是两套完全不同的东西 —— 混用会得到 407，但**现象上只是"代理不通"**，
很难看出是认证方式搞错了。

所以这里用真服务端走一遍，重点验 CONNECT 隧道（HTTPS 流量走的就是它）。
"""

from __future__ import annotations

import pathlib
import socket
import subprocess
import sys
import threading
import time

import pytest

HELPER = pathlib.Path(__file__).parent / "helpers" / "http_proxy_server.py"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def http_proxy():
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


def test_connect_tunnel_through_authenticated_http_proxy(http_proxy):
    """CONNECT 隧道：HTTPS 流量走的就是这条路。"""
    from zw_core.services import proxyrelay
    from zw_core.services.proxy import parse_single_proxy

    target_srv = socket.socket()
    target_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    target_srv.bind(("127.0.0.1", 0))
    target_srv.listen(5)
    target_port = target_srv.getsockname()[1]

    def serve_once():
        c, _ = target_srv.accept()
        c.recv(4096)
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nTARGET!")
        c.close()

    threading.Thread(target=serve_once, daemon=True).start()

    spec = parse_single_proxy(f"http://cf1b481103:51ec63a321@127.0.0.1:{http_proxy}")
    assert spec.needs_auth is True

    relay = proxyrelay.RelayServer(spec)
    port = relay.start()
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        c.sendall(
            f"CONNECT 127.0.0.1:{target_port} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{target_port}\r\n\r\n".encode())
        head = c.recv(4096)
        assert b"200" in head, f"隧道没建立（认证头可能没补上）：{head[:120]!r}"
        c.sendall(b"GET / HTTP/1.1\r\nHost: t\r\n\r\n")
        data = c.recv(4096)
        c.close()
        assert b"TARGET!" in data, f"隧道内没拿到数据：{data[:120]!r}"
        assert not relay.stats().get("last_error"), relay.stats().get("last_error")
    finally:
        relay.stop()
        target_srv.close()


def test_http_proxy_without_credentials_gets_407(http_proxy):
    """不带凭据必须被上游拒（407），中继不能把它当成成功。"""
    from zw_core.services import proxyrelay
    from zw_core.services.proxy import parse_single_proxy

    spec = parse_single_proxy(f"http://127.0.0.1:{http_proxy}")
    assert spec.needs_auth is False, "没写用户名就不该被标成需要认证"

    relay = proxyrelay.RelayServer(spec)
    port = relay.start()
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        c.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\nHost: 127.0.0.1:1\r\n\r\n")
        try:
            data = c.recv(4096)
        except OSError:
            data = b""
        c.close()
        assert b"200 Connection Established" not in data, "没凭据却建立成功了"
    finally:
        relay.stop()

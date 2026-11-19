"""真起一个 SOCKS5 服务端，验证中继真的能和「要认证的上游」打通。

## 为什么值得写

用户需求第一条是「直接导入 `socks5://user:pass@host:port` 这种代理」。
单测通常只断言"命令拼对了"，但**带认证的 SOCKS5 握手**（RFC 1929）
很容易在这一层出错：凭据长度编码、要不要先发方法协商、失败后怎么回。

所以这里不用 mock，直接起一个真 TCP 服务端，走完整握手，再确认数据真的流过去了。

> 附带一个教训：这个测试桩的第一版我写错了 —— 把 RFC 1929 认证包的第二字节
> 当成版本号（实际是用户名长度），合法客户端在认证阶段就被断开。
> 排查时一度以为是产品代码有问题。桩也要认真写。
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
def socks_server():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, str(HELPER), str(port), "auth"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 等它真的 listen（最多 5 秒）
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


def test_relay_reaches_target_through_authenticated_socks5(socks_server):
    """完整链路：中继 ← 客户端 → 带认证 SOCKS5 → 目标服务。"""
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
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nHELLO")
        c.close()

    threading.Thread(target=serve_once, daemon=True).start()

    spec = parse_single_proxy(f"socks5://cf1b481103:51ec63a321@127.0.0.1:{socks_server}")
    assert spec.needs_auth is True, "解析出来的代理应当标记为需要认证"

    relay = proxyrelay.RelayServer(spec)
    port = relay.start()
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        c.sendall(f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{target_port}\r\n\r\n".encode())
        data = c.recv(4096)
        c.close()
        assert b"HELLO" in data, f"没拿到目标服务的响应：{data[:120]!r}"
        st = relay.stats()
        assert st.get("connections", 0) >= 1
        assert not st.get("last_error"), f"中继报了错：{st.get('last_error')}"
    finally:
        relay.stop()
        target_srv.close()


def test_wrong_password_is_rejected(socks_server):
    """密码错了要**明确失败**，不能假装通了。"""
    from zw_core.services import proxyrelay
    from zw_core.services.proxy import parse_single_proxy

    spec = parse_single_proxy(f"socks5://cf1b481103:WRONGPASS@127.0.0.1:{socks_server}")
    relay = proxyrelay.RelayServer(spec)
    port = relay.start()
    try:
        c = socket.create_connection(("127.0.0.1", port), timeout=10)
        c.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:1\r\n\r\n")
        try:
            data = c.recv(4096)
        except OSError:
            data = b""
        c.close()
        # 认证失败时上游会拒绝；中继应当给 502 或直接断开，不能返回 200
        assert b"200 OK" not in data, "密码错了却当成成功"
    finally:
        relay.stop()

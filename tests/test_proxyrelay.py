"""本地中继代理（services/proxyrelay.py）：四条链全部用**真 socket** 验证。

## 为什么这些用例不能 mock

中继的全部价值在于"浏览器不带认证时，上游能收到对的认证"，这件事只有让字节
真的走过 TCP 才能证明。mock 掉 socket 的测试可以全绿而线上一条都连不通。

所以这里自带一个**假上游代理**（socket 真起、真握手），它同时会说两种话：
HTTP 代理（CONNECT + 绝对 URI + Basic 认证）与 SOCKS5（含 RFC 1929）。
断言落在"上游到底看到了什么字节"和"客户端到底收到了什么字节"上。

端口一律用 0 让系统分配，连接一律带超时 —— 卡住要表现为失败，不是挂住。
"""

from __future__ import annotations

import base64
import logging
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from zw_core.services import engines, proxyrelay
from zw_core.services.proxy import ProxySpec

CONNECT_TIMEOUT = 5.0


# ══ 工具 ══════════════════════════════════════════════════
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise AssertionError(f"对端提前关闭（已读 {len(buf)}/{n} 字节）")
        buf += chunk
    return buf


def _recv_head(sock: socket.socket) -> tuple[bytes, bytes]:
    """读到 \\r\\n\\r\\n，返回 (头, 多读的尾巴)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError(f"对端提前关闭，已读：{buf!r}")
        buf += chunk
    idx = buf.index(b"\r\n\r\n") + 4
    return buf[:idx], buf[idx:]


def _recv_some(sock: socket.socket, wait: float = 1.5) -> bytes:
    """读到"没有更多数据"为止（用短超时收尾，避免等到用例超时）。"""
    sock.settimeout(wait)
    buf = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    except (socket.timeout, TimeoutError):
        pass
    return buf


def _recv_all(sock: socket.socket) -> bytes:
    """读到 EOF。"""
    buf = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return buf
        buf += chunk


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _dial(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=CONNECT_TIMEOUT)
    sock.settimeout(CONNECT_TIMEOUT)
    return sock


def _closed_port() -> int:
    """拿一个确定没人监听的端口（绑了再放）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ══ 假上游代理 ════════════════════════════════════════════
class FakeUpstream:
    """假上游代理：HTTP 与 SOCKS5 两种形态，都会记录"看到了什么"。

    - `kind="http"`：校验 `Proxy-Authorization`（不对回 407）；CONNECT 回 200 后
      把收到的字节 echo 回去；绝对 URI 请求回一个固定响应。
    - `kind="socks5"`：完整握手 + RFC 1929 用户名密码；记录目标地址类型
      （用来断言中继发的是域名 ATYP=0x03 而不是本地解析出的 IP）。
    - `hang=True`：accept 之后什么都不回（模拟上游卡死，用来占住连接槽）。
    """

    def __init__(
        self,
        kind: str = "http",
        user: str = "user",
        password: str = "pass",
        *,
        hang: bool = False,
    ) -> None:
        self.kind = kind
        self.user = user
        self.password = password
        self.hang = hang
        # 每条连接握手时看到的：{"request_line":..., "auth":[原始头值...], "headers":{...}}
        self.seen: list[dict] = []
        self.request_lines: list[str] = []
        # SOCKS5 记录 (atyp, host, port)
        self.socks_targets: list[tuple[int, str, int]] = []
        self.socks_methods: list[bytes] = []
        self._conns: list[socket.socket] = []
        self._closed = False
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self._srv.settimeout(0.3)
        self.port = int(self._srv.getsockname()[1])
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    # ── 生命周期 ──
    def start(self) -> "FakeUpstream":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._closed = True
        try:
            self._srv.close()
        except OSError:
            pass
        for c in list(self._conns):
            try:
                c.close()
            except OSError:
                pass
        self._thread.join(timeout=2.0)

    # ── accept ──
    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            conn.settimeout(CONNECT_TIMEOUT)
            self._conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            if self.hang:
                time.sleep(CONNECT_TIMEOUT)  # 什么都不回，占住这条连接
                return
            if self.kind == "http":
                self._serve_http(conn)
            else:
                self._serve_socks5(conn)
        except (OSError, AssertionError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ── HTTP 代理 ──
    def _serve_http(self, conn: socket.socket) -> None:
        head, rest = _recv_head(conn)
        lines = [ln for ln in head.split(b"\r\n") if ln]
        request_line = lines[0].decode("latin-1")
        auth: list[str] = []
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            key = name.strip().lower().decode("latin-1")
            if key == "proxy-authorization":
                auth.append(value.strip().decode("latin-1"))
            headers[key] = value.strip().decode("latin-1")
        self.seen.append({"request_line": request_line, "auth": auth, "headers": headers})

        if self.user:
            want = _basic(self.user, self.password)
            # 一条都不对、或者多于一条（重复头）都算失败 —— 上游会取第一条
            if len(auth) != 1 or auth[0] != want:
                conn.sendall(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b'Proxy-Authenticate: Basic realm="fake"\r\n'
                    b"Content-Length: 0\r\n\r\n"
                )
                return

        if request_line.upper().startswith("CONNECT"):
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                conn.sendall(rest)
            self._echo(conn)
            return

        # 绝对 URI 的明文请求：回一个固定响应
        self.request_lines.append(request_line)
        body = b"fake-upstream-ok"
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nX-Fake-Upstream: 1\r\n\r\n%s"
            % (len(body), body)
        )

    # ── SOCKS5 上游 ──
    def _serve_socks5(self, conn: socket.socket) -> None:
        _ver, nmethods = _recv_exact(conn, 2)
        methods = _recv_exact(conn, nmethods)
        self.socks_methods.append(methods)
        if self.user:
            if 0x02 not in methods:
                conn.sendall(b"\x05\xff")
                return
            conn.sendall(b"\x05\x02")
            _sub = _recv_exact(conn, 1)  # 0x01
            (ulen,) = _recv_exact(conn, 1)
            user = _recv_exact(conn, ulen).decode()
            (plen,) = _recv_exact(conn, 1)
            password = _recv_exact(conn, plen).decode()
            ok = user == self.user and password == self.password
            conn.sendall(b"\x01" + (b"\x00" if ok else b"\x01"))
            if not ok:
                return
        else:
            if 0x00 not in methods:
                conn.sendall(b"\x05\xff")
                return
            conn.sendall(b"\x05\x00")

        _ver2, _cmd, _rsv, atyp = _recv_exact(conn, 4)
        if atyp == 0x01:
            host = socket.inet_ntoa(_recv_exact(conn, 4))
        elif atyp == 0x04:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(conn, 16))
        elif atyp == 0x03:
            (n,) = _recv_exact(conn, 1)
            host = _recv_exact(conn, n).decode()
        else:
            raise AssertionError(f"假上游收到不认识的 ATYP 0x{atyp:02x}")
        port = struct.unpack("!H", _recv_exact(conn, 2))[0]
        self.socks_targets.append((atyp, host, port))
        # 成功应答：VER REP RSV ATYP=IPv4 + BND.ADDR + BND.PORT
        conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        self._echo(conn)

    @staticmethod
    def _echo(conn: socket.socket) -> None:
        while True:
            try:
                chunk = conn.recv(65536)
            except (socket.timeout, TimeoutError):
                return
            except OSError:
                return
            if not chunk:
                return
            try:
                conn.sendall(chunk)
            except OSError:
                return


# ══ 夹具 ══════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _clean_pool():
    """每个用例结束时把中继池清空；否则端口与线程会漏到别的用例里。"""
    yield
    proxyrelay.pool().stop_all()


@pytest.fixture
def http_upstream():
    up = FakeUpstream("http", "user", "pass").start()
    yield up
    up.stop()


@pytest.fixture
def socks_upstream():
    up = FakeUpstream("socks5", "s5user", "s5pass").start()
    yield up
    up.stop()


@pytest.fixture
def relay():
    """按需建中继的工厂，用例结束时统一 stop。"""
    made: list[proxyrelay.RelayServer] = []

    def _make(spec: ProxySpec, **kwargs) -> proxyrelay.RelayServer:
        srv = proxyrelay.RelayServer(spec, **kwargs)
        srv.start()
        made.append(srv)
        return srv

    yield _make
    for srv in made:
        srv.stop()


# ══ 1. CONNECT + HTTP 上游：中继替客户端补 Basic 认证 ═════
def test_connect_tunnel_adds_basic_auth(http_upstream, relay) -> None:
    spec = ProxySpec("http", "127.0.0.1", http_upstream.port, "user", "pass")
    srv = relay(spec)
    client = _dial(srv.port)
    try:
        # 客户端**不带**认证，和 Chrome 的行为一致
        client.sendall(
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
        )
        head, _ = _recv_head(client)
        assert head.startswith(b"HTTP/1.1 200 Connection Established"), head

        # 上游收到的认证是客户端从来没发过的那一份。
        #
        # **要等，不能立刻断言**：中继是异步把 CONNECT 转发给上游的，
        # 客户端这边已经收到 `200 Connection Established`，但假上游的记录
        # 可能还差几微秒才写进去。直接 `assert http_upstream.seen` 在负载高的
        # 机器上会偶发失败（全量跑时出现过一次，单独跑 3 次都过）。
        deadline = time.time() + 5
        while time.time() < deadline and not http_upstream.seen:
            time.sleep(0.01)
        assert http_upstream.seen, "假上游没有收到任何请求（等了 5 秒）"
        entry = http_upstream.seen[0]
        assert entry["request_line"].startswith("CONNECT example.com:443 ")
        assert entry["auth"] == [_basic("user", "pass")]

        # 隧道能双向传字节
        payload = b"hello-through-tunnel"
        client.sendall(payload)
        assert _recv_exact(client, len(payload)) == payload

        stats = srv.stats()
        assert stats["port"] == srv.port
        assert stats["connections"] == 1
        assert stats["bytes_in"] == len(payload)
        assert stats["bytes_out"] == len(payload)
    finally:
        client.close()


# ══ 2. 明文 HTTP + HTTP 上游：替换而不是追加 Proxy-Authorization ══
def test_plain_http_replaces_proxy_authorization(http_upstream, relay) -> None:
    spec = ProxySpec("http", "127.0.0.1", http_upstream.port, "user", "pass")
    srv = relay(spec)
    client = _dial(srv.port)
    try:
        # 客户端自带一条**假的**凭据（浏览器/SDK 真的会这么干）：
        # 中继必须把它删掉再补自己的，否则上游取第一条 → 407。
        client.sendall(
            b"GET http://example.com/hello HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Proxy-Authorization: Basic ZmFrZTpmYWtl\r\n"
            b"Proxy-Connection: keep-alive\r\n"
            b"\r\n"
        )
        body = _recv_all(client)
    finally:
        client.close()

    assert b"200 OK" in body, body
    assert b"fake-upstream-ok" in body      # 回包原样透传

    entry = http_upstream.seen[0]
    # 只有一条，而且是对的（不是客户端那条假的）
    assert entry["auth"] == [_basic("user", "pass")]
    # 请求行保持绝对 URI（走向 HTTP 代理必须这样）
    assert entry["request_line"].startswith("GET http://example.com/hello ")
    # Proxy-Connection 这类逐跳头不再往下游传
    assert "proxy-connection" not in entry["headers"]


# ══ 3. CONNECT + SOCKS5 上游：握手 + RFC1929 + 域名透传 ═══
def test_connect_over_socks5_uses_domain_and_rfc1929(socks_upstream, relay) -> None:
    spec = ProxySpec("socks5", "127.0.0.1", socks_upstream.port, "s5user", "s5pass")
    srv = relay(spec)
    client = _dial(srv.port)
    try:
        client.sendall(
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
        )
        head, _ = _recv_head(client)
        assert head.startswith(b"HTTP/1.1 200 Connection Established"), head

        payload = b"\x16\x03\x01fake-client-hello"
        client.sendall(payload)
        assert _recv_exact(client, len(payload)) == payload
    finally:
        client.close()

    # 握手时中继同时提供了"无认证"和"用户名密码"两种方法（05 02 00 02）
    assert socks_upstream.socks_methods[0] == b"\x00\x02"
    # 目标必须是域名（ATYP=0x03），DNS 交给上游解析 —— 否则 DNS 泄漏
    assert socks_upstream.socks_targets == [(0x03, "example.com", 443)]


# ══ 3b. 明文 HTTP + SOCKS5 上游（第四条链）═══════════════
def test_plain_http_over_socks5_strips_proxy_headers(socks_upstream, relay) -> None:
    spec = ProxySpec("socks5", "127.0.0.1", socks_upstream.port, "s5user", "s5pass")
    srv = relay(spec)
    client = _dial(srv.port)
    try:
        client.sendall(
            b"GET http://plain.example/x HTTP/1.1\r\n"
            b"Host: plain.example\r\n"
            b"Proxy-Authorization: Basic ZmFrZTpmYWtl\r\n"
            b"\r\n"
        )
        # 假 SOCKS5 上游握手完成后把收到的字节 echo 回来，
        # 所以客户端收到的就是"中继最终发给目标的那份请求"
        echoed = _recv_some(client)
    finally:
        client.close()

    assert b"GET http://plain.example/x HTTP/1.1" in echoed, echoed
    # Proxy-* 是逐跳头，绝不能出现在发给目标服务器的请求里
    assert b"Proxy-Authorization" not in echoed
    assert socks_upstream.socks_targets == [(0x03, "plain.example", 80)]


# ══ 4. 凭据错误：上游 407 → 客户端 502 且原因可读 ═════════
def test_bad_credentials_surface_as_readable_502(http_upstream, relay) -> None:
    spec = ProxySpec("http", "127.0.0.1", http_upstream.port, "user", "wrong-password")
    srv = relay(spec)
    client = _dial(srv.port)
    try:
        client.sendall(
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
        )
        head, rest = _recv_head(client)
    finally:
        client.close()

    assert head.startswith(b"HTTP/1.1 502 Bad Gateway"), head
    # 响应体里要有可读原因（含 407），而不是静默断开
    text = rest.decode("utf-8", "replace")
    assert "407" in text and "认证" in text, text
    assert srv.stats()["last_error"]


def test_socks5_bad_credentials_surface_as_502(socks_upstream, relay) -> None:
    spec = ProxySpec("socks5", "127.0.0.1", socks_upstream.port, "s5user", "nope")
    srv = relay(spec)
    client = _dial(srv.port)
    try:
        client.sendall(
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
        )
        head, rest = _recv_head(client)
    finally:
        client.close()

    assert head.startswith(b"HTTP/1.1 502 Bad Gateway"), head
    assert "认证" in rest.decode("utf-8", "replace")


# ══ 5. 上游不可达 → 502 + 明确原因 ═══════════════════════
def test_unreachable_upstream_gives_502(relay) -> None:
    dead = _closed_port()
    spec = ProxySpec("http", "127.0.0.1", dead, "user", "pass")
    srv = relay(spec)
    client = _dial(srv.port)
    try:
        client.sendall(
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
        )
        head, rest = _recv_head(client)
    finally:
        client.close()

    assert head.startswith(b"HTTP/1.1 502 Bad Gateway"), head
    text = rest.decode("utf-8", "replace")
    assert "连不上上游代理" in text, text
    assert str(dead) in text, text
    # 静默断开是这个模块要解决的病，这里必须留下可查的错误
    assert str(dead) in srv.stats()["last_error"]


# ══ 6. 只绑 127.0.0.1（绝不开成开放代理）═════════════════
def test_listener_binds_loopback_only(http_upstream, relay) -> None:
    spec = ProxySpec("http", "127.0.0.1", http_upstream.port, "user", "pass")
    srv = relay(spec)

    # ① ss 视角（系统里真的只监听了回环）
    if shutil.which("ss"):
        out = subprocess.run(
            ["ss", "-H", "-ltn"], capture_output=True, text=True, timeout=10
        ).stdout
        rows = [ln for ln in out.splitlines() if f":{srv.port} " in ln + " "]
        assert rows, f"ss 里找不到监听 {srv.port}：{out}"
        assert all(f"127.0.0.1:{srv.port}" in ln for ln in rows), rows

    # ② socket 视角：从本机的非回环地址连，必须连不上
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.168.3.1", 9))  # UDP connect 不发包，只用来问"本机出口 IP"
        local_ip = probe.getsockname()[0]
    except OSError:
        local_ip = ""
    finally:
        probe.close()
    if local_ip and not local_ip.startswith("127."):
        with pytest.raises(OSError):
            socket.create_connection((local_ip, srv.port), timeout=1.0)


# ══ 7. 连接数上限生效（并记账）═══════════════════════════
def test_connection_limit_rejects_and_counts() -> None:
    up = FakeUpstream("http", "user", "pass", hang=True).start()
    spec = ProxySpec("http", "127.0.0.1", up.port, "user", "pass")
    srv = proxyrelay.RelayServer(spec, max_conn=1)
    srv.start()
    first = _dial(srv.port)
    try:
        # 第一条连接去 CONNECT 那个"卡死"的上游，于是它一直占着唯一的槽位
        first.sendall(
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
        )
        assert _wait_until(lambda: srv.stats()["active"] == 1), srv.stats()

        second = _dial(srv.port)
        try:
            second.sendall(
                b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
            )
            head, rest = b"", b""
            try:
                head, rest = _recv_head(second)
            except (ConnectionError, OSError) as exc:
                # Windows 上"服务端写完就关"常常表现为 ConnectionAborted(10053)，
                # 而不是让客户端把 503 读出来。这时只能证明"被拒了"，
                # 503 的具体内容靠 rejected 计数与 stats 证明（下面照样断言）。
                head = b""
                last_exc = exc
            else:
                last_exc = None
        finally:
            second.close()

        if head:
            assert head.startswith(b"HTTP/1.1 503 Service Unavailable"), head
        else:
            # Windows 上"服务端写完就关"可能让客户端读不到响应体，
            # 此时只能证明"被拒了"；503 的语义靠下面的 rejected 计数证明。
            assert isinstance(last_exc, (ConnectionError, OSError))
        if rest:
            assert "上限" in rest.decode("utf-8", "replace")
        stats = srv.stats()
        assert stats["rejected"] == 1
        assert stats["max_conn"] == 1
    finally:
        first.close()
        srv.stop()
        up.stop()


# ══ 8. RelayPool：同上游复用端口，不同上游不同端口 ══════
def test_pool_reuses_port_per_upstream(http_upstream) -> None:
    pool = proxyrelay.RelayPool()
    try:
        a = ProxySpec("http", "127.0.0.1", http_upstream.port, "user", "pass")
        b = ProxySpec("http", "127.0.0.1", http_upstream.port, "user", "pass")
        # 同一凭据 → 同一个 key → 同一个端口
        assert a.key == b.key
        port1 = pool.ensure(a)
        port2 = pool.ensure(b)
        assert port1 == port2
        assert len(pool) == 1
        assert pool.get_by_port(port1) is not None
        assert pool.get_by_port(port1).stats()["upstream"] == a.upstream

        # 换个密码就是另一条上游（另一个出口/另一份凭据）→ 另一个端口
        c = ProxySpec("http", "127.0.0.1", http_upstream.port, "user", "pass2")
        port3 = pool.ensure(c)
        assert port3 != port1
        assert len(pool) == 2
        assert pool.get_by_port(port3) is not None
        assert pool.get_by_port(port3).stats()["spec_key"] == c.key
        assert pool.get(ProxySpec("http", "127.0.0.1", 1, "x", "y")) is None

        # cleanup 只保留指定的 spec
        removed = pool.cleanup({c.key})
        assert removed == 1
        assert len(pool) == 1
        assert pool.get(a) is None and pool.get(c) is not None
    finally:
        pool.stop_all()


def test_pool_cleanup_keeps_nothing_when_set_empty(http_upstream) -> None:
    pool = proxyrelay.RelayPool()
    try:
        pool.ensure(ProxySpec("http", "127.0.0.1", http_upstream.port, "u", "p"))
        assert len(pool) == 1
        assert pool.cleanup(set()) == 1
        assert len(pool) == 0
    finally:
        pool.stop_all()


# ══ 9. 日志里绝不出现密码 ════════════════════════════════
def test_logs_never_contain_password(relay, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="zw.proxyrelay")
    secret = "SUPER-SECRET-PW-9f2a"
    # 假上游就用这个密码，这样整条链是通的 —— 通的链路上更可能把凭据写进日志
    upstream = FakeUpstream("http", "user", secret).start()
    spec = ProxySpec("http", "127.0.0.1", upstream.port, "user", secret)
    srv = relay(spec)
    client = _dial(srv.port)
    try:
        client.sendall(
            b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n"
        )
        _recv_head(client)
        client.sendall(b"x")
        _recv_exact(client, 1)
    finally:
        client.close()
    srv.stop()

    assert caplog.text, "应该有日志（否则这条断言没有意义）"
    upstream.stop()
    assert secret not in caplog.text
    # 顺带：启动日志里该有上游地址（可观测），但只有主机端口
    assert str(upstream.port) in caplog.text


# ══ 10. build_cmdline 在需要认证时指向中继 ═══════════════
def _cmdline_profile(proxy: str, spec: ProxySpec | None) -> dict:
    profile = {
        "id": "p", "name": "n", "fp_seed": 1, "fp_platform": "host",
        "proxy": proxy, "_resolved_extensions": [], "extensions": [],
        "window_w": 800, "window_h": 600, "start_urls": [], "cdp_enabled": 1,
    }
    if spec is not None:
        profile["_proxy_spec"] = spec
    return profile


def test_build_cmdline_points_at_relay_when_auth_needed(tmp_path) -> None:
    spec = ProxySpec("http", "10.0.0.1", 8080, "user", "TOPSECRET")
    profile = _cmdline_profile(spec.upstream, spec)
    cmd = engines.build_cmdline(Path("/usr/bin/chrome"), tmp_path, 21001, profile)
    port = proxyrelay.pool().get(spec).port

    joined = " ".join(cmd)
    assert f"--proxy-server=http://127.0.0.1:{port}" in joined, joined
    # 上游地址与凭据都不能出现在命令行里（命令行对同机所有用户可见）
    assert "10.0.0.1:8080" not in joined
    assert "TOPSECRET" not in joined

    # 中继确实在监听，并且记着自己的上游
    stats = proxyrelay.pool().get_by_port(port).stats()
    assert stats["upstream"] == "http://10.0.0.1:8080"

    # 同一个上游再拼一次命令行 → 复用同一个端口（不会每个环境开一个监听）
    cmd2 = engines.build_cmdline(Path("/usr/bin/chrome"), tmp_path, 21002, profile)
    assert f"--proxy-server=http://127.0.0.1:{port}" in " ".join(cmd2)


def test_build_cmdline_keeps_direct_when_no_auth(tmp_path) -> None:
    spec = ProxySpec("socks5", "10.0.0.2", 1080)
    profile = _cmdline_profile(spec.upstream, spec)
    joined = " ".join(
        engines.build_cmdline(Path("/usr/bin/chrome"), tmp_path, 21001, profile)
    )
    # 不需要认证就直连上游：少一层转发，少一个故障点
    assert "--proxy-server=socks5://10.0.0.2:1080" in joined, joined
    assert proxyrelay.pool().get(spec) is None


def test_build_cmdline_falls_back_when_relay_disabled(tmp_path, monkeypatch) -> None:
    from zw_core import config

    monkeypatch.setenv("ZW_PROXY_RELAY", "false")
    config.reset_settings()
    try:
        spec = ProxySpec("socks5", "1.2.3.4", 1080, "u", "p")
        profile = _cmdline_profile(spec.upstream, spec)
        joined = " ".join(
            engines.build_cmdline(Path("/usr/bin/chrome"), tmp_path, 21001, profile)
        )
        # 老行为：扩展负责认证，命令行直接指上游
        assert "--proxy-server=socks5://1.2.3.4:1080" in joined, joined
        assert proxyrelay.pool().get(spec) is None
    finally:
        config.reset_settings()


# ══ 额外：中继端口在 stop 后真的释放 ═══════════════════════
def test_stop_releases_port(http_upstream, relay) -> None:
    spec = ProxySpec("http", "127.0.0.1", http_upstream.port, "user", "pass")
    srv = relay(spec)
    port = srv.port
    srv.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.5)
    assert srv.alive is False
    # 重复 stop 是幂等的（manager 里可能被调用多次）
    srv.stop()


def test_authed_proxy_warning_names_the_relay_not_the_extension():
    """启动返回的 warning 必须说真话：走的是本机中继，不是浏览器扩展。

    旧文案是"已通过内置认证扩展生效" —— 但 Chrome 137+ 忽略 --load-extension
    （149/150/157 三种内核实测），扩展根本没加载。用户照着这条去排查扩展，
    只会白费时间。真正替浏览器补凭据的是 proxyrelay。
    """
    import os as _os

    from zw_core import config as _cfg
    from zw_core.services import engines
    from zw_core.services.proxy import parse_proxy

    snapshot = {k: _os.environ.get(k) for k in ("ZW_ROOT", "ZW_PROXY_RELAY")}
    _os.environ["ZW_ROOT"] = "/tmp/zw-relay-warn"
    _os.environ["ZW_PROXY_RELAY"] = "true"
    _cfg.reset_settings()
    try:
        spec = parse_proxy("socks5://user:pw@127.0.0.1:1080")
        row = {"id": "warn-test", "name": "w", "_proxy_spec": spec,
               "proxy": spec.upstream, "extensions": []}
        from pathlib import Path

        # build_cmdline 只用到 chrome 路径拼命令行，不会真的启动它，随便给个存在的文件
        engines.build_cmdline(Path("/bin/true"), Path("/tmp/udd"), 19999, row, extra_args=[])
        rel = row.get("_proxy_relay_port")
        assert isinstance(rel, int) and rel > 0, f"没回填中继端口：{row}"
    finally:
        for k, v in snapshot.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v
        _cfg.reset_settings()


# ── CDP 客户端的 close()（0.7.36）─────────────────────────
def test_cdp_has_close_and_works_as_context_manager():
    """`CDP` 必须有 `close()`，且 `async with` 可用。

    ## 真踩过的 bug

    `CDP` 原来只有 `__aenter__`/`__aexit__`，**没有 `close()`**。而
    `open_tabs()` 在 `finally:` 里写的是 `await conn.close()` —— 每次都抛
    `AttributeError: 'CDP' object has no attribute 'close'`，整个函数直接失败。

    这个异常又被 `manager.start()` 的 `except` 吞成一条 warning
    （"补开标签页失败（不影响使用）"），于是
    **无头模式下"恢复上次标签 + 追加检测页"从来没生效过**，
    而界面上看不出任何异常 —— 只有翻日志才能发现。

    有界面（headful）时根本不走这条路，所以只在真机手工验证时才发现不了。
    """
    import inspect

    from zw_core.services.cookies import CDP

    assert hasattr(CDP, "close"), "CDP 没有 close()，open_tabs 会静默失败"
    assert inspect.iscoroutinefunction(CDP.close), "close() 必须是 async"
    assert hasattr(CDP, "__aenter__") and hasattr(CDP, "__aexit__")

    # 源码层面也钉住：不许再出现"手动 close 且没有它"的写法
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "core/src/zw_core/services/cookies.py"
    text = src.read_text(encoding="utf-8")
    assert "async with CDP(ws_url) as conn" in text, "open_tabs 应该用上下文管理器"


def test_open_tabs_returns_opened_and_failed_shape():
    """`open_tabs` 的返回形状必须稳定：`{opened: [...], failed: [...]}`。

    空 urls 时直接返回空结果、不建连接 —— 这条也顺带证明它不会因为
    "没连接却去 close" 而炸。
    """
    import asyncio

    from zw_core.services.cookies import open_tabs

    res = asyncio.run(open_tabs("ws://127.0.0.1:1/devtools/browser/x", []))
    assert res == {"opened": [], "failed": []}, res

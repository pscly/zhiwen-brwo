"""链式代理（前置 → 落地）：全部用**真 socket** 验证。

## 为什么这些用例不能 mock

链式代理的全部价值在于"两跳的握手字节真的按顺序落在了对的连接上"：
前置先收到 `CONNECT 落地:port`，然后在**同一条 TCP 连接**里再出现落地的
SOCKS5 握手，之后才是浏览器与目标之间的裸流。这件事只有让字节真的走过两个
TCP 端点才能证明 —— mock 掉 socket 的测试可以全绿而线上一跳都连不通。

## 假上游必须"真的会转发"

这里的 `FakeProxy` 不是回一个 200 就算数：它**真的**去连请求里的目标并把字节
双向搬运。因为落地代理是通过前置连过去的，如果前置只回 200 不搭桥，第二跳的
握手永远到不了落地 —— 那种假上游只能测出"前置收没收到 CONNECT"，测不出链路。

最终目标用"落地侧固定转发到本地 echo"来模拟（`forward_to`）：
这样域名可以照原样发（断言 ATYP=0x03 与 DNS 不被本地解析），又不会真的去连外网。

端口一律用 0 让系统分配，连接一律带超时 —— 卡住要表现为失败，不是挂住。
"""

from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import time
import types

import pytest

from zw_core.services import engines, proxyrelay
from zw_core.services.proxy import ProxyError, ProxySpec, parse_proxy

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
    """读到 `\\r\\n\\r\\n`，返回 (头, 多读的尾巴)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise AssertionError(f"对端提前关闭，已读：{buf!r}")
        buf += chunk
    idx = buf.index(b"\r\n\r\n") + 4
    return buf[:idx], buf[idx:]


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _dial(port: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=CONNECT_TIMEOUT)
    sock.settimeout(CONNECT_TIMEOUT)
    return sock


def _split_hostport(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    return host.strip("[]"), int(port)


def _pump_both(a: socket.socket, b: socket.socket) -> None:
    """两个方向各一个线程搬字节，任一端 EOF 就收工。"""

    def one(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    t = threading.Thread(target=one, args=(b, a), daemon=True)
    t.start()
    one(a, b)
    t.join(timeout=CONNECT_TIMEOUT)


def _wait_until(pred, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ══ 假上游代理（会真的转发） ═══════════════════════════════
class FakeProxy:
    """假上游代理：HTTP 与 SOCKS5 两种形态，**真的去连目标并双向转发**。

    - `kind="http"`：校验 `Proxy-Authorization`（不对回 407）；只处理 CONNECT，
      连上目标后回 200 并搭桥。
    - `kind="socks5"`：完整握手 + RFC 1929；记录目标地址类型
      （用来断言中继发的是域名 ATYP=0x03 而不是本地解析出的 IP）。
    - `forward_to=(host, port)`：不管请求里的目标是什么，都连到这里。
      用来当"落地" —— 于是目标可以保持成域名，而实际连接落在本地 echo 上。
    """

    def __init__(
        self,
        kind: str = "http",
        user: str = "",
        password: str = "",
        *,
        forward_to: tuple[str, int] | None = None,
    ) -> None:
        self.kind = kind
        self.user = user
        self.password = password
        self.forward_to = forward_to
        # HTTP 侧记录：{"request_line": ..., "auth": [原始头值...]}
        self.seen: list[dict] = []
        # SOCKS5 侧记录
        self.socks_methods: list[bytes] = []
        self.socks_auth: list[tuple[str, str]] = []
        self.socks_targets: list[tuple[int, str, int]] = []
        # 这一跳实际去连了谁（HTTP CONNECT / SOCKS5 CONNECT 都会记）
        self.dialed: list[tuple[str, int]] = []
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
    def start(self) -> "FakeProxy":
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
            if self.kind == "http":
                self._serve_http(conn)
            else:
                self._serve_socks5(conn)
        except (OSError, AssertionError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dest(self, host: str, port: int) -> tuple[str, int]:
        return self.forward_to if self.forward_to is not None else (host, port)

    # ── HTTP 代理 ──
    def _serve_http(self, conn: socket.socket) -> None:
        head, rest = _recv_head(conn)
        lines = [ln for ln in head.split(b"\r\n") if ln]
        request_line = lines[0].decode("latin-1")
        auth: list[str] = []
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"proxy-authorization":
                auth.append(value.strip().decode("latin-1"))
        self.seen.append({"request_line": request_line, "auth": auth})

        if self.user:
            if auth != [_basic(self.user, self.password)]:
                conn.sendall(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b'Proxy-Authenticate: Basic realm="fake"\r\n'
                    b"Content-Length: 0\r\n\r\n"
                )
                return

        if not request_line.upper().startswith("CONNECT"):
            # 链式用例只发 CONNECT；收到别的说明中继把明文请求直接塞过来了
            raise AssertionError(f"假上游只处理 CONNECT，收到：{request_line}")
        host, port = _split_hostport(request_line.split()[1])
        dest = self._dest(host, port)
        self.dialed.append(dest)
        up = socket.create_connection(dest, timeout=CONNECT_TIMEOUT)
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if rest:
            up.sendall(rest)
        try:
            _pump_both(conn, up)
        finally:
            up.close()

    # ── SOCKS5 代理 ──
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
            self.socks_auth.append((user, password))
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

        dest = self._dest(host, port)
        self.dialed.append(dest)
        up = socket.create_connection(dest, timeout=CONNECT_TIMEOUT)
        # 成功应答：VER REP RSV ATYP=IPv4 + BND.ADDR + BND.PORT
        conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        try:
            _pump_both(conn, up)
        finally:
            up.close()


class EchoTarget:
    """最终目标：收到什么就回什么（用来证明隧道是**双向**通的）。"""

    def __init__(self) -> None:
        self.received = b""
        self._closed = False
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self._srv.settimeout(0.3)
        self.port = int(self._srv.getsockname()[1])
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> "EchoTarget":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._closed = True
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)

    def _accept_loop(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                self.received += chunk
                conn.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ══ 夹具 ══════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _clean_pool():
    """每个用例结束时清空中继池（否则端口和线程会漏到别的用例）。"""
    yield
    proxyrelay.pool().stop_all()


@pytest.fixture
def echo():
    t = EchoTarget().start()
    yield t
    t.stop()


@pytest.fixture
def make_proxy():
    """按需起假上游，用例结束统一 stop。"""
    made: list[FakeProxy] = []

    def _make(kind: str = "http", user: str = "", password: str = "", **kwargs) -> FakeProxy:
        p = FakeProxy(kind, user, password, **kwargs).start()
        made.append(p)
        return p

    yield _make
    for p in made:
        p.stop()


@pytest.fixture
def relay():
    """按需起中继，用例结束统一 stop。"""
    made: list[proxyrelay.RelayServer] = []

    def _make(spec: ProxySpec, **kwargs) -> proxyrelay.RelayServer:
        srv = proxyrelay.RelayServer(spec, **kwargs)
        srv.start()
        made.append(srv)
        return srv

    yield _make
    for srv in made:
        srv.stop()


def _chain_spec(hop: FakeProxy, hop_user: str, hop_pass: str,
                exit_proxy: FakeProxy, exit_user: str, exit_pass: str) -> ProxySpec:
    """按"前置 | 落地"拼出链式 spec（两跳都是 127.0.0.1 上的假上游）。"""
    return ProxySpec(
        scheme="socks5" if exit_proxy.kind == "socks5" else "http",
        host="127.0.0.1",
        port=exit_proxy.port,
        username=exit_user,
        password=exit_pass,
        hop=ProxySpec(
            scheme="socks5" if hop.kind == "socks5" else "http",
            host="127.0.0.1",
            port=hop.port,
            username=hop_user,
            password=hop_pass,
        ),
    )


# ══ 1. HTTP 前置 → SOCKS5 落地 ════════════════════════════
def test_chain_http_hop_socks5_exit(make_proxy, echo, relay) -> None:
    """浏览器 → 中继 → (HTTP 前置带 Basic) → (SOCKS5 落地带 RFC1929) → 目标。

    这条是最常遇到的形态（机场/前置是 HTTP，落地是 SOCKS5）。
    """
    exit_proxy = make_proxy("socks5", "s5user", "s5pass", forward_to=("127.0.0.1", echo.port))
    hop = make_proxy("http", "hopuser", "hoppass")
    spec = _chain_spec(hop, "hopuser", "hoppass", exit_proxy, "s5user", "s5pass")
    srv = relay(spec)

    client = _dial(srv.port)
    try:
        # 客户端（浏览器）永远不带认证，和 Chrome 的行为一致
        client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        head, _ = _recv_head(client)
        assert head.startswith(b"HTTP/1.1 200 Connection Established"), head

        # ① 前置收到的是"连到落地代理"，而且带的是**前置自己的** Basic 认证
        assert hop.seen, "前置没有收到任何请求"
        assert hop.seen[0]["request_line"] == (
            f"CONNECT 127.0.0.1:{exit_proxy.port} HTTP/1.1"
        ), hop.seen[0]["request_line"]
        assert hop.seen[0]["auth"] == [_basic("hopuser", "hoppass")]
        # 前置真的去连了落地（而不是回个 200 装样子）
        assert hop.dialed == [("127.0.0.1", exit_proxy.port)], hop.dialed

        # ② 落地收到 RFC1929 认证，而且是**落地自己的**凭据（不是前置的）
        assert exit_proxy.socks_auth == [("s5user", "s5pass")], exit_proxy.socks_auth
        # 中继同时声明"无认证 + 用户名密码"两种方法
        assert exit_proxy.socks_methods == [b"\x00\x02"], exit_proxy.socks_methods

        # ③ 落地收到的目标是**域名**（ATYP=0x03）：DNS 由落地解析，不在本地泄漏
        assert exit_proxy.socks_targets == [(0x03, "example.com", 443)], exit_proxy.socks_targets

        # ④ 双层隧道里字节双向通
        payload = b"\x16\x03\x01fake-client-hello-through-two-hops"
        client.sendall(payload)
        assert _recv_exact(client, len(payload)) == payload
        assert _wait_until(lambda: echo.received == payload), echo.received
    finally:
        client.close()


# ══ 2. SOCKS5 前置 → SOCKS5 落地（两跳都要认证）═══════════
def test_chain_socks5_hop_socks5_exit(make_proxy, echo, relay) -> None:
    """两跳都是 SOCKS5，且各自要求不同的用户名密码。

    这条最容易出错的点：**凭据串号** —— 把落地的密码发给前置（或反过来），
    现象都是"认证失败"，从错误信息里根本看不出是哪一跳。
    """
    exit_proxy = make_proxy("socks5", "land", "landpw", forward_to=("127.0.0.1", echo.port))
    hop = make_proxy("socks5", "hop", "hoppw")
    spec = _chain_spec(hop, "hop", "hoppw", exit_proxy, "land", "landpw")
    srv = relay(spec)

    client = _dial(srv.port)
    try:
        client.sendall(b"CONNECT target.example:443 HTTP/1.1\r\nHost: target.example:443\r\n\r\n")
        head, _ = _recv_head(client)
        assert head.startswith(b"HTTP/1.1 200 Connection Established"), head

        # 前置先被连上（SOCKS5 握手在**直连前置**的 socket 上做）
        assert hop.dialed == [("127.0.0.1", exit_proxy.port)], hop.dialed
        assert hop.socks_auth == [("hop", "hoppw")], hop.socks_auth
        assert hop.socks_targets == [(0x01, "127.0.0.1", exit_proxy.port)], hop.socks_targets
        # 落地是在**前置隧道里**完成握手与认证的
        assert exit_proxy.socks_auth == [("land", "landpw")], exit_proxy.socks_auth
        # 最终目标仍然是域名，且由落地解析
        assert exit_proxy.socks_targets == [(0x03, "target.example", 443)], exit_proxy.socks_targets

        client.sendall(b"ping-through-both")
        assert _recv_exact(client, len(b"ping-through-both")) == b"ping-through-both"
    finally:
        client.close()


# ══ 3. HTTP 前置 → HTTP 落地（两跳都要认证）═══════════════
def test_chain_http_hop_http_exit(make_proxy, echo, relay) -> None:
    """两跳都是 HTTP 代理：先 CONNECT 落地，再在隧道里对目标发 CONNECT。"""
    exit_proxy = make_proxy("http", "land", "landpw", forward_to=("127.0.0.1", echo.port))
    hop = make_proxy("http", "hop", "hoppw")
    spec = _chain_spec(hop, "hop", "hoppw", exit_proxy, "land", "landpw")
    srv = relay(spec)

    client = _dial(srv.port)
    try:
        client.sendall(b"CONNECT encrypt.example:443 HTTP/1.1\r\nHost: encrypt.example:443\r\n\r\n")
        head, _ = _recv_head(client)
        assert head.startswith(b"HTTP/1.1 200 Connection Established"), head

        assert hop.seen[0]["request_line"] == (
            f"CONNECT 127.0.0.1:{exit_proxy.port} HTTP/1.1"
        )
        assert hop.seen[0]["auth"] == [_basic("hop", "hoppw")]
        # 落地收到的是**目标**（域名保留，不本地解析）
        assert exit_proxy.seen[0]["request_line"] == "CONNECT encrypt.example:443 HTTP/1.1"
        assert exit_proxy.seen[0]["auth"] == [_basic("land", "landpw")]

        body = b"hello-over-two-http-hops"
        client.sendall(body)
        assert _recv_exact(client, len(body)) == body
    finally:
        client.close()


# ══ 4. 前置认证错 → 502，且能看出是"前置" ═════════════════
def test_chain_hop_auth_error_is_attributed_to_hop(make_proxy, echo, relay) -> None:
    """前置 407：错误信息必须带"前置"，否则用户会去改落地的密码（改了也没用）。"""
    exit_proxy = make_proxy("socks5", "s5user", "s5pass", forward_to=("127.0.0.1", echo.port))
    hop = make_proxy("http", "hopuser", "hoppass")
    # 故意写错前置的密码
    spec = _chain_spec(hop, "hopuser", "WRONG", exit_proxy, "s5user", "s5pass")
    srv = relay(spec)

    client = _dial(srv.port)
    try:
        client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        head, rest = _recv_head(client)
    finally:
        client.close()

    assert head.startswith(b"HTTP/1.1 502 Bad Gateway"), head
    text = rest.decode("utf-8", "replace")
    assert "前置" in text, text
    assert "407" in text and "认证" in text, text
    assert "落地" not in text, text          # 不能把锅甩给落地
    # 落地根本没被碰到（前置就拦下了）
    assert exit_proxy.socks_auth == [], exit_proxy.socks_auth
    assert "前置" in srv.stats()["last_error"], srv.stats()["last_error"]


# ══ 5. 落地认证错 → 502，且能看出是"落地" ═════════════════
def test_chain_exit_auth_error_is_attributed_to_exit(make_proxy, echo, relay) -> None:
    """落地认证失败：错误信息必须带"落地"（前置是通的，改前置没用）。"""
    exit_proxy = make_proxy("socks5", "s5user", "s5pass", forward_to=("127.0.0.1", echo.port))
    hop = make_proxy("http", "hopuser", "hoppass")
    spec = _chain_spec(hop, "hopuser", "hoppass", exit_proxy, "s5user", "WRONG")
    srv = relay(spec)

    client = _dial(srv.port)
    try:
        client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        head, rest = _recv_head(client)
    finally:
        client.close()

    assert head.startswith(b"HTTP/1.1 502 Bad Gateway"), head
    text = rest.decode("utf-8", "replace")
    assert "落地" in text, text
    assert "认证" in text, text
    # 前置是**通的**（它已经收到了 CONNECT 并转发了字节）
    assert hop.seen and hop.seen[0]["auth"] == [_basic("hopuser", "hoppass")]
    # 落地确实收到了错误凭据 → 说明第二跳真的发生在隧道里
    assert exit_proxy.socks_auth == [("s5user", "WRONG")], exit_proxy.socks_auth
    assert "落地" in srv.stats()["last_error"], srv.stats()["last_error"]


# ══ 5b. 链式 + 明文 HTTP（浏览器走代理也会发明文请求）═════
def test_chain_plain_http_uses_absolute_form(make_proxy, echo, relay) -> None:
    """明文 HTTP 走链式：中继要连到**落地**再发绝对 URI 请求，不能套 CONNECT 到目标。

    这一条和 CONNECT 很不一样：CONNECT 之后是裸流，而明文 HTTP 必须把
    `GET http://h/...` 原样交给代理（absolute-form）。链式下这条请求要先
    穿过前置、落到落地，才发得出去。
    """
    exit_proxy = make_proxy("socks5", "land", "landpw", forward_to=("127.0.0.1", echo.port))
    hop = make_proxy("http", "hop", "hoppw")
    spec = _chain_spec(hop, "hop", "hoppw", exit_proxy, "land", "landpw")
    srv = relay(spec)

    client = _dial(srv.port)
    try:
        client.sendall(
            b"GET http://plain.example/x HTTP/1.1\r\n"
            b"Host: plain.example\r\n"
            b"Proxy-Authorization: Basic ZmFrZTpmYWtl\r\n"
            b"\r\n"
        )
        echoed = b""
        deadline = time.time() + 5.0
        client.settimeout(0.5)
        while time.time() < deadline:
            try:
                chunk = client.recv(65536)
            except (socket.timeout, TimeoutError):
                break
            if not chunk:
                break
            echoed += chunk
    finally:
        client.close()

    # 前置收到的是"连到落地"，落地收到的是"连到目标"（域名，ATYP=0x03）
    assert hop.seen[0]["request_line"] == f"CONNECT 127.0.0.1:{exit_proxy.port} HTTP/1.1"
    assert exit_proxy.socks_targets == [(0x03, "plain.example", 80)], exit_proxy.socks_targets
    # 最终发出去的是 absolute-form，且**不含**客户端自带的假凭据（逐跳头必须摘掉）
    assert b"GET http://plain.example/x HTTP/1.1" in echoed, echoed
    assert b"Proxy-Authorization" not in echoed, echoed
    assert b"ZmFrZTpmYWtl" not in echoed, echoed


# ══ 6. key 含 hop：不同前置 → 不同 key / 不同中继端口 ═════
def test_key_includes_hop_so_pool_allocates_distinct_ports(make_proxy, echo) -> None:
    """同落地不同前置是两条**出口不同**的链路，绝不允许复用同一个中继端口。

    复用就是串号：A 环境的流量会从 B 的前置发出去，而且没有任何报错。
    """
    exit_proxy = make_proxy("socks5", "s5user", "s5pass", forward_to=("127.0.0.1", echo.port))
    hop_a = make_proxy("http", "a", "apw")
    hop_b = make_proxy("http", "b", "bpw")
    spec_a = _chain_spec(hop_a, "a", "apw", exit_proxy, "s5user", "s5pass")
    spec_b = _chain_spec(hop_b, "b", "bpw", exit_proxy, "s5user", "s5pass")
    # 同一个落地的单跳版（无前置）
    spec_direct = ProxySpec("socks5", "127.0.0.1", exit_proxy.port, "s5user", "s5pass")

    assert spec_a.upstream == spec_b.upstream == spec_direct.upstream
    assert spec_a.key != spec_b.key, "不同前置必须是不同的 key"
    assert spec_a.key != spec_direct.key, "链式与直连必须是不同的 key"
    # 同一个链反复算 key 必须稳定（否则每次启动都会开一个新端口）
    assert spec_a.key == _chain_spec(hop_a, "a", "apw", exit_proxy, "s5user", "s5pass").key

    pool = proxyrelay.RelayPool()
    try:
        port_a = pool.ensure(spec_a)
        port_b = pool.ensure(spec_b)
        port_direct = pool.ensure(spec_direct)
        assert len({port_a, port_b, port_direct}) == 3, (port_a, port_b, port_direct)
        assert len(pool) == 3
        # 同一个链再要一次 → 复用同一个端口
        assert pool.ensure(spec_a) == port_a
        # stats 里能看出这是链式，且展示串不含凭据
        stats = pool.get_by_port(port_a).stats()
        assert stats["is_chain"] is True
        assert stats["chain"] == (
            f"http://127.0.0.1:{hop_a.port}|socks5://127.0.0.1:{exit_proxy.port}"
        )
        assert "apw" not in stats["chain"] and "s5pass" not in stats["chain"]
    finally:
        pool.stop_all()


# ══ 7. parse_proxy：三种分隔符 + 三跳报错 ═════════════════
@pytest.mark.parametrize("sep", ["|", ",", ">>"])
def test_parse_proxy_accepts_three_separators(sep: str) -> None:
    # 用户可能随手写这三种分隔符（还常带空格）
    raw = f"http://1.2.3.4:21080 {sep} socks5://u:p@66.80.12.66:20000"
    spec = parse_proxy(raw)
    assert spec is not None
    assert (spec.scheme, spec.host, spec.port) == ("socks5", "66.80.12.66", 20000)
    assert spec.hop is not None
    assert (spec.hop.scheme, spec.hop.host, spec.hop.port) == ("http", "1.2.3.4", 21080)
    # 用户真正要填的那一行（就是他给的这个落地）
    real = f"http://127.0.0.1:21080 {sep} socks5://cf1b481103:51ec63a321@66.80.12.66:20000"
    assert parse_proxy(real).hop.port == 21080


def test_parse_proxy_rejects_three_hops() -> None:
    with pytest.raises(ProxyError) as ei:
        parse_proxy("http://a:1|http://b:2|socks5://c:3")
    assert ei.value.code == "PROXY_CHAIN_TOO_LONG"
    assert "两跳" in ei.value.message

    # 空的一段（写成 `前置 |`）必须是"格式错"，不能当成单跳放过
    with pytest.raises(ProxyError) as ei2:
        parse_proxy("http://a:1|")
    assert ei2.value.code == "PROXY_INVALID"
    assert "两段都要填" in ei2.value.message

    # 两跳是合法的（对照组，证明上面不是"什么都报错"）
    ok = parse_proxy("http://a:1|http://b:2")
    assert ok.hop is not None and ok.hop.port == 1 and ok.port == 2


def test_parse_proxy_single_hop_unchanged() -> None:
    """单跳行为必须完全不变（向后兼容）：没有 hop、key 与以前一致。"""
    spec = parse_proxy("socks5://u:p@1.2.3.4:1080")
    assert spec.hop is None
    assert spec.upstream == "socks5://1.2.3.4:1080"
    assert spec.chain_text == spec.upstream
    # 密码里带逗号的单跳不能被当成链
    odd = parse_proxy("http://u:pa,ss@1.2.3.4:8080")
    assert odd.hop is None and odd.password == "pa,ss"


# ══ 8. parse_line / import_batch：解析、去重、落库 ═════════
@pytest.fixture()
def lib(tmp_path, monkeypatch):
    """独立数据目录 + 干净单例（和 tests/test_proxylib.py 的 lib 同构）。"""
    from zw_core import config, db

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    db.reset_db()
    from zw_core.services import proxylib as pl

    ns = types.SimpleNamespace(proxylib=pl, db=db)
    try:
        yield ns
    finally:
        config.reset_settings()
        db.reset_db()


def test_parse_line_and_import_batch_keep_hop(lib) -> None:
    pl = lib.proxylib
    # 前置用代理商常见的四段简写，落地用 URL 形态
    spec, note = pl.parse_line(
        "127.0.0.1:21080:hopuser:hoppass | socks5://land:landpw@66.80.12.66:20000 # 落地s5"
    )
    assert note == "落地s5"
    assert (spec.scheme, spec.host, spec.port) == ("socks5", "66.80.12.66", 20000)
    assert spec.hop is not None
    assert (spec.hop.host, spec.hop.port) == ("127.0.0.1", 21080)
    assert (spec.hop.username, spec.hop.password) == ("hopuser", "hoppass")

    line_chain = "127.0.0.1:21080:hopuser:hoppass | socks5://land:landpw@66.80.12.66:20000"
    line_other_hop = "127.0.0.1:21081:hopuser:hoppass | socks5://land:landpw@66.80.12.66:20000"
    out = pl.import_batch("\n".join([line_chain, line_chain, line_other_hop]))
    data = out["data"]
    assert data["imported"] == 2, data      # 同串去重一次；不同前置各留一条
    assert data["skipped"] == 1, data

    # 再导一次同样的两行 → 全部跳过（去重键含 hop，跨调用也成立）
    again = pl.import_batch("\n".join([line_chain, line_other_hop]))["data"]
    assert again["imported"] == 0 and again["skipped"] == 2, again

    # 库里真的存下了前置（否则重启后链式会退化成直连落地）
    rows = pl.list_all()
    assert len(rows) == 2
    assert all(r["is_chain"] for r in rows), rows
    assert {r["chain"] for r in rows} == {
        "http://127.0.0.1:21080|socks5://66.80.12.66:20000",
        "http://127.0.0.1:21081|socks5://66.80.12.66:20000",
    }, rows
    # 列表里绝不能出现密码
    dumped = json.dumps(rows, ensure_ascii=False)
    assert "landpw" not in dumped and "hoppass" not in dumped, dumped

    # 从库里读回来的 spec 仍然带 hop（`_spec_of_row`）
    from zw_core.db import get_db

    with get_db().conn() as c:
        row = c.execute("SELECT * FROM proxies ORDER BY id").fetchone()
    back = pl._spec_of_row(row)
    assert back.hop is not None and back.hop.port == 21080
    # attach_hop：启动路径靠它把 manager 重建出来的五列 spec 补回链式
    five_col = parse_proxy("socks5://land:landpw@66.80.12.66:20000")
    assert five_col.hop is None
    patched = pl.attach_hop(int(row["id"]), five_col)
    assert patched.hop is not None and patched.hop.port == 21080
    # 已经是链式的就原样返回（幂等）
    assert pl.attach_hop(int(row["id"]), patched).hop.port == 21080

    # 只改字段（不给 raw）时前置必须原样保留：`_normalize` 造出来的 spec 没有 hop，
    # 直白地写回去会把链式降级成"直连落地"，而且不报任何错。
    pid = int(row["id"])
    out2 = pl.update_proxy(pid, label="落地s5-改名")
    assert out2["ok"] is True, out2
    assert "hop" not in out2["changed"], out2["changed"]
    kept = pl.list_all()
    assert kept[0]["is_chain"] is True, kept[0]
    with get_db().conn() as c:
        assert (c.execute("SELECT hop FROM proxies WHERE id=?", (pid,)).fetchone()["hop"] or "")

    # 用 raw 改成单跳 → 前置被真正清掉
    out3 = pl.update_proxy(pid, raw="socks5://land:landpw@66.80.12.66:20000")
    assert out3["ok"] is True and "hop" in out3["changed"], out3
    assert pl.list_all()[0]["is_chain"] is False


# ══ 9. display_summary：`前置 → 落地` 且不含密码 ══════════
def test_display_summary_shows_chain_without_passwords() -> None:
    from zw_core.services import proxylib

    spec = parse_proxy(
        "http://hopuser:HOP-SECRET@127.0.0.1:21080 | "
        "socks5://land:EXIT-SECRET@66.80.12.66:20000"
    )
    out = proxylib.display_summary(spec, label="p1")
    assert out["is_chain"] is True
    assert "→" in out["chain"], out["chain"]
    assert "127.0.0.1:21080" in out["chain"] and "66.80.12.66:20000" in out["chain"]
    # 落地才是"出口"，所以顶层字段讲的是落地
    assert (out["host"], out["port"], out["scheme"]) == ("66.80.12.66", 20000, "socks5")
    dumped = json.dumps(out, ensure_ascii=False)
    assert "HOP-SECRET" not in dumped and "EXIT-SECRET" not in dumped, dumped
    assert dumped.count("***") >= 2

    # 单跳时 chain 就是 url 本身（界面不用分叉），密码照样脱敏
    single = proxylib.display_summary(parse_proxy("http://u:ONLY-ONE-HOP@1.1.1.1:8080"))
    assert single["is_chain"] is False and single["chain"] == single["url"]
    assert "ONLY-ONE-HOP" not in single["url"]
    assert single["url"] == "http://u:***@1.1.1.1:8080", single["url"]


# ══ 10. engines：链式一律指向本机中继 ═════════════════════
def test_engines_points_chain_at_local_relay(tmp_path, make_proxy, echo) -> None:
    """`--proxy-server` 只能表达一层代理，所以链式必须交给本机中继。

    这里同时钉住两件事：不能把**落地**地址直接给 Chrome；
    也不能把前置/落地的凭据写进命令行（命令行对同机所有用户可见）。
    """
    exit_proxy = make_proxy("socks5", "land", "LAND-SECRET", forward_to=("127.0.0.1", echo.port))
    hop = make_proxy("http", "hop", "HOP-SECRET")
    spec = _chain_spec(hop, "hop", "HOP-SECRET", exit_proxy, "land", "LAND-SECRET")

    profile: dict = {
        "id": "p", "name": "n", "fp_seed": 1, "fp_platform": "host",
        "proxy": spec.upstream,      # manager 脱敏后的样子：只有落地
        "_proxy_spec": spec,
        "_resolved_extensions": [], "extensions": [],
        "window_w": 800, "window_h": 600, "start_urls": [], "cdp_enabled": 1,
    }
    chosen = engines._proxy_server_for(profile, spec.upstream)
    port = proxyrelay.pool().get(spec).port
    assert chosen == f"http://127.0.0.1:{port}", chosen
    assert str(exit_proxy.port) not in chosen

    # 走完整命令行也一个凭据都不许出现
    cmd = engines.build_cmdline(__import__("pathlib").Path("/usr/bin/chrome"), tmp_path, 21001, profile)
    joined = " ".join(cmd)
    assert f"--proxy-server=http://127.0.0.1:{port}" in joined, joined
    assert "LAND-SECRET" not in joined and "HOP-SECRET" not in joined
    assert f"127.0.0.1:{exit_proxy.port}" not in joined

    # 再拼一次 → 复用同一个中继端口（不会每个环境开一个监听）
    assert engines._proxy_server_for(profile, spec.upstream) == chosen

    # 对照：无认证的单跳仍然直连上游（少一层转发，行为与以前一致）
    plain = ProxySpec("socks5", "10.0.0.9", 1080)
    assert engines._proxy_server_for(
        {"_proxy_spec": plain}, plain.upstream
    ) == "socks5://10.0.0.9:1080"

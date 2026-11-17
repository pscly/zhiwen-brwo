"""本地中继代理：替浏览器做**上游代理认证**。

## 为什么要有这个模块（背景是实测出来的，不是推测）

Chromium 的 `--proxy-server` 不支持把用户名密码写进命令行 —— 写成
`http://user:pass@host:port` 时 Chrome 会把整串当主机名，结果是**连不上且
日志里没有任何线索**（这条 0.1.0 就踩过，见 `services/proxy.py` 顶部）。

一直以来的标准解法是生成一个 MV3 扩展，用 `chrome.proxy.settings` 下发代理、
用 `chrome.webRequest.onAuthRequired` 应答 407 挑战，再 `--load-extension` 加载。

**2026-11-17 实测：Chrome 149 已经完全忽略 `--load-extension`。**
复现方式（带 content_script 的 MV3 扩展 + `--headless=new` + CDP
`Runtime.evaluate(globalThis.__probe)`）拿到的是 MISSING。
也就是说：**带认证的代理在这个内核上是静默失效的** —— 网页一直转圈，
而你会以为是网络问题。扩展那条路不能作为主路径。

## 所以改成：本机起一个极简代理，替浏览器做认证

```
Chrome --proxy-server=http://127.0.0.1:PORT      （无认证，Chromium 原生支持）
        │
        ▼
  本地中继 127.0.0.1:PORT
        │  在这里补 Proxy-Authorization，或做 SOCKS5 用户名密码握手
        ▼
  上游 socks5://user:pass@host:port  或  http://user:pass@host:port
```

好处是**引擎无关**（不依赖扩展、不依赖 Chrome 版本）、可测、可观测
（中继自己知道到底连没连上上游，`stats()` 能拿到 `last_error`）。

## 四条链，字节细节

1. **HTTPS 走 HTTP 上游**：客户端 `CONNECT h:443`（不带认证）→ 中继向上游发
   `CONNECT h:443` + `Proxy-Authorization: Basic base64(user:pass)` → 上游回
   `HTTP/1.1 200 ...` → 中继回 `HTTP/1.1 200 Connection Established` → 双向裸转发。
2. **明文 HTTP 走 HTTP 上游**：客户端发绝对 URI（`GET http://h/ HTTP/1.1`），
   中继**替换**（不是追加）`Proxy-Authorization` 后原样转发。替换是硬要求：
   浏览器/上游 SDK 常常自带一条空的 `Proxy-Authorization`，两条同名头会让上游
   取第一条（假的那条）→ 407。
3. **HTTPS 走 SOCKS5 上游**：客户端 `CONNECT h:443` → 中继做 SOCKS5 握手
   （`05 02 00 02` 或 `05 01 00`）→ RFC 1929 用户名密码（`01 len(u) u len(p) p`）
   → `05 01 00 03 len(host) host port` → 成功应答 → 回 200 → 双向转发。
4. **明文 HTTP 走 SOCKS5 上游**：解析请求里的目标 host:port → SOCKS5 连上 →
   把原始请求（去掉 `Proxy-*` 头）发过去 → 回包返回。

**SOCKS5 一律发域名（ATYP=0x03）而不是先本地解析成 IP**：DNS 必须在代理侧完成，
否则指纹浏览器照样 DNS 泄漏，等于代理白做。

## 几条不能违反的约束

- 只绑 `127.0.0.1`。绑 `0.0.0.0` 等于给整个网段开了一个**免认证的开放代理**。
- 上游连不上必须给客户端一个**带原因的 502**，不能静默断开：静默断开在浏览器里
  的表现就是"网页一直转圈"，极难排查（这正是本模块要解决的病）。
- 日志里绝不能出现密码，一律走 `spec.safe_dict()`。
- 每个连接一个线程（不做 asyncio）：现有生命周期/网关都是线程模型，混用只会
  多出一类"偶发卡死"的问题。
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import socket
import threading
from typing import Any

from .proxy import ProxySpec

logger = logging.getLogger("zw.proxyrelay")

# 连接数上限：浏览器开页面会并发几十条，200 足够；超了直接拒绝并记账，
# 不能无限开线程（线程也是资源，爆掉的是整个 core 进程）。
DEFAULT_MAX_CONN = 200
# 空闲超时：一条隧道两端都没数据 120 秒就关。不设的话 keep-alive 连接会一直挂着，
# 端口和线程都收不回来。
DEFAULT_IDLE_TIMEOUT = 120.0
# 请求头上限：正常请求头几 KB，给 64KB 已经很宽松；超了就是有人在灌数据
_MAX_HEADER_BYTES = 64 * 1024
_RECV = 65536

# SOCKS5 应答码 → 中文说明（照 RFC 1928 §6，508 是各家实现自己加的扩展，不列）
_SOCKS5_REPLY = {
    1: "上游返回：general SOCKS server failure（上游自己出错）",
    2: "上游返回：connection not allowed by ruleset（上游策略不允许）",
    3: "上游返回：network unreachable（上游到目标网段不可达）",
    4: "上游返回：host unreachable（上游解析不到/到不了目标主机）",
    5: "上游返回：connection refused（目标端口拒绝连接）",
    6: "上游返回：TTL expired",
    7: "上游返回：command not supported（上游不支持 CONNECT）",
    8: "上游返回：address type not supported（上游不认识我发的地址类型）",
}


class RelayError(Exception):
    """中继内部的可读错误。会被翻译成给浏览器的 502/503 响应体。

    注意：这里的消息**只放主机名/状态码/系统 errno 文本**，绝不放凭据 ——
    它会进日志、也会进 HTTP 响应体。
    """


# ── 小工具 ────────────────────────────────────────────────
# 链式代理里两跳的角色名。错误消息带上它，排错时才能一眼看出
# "到底前置坏了还是落地坏了" —— 这两种故障的症状一模一样（连不上 / 认证失败）。
_ROLE_HOP = "前置"
_ROLE_EXIT = "落地"


def _tag(role: str, msg: str) -> str:
    """给错误消息加上"是哪一跳出的错"。`role` 为空时**原样返回**（单跳行为不变）。"""
    return f"{role}代理：{msg}" if role else msg


def _is_socks(scheme: str) -> bool:
    return scheme.lower().startswith("socks")


class _Prefixed:
    """把"上游抢跑的字节"接到一个套接字前面的薄包装。

    链式里前置的 `CONNECT` 应答后面可能已经跟着落地代理发来的字节。
    那些字节属于**下一层的协议流**，必须喂给下一跳的握手；直接丢掉等于
    让落地的握手永远等不到数据（表现为卡到空闲超时才 502）。
    只在前置真的抢跑了字节时才用这个包装 —— 正常路径上没有任何额外开销。
    """

    def __init__(self, sock: socket.socket, prefix: bytes) -> None:
        self._sock = sock
        #: 底层真实套接字。`_conns` 里登记的是它，`_forget(wrapper)` 要靠它精确摘掉。
        self.raw = sock
        self._buf = bytearray(prefix or b"")

    def recv(self, n: int) -> bytes:
        if self._buf:
            take = bytes(self._buf[:n])
            del self._buf[:n]
            return take
        return self._sock.recv(n)

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)

    def settimeout(self, value: float | None) -> None:
        self._sock.settimeout(value)

    def shutdown(self, how: int) -> None:
        try:
            self._sock.shutdown(how)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def fileno(self) -> int:
        return self._sock.fileno()


def _read_until(sock: socket.socket, marker: bytes, limit: int = _MAX_HEADER_BYTES) -> tuple[bytes, bytes]:
    """读到 marker 出现为止，返回 (含 marker 的头部, 多读出来的尾巴)。

    尾巴必须还回去：明文 HTTP 的 body（或 CONNECT 之后客户端抢跑的 TLS ClientHello）
    很可能和头部在同一批字节里到，丢掉就等于悄悄截断数据。
    """
    buf = b""
    while marker not in buf:
        chunk = sock.recv(_RECV)
        if not chunk:
            return buf, b""  # 对端关了；把已读到的交给上层判断
        buf += chunk
        if len(buf) > limit:
            raise RelayError(f"请求头超过 {limit} 字节上限，已断开")
    idx = buf.find(marker)
    end = idx + len(marker)
    return buf[:end], buf[end:]


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """读满 n 字节（SOCKS5 握手帧都是定长的，短读必须补齐）。"""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RelayError("上游 SOCKS5 在握手过程中断开连接")
        buf += chunk
    return buf


def _split_hostport(target: str, default_port: int) -> tuple[str, int]:
    """把 `host:port` 拆开。CONNECT 的 target 永远是这种形式。

    要处理 `[::1]:443` 这种带方括号的 IPv6；不带方括号又要当成 IPv6 处理
    （`::1:443` 是有歧义的，按"整串是主机、端口用默认"来解，宁可不支持也不猜错）。
    """
    t = (target or "").strip()
    if not t:
        raise RelayError("目标是空的，无法转发")
    if t.startswith("["):
        host, _, rest = t[1:].partition("]")
        port_text = rest.lstrip(":")
        if not host:
            raise RelayError(f"目标是空的 IPv6 地址：{target}")
        return host, _port_of(port_text, default_port, target)
    if t.count(":") == 1:
        host, port_text = t.rsplit(":", 1)
        if not host:
            raise RelayError(f"目标缺少主机名：{target}")
        return host, _port_of(port_text, default_port, target)
    if t.count(":") > 1:
        return t, default_port  # 裸 IPv6，没有端口
    return t, default_port


def _port_of(text: str, default: int, raw: str) -> int:
    text = (text or "").strip()
    if not text:
        return default
    try:
        port = int(text)
    except ValueError as exc:
        raise RelayError(f"目标端口不是数字：{raw}") from exc
    if not 0 < port < 65536:
        raise RelayError(f"目标端口越界：{raw}")
    return port


def _status_line(head: bytes) -> tuple[int, str]:
    """从响应头里取 (状态码, 首行)。首行进错误消息便于定位。"""
    first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    parts = first.split()
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1]), first
    return 0, first or "(空响应)"


def _rewrite_headers(head: bytes, add_auth: bytes | None) -> bytes:
    """去掉客户端自带的 `Proxy-*` 头，再补上我们自己的 `Proxy-Authorization`。

    为什么是"替换"不是"追加"：Chromium 有时会发一条空的
    `Proxy-Authorization`（扩展/SDK 也会），上游按 RFC 取第一条就用到了假的，
    于是 407。这里先全删再补一条，保证上游只会看到一条、而且是对的。
    """
    lines = [ln for ln in head.split(b"\r\n") if ln]
    keep: list[bytes] = []
    for i, line in enumerate(lines):
        if i == 0:
            keep.append(line)  # 请求行原样（明文 HTTP 走代理时这里必须是绝对 URI）
            continue
        name = line.split(b":", 1)[0].strip().lower()
        if name in (b"proxy-authorization", b"proxy-connection"):
            continue
        keep.append(line)
    body = b"\r\n".join(keep) + b"\r\n"
    if add_auth:
        body += add_auth
    return body + b"\r\n"


def _request_target(head: bytes) -> tuple[str, int]:
    """从明文 HTTP 请求里取出真实目标 host:port。

    走代理的请求，请求行里是绝对 URI（`GET http://h:8080/p HTTP/1.1`）；
    万一客户端发的是 origin-form，就只能靠 Host 头 —— 两种都认。
    """
    first = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = first.split()
    if len(parts) < 3:
        raise RelayError(f"请求行不完整：{first[:120]}")
    target, version = parts[1], parts[2]
    if "://" in target:
        rest = target.split("://", 1)[1]
        authority = rest.split("/", 1)[0].split("?", 1)[0]
        default = 443 if target.lower().startswith("https://") else 80
        host, port = _split_hostport(authority, default)
        if not host:
            raise RelayError(f"请求行里没有主机名：{first[:120]}")
        return host, port
    # origin-form：目标只能从 Host 头拿
    for raw in head.split(b"\r\n")[1:]:
        name, _, value = raw.partition(b":")
        if name.strip().lower() == b"host" and value.strip():
            return _split_hostport(value.decode("latin-1").strip(), 80)
    raise RelayError(
        f"无法判断目标：请求行既不是绝对 URI（{target[:60]}，{version}）也没有 Host 头"
    )


def _socks5_addr(host: str, port: int) -> bytes:
    """编码 SOCKS5 的目标地址。

    **域名一律发 ATYP=0x03 让上游去解析**：如果在本地先 resolve 成 IP，
    DNS 查询就跑在直连网络上 —— 对指纹浏览器来说这是明显的泄漏点。
    """
    raw_ip = None
    try:
        raw_ip = ipaddress.ip_address(host)
    except ValueError:
        raw_ip = None
    if raw_ip is not None:
        atyp = b"\x01" if raw_ip.version == 4 else b"\x04"
        return atyp + raw_ip.packed + port.to_bytes(2, "big")
    try:
        name = host.encode("idna")
    except UnicodeError:
        name = host.encode("utf-8", "replace")
    if len(name) > 255:
        raise RelayError(f"目标域名太长（{len(name)} > 255 字节），SOCKS5 装不下")
    return b"\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big")


def _uri_host(host: str) -> str:
    """放进 CONNECT 请求行的主机写法：IPv6 要加方括号。"""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


# ── 一个上游 = 一个监听端口 ───────────────────────────────
class RelayServer:
    """一个上游代理对应一个监听端口。

    生命周期：`start()` 绑 127.0.0.1:0（让系统分配）→ 每次 accept 起一个线程 →
    `stop()` 关监听并把在途连接一起掐掉（否则线程会挂到空闲超时才退）。

    stats() 里的 `bytes_in` 是"客户端→上游"，`bytes_out` 是"上游→客户端"。
    """

    def __init__(
        self,
        spec: ProxySpec,
        *,
        max_conn: int = DEFAULT_MAX_CONN,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self._spec = spec
        self._max_conn = max(1, int(max_conn))
        self._idle = float(idle_timeout)
        self._listen: socket.socket | None = None
        self._port = 0
        self._thread: threading.Thread | None = None
        self._closed = False
        self._lock = threading.Lock()
        # 在途套接字（客户端 + 上游）都要登记：stop() 靠它把阻塞在 recv 的线程踢醒
        self._conns: set[socket.socket] = set()
        self._active = 0
        self._connections = 0
        self._rejected = 0
        self._bytes_in = 0
        self._bytes_out = 0
        self._last_error = ""

    # ── 生命周期 ─────────────────────────────────────────
    @property
    def port(self) -> int:
        return self._port

    @property
    def alive(self) -> bool:
        return self._listen is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> int:
        """绑定并开始 accept。返回分配到的 127.0.0.1 端口。重复调用返回同一个端口。"""
        with self._lock:
            # 判据用 _listen 而不是 _port：stop() 之后 _port 仍然留着（stats 要能事后
            # 说清"是哪个端口停的"），但那个端口已经没人监听了，不能再当成"已在跑"。
            if self._listen is not None:
                return self._port
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # 只绑回环。绑 0.0.0.0 等于把"免认证的上游代理"开放给整个网段。
            srv.bind(("127.0.0.1", 0))
            srv.listen(128)
            srv.settimeout(0.5)  # 让 accept 能定期回头看 _closed，而不是卡到进程退出
            self._listen = srv
            self._port = int(srv.getsockname()[1])
            self._closed = False
            self._thread = threading.Thread(
                target=self._accept_loop,
                name=f"zw-relay-{self._port}",
                daemon=True,
            )
            self._thread.start()
        logger.info(
            "本地中继已启动 port=%s 上游=%s 上限=%s 空闲超时=%ss",
            self._port,
            self._spec.chain_text,  # 链式时是 `前置|落地`（不含凭据），排错先看这里
            self._max_conn,
            self._idle,
        )
        return self._port

    def stop(self) -> None:
        with self._lock:
            if self._closed and self._listen is None:
                return
            self._closed = True
            srv, self._listen = self._listen, None
            conns = list(self._conns)
            self._conns.clear()
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        for conn in conns:
            # close 而不是 shutdown：直接让阻塞中的 recv 抛 OSError 退出
            try:
                conn.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        if self._port:
            logger.info("本地中继已停止 port=%s 累计连接=%s", self._port, self._connections)

    # ── 观测 ─────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "port": self._port,
                "upstream": self._spec.upstream,
                "scheme": self._spec.scheme,
                # 链式时把 `前置|落地` 也报出来（不含凭据）：只看 upstream 会让人
                # 以为这个中继在直连落地，从而把"前置挂了"错判成"落地挂了"。
                "chain": self._spec.chain_text,
                "is_chain": self._spec.hop is not None,
                "connections": self._connections,
                "bytes_in": self._bytes_in,
                "bytes_out": self._bytes_out,
                "last_error": self._last_error,
                # 以下三项是给排错用的额外信息（不在最小契约里，但很有用）
                "active": self._active,
                "rejected": self._rejected,
                "max_conn": self._max_conn,
                "spec_key": self._spec.key,
            }

    # ── accept ───────────────────────────────────────────
    def _accept_loop(self) -> None:
        srv = self._listen
        while not self._closed and srv is not None:
            try:
                conn, _peer = srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break  # 监听套接字被 stop() 关了
            with self._lock:
                over = self._active >= self._max_conn
                if over:
                    self._rejected += 1
                    self._last_error = f"连接数达上限 {self._max_conn}，已拒绝新连接"
                    self._connections += 1  # 被拒的也算一次"来过"，便于对照日志
                else:
                    self._active += 1
                    self._connections += 1
            if over:
                self._send_error(
                    conn,
                    503,
                    "Service Unavailable",
                    f"本地中继连接数已达上限（{self._max_conn}），本次连接被拒绝。"
                    "常见原因是浏览器开了太多页面、连接没释放；稍等重试或调大上限。",
                )
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with self._lock:
            self._conns.add(conn)
        try:
            conn.settimeout(self._idle)
            self._handle(conn)
        except Exception as exc:  # noqa: BLE001 —— 一条连接出错绝不能带崩中继
            msg = str(exc) or type(exc).__name__
            with self._lock:
                self._last_error = msg
            logger.warning("中继 port=%s 处理连接出错：%s", self._port, msg)
            self._send_error(conn, 502, "Bad Gateway", msg)
        finally:
            with self._lock:
                self._conns.discard(conn)
                self._active -= 1
            try:
                conn.close()
            except OSError:
                pass

    # ── 请求处理 ─────────────────────────────────────────
    def _handle(self, conn: socket.socket) -> None:
        head, leftover = _read_until(conn, b"\r\n\r\n")
        if not head:
            return  # 连上来就关（端口探测/用户点了取消），不用记错

        method = head.split(b" ", 1)[0].decode("latin-1", "replace").upper()
        established = False
        try:
            if method == "CONNECT":
                # 请求行形如：CONNECT example.com:443 HTTP/1.1
                line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
                parts = line.split()
                if len(parts) < 2:
                    raise RelayError(f"CONNECT 请求行不完整：{line[:120]}")
                host, port = _split_hostport(parts[1], 443)
                up, from_up = self._tunnel_to(host, port)
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                established = True
                # leftover 是客户端抢跑在 CONNECT 后面的字节（TLS ClientHello），
                # 必须补发给上游，否则 TLS 握手会卡住；from_up 同理反向补发。
                self._tunnel(conn, up, to_up=leftover, to_client=from_up)
            else:
                host, port = _request_target(head)
                if _is_socks(self._spec.scheme):
                    up, from_up = self._open_to_target(host, port)
                    # SOCKS5 不是 HTTP 代理：去掉 Proxy-* 头后按原样发。
                    # 请求行保留了绝对 URI 形式 —— RFC 9112 §3.2.2 要求服务端
                    # 必须接受 absolute-form，实际主流服务端（nginx/CDN）都认。
                    out = _rewrite_headers(head, None)
                else:
                    # 明文 HTTP 走 HTTP 上游：连上落地（必要时穿过前置）之后
                    # 直接发绝对 URI 请求，不套 CONNECT —— 这是代理协议本身的样子。
                    up = self._open_to_proxy()
                    from_up = b""
                    out = _rewrite_headers(head, self._auth_header_of(self._spec))
                # 明文 HTTP 还没给客户端写过任何字节，所以这一步失败仍然可以回 502，
                # 因此 established 放在"请求确实发出去了"之后。
                up.sendall(out + leftover)
                self._count_in(len(out) + len(leftover))
                established = True
                self._tunnel(conn, up, to_client=from_up)
        except RelayError as exc:
            with self._lock:
                self._last_error = str(exc)
            if established:
                logger.warning("中继 port=%s 隧道中途出错：%s", self._port, exc)
            else:
                logger.warning("中继 port=%s 无法转发到 %s：%s", self._port, self._spec.upstream, exc)
                self._send_error(conn, 502, "Bad Gateway", str(exc))
        except Exception as exc:  # noqa: BLE001 —— 含 OSError；一条连接出错不能影响别的连接
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            if established:
                # 已经给过 200，绝不能再往里塞 HTTP 响应：那会把 TLS 流写坏
                logger.debug("中继 port=%s 隧道结束：%s", self._port, exc)
            else:
                logger.warning("中继 port=%s 网络错误：%s", self._port, exc)
                self._send_error(conn, 502, "Bad Gateway", f"与上游/客户端通信失败：{exc}")

    # ── 上游握手 ─────────────────────────────────────────
    def _auth_header_of(self, spec: ProxySpec) -> bytes:
        """Basic 认证头（不放进日志、不进命令行，只进这条连接的请求头）。

        带 `spec` 参数而不是用 `self._spec`：链式代理里前置与落地**各有各的凭据**，
        两跳都必须能生成自己的认证头，用 self 就写不出来。
        """
        if not spec.username:
            return b""
        token = base64.b64encode(
            f"{spec.username}:{spec.password}".encode("utf-8")
        ).decode("ascii")
        return f"Proxy-Authorization: Basic {token}\r\n".encode("ascii")

    def _auth_header(self) -> bytes:
        """本跳（落地）的认证头。"""
        return self._auth_header_of(self._spec)

    def _connect_proxy(self, spec: ProxySpec, *, role: str = "") -> socket.socket:
        """连上某个代理本体的 TCP（https 上游在这里套 TLS）。**只连接，不握手。**

        "只连接"这一条是链式能成立的前提：连接与握手分开之后，
        "在一条已经建好的隧道上再对下一个代理握一次手"才有可能 —— 见 `_open_tunnel`。
        """
        try:
            sock = socket.create_connection((spec.host, spec.port), timeout=self._idle)
        except OSError as exc:
            raise RelayError(
                _tag(role, f"连不上上游代理 {spec.host}:{spec.port}（{type(exc).__name__}: {exc}）")
            ) from exc
        sock.settimeout(self._idle)
        with self._lock:
            self._conns.add(sock)
        if spec.scheme.lower() == "https":
            import ssl

            try:
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=spec.host)
            except OSError as exc:
                self._forget(sock)
                raise RelayError(
                    _tag(role, f"与上游 https 代理 {spec.host}:{spec.port} 建立 TLS 失败：{exc}")
                ) from exc
        return sock

    def _forget(self, sock: socket.socket) -> None:
        # `_conns` 里存的是**真实**套接字；链式路径上可能传进来 `_Prefixed` 包装，
        # 直接 discard 包装对象是无效操作（会把真实套接字永远留在集合里）。
        with self._lock:
            self._conns.discard(getattr(sock, "raw", sock))
        try:
            sock.close()
        except OSError:
            pass

    def _http_connect(
        self,
        sock: socket.socket,
        spec: ProxySpec,
        host: str,
        port: int,
        *,
        role: str = "",
    ) -> bytes:
        """在**已经连上的** socket 上发 `CONNECT host:port` 并校验 200。

        返回上游抢跑的尾巴字节（常为空）：那已经属于隧道里的应用数据，丢掉等于截断。
        """
        target = f"{_uri_host(host)}:{port}"
        req = (
            f"CONNECT {target} HTTP/1.1\r\n"
            f"Host: {target}\r\n"
            f"User-Agent: zhiwen-relay/{self._port}\r\n"
        ).encode("latin-1")
        req += self._auth_header_of(spec)
        req += b"Proxy-Connection: Keep-Alive\r\n\r\n"
        try:
            # 这条 CONNECT 是我们自己生成的，不计入 bytes_in（那是"客户端发来的字节"）
            sock.sendall(req)
            head, rest = _read_until(sock, b"\r\n\r\n")
        except OSError as exc:
            raise RelayError(_tag(role, f"向上游代理发起 CONNECT {target} 失败：{exc}")) from exc
        if not head:
            raise RelayError(
                _tag(role, f"上游代理 {spec.upstream} 在 CONNECT {target} 时直接断开，没有任何响应")
            )
        code, line = _status_line(head)
        if code != 200:
            if code == 407:
                raise RelayError(
                    _tag(role, f"上游代理拒绝认证（407）—— 用户名或密码不对，或者该代理要求 IP 白名单：{line}")
                )
            raise RelayError(_tag(role, f"上游代理拒绝 CONNECT {target}：{line}"))
        return rest

    def _socks5_handshake(
        self,
        sock: socket.socket,
        spec: ProxySpec,
        host: str,
        port: int,
        *,
        role: str = "",
    ) -> None:
        """在**已经连上的** socket 上做 SOCKS5 握手 + RFC 1929 + CONNECT。

        字节序列（写清楚是为了以后能照着对抓包）：
          客户端(本中继) → 上游：05 02 00 02      （5 版本、2 种方法、无认证、用户名密码）
          上游 → 本中继：          05 02          （选了用户名密码）
          本中继 → 上游：          01 len(u) u len(p) p
          上游 → 本中继：          01 00          （00 = 成功）
          本中继 → 上游：          05 01 00 ATYP ADDR PORT
          上游 → 本中继：          05 00 00 ATYP BND.ADDR BND.PORT
        """
        has_auth = bool(spec.username)
        try:
            sock.sendall(b"\x05\x02\x00\x02" if has_auth else b"\x05\x01\x00")
            ver, method = _recv_exact(sock, 2)
        except OSError as exc:
            raise RelayError(_tag(role, f"与上游 SOCKS5 握手时连接出错：{exc}")) from exc
        if ver != 5:
            raise RelayError(
                _tag(role, f"上游不是 SOCKS5（握手版本字节是 0x{ver:02x}，应为 0x05）")
            )
        if method == 0x02:
            if not has_auth:
                raise RelayError(_tag(role, "上游 SOCKS5 要求用户名密码，但代理没配凭据"))
            raw_u = spec.username.encode("utf-8")[:255]
            raw_p = spec.password.encode("utf-8")[:255]
            sock.sendall(
                b"\x01" + bytes([len(raw_u)]) + raw_u + bytes([len(raw_p)]) + raw_p
            )
            _sver, status = _recv_exact(sock, 2)
            if status != 0:
                raise RelayError(
                    _tag(
                        role,
                        f"上游 SOCKS5 用户名密码认证失败（状态 0x{status:02x}）"
                        " —— 凭据不对，或该代理只允许白名单 IP",
                    )
                )
        elif method == 0x00:
            if has_auth:
                logger.debug("上游 SOCKS5 不需要认证，跳过 RFC1929")
        elif method == 0xFF:
            raise RelayError(
                _tag(role, "上游 SOCKS5 拒绝了所有认证方式（0xFF）—— 通常是没有提供它要求的凭据")
            )
        else:
            raise RelayError(_tag(role, f"上游 SOCKS5 选了不支持的认证方式 0x{method:02x}"))

        # 目标地址：域名走 ATYP=0x03 让上游解析（DNS 不泄漏）
        sock.sendall(b"\x05\x01\x00" + _socks5_addr(host, port))
        _ver, rep, _rsv, atyp = _recv_exact(sock, 4)
        if rep != 0:
            raise RelayError(_tag(role, _SOCKS5_REPLY.get(rep, f"上游返回未知应答码 {rep}")))
        # BND.ADDR / BND.PORT 必须吃干净，否则它们会被当成隧道里的应用数据
        if atyp == 0x01:
            _recv_exact(sock, 4 + 2)
        elif atyp == 0x04:
            _recv_exact(sock, 16 + 2)
        elif atyp == 0x03:
            (n,) = _recv_exact(sock, 1)
            _recv_exact(sock, n + 2)
        else:
            raise RelayError(_tag(role, f"上游 SOCKS5 应答里的地址类型不认识：0x{atyp:02x}"))

    def _handshake(
        self,
        sock: socket.socket,
        spec: ProxySpec,
        host: str,
        port: int,
        *,
        role: str = "",
    ) -> bytes:
        """按 `spec` 的协议，在一条**已连上的** socket 上完成"到 host:port"的握手。"""
        if _is_socks(spec.scheme):
            if spec.scheme.lower().startswith("socks4"):
                raise RelayError(
                    _tag(role, "上游是 socks4，本地中继只实现了 socks5（请把代理改成 socks5://）")
                )
            self._socks5_handshake(sock, spec, host, port, role=role)
            return b""
        return self._http_connect(sock, spec, host, port, role=role)

    def _open_tunnel(
        self,
        spec: ProxySpec,
        host: str,
        port: int,
        *,
        role: str = "",
    ) -> tuple[socket.socket, bytes]:
        """建立一条"按 spec 的协议到 host:port"的隧道，返回 (套接字, 抢跑的尾巴)。

        这是链式代理的地基。把"连上代理"（`_connect_proxy`）与"在连接上握手"
        （`_handshake`）拆开之后，链式就是三步、每步都是现成的：

            sock = _open_tunnel(前置, 落地host, 落地port)   # 先穿过前置
            _handshake(sock, 落地spec, 目标host, 目标port)   # 在隧道上再握一次

        旧实现把这两件事焊死在 `_socks5_connect` 里（它自己 create_connection），
        所以第二条连接根本没法复用 —— 这就是这次必须拆的原因。
        """
        sock = self._connect_proxy(spec, role=role)
        try:
            rest = self._handshake(sock, spec, host, port, role=role)
        except BaseException:
            self._forget(sock)
            raise
        return sock, rest

    def _open_to_proxy(self) -> socket.socket:
        """连到**落地代理本体**（有前置就先穿过前置）。

        用途：明文 HTTP 走 HTTP 上游时，要在这条连接上直接发绝对 URI 请求，
        而不是再套一层 CONNECT（RFC 9112 §3.2.2 的 absolute-form）。
        """
        hop = self._spec.hop
        if hop is None:
            return self._connect_proxy(self._spec)
        sock, rest = self._open_tunnel(hop, self._spec.host, self._spec.port, role=_ROLE_HOP)
        if rest:
            sock = _Prefixed(sock, rest)
        return sock

    def _open_to_target(self, host: str, port: int) -> tuple[socket.socket, bytes]:
        """建一条到**最终目标**的隧道；链式就先过前置，再让落地去连目标。

        两跳时的字节流长这样（以 HTTP 前置 + SOCKS5 落地为例）：

            本中继 --CONNECT 落地:20000--> 前置
            本中继 --05 02 00 02 / 认证 / 05 01 00 03 len host port--> 落地   （走在前置隧道里）
            之后的字节全部是"目标 ↔ 浏览器"的裸流
        """
        hop = self._spec.hop
        if hop is None:
            return self._open_tunnel(self._spec, host, port)
        sock, rest = self._open_tunnel(hop, self._spec.host, self._spec.port, role=_ROLE_HOP)
        if rest:
            sock = _Prefixed(sock, rest)
        try:
            tail = self._handshake(sock, self._spec, host, port, role=_ROLE_EXIT)
        except BaseException:
            self._forget(sock)
            raise
        return sock, tail

    def _tunnel_to(self, host: str, port: int) -> tuple[socket.socket, bytes]:
        """老名字（`_handle` 在用），语义就是 `_open_to_target`。"""
        return self._open_to_target(host, port)


    # ── 双向转发 ─────────────────────────────────────────
    def _tunnel(
        self,
        client: socket.socket,
        up: socket.socket,
        *,
        to_up: bytes = b"",
        to_client: bytes = b"",
    ) -> None:
        """两个方向各一个线程裸转发；任一端 EOF/超时就把对面也关掉。"""
        try:
            if to_client:
                client.sendall(to_client)
                self._count_out(len(to_client))
            if to_up:
                up.sendall(to_up)
                self._count_in(len(to_up))
            t_up = threading.Thread(
                target=self._pump, args=(client, up, "in"), daemon=True
            )
            t_down = threading.Thread(
                target=self._pump, args=(up, client, "out"), daemon=True
            )
            t_up.start()
            t_down.start()
            t_up.join()
            t_down.join()
        finally:
            for sock in (up, client):
                try:
                    sock.close()
                except OSError:
                    pass
            self._forget(up)

    def _pump(self, src: socket.socket, dst: socket.socket, direction: str) -> None:
        while True:
            try:
                chunk = src.recv(_RECV)
            except (socket.timeout, TimeoutError):
                # 空闲超时：keep-alive 连接不能永远占着线程
                logger.debug("中继 port=%s 空闲超时，关闭连接", self._port)
                break
            except OSError:
                break
            if not chunk:
                break
            try:
                dst.sendall(chunk)
            except OSError:
                break
            if direction == "in":
                self._count_in(len(chunk))
            else:
                self._count_out(len(chunk))
        # 半关闭：让对面看到 EOF 从而退出自己的 recv，避免两个线程互相等
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def _count_in(self, n: int) -> None:
        with self._lock:
            self._bytes_in += n

    def _count_out(self, n: int) -> None:
        with self._lock:
            self._bytes_out += n

    # ── 给客户端的错误响应 ───────────────────────────────
    @staticmethod
    def _send_error(conn: socket.socket, status: int, reason: str, text: str) -> None:
        """明确回一个响应体。静默断开在浏览器里只表现为"一直转圈"，最难排查。"""
        body = f"知纹本地代理中继：{text}\n".encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "X-Zw-Relay: 1\r\n"
            "\r\n"
        ).encode("latin-1")
        try:
            conn.sendall(head + body)
        except OSError:
            pass


# ── 端口池：同一个上游只开一个中继 ────────────────────────
class RelayPool:
    """按 `spec.key`（协议+主机+端口+用户名+密码的哈希）复用中继端口。

    为什么要复用：同一台机器上几十个环境常常共用一条代理，每个环境都开一个
    监听端口既浪费也难观测；而**不同上游必须用不同端口**（否则串代理，
    等于把 A 环境的流量从 B 的出口发出去，指纹就串了）。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_key: dict[str, RelayServer] = {}

    def ensure(self, spec: ProxySpec, **kwargs: Any) -> int:
        """确保这个上游有中继在监听，返回本地端口。"""
        with self._lock:
            server = self._by_key.get(spec.key)
            if server is not None and server.alive:
                return server.port
            if server is not None:
                server.stop()  # 端口被外部关了/线程死了，重建
            server = RelayServer(spec, **kwargs)
            port = server.start()
            self._by_key[spec.key] = server
            return port

    def get_by_port(self, port: int) -> RelayServer | None:
        with self._lock:
            for server in self._by_key.values():
                if server.port == int(port):
                    return server
        return None

    def get(self, spec: ProxySpec) -> RelayServer | None:
        with self._lock:
            return self._by_key.get(spec.key)

    def servers(self) -> list[RelayServer]:
        with self._lock:
            return list(self._by_key.values())

    def stats(self) -> list[dict[str, Any]]:
        return [s.stats() for s in self.servers()]

    def stop_all(self) -> None:
        with self._lock:
            servers = list(self._by_key.values())
            self._by_key.clear()
        for server in servers:
            server.stop()

    def cleanup(self, keep_specs: set[str]) -> int:
        """停掉不在 `keep_specs`（`spec.key` 集合）里的中继，返回停掉的个数。

        为什么要主动清：中继端口是**运行期资源**，环境关了就该释放；
        不清理的话每次开关环境都会多留一个监听端口，跑一天就积出几十个。
        """
        keep = set(keep_specs or ())
        removed = 0
        with self._lock:
            for key, server in list(self._by_key.items()):
                if key in keep:
                    continue
                server.stop()
                self._by_key.pop(key, None)
                removed += 1
        if removed:
            logger.info("本地中继清理：停掉 %s 个不再使用的端口", removed)
        return removed

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_key)


_pool: RelayPool | None = None
_pool_lock = threading.Lock()


def pool() -> RelayPool:
    """进程内单例。测试里可以 `pool().stop_all()` 清干净。"""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = RelayPool()
    return _pool

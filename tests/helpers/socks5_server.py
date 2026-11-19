"""极简 SOCKS5 服务端（支持 RFC 1929 用户名/密码认证）。

**仅供测试**：用来验证 `proxyrelay` 真的能和「要认证的 SOCKS5 上游」握手成功。
不追求完整实现 —— 只实现 CONNECT + 可选认证 + 双向转发。

## 一个踩过的坑（值得记下来）

RFC 1929 的认证包布局是：

    VER(1) ULEN(1) UNAME PLEN(1) PASSWD

第二字节是**用户名长度**，不是版本号。第一版把它当版本号校验，
结果合法客户端在认证阶段就被断开 —— 排查半天才发现是**测试桩自己的 bug**，
不是产品代码的问题。

用法：
    python tests/helpers/socks5_server.py PORT [auth|noauth]
"""

import select
import socket
import struct
import sys
import threading

USER, PASS = b"cf1b481103", b"51ec63a321"
MODE = sys.argv[2] if len(sys.argv) > 2 else "auth"


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return buf
        buf += chunk
    return buf


def handle(c):
    try:
        hdr = _recv(c, 2)
        if len(hdr) < 2 or hdr[0] != 5:
            return
        methods = _recv(c, hdr[1])
        if MODE == "auth":
            if 2 not in methods:
                c.sendall(b"\x05\xff")
                return
            c.sendall(b"\x05\x02")
            # RFC 1929：VER(1) ULEN(1) UNAME PLEN(1) PASSWD
            v = _recv(c, 2)
            if len(v) < 2 or v[0] != 1:
                return
            u = _recv(c, v[1]) if v[1] else b""
            b = _recv(c, 1)
            if not b:
                return
            p = _recv(c, b[0]) if b[0] else b""
            ok = (u == USER and p == PASS)
            c.sendall(b"\x01\x00" if ok else b"\x01\x01")
            if not ok:
                return
        else:
            c.sendall(b"\x05\x00")

        req = _recv(c, 4)
        if len(req) < 4 or req[1] != 1:
            return
        atyp = req[3]
        if atyp == 3:
            ln = _recv(c, 1)[0]
            host = _recv(c, ln).decode()
        elif atyp == 1:
            host = socket.inet_ntoa(_recv(c, 4))
        else:
            host = socket.inet_ntop(socket.AF_INET6, _recv(c, 16))
        port = struct.unpack(">H", _recv(c, 2))[0]

        try:
            up = socket.create_connection((host, port), timeout=5)
        except OSError:
            c.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
            return
        c.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)

        while True:
            r, _, _ = select.select([c, up], [], [], 10)
            if not r:
                break
            for s in r:
                d = s.recv(4096)
                if not d:
                    return
                (up if s is c else c).sendall(d)
    except OSError:
        pass
    finally:
        try:
            c.close()
        except OSError:
            pass


def serve(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(20)
    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


if __name__ == "__main__":
    serve(int(sys.argv[1]))

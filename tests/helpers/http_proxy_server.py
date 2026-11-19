"""极简 HTTP 代理服务端（支持 Basic 认证），仅测试用。

实现 CONNECT（HTTPS 隧道）和普通 GET 转发两种，用来验证 `proxyrelay`
往上游补的 `Proxy-Authorization` 是否正确。
"""

import base64
import select
import socket
import sys
import threading

USER, PASS = "cf1b481103", "51ec63a321"
MODE = sys.argv[2] if len(sys.argv) > 2 else "auth"


def _recv_head(sock, limit=65536):
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = sock.recv(4096)
        if not d:
            return buf
        buf += d
        if len(buf) > limit:
            return buf
    return buf


def handle(c):
    try:
        head = _recv_head(c)
        if not head:
            return
        text = head.decode("latin-1", "replace")
        first = text.split("\r\n", 1)[0]
        if MODE == "auth":
            ok = False
            for line in text.split("\r\n")[1:]:
                if line.lower().startswith("proxy-authorization:"):
                    val = line.split(":", 1)[1].strip()
                    if val.lower().startswith("basic "):
                        try:
                            u, p = base64.b64decode(val[6:]).decode().split(":", 1)
                            ok = (u == USER and p == PASS)
                        except Exception:
                            ok = False
            if not ok:
                c.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                          b"Proxy-Authenticate: Basic realm=\"test\"\r\n"
                          b"Content-Length: 0\r\n\r\n")
                return
        # CONNECT 隧道
        if first.upper().startswith("CONNECT"):
            target = first.split()[1]
            host, _, port = target.rpartition(":")
            try:
                up = socket.create_connection((host, int(port)), timeout=5)
            except OSError:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            while True:
                r, _, _ = select.select([c, up], [], [], 10)
                if not r:
                    break
                for s in r:
                    d = s.recv(4096)
                    if not d:
                        return
                    (up if s is c else c).sendall(d)
            up.close()
        else:
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nPROXY")
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

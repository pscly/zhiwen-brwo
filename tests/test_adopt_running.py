"""核心重启后，**已在运行的浏览器要能被重新接管**，而不是判"失败"。

## 为什么（2026-11-22 云电脑实机）

用户开着好几个环境（c01/c02/c03），客户端重启后，数据库里它们变成 `failed`：

    c03  state=failed  err=端口 21004 被别的进程（pid=8992）占用，不是刚启动的浏览器
    c02  state=failed  err=端口 21003 被别的进程（pid=9612）占用，不是刚启动的浏览器
    c01  state=failed  err=端口 21002 被别的进程（pid=19900）占用，不是刚启动的浏览器

而那些 chrome **好端端在跑**、仍占着 CDP 端口（实测 udd 都对得上）。
再点「启动」只会撞 `PORT_MISMATCH`，用户看到"全是失败"，其实什么都没坏 ——
只是 core 丢了账本。

## 另一条同源缺陷：死租约挡住启动

core 被 kill 后，租约里写着那个**已死进程**的 id，还能挡 90 秒：

    PROFILE_BUSY  正被 psubs3.6:1928972:8e15412d 占用（剩余 49 秒）
                  如果确认那是已经死掉的进程，可以用 zw profile unlock 强制释放

普通用户不知道 `zw profile unlock` 是什么。现在 `start()` 会先自己清死租约。
"""

from __future__ import annotations


def _setup(tmp_path, monkeypatch):
    from zw_core import config
    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_DEVICE_ID", "zwd_adopt_0001")
    monkeypatch.setenv("ZW_SHELL_ENABLED", "false")
    config.reset_settings()
    from zw_core.services import manager
    return manager


def test_adopt_returns_none_when_nothing_running(tmp_path, monkeypatch):
    """没在跑时必须返回 None（不能瞎认，否则会把 A 的端口记到 B 头上）。"""
    manager = _setup(tmp_path, monkeypatch)
    try:
        p = manager.create_profile({"name": "没在跑", "with_default_packs": False})
        assert manager._adopt_if_running(manager.get_profile(p["id"])) is None
    finally:
        from zw_core import config
        config.reset_settings()


def test_adopt_rejects_port_owned_by_other_profile(tmp_path, monkeypatch):
    """端口有响应，但持有进程的 udd 不是本环境的 → 不接管。

    这是防"串号"的关键一条：只看"端口通不通"会把别的环境的浏览器据为己有，
    之后所有 CDP 操作都打到错误的环境上。
    """
    import socket
    import threading

    manager = _setup(tmp_path, monkeypatch)
    try:
        p = manager.create_profile({"name": "串号防护", "with_default_packs": False})

        # 起一个假 CDP 服务占住端口，但**没有任何真实的 chrome 进程**持有它
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        port = srv.getsockname()[1]

        def serve():
            try:
                srv.settimeout(3)
                while True:
                    conn, _ = srv.accept()
                    conn.recv(4096)
                    body = b'{"webSocketDebuggerUrl":"ws://127.0.0.1:%d/devtools/browser/fake"}' % port
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
                    )
                    conn.close()
            except OSError:
                pass

        threading.Thread(target=serve, daemon=True).start()
        manager._update(p["id"], cdp_port=port)

        got = manager._adopt_if_running(manager.get_profile(p["id"]))
        assert got is None, f"不该接管一个没有对应 chrome 进程的端口：{got}"
        srv.close()
    finally:
        from zw_core import config
        config.reset_settings()


def test_stale_lease_is_reaped_before_busy_error(tmp_path, monkeypatch):
    """持有者进程已经死掉的租约，必须自动清掉，不能报 PROFILE_BUSY。"""
    manager = _setup(tmp_path, monkeypatch)
    try:
        from zw_core.services.leases import LocalLease

        p = manager.create_profile({"name": "死租约", "with_default_packs": False})
        lease = LocalLease(manager.profile_dir(p["id"]))
        # 手写一个"持有者是不存在的 pid"的租约
        lease.path.write_text(
            '{"owner": "' + __import__("socket").gethostname() + ':999999:deadbeef",'
            ' "kind": "run", "ts": ' + str(__import__("time").time()) + ', "ttl": 90}',
            encoding="utf-8",
        )
        assert lease.reap_if_stale() is True, "死租约应该被清掉"
        # 清掉之后能正常拿到
        lease.acquire("run")
        lease.release()
    finally:
        from zw_core import config
        config.reset_settings()

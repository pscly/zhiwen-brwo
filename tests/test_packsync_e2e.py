"""`packsync.pull()` 真的走 hub HTTP —— 以往的测试没覆盖这一段。

## 为什么补（2026-11-20 对抗审查指出）

原来的 `test_packsync_roundtrip.py` 注释写着"走和 packsync 一样的路径"，
但**没有调用 `packsync.pull()`**，也没碰任何 hub 端点 —— 它只是把同一份字节
喂给 `import_pack`。所以"从控制台拉包"这条链**一行覆盖都没有**。

这里起真 hub（uvicorn，随机端口）、真注册设备、真走 HTTP 下载、
真导入，并盯住审查发现的三个坑：

1. core 产的包形状（`extensions/`）上传后扩展数不能是 0
2. 删掉本机包之后再拉，必须**真的拉回来**（不能报"已是最新"却什么都没有）
3. 未授权的匿名请求必须被拒
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
import zipfile

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _core_shaped_zip(*, ext: int = 2) -> bytes:
    """按 **core 真实产出**的形状打包：`extensions/<key>/` + `data/<key>/`。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(ext):
            key = f"abcd{i:04d}"
            z.writestr(f"extensions/{key}/manifest.json",
                       json.dumps({"name": f"扩展{i}", "version": "1.0"}))
            z.writestr(f"extensions/{key}/sw.js", "void 0;")
            z.writestr(f"data/{key}/000003.log", b"LOGIN-STATE")
    return buf.getvalue()


@pytest.fixture()
def live_hub(tmp_path, monkeypatch):
    """真起一个 hub（空 token，验证鉴权修复后仍要求管理员身份）。"""
    import uvicorn

    from zw_hub import main as hub_main

    monkeypatch.setenv("ZW_DATA_DIR", str(tmp_path / "hub"))
    monkeypatch.setenv("ZW_HUB_TOKEN", "e2e-token")
    import zw_hub.config as cfg
    cfg._settings = None

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(hub_main.app, host="127.0.0.1", port=port,
                                       log_level="error", access_log=False))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    time.sleep(0.3)


def _http(method: str, url: str, body: bytes | None = None,
          headers: dict[str, str] | None = None):
    import httpx

    with httpx.Client(timeout=30) as c:
        return c.request(method, url, content=body, headers=headers or {})


def test_upload_recognises_core_pack_shape(live_hub):
    """审查发现的坑 ①：core 形状的包上传后扩展数不能是 0。"""
    h = {"Authorization": "Bearer e2e-token"}
    r = _http("POST", f"{live_hub}/v1/packs?name=core-shape", _core_shaped_zip(ext=2),
              {**h, "Content-Type": "application/zip"})
    assert r.status_code == 200, r.text[:200]
    d = r.json()["data"]
    assert d["extension_count"] == 2, f"扩展数错：{d}"
    assert d["has_data"] is True


def test_anonymous_upload_is_rejected(live_hub):
    """审查发现的坑 ③：没有管理员身份不能读、也不能下载。"""
    assert _http("GET", f"{live_hub}/v1/packs").status_code == 401
    assert _http("POST", f"{live_hub}/v1/packs?name=x", b"zz").status_code == 401


def test_pull_end_to_end_and_repull_after_delete(live_hub, tmp_path, monkeypatch):
    """审查发现的坑 ②：删掉本机包后再拉，必须真的拉回来。"""
    h = {"Authorization": "Bearer e2e-token"}
    dev = "zwd_e2epull0001"
    # 注册并批准设备（真实流程）
    _http("POST", f"{live_hub}/v1/devices/enroll",
          json.dumps({"device_id": dev, "label": "e2e", "hostname": "e2e",
                      "platform": "Linux", "arch": "x86_64",
                      "app_version": "0.8.20"}).encode(),
          {**h, "Content-Type": "application/json"})
    from zw_hub.devices import DeviceStore
    DeviceStore(tmp_path / "hub" / "devices.db").approve(dev, actor="test")

    up = _http("POST", f"{live_hub}/v1/packs?name=CLI-PACK", _core_shaped_zip(ext=1),
               {**h, "Content-Type": "application/zip"})
    assert up.status_code == 200, up.text[:200]

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "core"))
    monkeypatch.setenv("ZW_HUB_URL", live_hub)
    monkeypatch.setenv("ZW_HUB_TOKEN", "e2e-token")
    monkeypatch.setenv("ZW_DEVICE_ID", dev)
    monkeypatch.setenv("ZW_PORT", str(_free_port()))
    monkeypatch.setenv("ZW_HOST", "127.0.0.1")
    monkeypatch.setenv("ZW_ADMIN_TOKEN", "")
    from zw_core import config
    config.reset_settings()
    from zw_core.services import packs, packsync

    try:
        r1 = packsync.pull()
        assert r1["ok"] is True, r1
        assert r1["added"], f"第一次就该拉到：{r1}"
        local = packs.list_packs()
        assert len(local) == 1, local
        pid = local[0]["id"]
        assert local[0]["extension_count"] == 1

        # 删掉本机包
        packs.delete_pack(pid)
        assert packs.list_packs() == []

        # 再拉：必须重新下载，而不是报"已是最新"
        r2 = packsync.pull()
        assert r2["ok"] is True, r2
        assert r2["added"], f"删了本机包却没重新拉：{r2}"
        assert len(packs.list_packs()) == 1, "本机包没恢复"
    finally:
        config.reset_settings()

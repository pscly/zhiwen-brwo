"""从 Chrome 商店链接 / 本地 .crx 安装扩展。

用户要求：输入商店链接就能添加；能自己更新；也能手动拖一个扩展放上去自动装好。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from zw_core.services import extstore

COOKIEMANAGER = (
    "https://chromewebstore.google.com/detail/cookiemanager-cookie-edit/"
    "hdhngoamekjhmnpenphenpaiindoinpo?hl=zh-CN"
)
CM_ID = "hdhngoamekjhmnpenphenpaiindoinpo"


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _crx3(zip_bytes: bytes, header_len: int = 1310) -> bytes:
    """造一个结构合法的 CRX3：Cr24 + ver3 + header_len + header + zip。"""
    header = b"\x00" * header_len
    return b"Cr24" + (3).to_bytes(4, "little") + header_len.to_bytes(4, "little") + header + zip_bytes


def _crx2(zip_bytes: bytes) -> bytes:
    pub = b"P" * 100
    sig = b"S" * 200
    return (b"Cr24" + (2).to_bytes(4, "little") + len(pub).to_bytes(4, "little")
            + len(sig).to_bytes(4, "little") + pub + sig + zip_bytes)


def _manifest(name="CookieManager", version="1.2.3"):
    return json.dumps({"manifest_version": 3, "name": name, "version": version}).encode()


# ── 链接解析 ────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    COOKIEMANAGER,
    "https://chromewebstore.google.com/detail/cookiemanager-cookie-edit/" + CM_ID,
    "https://chrome.google.com/webstore/detail/x/" + CM_ID,
    "https://chromewebstore.google.com/detail/" + CM_ID + "?hl=zh-CN",
    "https://chromewebstore.google.com/x?hl=zh-CN&id=" + CM_ID,
    CM_ID,
])
def test_parse_extension_id_accepts_url_forms(text):
    """用户粘的是完整链接（还带 ?hl=zh-CN），必须能认出 ID。"""
    assert extstore.parse_extension_id(text) == CM_ID


def test_parse_extension_id_rejects_garbage():
    with pytest.raises(extstore.StoreError) as ei:
        extstore.parse_extension_id("https://example.com/not-a-store-link")
    assert ei.value.code == "EXT_STORE_INVALID_URL"
    assert "32" in ei.value.message or "详情页" in ei.value.message


def test_crx_url_encodes_x_param():
    """`x=id%3D<id>%26uc` 的 %26 不能丢，否则商店 400。"""
    url = extstore.crx_url(CM_ID)
    assert f"x=id%3D{CM_ID}%26uc" in url
    assert "acceptformat=crx2,crx3" in url


# ── CRX 去头 ────────────────────────────────────────────────
def test_strip_crx3_header():
    z = _zip({"manifest.json": _manifest()})
    out = extstore.strip_crx_header(_crx3(z))
    assert out == z


def test_strip_crx2_header():
    z = _zip({"manifest.json": _manifest()})
    assert extstore.strip_crx_header(_crx2(z)) == z


def test_strip_crx_accepts_plain_zip():
    z = _zip({"manifest.json": _manifest()})
    assert extstore.strip_crx_header(z) == z


def test_strip_crx_rejects_html_error_page():
    """商店限流时会回一个 HTML 页面，要给出可读原因而不是烂在 zipfile 里。"""
    with pytest.raises(extstore.StoreError) as ei:
        extstore.strip_crx_header(b"<!DOCTYPE html><html>rate limited</html>")
    assert ei.value.code == "EXT_STORE_BAD_CRX"
    assert "限流" in ei.value.message or "代理" in ei.value.message


def test_strip_crx_rejects_truncated():
    with pytest.raises(extstore.StoreError) as ei:
        extstore.strip_crx_header(b"Cr24" + (3).to_bytes(4, "little"))
    assert ei.value.code == "EXT_STORE_BAD_CRX"


def test_strip_crx_rejects_bad_header_offset():
    """header_len 指到文件末尾之外 = 文件坏了。"""
    blob = b"Cr24" + (3).to_bytes(4, "little") + (9999).to_bytes(4, "little") + b"xx"
    with pytest.raises(extstore.StoreError):
        extstore.strip_crx_header(blob)


# ── 安装进库 ────────────────────────────────────────────────
@pytest.fixture()
def home(tmp_path, monkeypatch):
    from zw_core import config

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    yield tmp_path
    config.reset_settings()


def test_install_from_local_crx(home, tmp_path):
    """用户手动拖一个 .crx 进来 → 自动解包入库。"""
    crx = tmp_path / "cookiemanager.crx"
    crx.write_bytes(_crx3(_zip({"manifest.json": _manifest(), "svc.js": b"//x"})))
    r = extstore.install(crx_path=str(crx))
    assert r["ok"] and r["slug"] == "cookiemanager" and r["version"] == "1.2.3"
    dest = Path(r["path"])
    assert (dest / "manifest.json").is_file()
    assert (dest / "svc.js").read_bytes() == b"//x"


def test_install_from_zip_with_wrapping_dir(home, tmp_path):
    """有些包外面多套一层目录，要能自动进去。"""
    z = _zip({"myslug/manifest.json": _manifest(name="Wrapped"), "myslug/a.js": b"1"})
    p = tmp_path / "wrapped.zip"
    p.write_bytes(z)
    r = extstore.install(crx_path=str(p))
    dest = Path(r["path"])
    assert (dest / "manifest.json").is_file(), "没剥掉外层目录"
    assert (dest / "a.js").is_file()


def test_install_writes_id_marker_and_version_dir(home, tmp_path, monkeypatch):
    """装完要留下扩展 ID 标记（更新检查靠它），目录名是 <slug>-<version>。"""
    z = _zip({"manifest.json": _manifest()})
    # 必须把下载也换掉：不换就会真去连商店（测试机连不上，白等 60 秒超时）
    monkeypatch.setattr(extstore, "download_crx", lambda *_a, **_k: z)
    r = extstore.install(url=COOKIEMANAGER)
    assert Path(r["path"]).name == f"cookiemanager-{r['version']}"
    assert (Path(r["path"]) / ".zw-ext-id").read_text() == CM_ID


def test_install_rejects_zip_without_manifest(home, tmp_path):
    p = tmp_path / "bad.zip"
    p.write_bytes(_zip({"readme.txt": b"no manifest"}))
    with pytest.raises(extstore.StoreError) as ei:
        extstore.install(crx_path=str(p))
    assert ei.value.code == "EXT_STORE_NO_MANIFEST"


def test_install_rejects_path_traversal(home, tmp_path):
    """恶意 zip 想写到目录外面，必须整体拒绝。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", _manifest())
        zf.writestr("../../evil.txt", b"pwn")
    p = tmp_path / "evil.zip"
    p.write_bytes(buf.getvalue())
    with pytest.raises(extstore.StoreError) as ei:
        extstore.install(crx_path=str(p))
    assert ei.value.code == "EXT_STORE_UNSAFE_ARCHIVE"


def test_install_missing_source(home):
    with pytest.raises(extstore.StoreError) as ei:
        extstore.install()
    assert ei.value.code == "EXT_STORE_NO_SOURCE"


def test_install_is_idempotent_but_update_flag_controls(home, tmp_path):
    """同版本再装一次：不 update 就跳过，update=True 则覆盖（幂等）。"""
    crx = tmp_path / "x.crx"
    crx.write_bytes(_crx3(_zip({"manifest.json": _manifest()})))
    first = extstore.install(crx_path=str(crx))
    again = extstore.install(crx_path=str(crx), update=False)
    assert again.get("skipped")
    forced = extstore.install(crx_path=str(crx), update=True)
    assert forced["ok"] and forced["already_existed"] is True


def test_check_updates_skips_entries_without_id(home, tmp_path):
    """手工导入的包没有扩展 ID 标记 → 不瞎猜、不去下载别人的扩展。"""
    crx = tmp_path / "y.crx"
    crx.write_bytes(_crx3(_zip({"manifest.json": _manifest(name="Manual")})))
    extstore.install(crx_path=str(crx))
    assert extstore.check_updates() == []


def test_check_updates_reports_newer_version(home, tmp_path, monkeypatch):
    """库里是 1.0.0、商店是 2.0.0 → update_available=True。"""
    crx = tmp_path / "z.crx"
    crx.write_bytes(_crx3(_zip({"manifest.json": _manifest(version="1.0.0")})))
    monkeypatch.setattr(extstore, "download_crx",
                        lambda *_a, **_k: _zip({"manifest.json": _manifest(version="1.0.0")}))
    extstore.install(url=COOKIEMANAGER)

    monkeypatch.setattr(extstore, "fetch_info",
                        lambda *_a, **_k: {"version": "2.0.0", "name": "CookieManager"})
    items = extstore.check_updates()
    assert len(items) == 1
    assert items[0]["current"] == "1.0.0"
    assert items[0]["latest"] == "2.0.0"
    assert items[0]["update_available"] is True


def test_download_unreachable_gives_actionable_message(monkeypatch):
    """连不上商店时要说清"这台机器需要能访问 clients2.google.com"，别只说失败。"""
    def boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(extstore.httpx, "Client", boom)
    with pytest.raises(extstore.StoreError) as ei:
        extstore.download_crx(CM_ID)
    assert ei.value.code == "EXT_STORE_UNREACHABLE"
    assert "clients2.google.com" in ei.value.message


# ── 装完自动建包 + 设为默认（这才是"新建环境自动带上"的完整链路）──
def test_install_makes_pack_by_default(home, tmp_path):
    """装完必须自动建包。

    只把代码放进 ext_dir 是**没用的**：没有任何环境会引用它
    （`create_profile` 只按"默认包"挂扩展）。这就是用户"以后新建就带上"
    这条要求缺的最后一环。
    """
    from zw_core.services import packs

    crx = tmp_path / "c.crx"
    crx.write_bytes(_crx3(_zip({"manifest.json": _manifest()})))
    r = extstore.install(crx_path=str(crx))
    assert r["pack_created"] is True
    assert r["pack_id"], r
    ids = [p["id"] for p in packs.list_packs()]
    assert r["pack_id"] in ids


def test_install_as_default_flows_into_new_profile(home, tmp_path):
    """as_default=True → 新建环境自动带上这个扩展（端到端）。"""
    from zw_core.services import manager as mgr, packs

    crx = tmp_path / "d.crx"
    crx.write_bytes(_crx3(_zip({"manifest.json": _manifest()})))
    r = extstore.install(crx_path=str(crx), as_default=True)
    assert r["is_default"] is True
    assert r["pack_id"] in packs.default_pack_ids()

    prof = mgr.create_profile({"name": "自动带扩展"})
    assert "cookiemanager" in (mgr.get_profile(prof["id"]).get("extensions") or [])


def test_install_without_pack_leaves_library_only(home, tmp_path):
    """make_pack=False 时只入库、不建包（给"我自己管包"的场景留口子）。"""
    from zw_core.services import packs

    crx = tmp_path / "e.crx"
    crx.write_bytes(_crx3(_zip({"manifest.json": _manifest()})))
    r = extstore.install(crx_path=str(crx), make_pack=False)
    assert r["pack_created"] is False and r["pack_id"] == ""
    assert packs.list_packs() == []


def test_pack_creation_failure_does_not_lose_the_extension(home, tmp_path, monkeypatch):
    """建包失败不能让整个安装失败 —— 代码已经落好了，用户还能手动建。"""
    from zw_core.services import packs

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(packs, "import_pack", boom)
    crx = tmp_path / "f.crx"
    crx.write_bytes(_crx3(_zip({"manifest.json": _manifest()})))
    r = extstore.install(crx_path=str(crx))
    assert r["ok"] is True
    assert r["pack_created"] is False
    assert "disk full" in r.get("pack_error", "")
    assert Path(r["path"], "manifest.json").is_file(), "扩展本体不能丢"


# ── 真浏览器集成（需要支持 --load-extension 的内核，CI 上跳过）──
def _loadable_engine():
    """找一个真的能加载扩展的内核（官方 Chrome 137+ 不行，ungoogled 可以）。"""
    import shutil

    cands = [
        "/opt/bsm-p0/ungoogled-chromium-150.0.7871.186-1-x86_64_linux/chrome",
        shutil.which("ungoogled-chromium") or "",
        shutil.which("chromium") or "",
    ]
    from zw_core.services import engines

    for c in cands:
        if not c or not Path(c).exists():
            continue
        ok, _why = engines.extensions_loadable(Path(c))
        if ok:
            return c
    return ""


def test_installed_extension_actually_runs_in_browser(home, tmp_path):
    """装进来的扩展要在**真浏览器**里真的跑起来。

    这条是整条链路的终点判据：CRX 安装 → 建包 → 设默认 → 新建环境 → 内容脚本执行。
    只断言"文件在磁盘上"是不够的——用户要的是"插件能用"。

    没有支持 --load-extension 的内核时跳过（官方 Chrome 137+ 会明确不加载）。
    """
    import json as _json
    import time as _time
    import urllib.request

    import pytest as _pytest

    engine = _loadable_engine()
    if not engine:
        _pytest.skip("这台机器上没有能加载扩展的内核（官方 Chrome 137+ 不支持）")

    from zw_core import config as _cfg
    from zw_core.services import extstore as _es, manager as _mgr

    crx = tmp_path / "real.crx"
    crx.write_bytes(_crx3(_zip({
        "manifest.json": _json.dumps({
            "manifest_version": 3, "name": "Runner", "version": "1.0",
            "content_scripts": [{"matches": ["<all_urls>"], "js": ["cs.js"],
                                 "run_at": "document_end"}],
        }).encode(),
        "cs.js": b"document.documentElement.setAttribute('data-zw-run','OK');",
    })))
    import os as _os

    _os.environ["ZW_CHROME_BIN"] = engine
    _cfg.reset_settings()

    r = _es.install(crx_path=str(crx), as_default=True)
    assert r["pack_created"] and r["is_default"]

    prof = _mgr.create_profile({"name": "runner"})
    assert "runner" in (_mgr.get_profile(prof["id"]).get("extensions") or [])

    res = _mgr.start(prof["id"])
    port = (res.get("local") or {}).get("port")
    assert port, res
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
                break
            except Exception:
                _time.sleep(0.4)
        _time.sleep(3)
        targets = _json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=3))
        pages = [t for t in targets if t.get("type") == "page"]
        assert pages, targets

        import websockets.sync.client as _wsc

        with _wsc.connect(pages[0]["webSocketDebuggerUrl"]) as ws:
            ws.send(_json.dumps({"id": 1, "method": "Page.navigate",
                                 "params": {"url": "https://example.com/"}}))
            _time.sleep(5)
            ws.send(_json.dumps({"id": 2, "method": "Runtime.evaluate",
                                 "params": {"expression":
                                     "document.documentElement.getAttribute('data-zw-run')",
                                     "returnByValue": True}}))
            got = _json.loads(ws.recv())
            while got.get("id") != 2:
                got = _json.loads(ws.recv())
            marker = got.get("result", {}).get("result", {}).get("value")
        assert marker == "OK", f"插件没在页面里生效：{marker!r}"
    finally:
        try:
            _mgr.stop(prof["id"], force=True)
        except Exception:
            pass


# ── 真 HTTP 下载路径（本地假商店，不需要外网）───────────────
def _serve_store(crx_bytes: bytes, *, status: int = 200, redirect: bool = True):
    """起一个本地"假 Chrome 商店"。

    为什么要它：`download_crx` 之前一直被 monkeypatch 掉，等于**真实的 HTTP 路径
    从没被测过** —— URL 拼错、不跟随重定向、状态码没判断，这些都测不出来。
    商店真实行为是：`/service/update2/crx` 返回 302，再跳到实际文件。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/service/update2/crx"):
                if redirect:
                    self.send_response(302)
                    self.send_header("Location", "/actual.crx")
                    self.end_headers()
                    return
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.startswith("/actual.crx"):
                if status != 200:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(crx_bytes)))
                self.end_headers()
                self.wfile.write(crx_bytes)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_a):
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, int(srv.server_address[1])


def test_download_crx_over_real_http_with_redirect(home, monkeypatch):
    """真发一次 HTTP：跟随 302 → 拿到 CRX → 去头解包 → 建包。

    这条补的是"输入商店链接就能装"里唯一没测过的一环。
    """
    import io
    import json as _json
    import zipfile as _zf

    from zw_core.services import extstore as es

    buf = io.BytesIO()
    with _zf.ZipFile(buf, "w") as z:
        z.writestr("manifest.json",
                   _json.dumps({"manifest_version": 3, "name": "HttpExt", "version": "3.1.4"}))
        z.writestr("sw.js", "// x")
    crx = _crx3(buf.getvalue())

    srv, port = _serve_store(crx, redirect=True)
    try:
        # 把商店地址指到本地假服务器
        monkeypatch.setattr(es, "UPDATE_URL", f"http://127.0.0.1:{port}/service/update2/crx")
        r = es.install(url="hdhngoamekjhmnpenphenpaiindoinpo", as_default=True)
        assert r["slug"] == "httpext" and r["version"] == "3.1.4", r
        assert r["pack_created"] is True and r["is_default"] is True, r
        from pathlib import Path

        assert (Path(r["path"]) / "manifest.json").is_file()
        assert (Path(r["path"]) / "sw.js").is_file()
    finally:
        srv.shutdown()


def test_download_crx_http_error_is_readable(home, monkeypatch):
    """商店回 404（扩展下架/ID 写错）时要给出可读错误，而不是烂在 zipfile 里。"""
    from zw_core.services import extstore as es

    srv, port = _serve_store(b"", status=404)
    try:
        monkeypatch.setattr(es, "UPDATE_URL", f"http://127.0.0.1:{port}/service/update2/crx")
        with pytest.raises(es.StoreError) as ei:
            es.download_crx("hdhngoamekjhmnpenphenpaiindoinpo", timeout=10)
        assert ei.value.code in ("EXT_STORE_HTTP_ERROR", "EXT_STORE_BAD_CRX"), ei.value.code
        assert "404" in ei.value.message or "下架" in ei.value.message, ei.value.message
    finally:
        srv.shutdown()


def test_download_crx_unreachable_host(home, monkeypatch):
    """连不上时要说清"需要能访问 clients2.google.com"，并建议配代理。"""
    from zw_core.services import extstore as es

    # 127.0.0.1:1 基本不可能有服务
    monkeypatch.setattr(es, "UPDATE_URL", "http://127.0.0.1:1/service/update2/crx")
    with pytest.raises(es.StoreError) as ei:
        es.download_crx("hdhngoamekjhmnpenphenpaiindoinpo", timeout=5)
    assert ei.value.code == "EXT_STORE_UNREACHABLE"
    assert "clients2" in ei.value.message and "代理" in ei.value.message

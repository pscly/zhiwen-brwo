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

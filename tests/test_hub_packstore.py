"""hub 的扩展包库：让「把插件打包放到控制台」真正成立。

## 为什么（2026-11-20）

用户原话：「把现在 RXYCDP 浏览器的插件及其数据，最好能一起**打包放到我的控制台里面**。
这样以后创建浏览器时，可以默认就把这些插件的数据都带上。」

但在此之前，扩展包**只存在每台机器的 core 本地**（`<数据目录>/packs/`）——
换一台机器就要重新导入一次。那是本机缓存，不叫"放到控制台"。

这组测试盯住那条链的关键性质：
上传能识别出扩展数与"是否带数据"、下载 sha 一致、**同 id 不覆盖**、删除干净。
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile


def _store(tmp_path, monkeypatch):
    from zw_hub import packstore
    from zw_hub.config import Settings

    monkeypatch.setenv("ZW_DATA_DIR", str(tmp_path / "data"))
    s = Settings()
    s.data_dir = tmp_path / "data"
    s.data_dir.mkdir(parents=True, exist_ok=True)
    packstore.ensure_schema(s)
    return packstore, s


def _pack_zip(*, with_data: bool = True, ext: int = 1) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(ext):
            key = f"ext{i:02d}"
            z.writestr(f"code/{key}/manifest.json",
                       json.dumps({"name": f"扩展{i}", "version": "1.0"}))
            z.writestr(f"code/{key}/sw.js", "void 0;")
            if with_data:
                z.writestr(f"data/{key}/000003.log", b"DATA")
    return buf.getvalue()


def test_put_inspects_extensions_and_data(tmp_path, monkeypatch):
    packstore, s = _store(tmp_path, monkeypatch)
    info = packstore.put(s, _pack_zip(ext=3, with_data=True), name="三扩展")
    assert info["extension_count"] == 3, info
    assert info["has_data"] is True
    assert info["name"] == "三扩展"


def test_put_without_data_reports_has_data_false(tmp_path, monkeypatch):
    """只带代码不带数据的包，`has_data` 必须是 False（别骗调用方）。"""
    packstore, s = _store(tmp_path, monkeypatch)
    info = packstore.put(s, _pack_zip(with_data=False), name="纯代码")
    assert info["extension_count"] == 1
    assert info["has_data"] is False


def test_download_sha_matches(tmp_path, monkeypatch):
    """下载回来的字节 sha256 必须与上传时一致 —— 换机器导入靠它验完整性。"""
    packstore, s = _store(tmp_path, monkeypatch)
    data = _pack_zip()
    info = packstore.put(s, data, name="校验包")
    got, sha = packstore.read_bytes(s, info["id"])
    assert got == data
    assert sha == hashlib.sha256(data).hexdigest() == info["sha256"]


def test_same_id_is_not_overwritten(tmp_path, monkeypatch):
    """CAS：同 id 再传必须 409，不能悄悄覆盖别人的包。"""
    packstore, s = _store(tmp_path, monkeypatch)
    info = packstore.put(s, _pack_zip(), name="首次")
    try:
        packstore.put(s, _pack_zip(ext=2), name="覆盖", pack_id=info["id"])
    except packstore.PackStoreError as exc:
        assert exc.code == "PACK_EXISTS"
        assert exc.status == 409
    else:
        raise AssertionError("同 id 覆盖应当被拒绝")


def test_tampered_file_is_reported(tmp_path, monkeypatch):
    """磁盘上的包被改动过 → 拒绝下发，而不是把坏包送出去。"""
    packstore, s = _store(tmp_path, monkeypatch)
    info = packstore.put(s, _pack_zip(), name="被动过")
    zpath = packstore.pack_dir(s, info["id"]) / f"{info['id']}.zip"
    zpath.write_bytes(b"tampered")
    try:
        packstore.read_bytes(s, info["id"])
    except packstore.PackStoreError as exc:
        assert exc.code == "PACK_CORRUPTED"
    else:
        raise AssertionError("内容不一致时应当报错")


def test_bad_id_is_rejected(tmp_path, monkeypatch):
    """包 id 会拼进文件路径 → 必须挡住路径穿越。"""
    packstore, s = _store(tmp_path, monkeypatch)
    for bad in ("../etc", "a/b", "..", ""):
        try:
            packstore.get(s, bad)
        except packstore.PackStoreError:
            continue
        raise AssertionError(f"非法 id 没被拒：{bad!r}")


def test_delete_removes_files_and_row(tmp_path, monkeypatch):
    packstore, s = _store(tmp_path, monkeypatch)
    info = packstore.put(s, _pack_zip(), name="待删")
    pdir = packstore.pack_dir(s, info["id"])
    assert pdir.is_dir()
    packstore.delete(s, info["id"])
    assert not pdir.exists(), "文件目录没删掉"
    assert packstore.listing(s) == []


def test_empty_payload_rejected(tmp_path, monkeypatch):
    packstore, s = _store(tmp_path, monkeypatch)
    try:
        packstore.put(s, b"", name="空")
    except packstore.PackStoreError as exc:
        assert exc.code == "PACK_EMPTY"
    else:
        raise AssertionError("空包应当被拒")

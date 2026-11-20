"""把「浏览器的 User Data 目录」当 path 传进来时，必须自动带数据。

## 为什么（2026-11-20 我自己踩的）

界面上写的就是「填那个浏览器的用户数据目录」，所以用户（我）自然会传
`.../User Data` 或 `.../User Data/Default`。

以前的 `src_path` 分支会把它当**扩展目录**处理：在
`User Data/Default/Extensions` 里确实能翻到扩展、**导入成功**，
但 `Local Extension Settings` 里的数据**全丢了** —— 返回 `has_data=False`。

最坏的地方是它**不报错**：用户以为"插件和数据都带上了"，
新建环境也真带上了插件，只是登录态/配置全没跟着走。

这条测试盯住：两种传法都必须带上 `Local Extension Settings` 的数据。
"""

from __future__ import annotations

import json


def _make_userdata(root):
    """造一个与真实 Chrome/RXYCDP 同构的 User Data 目录。"""
    dflt = root / "User Data" / "Default"
    for eid, name, ver in (("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "脚本猫UI", "1.2.3"),
                           ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "CookieTool", "2.0")):
        d = dflt / "Extensions" / eid / ver
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(
            json.dumps({"manifest_version": 3, "name": name, "version": ver}))
        (d / "sw.js").write_text("void 0;")
        les = dflt / "Local Extension Settings" / eid
        les.mkdir(parents=True)
        (les / "000003.log").write_bytes(b"DATA")
    return dflt


def test_userdata_root_carries_extension_data(tmp_path, monkeypatch):
    from zw_core import config
    from zw_core.services import packs

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    try:
        _make_userdata(tmp_path)
        out = packs.import_pack(str(tmp_path / "User Data"), name="误用-根")
        assert out["extension_count"] == 2, out
        assert all(e["has_data"] for e in out["extensions"]), \
            f"数据没带上：{[(e['name'], e['has_data']) for e in out['extensions']]}"
    finally:
        config.reset_settings()


def test_default_layer_carries_extension_data(tmp_path, monkeypatch):
    from zw_core import config
    from zw_core.services import packs

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    try:
        _make_userdata(tmp_path)
        out = packs.import_pack(str(tmp_path / "User Data" / "Default"), name="误用-Default")
        assert out["extension_count"] == 2, out
        assert all(e["has_data"] for e in out["extensions"]), \
            f"数据没带上：{[(e['name'], e['has_data']) for e in out['extensions']]}"
    finally:
        config.reset_settings()


def test_plain_extension_dir_still_works(tmp_path, monkeypatch):
    """普通扩展目录（不是浏览器目录）不能被误判成 chrome 导入。"""
    from zw_core import config
    from zw_core.services import packs

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    try:
        ext = tmp_path / "single-ext"
        ext.mkdir()
        (ext / "manifest.json").write_text(json.dumps(
            {"manifest_version": 3, "name": "单独扩展", "version": "1.0"}))
        out = packs.import_pack(str(ext), name="普通目录")
        assert out["extension_count"] == 1, out
        assert out["extensions"][0]["name"] == "单独扩展", out["extensions"][0]
    finally:
        config.reset_settings()


def test_zip_import_carries_data_directory(tmp_path, monkeypatch):
    """从 zip 导入时，包内 `data/<key>/` 必须跟着带上。

    ## 为什么（2026-11-20，自己踩的）

    本项目的包格式本来就是：

    ```
    code/<key>/manifest.json   ← 扩展代码
    data/<key>/…               ← 它在 Local Extension Settings 里的数据
    ```

    但 zip 导入分支以前**只找 `code/` 下的扩展**，压根没看 `data/`，
    注释里还写着"从目录/zip 导入……没有历史数据可带"—— 这个假设是错的。

    后果很具体：从控制台拉下来的包，扩展在、登录态没了，而 hub 那边明明
    记录着 `has_data=True`。用户要的是"插件**及其数据**"。

    这条测试盯住：`data/` 必须被搬进新包，并反映在 `has_data` 上。
    """
    import io
    import zipfile

    from zw_core import config
    from zw_core.services import packs

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("code/extkey/manifest.json",
                       json.dumps({"name": "带数据的扩展", "version": "1.0"}))
            z.writestr("code/extkey/sw.js", "void 0;")
            z.writestr("data/extkey/000003.log", b"DATA-LOG")
        zp = tmp_path / "pack.zip"
        zp.write_bytes(buf.getvalue())

        info = packs.import_pack(str(zp), name="带数据")
        assert info["extension_count"] == 1
        ext = info["extensions"][0]
        assert ext["has_data"] is True, f"数据没带上：{ext}"
        assert ext.get("data_dir"), "data_dir 为空"

        # 数据文件真的落盘了
        pdir = packs.packs_root() / info["id"]
        got = pdir / str(ext["data_dir"]) / "000003.log"
        assert got.is_file(), f"数据文件不存在：{got}"
        assert got.read_bytes() == b"DATA-LOG"
    finally:
        config.reset_settings()


def test_zip_without_data_still_works(tmp_path, monkeypatch):
    """纯代码包（没有 data/）照样能导入，不能因为找不到数据就失败。"""
    import io
    import zipfile

    from zw_core import config
    from zw_core.services import packs

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("code/onlycode/manifest.json",
                       json.dumps({"name": "纯代码", "version": "1.0"}))
        zp = tmp_path / "pack.zip"
        zp.write_bytes(buf.getvalue())
        info = packs.import_pack(str(zp), name="纯代码")
        assert info["extension_count"] == 1
        assert info["extensions"][0]["has_data"] is False
    finally:
        config.reset_settings()


def test_chrome_dir_as_zip_keeps_data(tmp_path, monkeypatch):
    """把浏览器目录**打成 zip** 导入，数据不能丢。

    ## 为什么（2026-11-20 对抗审查实测）

    用户很自然会这样做：把 `Extensions/` + `Local Extension Settings/`
    一起压缩上传。而 zip 分支只认 `data/<key>`，于是：

        extension_count=1, data_dir=''   ← 数据没了
        而且**没有任何 warnings**

    同一个目录直接给路径是好的（`with_data=1`），打成 zip 就丢 ——
    这种"换个方式就静默失效"最难发现。
    """
    import io
    import zipfile

    from zw_core import config
    from zw_core.services import packs

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Extensions/aaa/manifest.json",
                       json.dumps({"name": "扩展A", "version": "1"}))
            z.writestr("Extensions/aaa/sw.js", "void 0;")
            z.writestr("Local Extension Settings/aaa/000003.log", b"STATE")
        zp = tmp_path / "chrome.zip"
        zp.write_bytes(buf.getvalue())

        info = packs.import_pack(str(zp), name="Chrome目录zip")
        assert info["extension_count"] == 1, info
        assert info["with_data"] == 1, f"数据丢了：{info}"
        # 数据文件真的落盘
        pdir = packs.packs_root() / info["id"]
        files = [p.name for p in (pdir / "data").rglob("*") if p.is_file()]
        assert "000003.log" in files, files
    finally:
        config.reset_settings()


def test_ext_id_only_recorded_when_it_looks_like_a_real_id(tmp_path, monkeypatch):
    """只有**真 ID 形状**才记进 `ext_id`。

    ## 为什么（2026-11-20 审查 + 我自己的两次写错）

    Chromium 的扩展 ID 是 **a-p 这 16 个字母**组成的 32 字符。目录名符合才算真 ID；
    未打包加载（开发者模式）时目录名可能是任意路径哈希。

    这个字段用于"按源环境实测 ID 另放一份数据"那条双写分支 —— 记错了，
    数据会落到一个 Chromium 永远不会读的位置，等于白搬。

    我的第一版判据是"在不在 `observed_extension_ids()` 里"，但那个集合
    同时包含 `Local Extension Settings/` 的目录名（就是同一个字符串），
    所以判断恒为真、毫无区分力。第二版才对。
    """
    from zw_core import config
    from zw_core.services import packs

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    try:
        # ① 非 a-p 形状 → 不记
        d1 = tmp_path / "c1" / "Default"
        (d1 / "Extensions" / "abcdefghijklmnop").mkdir(parents=True)
        (d1 / "Extensions" / "abcdefghijklmnop" / "manifest.json").write_text(
            json.dumps({"name": "未打包", "version": "1"}))
        les1 = d1 / "Local Extension Settings" / "abcdefghijklmnop"
        les1.mkdir(parents=True)
        (les1 / "000003.log").write_bytes(b"S")
        i1 = packs.import_pack(str(d1), name="非真 ID")
        e1 = i1["extensions"][0]
        assert e1["ext_id"] == "", f"不该记非真 ID：{e1['ext_id']!r}"
        assert e1["has_data"] is True, "数据仍要带上"

        # ② 真 ID 形状 → 记
        real = "a" * 32
        d2 = tmp_path / "c2" / "Default"
        (d2 / "Extensions" / real).mkdir(parents=True)
        (d2 / "Extensions" / real / "manifest.json").write_text(
            json.dumps({"name": "真 ID", "version": "1"}))
        les2 = d2 / "Local Extension Settings" / real
        les2.mkdir(parents=True)
        (les2 / "000003.log").write_bytes(b"S")
        i2 = packs.import_pack(str(d2), name="真 ID")
        assert i2["extensions"][0]["ext_id"] == real
    finally:
        config.reset_settings()

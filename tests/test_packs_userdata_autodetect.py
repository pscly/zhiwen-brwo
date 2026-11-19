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

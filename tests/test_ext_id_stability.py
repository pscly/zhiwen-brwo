"""扩展 ID 稳定性：库目录不带版本号，以及"升级后存储迁移"。

## 为什么写（2026-11-25 用户实测踩到）

未打包扩展的 ID 由**加载路径**推导（见 packs.chromium_unpacked_id）。库目录
以前是 `<slug>-<version>`，于是扩展一升级 → 目录变 → ID 变 → 浏览器去一个
空的 `Local Extension Settings/<新ID>/` 找数据。用户看到的是
「Bitwarden 突然要重新登录」「脚本猫的脚本没了」，而数据其实还在旧 ID 目录里。

这一组用例锁两件事：
1. 同一个 slug 装两次（模拟版本升级），库路径不变 → ID 不变
2. 历史遗留的「旧 ID 目录」能被 repair 搬到当前 ID，且旧目录被保留（不是删掉）
"""

from __future__ import annotations

import json

import pytest


def _manifest() -> str:
    return json.dumps({"name": "测试扩展", "version": "1.0", "manifest_version": 3})


@pytest.fixture()
def packs_env(tmp_path, monkeypatch):
    from zw_core import config, db

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_PACKS_DIR", str(tmp_path / "root" / "packs"))
    config.reset_settings()
    db.reset_db()
    yield tmp_path / "root"
    config.reset_settings()
    db.reset_db()


def _make_src(root, slug: str, version: str):
    """准备一个扩展源码目录（不导入包——这些用例只测库路径与存储迁移）。"""
    src = root / "_src" / slug / version / "code"
    src.mkdir(parents=True)
    (src / "manifest.json").write_text(_manifest(), encoding="utf-8")
    return src


def test_library_path_has_no_version_and_id_is_stable(packs_env):
    from zw_core.services import packs

    src1 = _make_src(packs_env, "stable-ext", "1.0")
    lib1 = packs._ensure_in_library(
        src1,
        {"slug": "stable-ext", "version": "1.0", "name": "测试扩展"},
    )
    id1 = packs.chromium_unpacked_id(lib1)

    # 同一个 slug 来个"新版本"
    src2 = _make_src(packs_env, "stable-ext", "2.0")
    lib2 = packs._ensure_in_library(
        src2,
        {"slug": "stable-ext", "version": "2.0", "name": "测试扩展"},
    )
    assert lib1 == lib2, "库路径不能带版本号，否则扩展 ID 会跟着版本漂移"
    assert packs.chromium_unpacked_id(lib2) == id1
    assert "2.0" not in lib2.name


def test_repair_moves_old_storage_and_keeps_backup(packs_env, tmp_path, monkeypatch):
    from zw_core.services import manager, packs

    src = _make_src(packs_env, "repair-ext", "1.0")
    lib = packs._ensure_in_library(
        src,
        {"slug": "repair-ext", "version": "1.0", "name": "测试扩展"},
    )
    want = packs.chromium_unpacked_id(lib)

    prof = manager.create_profile({"name": "repair-target", "pack_ids": []})
    manager._update(prof["id"], extensions=["repair-ext"])
    udd = manager.udd_path(prof["id"])
    les = udd / packs.LES_REL

    # 造一个"旧版本目录推导出来的 ID"，并把数据放在那儿（模拟历史遗留）
    legacy_dir = lib.parent / "repair-ext-0.9"
    legacy_dir.mkdir()
    (legacy_dir / "manifest.json").write_text(_manifest(), encoding="utf-8")
    legacy_id = packs.chromium_unpacked_id(legacy_dir)
    assert legacy_id != want
    (les / legacy_id).mkdir(parents=True)
    (les / legacy_id / "000001.log").write_bytes(b"legacy-settings")

    report = packs.repair_extension_storage(prof["id"])
    assert report["migrated"] == 1, report
    assert (les / want / "000001.log").read_bytes() == b"legacy-settings"
    assert (les / (legacy_id + ".migrated")).is_dir(), "旧目录必须保留，便于回滚"


def test_repair_does_not_overwrite_existing_newer_data(packs_env):
    from zw_core.services import manager, packs

    src = _make_src(packs_env, "keep-ext", "1.0")
    lib = packs._ensure_in_library(
        src,
        {"slug": "keep-ext", "version": "1.0", "name": "测试扩展"},
    )
    want = packs.chromium_unpacked_id(lib)
    prof = manager.create_profile({"name": "keep-target", "pack_ids": []})
    manager._update(prof["id"], extensions=["keep-ext"])
    les = manager.udd_path(prof["id"]) / packs.LES_REL

    legacy_dir = lib.parent / "keep-ext-0.9"
    legacy_dir.mkdir()
    (legacy_dir / "manifest.json").write_text(_manifest(), encoding="utf-8")
    legacy_id = packs.chromium_unpacked_id(legacy_dir)

    (les / legacy_id).mkdir(parents=True)
    (les / legacy_id / "old.log").write_bytes(b"old")
    (les / want).mkdir(parents=True)
    (les / want / "new.log").write_bytes(b"new")

    packs.repair_extension_storage(prof["id"])
    assert (les / want / "new.log").read_bytes() == b"new", "新数据不能被旧的盖掉"
    assert not (les / want / "old.log").exists()


def test_path_bytes_follows_platform_encoding():
    """Windows 用 UTF-16LE，其它平台用 UTF-8 —— 算错就是"扩展设置了但读不到"。"""
    from pathlib import Path

    from zw_core.services import packs

    import os as _os

    p = Path("/tmp/ext/x")
    # 当前平台自己的编码必须对（Windows runner 上 os.name 就是 nt）
    expect = str(p).encode("utf-16-le") if _os.name == "nt" else str(p).encode("utf-8")
    assert packs.path_bytes(p) == expect

    # 另一条分支也要覆盖：显式切 os.name 再算一次
    real = _os.name
    try:
        _os.name = "utf8-branch" if real == "nt" else "nt"
        expect2 = str(p).encode("utf-16-le") if _os.name == "nt" else str(p).encode("utf-8")
        assert packs.path_bytes(p) == expect2
    finally:
        _os.name = real


def test_known_windows_id_matches_chromium():
    """真机对照：这条路径在云电脑上 Chromium 用的 ID 是 ccgniplcdaeapohoocjeongbfnnihnjp。"""
    import os as _os

    from pathlib import Path

    from zw_core.services import packs

    # 直接对"Windows 路径字符串"的字节序列做哈希（本机是 Linux，造不出 WindowsPath）
    raw = r"C:\Users\Administrator\AppData\Local\zhiwen\extensions\bitwarden-password-manager-2026.10.2"
    import hashlib

    digest = hashlib.sha256(raw.encode("utf-16-le")).hexdigest()[:32]
    got = "".join(chr(ord("a") + int(c, 16)) for c in digest)
    assert got == "ccgniplcdaeapohoocjeongbfnnihnjp", "UTF-16LE 才算得出 Chromium 真机用的 ID"

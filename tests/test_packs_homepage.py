"""扩展包（packs）与启动检测页（launcher）。

## 这组测试锁死的语义

1. 包可以来自三种来源：扩展目录、zip、**从已有环境导出**（后者的重点是"代码 +
   `Local Extension Settings` 都要带上"——用户说的"插件及其数据"）。
2. 标记为默认的包，在 `create_profile` 之后自动出现在新环境的 `extensions` 里，
   而且代码复制进扩展库、数据写进该环境的 udd。
3. `ZW_DEFAULT_PACKS=false` 时一个包都不带。
4. `compose_start_urls` 的顺序是「恢复的在前、检测页在后」，并整体去重。
5. `ZW_DETECT_URLS` 能覆盖内置默认值。

这些用例**不启动真浏览器**：包与标签页逻辑都是纯文件/纯数据操作，起浏览器只会
让测试变慢变脆。唯一需要"真浏览器"的地方（CDP 抓标签页)已经在
`remember_open_tabs` 里退化成"读不到就保留旧记录"，因此不需要在这里验证。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from zw_core import config
from zw_core.services import launcher, manager, packs


# ── 隔离的数据目录 ────────────────────────────────────────
@pytest.fixture(scope="module", autouse=True)
def _isolated_root():
    """整个模块共用一个临时数据目录。

    真的要设 `ZW_ROOT`：`create_profile` 会落库、往 udd 写文件，不能写到开发机
    真实的 `~/.local/share/zhiwen` 里去。模块结束后必须**清理掉这个环境变量**，
    否则后面跑的测试模块会跟着用这个（然后目录已经被删了）。
    """
    tmp = tempfile.mkdtemp(prefix="zw-packs-")
    old_root = os.environ.get("ZW_ROOT")
    old_hub = os.environ.get("ZW_HUB_URL")
    os.environ["ZW_ROOT"] = tmp
    os.environ["ZW_HUB_URL"] = ""
    config.reset_settings()
    from zw_core import db

    db.reset_db()
    try:
        yield Path(tmp)
    finally:
        if old_root is None:
            os.environ.pop("ZW_ROOT", None)
        else:
            os.environ["ZW_ROOT"] = old_root
        if old_hub is None:
            os.environ.pop("ZW_HUB_URL", None)
        else:
            os.environ["ZW_HUB_URL"] = old_hub
        config.reset_settings()
        db.reset_db()
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def _env_zw(**kw: object):
    """临时改 ZW_* 环境变量并重建 Settings 单例，退出时严格还原。

    不用 monkeypatch：它的撤销发生在 fixture teardown 之后，而 settings 是单例，
    很容易留下"用旧环境变量构造、但已经不再匹配环境"的缓存对象（串味）。
    """
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    config.reset_settings()
    try:
        yield config.get_settings()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config.reset_settings()


def _make_ext(root: Path, name: str = "probe", version: str = "1.0") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": name, "version": version}, ensure_ascii=False),
        encoding="utf-8",
    )
    (d / "sw.js").write_text("chrome.storage.local.set({ok:1});", encoding="utf-8")
    return d


def _unique(prefix: str) -> str:
    """环境名要唯一（profiles.name UNIQUE），测试之间不能撞。"""
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:6]}"


# ── 导入：目录 / zip ──────────────────────────────────────
def test_import_pack_from_directory(tmp_path):
    src = _make_ext(tmp_path / "src", "dir-ext")
    info = packs.import_pack(str(src), name="目录来源的包")

    assert info["id"] and info["name"] == "目录来源的包"
    assert info["source"] == "dir"
    assert info["extension_count"] == 1
    pdir = Path(info["path"])
    assert (pdir / "pack.json").is_file()
    # 代码在 extensions/<key>/ 下，且这一层就有 manifest.json（"能直接复制进 udd"的形状）
    code = pdir / "extensions" / "dir-ext"
    assert (code / "manifest.json").is_file()
    assert (code / "sw.js").is_file()
    # 目录来源没有历史数据可带
    assert info["with_data"] == 0


def test_import_pack_from_zip_with_nested_dir(tmp_path):
    """zip 里多套了一层目录是最常见的形状（下载下来的包都这样）。"""
    ext = _make_ext(tmp_path / "zipsrc", "zip-ext")
    zpath = tmp_path / "pkg.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for f in ext.rglob("*"):
            if f.is_file():
                zf.write(f, f"nested/{f.relative_to(ext)}")

    info = packs.import_pack(str(zpath))
    assert info["source"] == "zip"
    assert info["extension_count"] == 1
    code = Path(info["path"]) / "extensions" / "zip-ext"
    assert (code / "manifest.json").is_file(), "多套一层目录时没自动纠正"


def test_import_pack_rejects_unsafe_archive(tmp_path):
    """zip 里的 `..` 必须整体拒绝，否则能往数据目录外面写文件。"""
    zpath = tmp_path / "evil.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../../pwned.txt", "x")
        zf.writestr("manifest.json", "{}")
    with pytest.raises(packs.PackError) as ei:
        packs.import_pack(str(zpath))
    assert ei.value.code == "PACK_UNSAFE_ARCHIVE"


# ── 导出：从已有环境的 udd ────────────────────────────────
def _fake_installed_ext(udd: Path, ext_id: str, name: str = "已装扩展") -> None:
    """在 udd 里造一个"装过的扩展 + 它的存储数据"。

    形状照 Chromium 的真实布局来：`Default/Extensions/<id>/<ver>/manifest.json`
    与 `Default/Local Extension Settings/<id>/<leveldb 文件>`。
    """
    code = udd / "Default" / "Extensions" / ext_id / "1.0"
    code.mkdir(parents=True, exist_ok=True)
    (code / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": name, "version": "1.0"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (code / "content.js").write_text("console.log('hi');", encoding="utf-8")
    data = udd / "Default" / "Local Extension Settings" / ext_id
    data.mkdir(parents=True, exist_ok=True)
    (data / "000003.log").write_bytes(b"\x00\x01stored-api-key")
    (data / "CURRENT").write_text("MANIFEST-000001\n", encoding="utf-8")


def test_export_pack_from_profile_carries_code_and_data():
    p = manager.create_profile({"name": _unique("带插件的环境")})
    udd = manager.udd_path(p["id"])
    ext_id = "abcdefghijklmnopabcdefghijklmnop"
    _fake_installed_ext(udd, ext_id)

    info = packs.import_pack(from_profile=p["name"])
    pdir = Path(info["path"])

    # 1) 代码在
    assert (pdir / "extensions" / ext_id / "manifest.json").is_file()
    assert (pdir / "extensions" / ext_id / "content.js").is_file()
    # 2) 数据在（这是"插件及其数据"里最容易被漏掉的一半）
    assert (pdir / "data" / ext_id / "000003.log").read_bytes() == b"\x00\x01stored-api-key"
    assert (pdir / "data" / ext_id / "CURRENT").is_file()
    # 3) 元数据把"源环境实测到的扩展 ID"记下来了（应用时用它做数据双写）
    ext_meta = info["extensions"][0]
    assert ext_meta["ext_id"] == ext_id
    assert ext_meta["has_data"] is True
    assert info["with_data"] == 1
    assert info["source"] == "profile"
    assert info["source_profile"] == p["name"]


def test_export_pack_from_profile_without_extensions_raises():
    p = manager.create_profile({"name": _unique("空环境")})
    with pytest.raises(packs.PackError) as ei:
        packs.import_pack(from_profile=p["name"])
    assert ei.value.code == "PACK_NO_EXTENSION"


# ── 默认包：新建环境自动带上 ──────────────────────────────
def test_default_pack_is_applied_on_create_profile():
    p = manager.create_profile({"name": _unique("导出源")})
    udd = manager.udd_path(p["id"])
    ext_id = "ponmlkjihgfedcbaponmlkjihgfedcba"
    _fake_installed_ext(udd, ext_id, "默认包里的扩展")
    pack = packs.import_pack(from_profile=p["name"], name="默认包")
    packs.set_default(pack["id"], True)

    assert pack["id"] in packs.default_pack_ids()

    fresh = manager.create_profile({"name": _unique("新环境")})
    slugs = [str(x.get("slug") if isinstance(x, dict) else x) for x in fresh["extensions"]]
    slug = pack["extensions"][0]["slug"]
    assert slug in slugs, "默认包没被应用到新环境"

    # 代码进了扩展库（这样 --load-extension 才有路径可用）
    code_in_lib = config.get_settings().ext_dir / f"{slug}-1.0"
    assert (code_in_lib / "manifest.json").is_file()

    # 数据进了新环境的 udd：算出来的 ID + 源环境实测 ID，两个位置各一份
    les = manager.udd_path(fresh["id"]) / "Default" / "Local Extension Settings"
    computed = packs.chromium_unpacked_id(code_in_lib)
    assert (les / computed / "000003.log").is_file()
    assert (les / ext_id / "000003.log").is_file(), "源环境实测 ID 的那份没写"

    # 清掉默认标记，别影响后面的用例
    packs.set_default(pack["id"], False)


def test_default_packs_switch_off():
    p = manager.create_profile({"name": _unique("导出源2")})
    _fake_installed_ext(manager.udd_path(p["id"]), "zyxwvutsrqponmlkzyxwvutsrqponmlk", "开关测试扩展")
    pack = packs.import_pack(from_profile=p["name"], name="开关包")
    packs.set_default(pack["id"], True)
    try:
        with _env_zw(ZW_DEFAULT_PACKS="false"):
            fresh = manager.create_profile({"name": _unique("不带包")})
            assert fresh["extensions"] == [], "ZW_DEFAULT_PACKS=false 时不该带任何包"
    finally:
        packs.set_default(pack["id"], False)


def test_apply_to_profile_explicit_packs_and_delete():
    src = _make_ext(Path(tempfile.mkdtemp(prefix="zw-apply-")), "apply-ext")
    pack = packs.import_pack(str(src), name="手动应用包")
    target = manager.create_profile({"name": _unique("手动应用目标")})

    res = packs.apply_to_profile(target["name"], [pack["id"]])
    assert res["applied"], "手工应用没有生效"
    assert res["needs_restart"] == []  # 环境没在跑，不需要重启
    slugs = [str(x.get("slug") if isinstance(x, dict) else x) for x in res["profile"]["extensions"]]
    assert "apply-ext" in slugs

    # 删包不影响已经应用过的环境：代码已经复制进扩展库，包只是"出厂模板"
    lib = config.get_settings().ext_dir / "apply-ext-1.0"
    packs.delete_pack(pack["id"])
    assert not (Path(pack["path"])).exists()
    assert (lib / "manifest.json").is_file()
    with pytest.raises(packs.PackError) as ei:
        packs.get_pack(pack["id"])
    assert ei.value.code == "PACK_NOT_FOUND"


def test_list_packs_reports_size_and_source():
    rows = packs.list_packs()
    assert rows, "库里应该有前面用例建的包"
    for r in rows:
        assert {"id", "name", "size_bytes", "size_human", "is_default", "source"} <= set(r)
        assert r["size_bytes"] >= 0


# ── 启动页 ────────────────────────────────────────────────
def test_compose_start_urls_restores_then_detects():
    profile = {"start_urls": ["https://example.com/a", "https://example.com/b"]}
    urls = launcher.compose_start_urls(profile)
    assert urls[:2] == ["https://example.com/a", "https://example.com/b"], "恢复的标签必须在最前面"
    assert urls[2:] == list(config.DEFAULT_DETECT_URLS), "检测页必须排在恢复的标签之后"


def test_compose_start_urls_dedupes_and_keeps_first_position():
    first_detect = config.DEFAULT_DETECT_URLS[0]
    # 用户上次就把检测页开着（且在最前），重启后它应保持"第一次出现"的位置
    profile = {"start_urls": [first_detect, "https://example.com/x"]}
    urls = launcher.compose_start_urls(profile)
    assert urls.count(first_detect) == 1
    assert urls[0] == first_detect
    assert urls[1] == "https://example.com/x"
    # 尾斜杠差异不算两个地址
    profile2 = {"start_urls": ["https://ip123.in", "https://ip123.in/"]}
    got = launcher.compose_start_urls(profile2)
    assert sum(1 for u in got if u.rstrip("/") == "https://ip123.in") == 1


def test_remembered_urls_drops_non_web_schemes():
    profile = {
        "start_urls": [
            "https://a.example/1",
            "chrome://settings",
            "about:blank",
            "devtools://devtools/x",
            "chrome-extension://abc/popup.html",
            "http://b.example/2",
        ]
    }
    assert launcher.remembered_urls(profile) == ["https://a.example/1", "http://b.example/2"]


def test_detect_urls_env_override():
    with _env_zw(ZW_DETECT_URLS="https://one.test/d,https://two.test/d"):
        assert launcher.detect_urls({}) == ["https://one.test/d", "https://two.test/d"]
        # 恢复的标签仍然在前，检测页用覆盖后的
        got = launcher.compose_start_urls({"start_urls": ["https://kept.example/"]})
        assert got == ["https://kept.example/", "https://one.test/d", "https://two.test/d"]


def test_detect_switch_off_leaves_only_restored():
    with _env_zw(ZW_OPEN_DETECT_ON_START="false"):
        got = launcher.compose_start_urls({"start_urls": ["https://kept.example/"]})
        assert got == ["https://kept.example/"]


def test_restore_switch_off_leaves_only_detect():
    with _env_zw(ZW_RESTORE_TABS="false"):
        got = launcher.compose_start_urls({"start_urls": ["https://ignored.example/"]})
        assert got == list(config.DEFAULT_DETECT_URLS)


def test_new_profile_gets_detect_urls_seeded():
    """新建环境时检测页要落进 start_urls —— 否则第一次启动就是 about:blank。"""
    p = manager.create_profile({"name": _unique("检测页")})
    assert p["start_urls"] == list(config.DEFAULT_DETECT_URLS)


def test_remember_open_tabs_without_cdp_reports_reason():
    """没在跑的环境不该抛异常，返回原因即可（停止流程要能走下去）。"""
    res = launcher.remember_open_tabs({"id": "x" * 20, "cdp_enabled": False, "cdp_port": 0})
    assert res["saved"] == 0 and "调试端口" in res["reason"]


def test_chromium_unpacked_id_is_stable_and_path_dependent(tmp_path):
    a = packs.chromium_unpacked_id(tmp_path / "one")
    b = packs.chromium_unpacked_id(tmp_path / "two")
    assert a != b, "不同加载路径必须得到不同 ID（否则数据目录会串）"
    assert packs.chromium_unpacked_id(tmp_path / "one") == a
    assert len(a) == 32 and set(a) <= set("abcdefghijklmnop"), a

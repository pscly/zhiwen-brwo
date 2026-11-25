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
    code_in_lib = config.get_settings().ext_dir / slug
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

    # 删包不影响已经应用过的环境：代码已经复制进扩展库，包只是"出厂模板"。
    #
    # 注意 `force=True`：2026-11-20 起 `delete_pack` 会在**有环境在用**时抛
    # `PACK_IN_USE`（免得用户不知道删了以后新建环境带不上插件）。
    # 这条用例本来就是要验"删完之后已应用的环境不受影响"，所以它必须强删 ——
    # 不加 force 会先撞上那条保护，测的就不是这里想测的东西了。
    lib = config.get_settings().ext_dir / "apply-ext"
    packs.delete_pack(pack["id"], force=True)
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


# ── 从任意 Chrome 用户目录导入（RXYCDP / Roxy 那种）─────────────────────
def _fake_chrome(tmp_path, ext_id="abcdefghijklmnopabcdefghijklmnop", with_data=True):
    """造一个标准 Chrome 用户目录：User Data/Default/Extensions/<id>/<ver>/ + 数据。"""
    udd = tmp_path / "User Data" / "Default"
    code = udd / "Extensions" / ext_id / "1.2.3"
    code.mkdir(parents=True)
    (code / "manifest.json").write_text(
        '{"manifest_version":3,"name":"RXYCDP helper","version":"1.2.3"}', encoding="utf-8")
    (code / "sw.js").write_text("// probe", encoding="utf-8")
    if with_data:
        les = udd / "Local Extension Settings" / ext_id
        les.mkdir(parents=True)
        (les / "000003.log").write_bytes(b"data-here")
    return tmp_path / "User Data"


def test_import_from_chrome_user_data(tmp_path):
    """用户说的"把 RXYCDP 浏览器的插件及其数据一起打包"：那浏览器不是知纹起的，
    扩展装在标准 Chrome 用户目录里，所以要支持从那个目录直接导入。"""
    from zw_core.services import packs

    userdata = _fake_chrome(tmp_path)
    r = packs.import_pack(from_chrome=str(userdata), name="RXYCDP 全套")
    assert r["name"] == "RXYCDP 全套"
    assert r["extension_count"] == 1
    assert r["with_data"] == 1, "扩展的本地数据（Local Extension Settings）也要带上"
    assert r["source"] == "chrome"
    # 包目录里代码与数据都在
    import pathlib

    pdir = pathlib.Path(r["path"])
    assert (pdir / "extensions" / "abcdefghijklmnopabcdefghijklmnop" / "manifest.json").exists()
    assert (pdir / "data" / "abcdefghijklmnopabcdefghijklmnop" / "000003.log").exists()


def test_import_from_chrome_accepts_default_layer(tmp_path):
    """给 `User Data` 或 `User Data/Default` 都要能导入（用户分不清该给哪一层）。"""
    from zw_core.services import packs

    userdata = _fake_chrome(tmp_path)
    a = packs.import_pack(from_chrome=str(userdata))
    b = packs.import_pack(from_chrome=str(userdata / "Default"))
    assert a["extension_count"] == 1 and b["extension_count"] == 1


def test_import_from_chrome_without_extensions_is_clear(tmp_path):
    """目录里没有扩展时要给出可操作的错误，而不是空包。"""
    import pytest as _pytest

    from zw_core.services import packs

    empty = tmp_path / "User Data" / "Default"
    empty.mkdir(parents=True)
    with _pytest.raises(packs.PackError) as ei:
        packs.import_pack(from_chrome=str(empty))
    assert ei.value.code == "PACK_NO_EXTENSION"
    assert "装好" in ei.value.message or "Extensions" in ei.value.message


def test_import_from_chrome_bad_path(tmp_path):
    import pytest as _pytest

    from zw_core.services import packs

    with _pytest.raises(packs.PackError) as ei:
        packs.import_pack(from_chrome=str(tmp_path / "not-there"))
    assert ei.value.code == "PACK_INVALID_SOURCE"


def test_import_from_chrome_skips_helper_dirs(tmp_path):
    """Chrome 的 `_metadata` 之类辅助目录不能被当成扩展。"""
    from zw_core.services import packs

    userdata = _fake_chrome(tmp_path)
    helper = userdata / "Default" / "Extensions" / "_metadata"
    helper.mkdir(parents=True)
    (helper / "manifest.json").write_text('{"name":"not-an-extension"}', encoding="utf-8")
    r = packs.import_pack(from_chrome=str(userdata))
    assert r["extension_count"] == 1, "只应该导入真正的扩展"


def test_from_chrome_handles_spaces_and_non_ascii_paths(tmp_path):
    """Chrome 的用户目录里一定有空格（`User Data`、`Local Extension Settings`），
    Windows 上还可能落在中文用户名下。pathlib 应该原样处理，不能靠 shell 引号。

    这条是为了防"在 Linux 上测得好好的，到用户机器上一遇到空格就崩"。
    """
    from zw_core.services import packs

    root = tmp_path / "Users" / "张三" / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    ext_id = "ponmlkjihgfedcbaponmlkjihgfedcba"
    code = root / "Default" / "Extensions" / ext_id / "2.0.0"
    code.mkdir(parents=True)
    (code / "manifest.json").write_text(
        '{"manifest_version":3,"name":"空格测试","version":"2.0.0"}', encoding="utf-8")
    les = root / "Default" / "Local Extension Settings" / ext_id
    les.mkdir(parents=True)
    (les / "000005.ldb").write_bytes(b"with space and CJK")

    r = packs.import_pack(from_chrome=str(root), name="带空格与中文")
    assert r["extension_count"] == 1
    assert r["with_data"] == 1
    import pathlib

    pdir = pathlib.Path(r["path"])
    assert (pdir / "extensions" / ext_id / "manifest.json").is_file()
    assert (pdir / "data" / ext_id / "000005.ldb").read_bytes() == b"with space and CJK"


def test_profile_name_with_spaces_survives_pack_source(tmp_path):
    """包名/来源里带空格与中文都要能落盘再读回（Windows 路径常见）。"""
    from zw_core.services import packs

    userdata = _fake_chrome(tmp_path)
    r = packs.import_pack(from_chrome=str(userdata), name="RXYCDP 的插件 数据")
    listed = packs.list_packs()
    got = next(x for x in listed if x["id"] == r["id"])
    assert got["name"] == "RXYCDP 的插件 数据"
    assert got["source"] == "chrome"


# ── 扩展"能不能真的被加载"必须说清楚 ─────────────────────────────
def test_extensions_loadable_uses_empirical_probe(tmp_path, monkeypatch):
    """判定必须靠**实测**，不能靠版本号。

    踩过的坑：我按"版本 ≥137 不支持"来判，结果
    google-chrome 149 确实不支持，但 **ungoogled-chromium 150 正常支持** ——
    版本都 ≥137，结论相反。而且不能用"有没有 chrome-extension:// 目标"判断：
    ungoogled 上不出现该目标，但内容脚本执行了、扩展确实生效。

    所以这里只钉住契约：把探针结果透出来；探针说不能就返回 False + 原因。
    """
    from zw_core.services import engines, extprobe

    monkeypatch.setattr(extprobe, "probe",
                        lambda *_a, **_k: {"loadable": True, "reason": "实测可加载", "cached": False, "ms": 1})
    assert engines.extensions_loadable(tmp_path / "chrome") == (True, "")

    monkeypatch.setattr(extprobe, "probe",
                        lambda *_a, **_k: {"loadable": False, "reason": "实测：忽略 --load-extension",
                                           "cached": False, "ms": 1})
    ok, why = engines.extensions_loadable(tmp_path / "chrome")
    assert ok is False and "忽略" in why


def test_extensions_loadable_probe_error_warns(tmp_path, monkeypatch):
    """探针自己出错时按"不能加载"处理，绝不静默放行。"""
    from zw_core.services import engines, extprobe

    def boom(*_a, **_k):
        raise OSError("probe failed")

    monkeypatch.setattr(extprobe, "probe", boom)
    ok, why = engines.extensions_loadable(tmp_path / "chrome")
    assert ok is False and "出错" in why


def test_extprobe_probe_missing_engine(tmp_path):
    """内核不存在时探针要立刻返回，不能卡住。"""
    from zw_core.services import extprobe

    r = extprobe.probe(tmp_path / "no-such-chrome", timeout=3)
    assert r["loadable"] is False
    assert "不存在" in r["reason"]


def test_extprobe_cache_key_tracks_binary(tmp_path):
    """缓存键含路径/大小/mtime —— 换了内核版本必须重新探测。"""
    from zw_core.services import extprobe

    f = tmp_path / "chrome"
    f.write_bytes(b"v1")
    k1 = extprobe._cache_key(f)
    f.write_bytes(b"v2-longer")
    k2 = extprobe._cache_key(f)
    assert k1 != k2


def test_apply_reports_extensions_will_not_load(tmp_path, monkeypatch):
    """应用扩展包时必须告诉调用方"插件不会出现在浏览器里"。

    代码和数据照样搬（换内核/换版本的机器上仍然有用），但沉默地搬完
    会让用户以为是自己的操作错了。
    """
    from zw_core import config, db

    import sys as _sys

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    # 用当前解释器当"存在的引擎路径"：Linux/macOS/Windows 都成立。
    # 第一版写死 /usr/bin/google-chrome，Windows CI 上 find_chrome 直接抛
    # EngineError，于是文案变成"判断出错"而不是真正的原因。
    monkeypatch.setenv("ZW_CHROME_BIN", _sys.executable)
    config.reset_settings()
    db.reset_db()
    try:
        from zw_core.services import engines, extprobe, manager as mgr, packs

        # 探针结论：这个内核忽略 --load-extension（正式版 Chrome 的真实行为）
        monkeypatch.setattr(extprobe, "probe", lambda *_a, **_k: {
            "loadable": False, "reason": "实测：这个内核忽略 --load-extension（内容脚本没有执行）",
            "cached": False, "ms": 1})

        src = tmp_path / "ext"
        src.mkdir()
        (src / "manifest.json").write_text(
            '{"manifest_version":3,"name":"Probe","version":"1.0"}', encoding="utf-8")
        pack = packs.import_pack(src_path=str(src), name="探针包")
        prof = mgr.create_profile({"name": "扩展验证"})
        out = packs.apply_to_profile(prof["id"], [pack["id"]])

        assert out["applied"], out
        assert out["extensions_active"] is False
        assert "忽略" in out["extensions_note"]
        # 数据/代码这一半必须照常完成
        assert out["applied"][0]["slug"] == "probe"
    finally:
        config.reset_settings()
        db.reset_db()


def test_apply_works_when_profile_has_no_engine_path(tmp_path, monkeypatch):
    """`profile.engine_path` 可能为空（Windows CI 上就是），这时要回落到
    `engines.find_chrome(settings)`。

    我第一版写成 `s.engine_path` —— `Settings` 上根本没这个字段（叫 `chrome_bin`），
    于是 Windows 上 profile.engine_path 为空时抛 AttributeError，
    被 except 吞成"按不加载处理"，警告文案里全是异常信息而不是真正的原因。
    """
    from zw_core import config, db

    import sys as _sys

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_CHROME_BIN", _sys.executable)
    config.reset_settings()
    db.reset_db()
    try:
        from zw_core.services import engines, extprobe, manager as mgr, packs

        monkeypatch.setattr(extprobe, "probe", lambda *_a, **_k: {
            "loadable": False, "reason": "实测：这个内核忽略 --load-extension（内容脚本没有执行）",
            "cached": False, "ms": 1})
        src = tmp_path / "ext2"
        src.mkdir()
        (src / "manifest.json").write_text(
            '{"manifest_version":3,"name":"Probe2","version":"1.0"}', encoding="utf-8")
        pack = packs.import_pack(src_path=str(src), name="探针包2")
        prof = mgr.create_profile({"name": "空引擎路径"})
        # 明确清掉 engine_path，模拟 Windows CI 的情形
        mgr._update(prof["id"], engine_path="")
        out = packs.apply_to_profile(prof["id"], [pack["id"]])
        assert out["applied"]
        assert out["extensions_active"] is False
        note = out["extensions_note"]
        assert "忽略" in note, note
        assert "出错" not in note, f"不该靠异常兜底：{note}"
    finally:
        config.reset_settings()
        db.reset_db()


# ── "默认带上插件数据 + 可以关掉" 这个开关（0.7.43）────────


def test_manifest_with_utf8_bom_is_recognized(tmp_path):
    """带 UTF-8 BOM 的 manifest.json 必须能读出来。

    真实场景：Windows 上用某些编辑器/构建工具做出来的 manifest 会带 BOM；
    PowerShell 的 `Set-Content -Encoding UTF8` 默认就写 BOM。
    用 `utf-8` 解码会多一个 `\\ufeff`，`json.loads` 直接抛 JSONDecodeError ——
    结果是这个扩展被**静默跳过**，用户只看到「里面没有可导入的扩展」，
    完全想不到是 BOM 的问题（实测在云电脑上就踩到了）。
    """
    import json as _json
    import pathlib

    from zw_core.services import packs as packsvc

    d = tmp_path / "ext_with_bom"
    d.mkdir()
    payload = {"manifest_version": 3, "name": "BomExt", "version": "1.0.0"}
    (d / "manifest.json").write_bytes(b"\xef\xbb\xbf" + _json.dumps(payload).encode())

    got = packsvc._read_manifest(d)
    assert got is not None, "带 BOM 的 manifest 被当成读不出来（已被静默跳过）"
    assert got.get("name") == "BomExt", got


def test_same_site_detect_page_does_not_duplicate_restored_url(monkeypatch):
    """同一站点的检测页不能和"上次记住的地址"重复。

    云电脑实测：配置里的检测页是 `https://ip123.in/`，它打开后会跳到
    `https://ip123.in/zh/`。停止时抓到 `/zh/`，下次启动又把配置里的 `/` 加回来 ——
    两个串不相等，`_dedupe` 不合并，用户看到**两条几乎一样的标签**。

    修法：按「协议+主机」归一并保留先出现的（恢复的优先于检测页）。
    """
    from zw_core import config
    from zw_core.services import launcher

    monkeypatch.setenv("ZW_RESTORE_TABS", "true")
    monkeypatch.setenv("ZW_OPEN_DETECT_ON_START", "true")
    monkeypatch.setenv("ZW_DETECT_URLS", "https://ip123.in/,https://browserleaks.com/ip")
    config.reset_settings()

    prof = {"start_urls": ["https://ip123.in/zh/"]}
    urls = launcher.compose_start_urls(prof)
    hosts = [u.split("/")[2] for u in urls]
    assert hosts.count("ip123.in") == 1, urls
    assert "https://ip123.in/zh/" in urls, urls      # 恢复的那条优先保留
    assert "https://browserleaks.com/ip" in urls, urls
    monkeypatch.delenv("ZW_RESTORE_TABS", raising=False)
    monkeypatch.delenv("ZW_OPEN_DETECT_ON_START", raising=False)
    monkeypatch.delenv("ZW_DETECT_URLS", raising=False)
    config.reset_settings()


def test_scan_browser_profiles_finds_importable_user_data(tmp_path, monkeypatch):
    """扫描本机要能找出"有扩展可导入"的浏览器配置目录。

    存在的理由：导入扩展包要用户填 `User Data` 绝对路径，而 RXYCDP / Roxy
    装在哪、Profile 是 `Default` 还是 `Profile 1`，用户根本不知道 ——
    卡在这一步就会放弃，功能等于没做。所以要能自动扫出来给人点。

    判据必须与导入一致：`Extensions/<id>/<ver>/manifest.json` 能读出来才算。
    空目录、只有内置组件的目录都不列（列出来只会让人更困惑）。
    """
    import json as _json
    from zw_core.services import packs as packsvc

    root = tmp_path / "fakehome"
    root.mkdir()
    # 一个"有扩展"的浏览器
    good = root / "RXYCDP" / "User Data" / "Default" / "Extensions" / ("a" * 32) / "1.0.0_0"
    good.mkdir(parents=True)
    (good / "manifest.json").write_text(_json.dumps({"name": "Ext1", "version": "1.0.0"}))
    # 一个"空"的浏览器（不该被列出）
    empty = root / "EmptyBrowser" / "User Data" / "Default" / "Extensions"
    empty.mkdir(parents=True)

    # 直接指定扫描根，不依赖这台机器的真实目录
    got = packsvc.scan_browser_profiles(max_depth=4, roots=[root])
    paths = [g["path"] for g in got]
    assert any("RXYCDP" in p and "Default" in p for p in paths), got
    assert not any("EmptyBrowser" in p for p in paths), got
    hit = [g for g in got if "RXYCDP" in g["path"]][0]
    assert hit["extension_count"] == 1, hit
    assert hit["browser"] == "RXYCDP", hit



def test_default_scan_roots_cover_windows_locations(monkeypatch, tmp_path):
    """Windows 上要扫的这些位置一个都不能少。

    上一轮的测试只走了 `roots=[...]` 这条显式路径，**没验平台分支**。而真实用户
    用的是 Windows：RXYCDP 可能装在 `%LOCALAPPDATA%\\RXYCDP`（按用户安装）、
    `%LOCALAPPDATA%\\Programs\\RXYCDP`、或者 `C:\\Program Files\\RXYCDP`
    （管理员安装）—— 少扫一个，用户就会"扫描找不到"。
    """
    from zw_core.services import packs as packsvc

    la = tmp_path / "Local"
    ra = tmp_path / "Roaming"
    pf = tmp_path / "PF"
    for d in (la, ra, pf):
        d.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(la))
    monkeypatch.setenv("APPDATA", str(ra))
    monkeypatch.setattr(packsvc.sys, "platform", "win32")
    # `Program Files` 在函数里是硬编码 `C:/` 下的路径；本机（Linux）上不存在，
    # 所以断言只要求"LOCALAPPDATA / APPDATA / Programs"三项，
    # Program Files 那两条在这里天然为空（真实 Windows 上目录存在才会加）。

    roots = [str(r) for r in packsvc._default_scan_roots()]
    assert str(la) in roots, roots
    assert str(ra) in roots, roots
    assert any(r.endswith("Programs") for r in roots), roots
    # 不断言 Program Files：本机没有 C:/Program Files，函数会按 is_dir() 跳过


def test_manifest_msg_placeholder_resolved_from_locales(tmp_path):
    """`__MSG_xxx__` 要解析成 `_locales` 里的真实名字。

    实测来源：云电脑上扫描 Edge 用户数据导入后，列表里显示 `__msg_extname__` ——
    Chromium 系扩展常把 name 写成占位符，真实文案在 `_locales/<lang>/messages.json`。
    不解析的话用户分不清那是哪个插件。

    解析顺序：浏览器界面语言优先（zh_CN / zh），再 en；都没有就保留原样。
    """
    import json as _json
    from zw_core.services import packs as packsvc

    root = tmp_path / "ext"
    (root / "_locales" / "zh_CN").mkdir(parents=True)
    (root / "_locales" / "en").mkdir(parents=True)
    (root / "_locales" / "zh_CN" / "messages.json").write_text(
        _json.dumps({"extName": {"message": "广告拦截助手"}}, ensure_ascii=False),
        encoding="utf-8")
    (root / "_locales" / "en" / "messages.json").write_text(
        _json.dumps({"extName": {"message": "Ad Blocker"}}), encoding="utf-8")

    got = packsvc._resolve_manifest_name(root, {"name": "__MSG_extName__"}, "k")
    assert got == "广告拦截助手", got          # 中文优先

    # 没有 locale 目录时保留原样，不能变成空字符串
    plain = tmp_path / "noloc"
    plain.mkdir()
    assert packsvc._resolve_manifest_name(plain, {"name": "__MSG_x__"}, "k") == "__MSG_x__"

    # 普通名字原样返回
    assert packsvc._resolve_manifest_name(root, {"name": "普通名字"}, "k") == "普通名字"


def test_pack_display_name_resolves_stale_msg_placeholder(tmp_path, monkeypatch):
    """包名存成 `__MSG_xxx__` 时，**读取时**要还原成真名。

    云电脑实测（2026-11-19）：早期版本建包时扩展名没做 `_locales` 解析，于是
    5 个默认包的显示名里有 4 个是占位符 —— 界面上直接显示：

        __MSG_extName__      __MSG_brandName__
        __MSG_scriptcat__    __MSG_name__

    用户看不懂。修法选"读取时解析"而不是写数据库迁移：存量包自动变好，
    也不用担心旧版本又写坏一次。
    """
    import json as _json

    from zw_core import config
    from zw_core.services import packs

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    try:
        ext = tmp_path / "src" / "MyExt"
        (ext / "_locales" / "zh_CN").mkdir(parents=True)
        (ext / "manifest.json").write_text(_json.dumps(
            {"manifest_version": 3, "name": "__MSG_extName__", "version": "1.0"},
            ensure_ascii=False), encoding="utf-8")
        (ext / "_locales" / "zh_CN" / "messages.json").write_text(_json.dumps(
            {"extName": {"message": "我的真名字"}}, ensure_ascii=False), encoding="utf-8")

        made = packs.import_pack(src_path=str(ext), name="__MSG_extName__")
        # 存储层保留原样（不改历史数据）
        assert packs._load_meta(made["id"])["name"] == "__MSG_extName__"

        listed = [p for p in packs.list_packs() if p["id"] == made["id"]][0]
        assert listed["name"] == "我的真名字", listed["name"]
        assert listed["name_raw"] == "__MSG_extName__", listed.get("name_raw")
    finally:
        config.reset_settings()

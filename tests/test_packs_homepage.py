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
def test_major_of_handles_vendor_prefixed_version():
    """`google-chrome --version` 返回的是 `Google Chrome 149.0.7827.53`。

    第一版按 `[.-]` 切了再找第一个纯数字片段，切出来是
    `["Google Chrome 149", "0", "7827", "53"]`，于是主版本被认成 **0**，
    "≥137 不支持扩展"这条判断永远不触发（实测踩到：正式版 Chrome
    被报告成"可以加载扩展"）。
    """
    from zw_core.services.engines import _major_of

    assert _major_of("Google Chrome 149.0.7827.53") == 149
    assert _major_of("150.0.7871.186") == 150
    assert _major_of("Chrome/157.0.8103.79") == 157
    assert _major_of("137.0.0.0") == 137
    assert _major_of("") == 0
    assert _major_of("unknown") == 0


def test_extensions_loadable_rejects_modern_chromium(tmp_path, monkeypatch):
    """Chrome 137+ 已移除命令行加载扩展的能力 —— 必须判成"不可加载"。

    实测（2026-11-17）：google-chrome 149 / ungoogled-chromium 150 /
    playwright chromium 三种内核、有界面与无头都试过，`--load-extension`
    的扩展 ID 从未出现在 chrome-extension:// 目标里；加
    `--disable-features=DisableLoadExtensionCommandLineSwitch` 也没用。
    """
    from zw_core.services import engines

    monkeypatch.setattr(engines, "engine_version", lambda _p: "Google Chrome 149.0.7827.53")
    ok, why = engines.extensions_loadable(tmp_path / "chrome")
    assert ok is False
    assert "137" in why and "不会加载" in why


def test_extensions_loadable_allows_old_engine(tmp_path, monkeypatch):
    from zw_core.services import engines

    monkeypatch.setattr(engines, "engine_version", lambda _p: "128.0.6613.84")
    ok, why = engines.extensions_loadable(tmp_path / "chrome")
    assert ok is True and why == ""


def test_extensions_loadable_unknown_version_warns(tmp_path, monkeypatch):
    """版本读不出来时**按"不能加载"告警**，不是默默放行。

    这条是 macOS CI 抓出来的：那边版本探测拿不到值，
    原来"未知就放行"导致警告在 macOS 上永远不出现（测试 `assert True is False`）。
    漏告警的代价是用户以为插件装好了、实际没加载，然后在"插件为什么没反应"上耗很久。
    """
    from zw_core.services import engines

    monkeypatch.setattr(engines, "engine_version", lambda _p: "")
    ok, why = engines.extensions_loadable(tmp_path / "chrome")
    assert ok is False
    assert "读不出" in why


def test_extensions_loadable_probe_error_warns(tmp_path, monkeypatch):
    """探测本身抛异常时同样按"不能加载"处理，绝不静默放行。"""
    from zw_core.services import engines

    def boom(_p):
        raise OSError("probe failed")

    monkeypatch.setattr(engines, "engine_version", boom)
    ok, why = engines.extensions_loadable(tmp_path / "chrome")
    assert ok is False and "读不出" in why


def test_apply_reports_extensions_will_not_load(tmp_path, monkeypatch):
    """应用扩展包时必须告诉调用方"插件不会出现在浏览器里"。

    代码和数据照样搬（换内核/换版本的机器上仍然有用），但沉默地搬完
    会让用户以为是自己的操作错了。
    """
    from zw_core import config, db

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_CHROME_BIN", "/usr/bin/google-chrome")
    config.reset_settings()
    db.reset_db()
    try:
        from zw_core.services import engines, manager as mgr, packs

        monkeypatch.setattr(engines, "engine_version", lambda _p: "Google Chrome 149.0.7827.53")

        src = tmp_path / "ext"
        src.mkdir()
        (src / "manifest.json").write_text(
            '{"manifest_version":3,"name":"Probe","version":"1.0"}', encoding="utf-8")
        pack = packs.import_pack(src_path=str(src), name="探针包")
        prof = mgr.create_profile({"name": "扩展验证"})
        out = packs.apply_to_profile(prof["id"], [pack["id"]])

        assert out["applied"], out
        assert out["extensions_active"] is False
        assert "137" in out["extensions_note"]
        # 数据/代码这一半必须照常完成
        assert out["applied"][0]["slug"] == "probe"
    finally:
        config.reset_settings()
        db.reset_db()

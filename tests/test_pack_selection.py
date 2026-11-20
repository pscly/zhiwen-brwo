"""新建环境时**指定带哪些插件**，以及「每个插件装到了哪些浏览器」。

## 为什么（2026-11-20 用户反馈）

> 新建浏览器的时候和进行窗口管理的时候或者是插件管理的时候，
> 怎么不能指定浏览器插件呢？例如新建浏览器的时候，
> 我希望是可以直接继承插件的

原来是"全带"或"全不带"两个选项，没法"只要 Bitwarden 和脚本猫，不要那两个测试用的"。

现在三种模式互斥，优先级从高到低：

1. `pack_ids`：只带这几个包（显式选择）
2. `with_default_packs=false`：一个都不带（干净环境）
3. 都不给：带所有 `is_default` 的包（老行为，保持兼容）

反向也要能查：**这个插件装到了哪些浏览器** —— 删包前才知道会影响谁。
"""

from __future__ import annotations

import json


def _setup(tmp_path, monkeypatch):
    from zw_core import config
    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    from zw_core.services import manager, packs
    return manager, packs


def _make_pack(packs, tmp_path, name: str, ext_name: str, eid: str) -> dict:
    d = tmp_path / f"chrome-{eid[:4]}" / "Default"
    (d / "Extensions" / eid).mkdir(parents=True)
    (d / "Extensions" / eid / "manifest.json").write_text(json.dumps(
        {"name": ext_name, "version": "1", "manifest_version": 3}))
    les = d / "Local Extension Settings" / eid
    les.mkdir(parents=True)
    (les / "000003.log").write_bytes(b"DATA")
    return packs.import_pack(str(d), name=name)


def test_pack_ids_brings_only_those_packs(tmp_path, monkeypatch):
    manager, packs = _setup(tmp_path, monkeypatch)
    try:
        pa = _make_pack(packs, tmp_path, "包A", "扩展A", "a" * 32)
        pb = _make_pack(packs, tmp_path, "包B", "扩展B", "b" * 32)
        pc = _make_pack(packs, tmp_path, "包C", "扩展C", "c" * 32)
        # B 设成默认，A/C 不设 —— 验证 pack_ids 压过 is_default
        packs.set_default(pb["id"], True)

        prof = manager.create_profile({"name": "只选AC", "pack_ids": [pa["id"], pc["id"]]})
        got = set(prof.get("extensions") or [])
        assert "扩展a" in {g.lower() for g in got} or any("扩展a" in g.lower() for g in got), got
        assert not any("扩展b" in g.lower() for g in got), f"不该带默认包 B：{got}"
    finally:
        from zw_core import config
        config.reset_settings()


def test_no_packs_when_disabled(tmp_path, monkeypatch):
    manager, packs = _setup(tmp_path, monkeypatch)
    try:
        p = _make_pack(packs, tmp_path, "默认包", "扩展X", "d" * 32)
        packs.set_default(p["id"], True)
        prof = manager.create_profile({"name": "干净环境", "with_default_packs": False})
        assert not (prof.get("extensions") or []), prof.get("extensions")
    finally:
        from zw_core import config
        config.reset_settings()


def test_defaults_when_nothing_specified(tmp_path, monkeypatch):
    manager, packs = _setup(tmp_path, monkeypatch)
    try:
        p = _make_pack(packs, tmp_path, "默认包", "扩展Y", "e" * 32)
        packs.set_default(p["id"], True)
        prof = manager.create_profile({"name": "默认带上"})
        assert prof.get("extensions"), "默认包没带上"
    finally:
        from zw_core import config
        config.reset_settings()


def test_pack_usage_lists_profiles(tmp_path, monkeypatch):
    """反向查询：这个包的扩展被哪些环境用着。"""
    manager, packs = _setup(tmp_path, monkeypatch)
    try:
        p = _make_pack(packs, tmp_path, "查询包", "扩展Z", "f" * 32)
        manager.create_profile({"name": "用它的环境", "pack_ids": [p["id"]]})
        usage = packs.pack_usage(p["id"])
        assert any(u["name"] == "用它的环境" for u in usage), usage
    finally:
        from zw_core import config
        config.reset_settings()


def test_remove_extensions_from_profile(tmp_path, monkeypatch):
    """从环境里摘掉扩展后，反向查询也随之消失。"""
    manager, packs = _setup(tmp_path, monkeypatch)
    try:
        p = _make_pack(packs, tmp_path, "待移除", "扩展W", "1" * 32)
        prof = manager.create_profile({"name": "移除测试", "pack_ids": [p["id"]]})
        before = list(prof.get("extensions") or [])
        assert before, "前置条件：环境应带着扩展"

        res = packs.remove_extensions_from_profile(prof["id"], before)
        assert res["extensions"] == [], res
        assert res["removed"], res
        # 反向查询也要跟着空
        assert packs.pack_usage(p["id"]) == [], packs.pack_usage(p["id"])
    finally:
        from zw_core import config
        config.reset_settings()

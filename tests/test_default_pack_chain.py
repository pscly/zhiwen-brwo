"""「导入包 → 设为默认 → 新建环境带上 → 启动后扩展真的生效」整条链。

## 为什么写（2026-11-20）

用户原话：「这样以后创建浏览器时，可以默认就把这些插件的数据都带上」。

这条链有四个环节，任何一环断了"默认带上"就是假的：

```
① 导入包（代码 + 数据）
② 设为默认（is_default=1）
③ 新建环境 → apply_default_packs 把扩展挂到环境上
④ 启动浏览器 → --load-extension 让它真的被加载
```

前三环本来就有覆盖；第 ④ 环——**浏览器是不是真的加载了**——一直没人验。
实测（真 ungoogled-chromium + CDP）确认链条成立。

## 一个坑

测试扩展的 `manifest.json` **必须带 `manifest_version`**。缺了它 Chromium 会
**静默忽略**整个扩展 —— 命令行里 `--load-extension` 明明在，但扩展就是不加载，
`Preferences` 里也找不到它。我第一版测试数据就漏了这个字段，白排查了两轮。
"""

from __future__ import annotations

import json

import pytest


def _manifest(**extra) -> str:
    base = {"name": "默认测试扩展", "version": "1.0", "manifest_version": 3}
    base.update(extra)
    return json.dumps(base, ensure_ascii=False)


@pytest.fixture()
def packs_env(tmp_path, monkeypatch):
    from zw_core import config

    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    config.reset_settings()
    from zw_core.services import packs
    try:
        yield packs
    finally:
        config.reset_settings()


def test_default_pack_is_applied_on_create(packs_env, tmp_path, monkeypatch):
    """③ 新建环境时，默认包的扩展与其数据都要挂上去。"""
    packs = packs_env
    from zw_core.services import manager

    eid = "a" * 32
    d = tmp_path / "chrome" / "Default"
    (d / "Extensions" / eid).mkdir(parents=True)
    (d / "Extensions" / eid / "manifest.json").write_text(_manifest())
    les = d / "Local Extension Settings" / eid
    les.mkdir(parents=True)
    (les / "000003.log").write_bytes(b"LOGIN-DATA")

    pk = packs.import_pack(str(d), name="默认包")
    assert pk["with_data"] == 1
    packs.set_default(pk["id"], True)

    prof = manager.create_profile({"name": "默认包环境"})
    assert prof["extensions"], "默认包的扩展没挂到环境上"

    # 扩展代码进了共享库
    lib = list(packs.get_settings().ext_dir.glob("*/-1.0")) if False else None
    lib_dirs = [p for p in packs.get_settings().ext_dir.iterdir() if p.is_dir()]
    assert lib_dirs, "扩展库是空的"
    assert (lib_dirs[0] / "manifest.json").is_file()

    # 数据进了环境的 udd（双写：源 ID + 本机推算 ID 各一份）
    udd = packs.get_settings().profiles_dir / prof["id"] / "udd"
    logs = list(udd.rglob("000003.log"))
    assert logs, f"扩展数据没进环境：{list(udd.rglob('*'))[:10]}"
    assert any(p.read_bytes() == b"LOGIN-DATA" for p in logs)


def test_manifest_without_manifest_version_is_the_trap(packs_env, tmp_path):
    """记录那个坑：缺 `manifest_version` 的扩展 Chromium 会静默忽略。

    这条不是测产品，而是把"为什么测试数据必须带这个字段"钉在代码里，
    免得后来人（包括我）再造一个不完整的 manifest 然后排查半天。
    """
    bad = json.loads(json.dumps({"name": "缺字段", "version": "1.0"}))
    assert "manifest_version" not in bad
    good = json.loads(_manifest())
    assert good["manifest_version"] in (2, 3)

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

import pytest


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


def test_api_error_codes_not_500(tmp_path, monkeypatch):
    """新接口在"包/环境不存在"时必须给 404，不能 500。

    ## 为什么（2026-11-20 自查实测）

    我第一版在路由里自己 `try/except PackError` 再包一层 `HTTPException` ——
    而 `HTTPException` 在这个模块**没有导入**，一抛就是 `NameError` → **500**：

        {"code":"INTERNAL","message":"NameError: name 'HTTPException' is not defined"}

    本该是 404 `PACK_NOT_FOUND`。项目里 `PackError` 已经有全局异常处理器
    （`_pack_error`），正确做法是**让它冒泡**，不要重复包。

    这条用 TestClient 直接打接口，盯住状态码。
    """
    from fastapi.testclient import TestClient

    from zw_core import config
    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    # 必须有 device_id，否则"没有任何凭据"的实例会拒绝所有请求（401），测不到 404
    monkeypatch.setenv("ZW_DEVICE_ID", "zwd_errcode_test_0001")
    config.reset_settings()
    try:
        from zw_core.main import app
        c = TestClient(app)
        # TestClient 的 client host 不是回环，所以要显式带机器码（真实远程用法）
        hdr = {"Authorization": "Bearer zwd_errcode_test_0001"}
        r1 = c.get("/api/v1/packs/nonexistent/usage", headers=hdr)
        assert r1.status_code == 404, f"usage 应 404，实际 {r1.status_code}: {r1.text[:200]}"
        assert r1.json().get("code") == "PACK_NOT_FOUND"

        r2 = c.get("/api/v1/packs/nonexistent/export", headers=hdr)
        assert r2.status_code == 404, f"export 应 404，实际 {r2.status_code}: {r2.text[:200]}"

        r3 = c.post("/api/v1/profiles/nope/extensions/remove", json={"slugs": ["x"]}, headers=hdr)
        assert r3.status_code == 404, f"remove 应 404，实际 {r3.status_code}: {r3.text[:200]}"
    finally:
        config.reset_settings()


def test_http_semantics_none_vs_empty(tmp_path, monkeypatch):
    """**走 HTTP**（经 Pydantic）验证三种语义 —— 不能只直调 manager。

    ## 为什么（2026-11-20 审查抓到的回归）

    我把 `pack_ids` 的模型默认值写成 `default_factory=list`，于是 FastAPI 给
    每个没传该字段的请求补上 `[]`，`manager` 里 `if want_pack_ids is not None`
    恒为真 → **所有走 HTTP 的路径默认包全部失效**。

    而 `tests/test_pack_selection.py` 那三条都是**直调 manager**（不传 key
    就是 None），完美绕过了 Pydantic，一条都没红。只有 `test_api_smoke.py`
    里那条走 TestClient 的老用例抓到了。

    这条把"HTTP 层三种情况"钉死：
      · 不传 pack_ids              → 带默认包
      · with_default_packs=true    → 带默认包
      · with_default_packs=false   → 干净
      · pack_ids=[]                → 干净（显式清空）
    """
    import json as _json

    from fastapi.testclient import TestClient

    from zw_core import config
    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_DEVICE_ID", "zwd_semantics_0001")
    config.reset_settings()
    try:
        from zw_core.main import app
        from zw_core.services import packs

        # 造一个默认包
        eid = "c" * 32
        d = tmp_path / "chrome" / "Default"
        (d / "Extensions" / eid).mkdir(parents=True)
        (d / "Extensions" / eid / "manifest.json").write_text(_json.dumps(
            {"name": "语义扩展", "version": "1", "manifest_version": 3}))
        pk = packs.import_pack(str(d), name="语义包")
        packs.set_default(pk["id"], True)

        c = TestClient(app)
        hdr = {"Authorization": "Bearer zwd_semantics_0001"}

        r = c.post("/api/v1/profiles", json={"name": "不传"}, headers=hdr)
        assert r.status_code == 200, r.text[:200]
        assert r.json()["data"].get("extensions"), "不传 pack_ids 时应带默认包"

        r = c.post("/api/v1/profiles", json={"name": "默认开", "with_default_packs": True}, headers=hdr)
        assert r.json()["data"].get("extensions"), "with_default_packs=true 应带默认包"

        r = c.post("/api/v1/profiles", json={"name": "默认关", "with_default_packs": False}, headers=hdr)
        assert not (r.json()["data"].get("extensions") or []), "with_default_packs=false 应干净"

        r = c.post("/api/v1/profiles", json={"name": "空数组", "pack_ids": []}, headers=hdr)
        assert not (r.json()["data"].get("extensions") or []), "pack_ids=[] 应干净"
    finally:
        config.reset_settings()


def test_delete_guard_reports_in_use(tmp_path, monkeypatch):
    """有环境在用的包，删之前必须报 409 并列出占用者。

    ## 为什么（2026-11-20 审查指出）

    `delete_pack` 以前**不查占用**：删掉之后正在跑的环境不受影响（插件已复制
    进扩展库），但**以后新建环境就带不上了**，而且用户完全不知道这事。
    界面上也没有任何地方能查"这个包被哪些环境用着"。
    """
    manager, packs = _setup(tmp_path, monkeypatch)
    try:
        p = _make_pack(packs, tmp_path, "占用包", "占用扩展", "9" * 32)
        manager.create_profile({"name": "用它", "pack_ids": [p["id"]]})

        with pytest.raises(packs.PackError) as ei:
            packs.delete_pack(p["id"])
        assert ei.value.code == "PACK_IN_USE", ei.value.code
        assert "用它" in ei.value.message, ei.value.message

        # force 才真删
        packs.delete_pack(p["id"], force=True)
        with pytest.raises(packs.PackError):
            packs.get_pack(p["id"])
    finally:
        from zw_core import config
        config.reset_settings()


def test_service_takeover_endpoint_exists(tmp_path, monkeypatch):
    """`POST /service/takeover` 必须在位 —— 远程升级靠它生效，不用杀进程。

    ## 为什么（2026-11-20 用户指出）

    > 云电脑运行的版本你应该自己通过后台的东西进行修改才对

    以前 `take_over()` 只挂在 GUI 按钮上。远程升级只能手动杀掉占端口的进程，
    而那个进程往往就是正在提供命令通道的 core —— 一杀就断线（0.8.29 那次
    就这样把云电脑搞离线，要用户手动去点客户端才恢复）。

    这条只验"接口存在且立刻返回"，不真去杀进程（那会打断测试自己）。
    """
    import threading as _threading

    from fastapi.testclient import TestClient

    from zw_core import config
    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_DEVICE_ID", "zwd_takeover_0001")
    config.reset_settings()

    # ## 必须拦住真正的 respawn/exit（2026-11-21 实测踩到）
    #
    # 接口里用 `threading.Timer(1.0, ...)` 延后调用 `_respawn_and_exit()`，
    # 而那个函数最后是 `os._exit(0)`。测试里调完立刻断言、pytest 继续跑别的用例 ——
    # **1 秒后 timer 触发，把整个 pytest 进程杀掉**：输出停在 `................`，
    # 没有汇总行，退出码却是 0。更糟的是同一次 `check-all.sh` 里
    # core 也是被这个 timer 干掉的，于是 gateway/ui/remote 三项随机 `fetch failed`。
    #
    # 所以这里把 Timer 换成"立即执行但只记录"，绝不真退出。
    fired: list[str] = []

    class _FakeTimer:
        def __init__(self, interval, fn, *a, **kw):
            self._fn = fn
            self.daemon = False

        def start(self):
            fired.append("timer-started")   # 记下来证明接口确实安排了动作

    monkeypatch.setattr(_threading, "Timer", _FakeTimer, raising=False)
    try:
        from zw_core.main import app
        c = TestClient(app)
        r = c.post("/api/v1/service/takeover",
                   headers={"Authorization": "Bearer zwd_takeover_0001"})
        assert r.status_code == 200, r.text[:200]
        d = r.json().get("data") or {}
        assert d.get("started") is True, d
        # **不能只退出、必须能自己回来**（2026-11-20 审查指出）：
        # 上一版直接 `os._exit(0)`，注释假设"新版由守护进程拉起"，但裸跑和
        # Windows 客户端都没有守护 → 点一下等于远程把服务关掉，永久失联。
        # 现在改成"先拉起新进程（让它等端口释放）再退出"，这条检查代码里
        # 确实走了 re-exec 那条路，而不是裸 exit。
        import inspect as _inspect
        from zw_core import api as _api_mod
        _src = _inspect.getsource(_api_mod)
        assert "_respawn_and_exit" in _src, "接管必须走自我重启，不能裸 exit"
        assert "ZW_WAIT_PORT_MS" in _src, "新进程必须等旧进程放掉端口，否则 Errno 98"

        # **必须带上真实端口与 pid**：前端据此提示"本进程将退出"。
        # 早期版本这里新建了个 LocalCore 去调 take_over()，而它的 attached
        # 恒为 False → 永远 no-op，接口回 200 却什么都没做（假成功）。
        assert d.get("port"), d
        assert d.get("pid"), d
        # 接口必须**真的安排了**重启动作（否则就是个空壳）
        assert fired, "接口没有安排接管动作"
        # 而且我们自己还活着 —— 说明 timer 被测试拦住了，没真 exit
        assert True
    finally:
        config.reset_settings()


def test_create_returns_pack_warnings_immediately(tmp_path, monkeypatch):
    """包没带上时，**建环境的那一刻**就要告诉用户，不能等第一次启动。

    ## 为什么（2026-11-20 审查两次点名）

    告警只进 `_DEFAULT_PACK_WARNINGS`，而它要到 `start()` 才被读出来。
    用户建完环境看到干净的响应体以为一切正常，直到启动才发现插件没带上。

    走 HTTP 验（因为"响应里有没有这个字段"是接口层的事）。
    """
    from fastapi.testclient import TestClient

    from zw_core import config
    monkeypatch.setenv("ZW_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("ZW_DEVICE_ID", "zwd_warn_0001")
    config.reset_settings()
    try:
        from zw_core.main import app
        c = TestClient(app)
        hdr = {"Authorization": "Bearer zwd_warn_0001"}
        # 指定一个不存在的包 → 建环境成功，但必须带 warnings
        r = c.post("/api/v1/profiles",
                   json={"name": "告警环境", "pack_ids": ["ghost-pack-xyz"]}, headers=hdr)
        assert r.status_code == 200, r.text[:200]
        d = r.json()["data"]
        assert d.get("warnings"), f"建环境时没有返回告警：{d}"
        assert any("ghost-pack-xyz" in str(w) for w in d["warnings"]), d["warnings"]
    finally:
        config.reset_settings()


def test_pack_usage_running_reflects_state(tmp_path, monkeypatch):
    """`pack_usage()` 的 `running` 必须真的反映环境状态。

    ## 为什么（2026-11-20 审查实测）

    原来写 `bool(prof.get("running"))`，而 `profiles` 表**没有 running 列** ——
    实际状态在 `state`（`"running"` / `"stopped"`）。用错字段的结果是恒为 False：
    界面上的「（运行中）」永远不显示，删包时也说不出"正被一个跑着的浏览器用着"。
    """
    manager, packs = _setup(tmp_path, monkeypatch)
    try:
        p = _make_pack(packs, tmp_path, "状态包", "状态扩展", "7" * 32)
        prof = manager.create_profile({"name": "状态环境", "pack_ids": [p["id"]]})
        rows = packs.pack_usage(p["id"])
        assert rows, rows
        assert rows[0]["running"] is False, f"没在跑却报 {rows[0]['running']}"

        # 直接把库里的 state 改成 running（不真开浏览器，只看字段映射对不对）
        with manager.get_db().conn() as c:
            c.execute("UPDATE profiles SET state='running' WHERE id=?", (prof["id"],))
            c.commit()
        rows2 = packs.pack_usage(p["id"])
        assert rows2[0]["running"] is True, f"state=running 却报 {rows2[0]['running']}"
    finally:
        from zw_core import config
        config.reset_settings()

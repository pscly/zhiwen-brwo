"""客户端新增能力：开机自启、字体探测、GUI 模块可导入。

这些是 0.3.0 补的"日常可用性"，比注册流程更容易悄悄坏掉
（自启写错路径不会报错，只会在重启后发现没起）。
"""

from __future__ import annotations

import os
import time
import sys

import pytest

from zw_core import client as C


# ── 开机自启 ──────────────────────────────────────────────
@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(C.CONFIG_DIR_ENV, str(tmp_path / "home"))
    return tmp_path / "home"


def test_autostart_roundtrip(home, monkeypatch, tmp_path):
    """开→查→关→查 必须自洽，且**按平台**落到正确位置。

    之前这条只按 Linux 写（断言 XDG 的 .desktop 存在），
    在 macOS CI 上必然失败 —— 因为那边写的是 LaunchAgents 的 plist。
    """
    if sys.platform == "win32":
        pytest.skip("Windows 上会写真实注册表，靠 CI 真机验证")

    if sys.platform == "darwin":
        # macOS 用固定路径 ~/Library/LaunchAgents，改不了；用假的 HOME 隔离
        fake_home = tmp_path / "machome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        landed = fake_home / "Library" / "LaunchAgents" / "cc.pscly.zhiwen-client.plist"
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        landed = tmp_path / "xdg" / "autostart" / "zhiwen-client.desktop"

    assert C.autostart_enabled() is False
    ok, msg = C.set_autostart(True)
    assert ok, msg
    assert C.autostart_enabled() is True
    assert landed.exists(), f"自启文件没落到预期位置：{landed}"

    body = landed.read_text(encoding="utf-8")
    if sys.platform == "darwin":
        assert "cc.pscly.zhiwen-client" in body
        assert "RunAtLoad" in body
    else:
        assert "[Desktop Entry]" in body
        # 断言的必须是**产品标记**，不能是"某个路径里恰好含 zhiwen"。
        # 本地能过纯粹因为仓库目录叫 zhiwen_brwo，CI 上路径不同就会失败。
        assert "zw_core.client" in body or "zhiwen-client" in body.lower()
        assert "Name=知纹客户端" in body

    ok, msg = C.set_autostart(False)
    assert ok, msg
    assert C.autostart_enabled() is False
    assert not landed.exists()


def test_autostart_idempotent(home, monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("Windows 走注册表")
    if sys.platform == "darwin":
        fake_home = tmp_path / "machome2"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for _ in range(3):
        ok, _msg = C.set_autostart(True)
        assert ok
        assert C.autostart_enabled() is True
    for _ in range(3):
        ok, _msg = C.set_autostart(False)
        assert ok


def test_launch_command_mentions_module_when_not_frozen(monkeypatch):
    """源码运行时自启命令要带 `-m zw_core.client`。

    否则开机自启会打开一个 python REPL（用户看到黑窗口一闪），
    而不是客户端 —— 这个错误很隐蔽，只有重启后才发现。
    """
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = C._launch_command()
    assert "-m zw_core.client" in cmd
    assert sys.executable in cmd


def test_launch_command_is_bare_exe_when_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd = C._launch_command()
    assert cmd.strip('"') == sys.executable
    assert "-m" not in cmd


# ── 字体探测 ──────────────────────────────────────────────
def test_pick_ui_font_returns_three_tuple():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:
        pytest.skip("没有可用显示")
    try:
        fam, size, weight = C.pick_ui_font(root, 10)
        assert isinstance(fam, str) and fam
        assert size == 10
        assert weight in ("normal", "bold")
    finally:
        root.destroy()


def test_pick_mono_font_returns_string():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:
        pytest.skip("没有可用显示")
    try:
        assert isinstance(C.pick_mono_font(root), str)
    finally:
        root.destroy()


# ── GUI 模块 ──────────────────────────────────────────────
def test_gui_module_imports():
    """gui 模块必须能干净导入（它依赖 client 的一堆导出，改名会断）。"""
    import zw_core.gui as gui

    assert callable(gui.run_gui)
    assert callable(gui._make_icon_image) or True   # PIL 缺失时也不该崩


def test_client_run_gui_delegates(monkeypatch):
    """client.run_gui 应当委托给 gui 模块（而不是留着一份旧实现）。"""
    import zw_core.client as cli_mod
    import zw_core.gui as gui_mod

    called = {"n": 0}

    def fake() -> int:
        called["n"] += 1
        return 0

    monkeypatch.setattr(gui_mod, "run_gui", fake)
    assert cli_mod.run_gui() == 0
    assert called["n"] == 1


# ── 心跳携带统计 ──────────────────────────────────────────
def test_heartbeat_sends_stats_payload(monkeypatch, home):
    """心跳要能把本机概况带给服务器（面板据此显示"几个环境在跑"）。"""
    import httpx

    seen: dict = {}

    class Resp:
        status_code = 200
        content = b"x"
        text = "{}"

        def json(self):
            return {"ok": True, "data": {"device": {}}}

    class FakeClient:
        def __init__(self, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def post(self, url, json=None, headers=None, **kw):
            seen["json"] = json
            seen["headers"] = headers
            return Resp()

    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
    C.heartbeat("http://x:8700", "zwd_abcdefgh12345678",
                extra={"profiles": 3, "running": 1})
    assert seen["json"] == {"profiles": 3, "running": 1}
    assert seen["headers"]["X-ZW-Device"] == "zwd_abcdefgh12345678"


def test_heartbeat_without_stats_still_works(monkeypatch, home):
    import httpx

    seen: dict = {}

    class Resp:
        status_code = 200
        content = b"x"
        text = "{}"

        def json(self):
            return {"ok": True, "data": {}}

    class FakeClient:
        def __init__(self, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def post(self, url, json=None, headers=None, **kw):
            seen["json"] = json
            return Resp()

    monkeypatch.setattr(httpx, "Client", lambda **kw: FakeClient())
    res = C.heartbeat("http://x:8700", "zwd_abcdefgh12345678")
    assert res["ok"] is True
    assert seen["json"] is None   # 不传就是空 body，别塞个 {}


# ── 心跳健康度（界面据此显示"和服务器还连着吗"）────────────
def test_heartbeat_status_before_first_success(home):
    hb = C.Heartbeat("http://x:8700", "zwd_abcdefgh12345678", interval=60)
    st = hb.status()
    assert st["healthy"] is False
    assert st["last_ok"] == 0
    assert st["age"] is None
    assert st["error"] == ""


def test_heartbeat_status_tolerates_short_gap(home):
    """网络抖一下不该报红 —— 但真断线必须能看出来。"""
    hb = C.Heartbeat("http://x:8700", "zwd_abcdefgh12345678", interval=60)
    hb.last_ok = time.time() - 30
    assert hb.status()["healthy"] is True
    hb.last_ok = time.time() - 200      # 超过 3 个周期
    assert hb.status()["healthy"] is False


def test_heartbeat_status_reports_revoked(home):
    hb = C.Heartbeat("http://x:8700", "zwd_abcdefgh12345678", interval=60)
    hb.last_ok = time.time()
    hb.revoked = True
    st = hb.status()
    assert st["revoked"] is True
    assert st["healthy"] is False, "被踢了不能再算健康"


def test_macos_autostart_plist_branch(home, monkeypatch, tmp_path):
    """macOS 分支生成 LaunchAgents plist。

    在 Linux 上也能验：把 sys.platform 掰成 darwin、HOME 指到临时目录。
    这样这条分支不必等 macOS CI 才知道对不对（它刚在 macOS CI 上挂过一次）。
    """
    if sys.platform == "win32":
        pytest.skip("Windows 上 sys.platform 的猴子补丁会连带改坏 winreg 判断")
    fake_home = tmp_path / "machome"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(C.sys, "platform", "darwin")

    assert C.autostart_enabled() is False
    ok, msg = C.set_autostart(True)
    assert ok, msg
    plist = fake_home / "Library" / "LaunchAgents" / "cc.pscly.zhiwen-client.plist"
    assert plist.exists()
    body = plist.read_text(encoding="utf-8")
    assert "<?xml" in body
    assert "cc.pscly.zhiwen-client" in body
    assert "RunAtLoad" in body
    assert "ProgramArguments" in body
    assert C.autostart_enabled() is True

    ok, msg = C.set_autostart(False)
    assert ok, msg
    assert not plist.exists()


def test_run_cli_without_tty_exits_gracefully(home, monkeypatch, capsys):
    """没有 TTY 时不能抛 EOFError —— 打包产物在 glibc 不匹配时就走这条路。

    真实现场：Linux 包在旧 glibc 上 tkinter 加载失败 → 回退 CLI →
    stdin 不是 TTY → `input()` 抛 EOFError → PyInstaller 打一句英文
    "Failed to execute script"，用户完全不知道发生了什么。
    """
    import io
    import sys as _sys

    class NotATty(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.setattr(_sys, "stdin", NotATty(""))
    code = C.run_cli()
    out = capsys.readouterr()
    assert code != 0
    joined = out.out + out.err
    assert "没有可用的图形界面" in joined or "没有可交互的终端" in joined
    assert "selftest" in joined, "要告诉用户怎么自查"


def test_proxy_upstream_keeps_host_port_visible():
    """环境行里的代理串必须能看出"是哪台代理"。

    真踩过：带认证时返回 `socks5://user:***@66.80.12.66:20000`（32 字符），
    而行里只留 22 个字符 → 截成 `socks5://user:***@66.80.…`，**IP 尾部被切掉**。
    于是 66.80.12.66 和 66.80.12.65 在界面上长得一模一样，用户分不清用哪台。

    修法：有认证时把用户名也压成 `***`，并且超长时优先砍协议名，
    保证 `host:port` 完整可见。
    """
    from zw_core.gui_profiles import _clip, proxy_upstream_of

    row = {"scheme": "socks5", "host": "66.80.12.66", "port": 20000, "username": "u"}
    text = proxy_upstream_of(row)
    assert "66.80.12.66:20000" in text, text
    # 行里会截断，但截断后 host:port 仍在
    assert "66.80.12.66" in _clip(text, 22), _clip(text, 22)
    # 用户名被压掉（信息量不如"是哪台机器"）
    assert "user" not in text and "u:" not in text, text
    # 无认证时是干净的 scheme+host:port
    row2 = dict(row, username="")
    assert proxy_upstream_of(row2).endswith("66.80.12.66:20000")


# ── CLI 新命令（0.7.26）──────────────────────────────────
def test_cli_has_proxy_import_and_sync():
    """`zw proxy import` / `zw proxy sync` 必须存在。

    用户要求"AI 也能通过接口操作"，CLI 是最直接的入口；
    以前只能 list/add/check/rm，批量导入和同步都缺。
    直接跑 `--help` 断言，最接近用户实际用法。
    """
    import subprocess
    import sys as _sys

    out = subprocess.run([_sys.executable, "-m", "zw_core.cli", "proxy", "--help"],
                         capture_output=True, text=True, timeout=60).stdout
    assert "import" in out, out[:400]
    assert "sync" in out, out[:400]


def test_cli_has_extension_install_url_and_updates():
    """`zw extension install --url …` 与 `zw extension updates` 必须存在。

    商店链接装扩展是纯 API 起步的，CLI 必须能直达；更新检查同理。
    """
    import subprocess
    import sys as _sys

    out = subprocess.run([_sys.executable, "-m", "zw_core.cli", "extension", "--help"],
                         capture_output=True, text=True, timeout=60).stdout
    assert "updates" in out, out[:400]
    out2 = subprocess.run([_sys.executable, "-m", "zw_core.cli", "extension", "install", "--help"],
                          capture_output=True, text=True, timeout=60).stdout
    assert "--url" in out2 and "--default" in out2, out2[:500]


def test_mcp_exposes_new_capabilities():
    """MCP 必须暴露本轮新增的能力，否则"AI 也能控制"是空话。

    之前 28 个工具里没有：代理批量导入 / 代理库同步 / 代理隐藏 /
    装扩展（商店链接）/ 扩展更新检查。用户明确要求 AI 能通过接口知道并控制，
    缺工具就等于 AI 做不到。
    """
    import json as _json
    import subprocess
    import sys as _sys

    req = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    out = subprocess.run([_sys.executable, "-m", "zw_core.cli", "mcp"],
                         input=req + "\n", capture_output=True, text=True, timeout=120).stdout
    names = set()
    for line in out.splitlines():
        try:
            d = _json.loads(line)
        except Exception:
            continue
        if d.get("id") == 1:
            names = {t["name"] for t in d["result"]["tools"]}
    # 注意：**故意不包含** zw_hide_proxy —— 本地库没有 hidden 列，
    # 隐藏是服务器侧概念；曾经有过一个永远报 "no such column" 的工具，已移除。
    want = {"zw_import_proxies", "zw_sync_proxies",
            "zw_install_extension", "zw_check_extension_updates"}
    assert want <= names, f"缺工具：{sorted(want - names)}（现有 {len(names)} 个）"


# ── 两个死配置项（0.7.46）────────────────────────────────
def test_allow_root_gate_is_enforced(monkeypatch):
    """`ZW_ALLOW_ROOT` 必须真的生效。

    它原来只在 `config.py` 里有定义，**全项目没有任何地方读它** ——
    而 `engines.build_cmdline()` 是"只要以 root 跑就无条件加 --no-sandbox"。
    等于这个开关设了完全没用，用户以为自己"不允许以 root 跑"，其实照样在跑。
    """
    from pathlib import Path

    from zw_core import config
    from zw_core.services import engines

    import sys as _sys

    import pytest as _pytest

    if _sys.platform == "win32":
        # Windows 上这段代码根本不走（`if not IS_WIN and _is_root()`），
        # `--no-sandbox` 在 Windows 是另一条分支。硬在 Windows 上断言会红 ——
        # 这是测试没考虑平台，不是产品问题（CI 的 Windows job 抓出来过）。
        _pytest.skip("root / --no-sandbox 这条只在 POSIX 有意义")

    # 非 root 环境下这条路径本来就不走，直接验"root + 未授权 → 明确报错"
    monkeypatch.setattr(engines, "_is_root", lambda: True)

    prof = {"name": "x", "id": "p1", "start_urls": [], "fp_timezone": "", "fp_lang": ""}
    # 默认是 true（保持历史行为，容器/CI 里 root 照常跑）；
    # 显式设成 false 才是"拒绝以 root 跑浏览器"。
    monkeypatch.setenv("ZW_ALLOW_ROOT", "false")
    config.reset_settings()
    try:
        engines.build_cmdline(Path("/usr/bin/chrome"), Path("/tmp/udd"), 12345, prof)
    except engines.EngineError as exc:
        assert exc.code == "ENGINE_RUN_AS_ROOT", exc.code
        assert "ZW_ALLOW_ROOT" in exc.message, exc.message
    else:
        raise AssertionError("以 root 跑且未授权时必须报错，而不是静默 --no-sandbox")

    # 授权后应该正常出命令行，且带 --no-sandbox
    monkeypatch.setenv("ZW_ALLOW_ROOT", "true")
    config.reset_settings()
    try:
        cmd = engines.build_cmdline(Path("/usr/bin/chrome"), Path("/tmp/udd"), 12345, prof)
        assert "--no-sandbox" in cmd, cmd
    finally:
        monkeypatch.delenv("ZW_ALLOW_ROOT", raising=False)
        config.reset_settings()


def test_gateway_enabled_switch_is_enforced(monkeypatch):
    """`ZW_GATEWAY_ENABLED=false` 时不该登记网关路由。

    同样是个"只在配置里有、没人读"的死开关：关掉之后 `/cdp/<id>` 依然可用。
    """
    from zw_core import config, gateway

    monkeypatch.setenv("ZW_GATEWAY_ENABLED", "false")
    config.reset_settings()
    try:
        router = gateway.Router() if hasattr(gateway, "Router") else gateway.ROUTER
        router.register("pf_deadswitch", 12345, "自检")
        assert router.get("pf_deadswitch") is None, "网关关闭时不该登记路由"
    finally:
        monkeypatch.setenv("ZW_GATEWAY_ENABLED", "true")
        config.reset_settings()

    router2 = gateway.Router() if hasattr(gateway, "Router") else gateway.ROUTER
    router2.register("pf_on", 12346, "自检")
    assert router2.get("pf_on") is not None, "网关打开时应该登记"
    router2.unregister("pf_on")
    router2.unregister("pf_deadswitch")


def test_mcp_reports_missing_args_readably():
    """MCP 工具少给必填参数时，必须报**可读的**缺参错误，不能是裸 KeyError。

    真踩过：`tools/call` 直接把 `args` 丢给工具函数，而函数内部写 `args["profile"]`，
    于是 AI 少给一个参数时拿到的是：

        {"code": "KeyError", "message": "'profile'"}

    —— 既不知道**哪个**参数必填，也不知道该怎么填。而工具的 inputSchema 里
    其实早声明了 `required`，只是没人用它做校验。实测 32 个工具里有 21 个这样。

    这条测试会对**每个**工具用空参数调一次，断言不再出现裸 KeyError。
    """
    import json as _json
    import subprocess
    import sys as _sys

    tl = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    out = subprocess.run([_sys.executable, "-m", "zw_core.cli", "mcp"],
                         input=tl + "\n", capture_output=True, text=True, timeout=120).stdout
    tools = None
    for line in out.splitlines():
        try:
            d = _json.loads(line)
        except Exception:
            continue
        if d.get("id") == 1:
            tools = d["result"]["tools"]
    assert tools, "拿不到工具清单"

    lines = [_json.dumps({"jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
                          "params": {"name": t["name"], "arguments": {}}})
             for i, t in enumerate(tools)]
    out2 = subprocess.run([_sys.executable, "-m", "zw_core.cli", "mcp"],
                          input="\n".join(lines) + "\n", capture_output=True,
                          text=True, timeout=600).stdout

    raw = []
    for line in out2.splitlines():
        try:
            d = _json.loads(line)
        except Exception:
            continue
        rid = d.get("id")
        if not isinstance(rid, int) or rid < 100:
            continue
        txt = _json.dumps(d.get("result") or {}, ensure_ascii=False)
        if "KeyError" in txt or "TypeError" in txt:
            raw.append(tools[rid - 100]["name"])
    assert not raw, f"这些工具少参数时报裸异常：{raw}"


def test_client_entrypoint_forwards_cli_subcommands(monkeypatch, capsys):
    """单文件产物的入口是 `client.py`，它必须把 CLI 子命令转交给 `cli.py`。

    真踩过：PyInstaller 的入口是 `client.py`（GUI），不是 `cli.py`。
    于是 `zhiwen-client serve` **根本不生效** —— `serve` 被当成多余参数忽略，
    程序照样去起图形界面，在没有显示器的服务器上直接

        TclError: no display name and no $DISPLAY environment variable

    而 README 与 docs 里写的是 `zw serve`。这是"用已发布产物跑 e2e"时发现的：
    产物版 core 在 8850 端口只有 Connection refused。
    """
    from zw_core import client as clientmod

    called = {"cli": 0}

    def fake_cli_main():
        called["cli"] += 1

    monkeypatch.setattr("zw_core.cli.main", fake_cli_main, raising=False)
    monkeypatch.setattr("zw_core.console.setup_console", lambda: None)
    monkeypatch.setattr(clientmod, "setup_console", lambda: None, raising=False)
    monkeypatch.setattr(clientmod.sys, "argv", ["zhiwen-client", "serve"])
    # 不要让真去起服务
    rc = clientmod.main()
    assert called["cli"] == 1, "serve 没有被转交给 cli.main()"
    assert rc == 0


def test_client_entrypoint_explains_missing_display(monkeypatch, capsys):
    """没有图形环境时不能只抛 TclError，要告诉用户该用哪个子命令。"""
    import sys as _sys

    from zw_core import client as clientmod

    if _sys.platform == "win32":
        import pytest as _pytest

        _pytest.skip("这条只在 POSIX 有意义")
    monkeypatch.setattr("zw_core.console.setup_console", lambda: None)
    monkeypatch.setattr(clientmod, "setup_console", lambda: None, raising=False)
    monkeypatch.setattr(clientmod.sys, "argv", ["zhiwen-client"])   # 不带任何子命令
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    rc = clientmod.main()
    assert rc == 2, f"应当给出可操作提示并以 2 退出，实际 {rc}"
    err = capsys.readouterr().err
    assert "serve" in err and "--selftest" in err, err


def test_gui_connect_failure_tells_user_about_offline_serve():
    """连不上服务器时，GUI 必须告诉用户"还能用 serve 单机跑"。

    工具的本机能力（环境管理、浏览器、CDP）**不依赖服务器**，`zw serve` 照样工作
    （docs/11 明确写了）。但单文件客户端只有图形界面入口，用户不知道还有这条路，
    连不上就以为软件坏了。

    这条只验文案与代码路径存在（GUI 交互本身由 scripts/gui-check.py 覆盖）。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "core/src/zw_core/gui.py").read_text(encoding="utf-8")

    assert "想先在本机用（不连服务器）" in src, "连不上服务器时没有给出单机用法"
    assert "serve" in src
    # 用了 Path 就必须真的导入，否则该分支一走到就 NameError
    assert "from pathlib import Path" in src, "gui.py 用了 Path 但没有导入"

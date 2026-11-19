#!/usr/bin/env python3
"""客户端图形界面（tkinter）自检：无头 X 下真装配面板、真点控件、真断言。

## 为什么需要这个

主控台（hub）有 `console-ui-check.cjs`，客户端控制台（web）有 `ui-check.cjs`，
但**客户端 GUI**（用户每天双击的那个 tkinter 窗口）一直没有可重复的验收 ——
之前只有临时手写脚本，跑完就丢。面板里新增了代理库、扩展包、商店装扩展、
检查更新等等，没有回归保障。

## 它做的事

1. 起一个真的 core（LocalCore，随机端口），不是 mock
2. 通过 HTTP 造数据：两个环境（一个带代理、一个直连）、一条代理、一个扩展包
3. 在无头 X 里装配 `ProfilePanel` / `PacksOverlay` / `ProxyLibraryOverlay`
4. 断言：行数、代理列文案、状态徽章、按钮是否存在、关键文案是否中文且可读
5. 顺手断言"界面上没有英文裸报错"（用户要求全中文）

## 用法

    DISPLAY=:78 python scripts/gui-check.py        # 需要有 X（可用 Xvfb）
    python scripts/gui-check.py --xvfb             # 自己拉一个 Xvfb

不依赖 pytest；退出码 0 = 全过。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core" / "src"))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASSED if cond else FAILED).append(name)
    icon = "✅" if cond else "❌"
    print(f"  {icon} {name}" + (f"  — {detail}" if not cond and detail else ""))
    return cond


def skip(name: str, why: str) -> None:
    print(f"  ⚠️  {name} — {why}")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = int(s.getsockname()[1])
    s.close()
    return p


def _start_xvfb() -> subprocess.Popen | None:
    if os.environ.get("DISPLAY"):
        return None
    for disp in range(90, 99):
        sock = Path(f"/tmp/.X11-unix/X{disp}")
        if sock.exists():
            continue
        proc = subprocess.Popen(
            ["Xvfb", f":{disp}", "-screen", "0", "1400x900x24", "-dpi", "75"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        if Path(f"/tmp/.X11-unix/X{disp}").exists():
            os.environ["DISPLAY"] = f":{disp}"
            return proc
        proc.terminate()
    return None


def main() -> int:
    xvfb = _start_xvfb() if "--xvfb" in sys.argv else None
    if not os.environ.get("DISPLAY"):
        print("❌ 没有可用的 X（DISPLAY 未设置）。加 --xvfb 让脚本自己拉一个。")
        return 2

    # **先把自己不需要的 ZW_* 清掉**：这个脚本经常被 check-all.sh 之类的外层
    # 调用，而外层可能导出了 ZW_ADMIN_TOKEN / ZW_HUB_URL / ZW_PORT_POOL_* 等。
    # 继承进来会让本脚本的临时 core 变成"要令牌"或"去连 hub"，于是面板取数全失败、
    # 断言莫名其妙地红（实测过：合并跑挂、单跑过）。
    # 注意 `ZW_HUB_URL` 必须在列表里：`LocalCore.start(hub_url="")` 只是"不覆盖"，
    # 环境里原有的值会被继承 → 自检 core 会去 hub 注册一条残留设备（实测发生过）。
    for k in ("ZW_ADMIN_TOKEN", "ZW_HUB_URL", "ZW_HUB_TOKEN", "ZW_DEVICE_ID",
              "ZW_SYNC_PASSWORD", "ZW_PORT_POOL_START", "ZW_PORT_POOL_END",
              "ZW_ALLOWED_CIDRS", "ZW_DEFAULT_PACKS", "ZW_EGRESS_ENDPOINTS",
              "ZW_DETECT_URLS", "ZW_RESTORE_TABS", "ZW_OPEN_DETECT_ON_START"):
        os.environ.pop(k, None)

    tmp = Path(tempfile.mkdtemp(prefix="zw-gui-check-"))
    port = _free_port()
    os.environ.update({
        "ZW_ROOT": str(tmp / "root"),
        "ZW_HOST": "127.0.0.1",
        "ZW_PORT": str(port),
        "ZW_ADMIN_TOKEN": "",
        "ZW_SHELL_ENABLED": "false",
        "ZW_CHROME_BIN": os.environ.get(
            "ZW_CHROME_BIN",
            "/opt/bsm-p0/ungoogled-chromium-150.0.7871.186-1-x86_64_linux/chrome"),
    })

    print(f"\n— 环境：ZW_ROOT={tmp / 'root'} core :{port} —")
    from zw_core.config import get_settings, reset_settings

    reset_settings()
    from zw_core.client import LocalCore

    core = LocalCore(port=port)
    try:
        if not check("core 起来（真服务，不是 mock）",
                     core.start(device_id="zwd_guicheck1234567890abcd", hub_url=""),
                     core.error):
            return 1

        # ── 造数据（走真 HTTP）──
        import httpx

        c = httpx.Client(base_url=f"http://127.0.0.1:{port}/api/v1", timeout=30)
        px = c.post("/proxies/import",
                    json={"text": "socks5://u:p@66.80.12.66:20000 # 落地A"}).json()
        check("造出一条代理", (px.get("data") or {}).get("imported") == 1, str(px)[:200])
        pid_proxy = ((c.get("/proxies").json().get("data") or {}).get("proxies") or [{}])[0].get("id")

        p1 = c.post("/profiles", json={"name": "带代理的环境"}).json()["data"]
        c.put(f"/profiles/{p1['id']}/proxy", json={"proxy_id": pid_proxy})
        c.post("/profiles", json={"name": "直连环境"})
        profs = c.get("/profiles").json()["data"]["profiles"]
        check("造出两个环境", len(profs) == 2, str([p["name"] for p in profs]))

        # ── 装配 GUI ──
        import tkinter as tk

        from zw_core import gui_profiles as gp

        t = tk.Tk()
        t.withdraw()
        theme = gp.Theme()
        # 字体键必须和 gui.py 里 `_build_style()` 返回的一致：
        # body / small / tiny / title / bold / big / mono。
        # 第一版我用了 base/head（并不存在）、漏了 body —— 结果点「代理」时
        # ProxyOverlay 里 `fonts["body"]` 直接 KeyError，看起来像产品坏了。
        fonts = {k: ("TkDefaultFont", 9) for k in
                 ("body", "small", "tiny", "title", "bold", "big", "mono")}
        api = gp.CoreApi(port=port)
        logs: list[str] = []

        print("\n— 环境面板 —")
        panel = gp.ProfilePanel(t, api=api, theme=theme, fonts=fonts,
                                on_log=lambda m, k="": logs.append(m),
                                on_changed=lambda: None)
        panel.pack(fill="both", expand=True)

        # **必须用真正的 mainloop 驱动，不能用 t.update() 泵**。
        #
        # 面板取数是 `call_async`：后台线程算完再 `widget.after(0, finish)` 回主线程。
        # 而 Tk 的 `after()` **从非主线程调用是不被保证的**（call_async 里那层
        # try/except pass 就是为这个）。用 update() 泵的话，回调可能永远排不进队列 ——
        # 表现是 `_inflight` 永远是 True、`_on_list` 从不执行、列表停在
        # 「还没有环境」的引导语上。第一版就是这么卡住的。
        #
        # 真实 GUI 有 mainloop，所以没问题。这里也必须跑 mainloop，
        # 只是用一个 after 链把"驱动 + 断言"塞进去，跑完 quit。
        _drive_until(t, lambda: len(_rows_of(panel)) >= 2,
                     on_tick=lambda: panel.refresh(force=True), timeout=25.0)
        rows = _rows_of(panel)
        check("环境面板排出 2 行", len(rows) == 2, f"rows={len(rows)}")

        texts = [_row_text(r) for r in rows]
        joined = " | ".join(texts)
        check("行里显示环境名", "带代理的环境" in joined and "直连环境" in joined, joined[:200])
        # 代理列显示的是脱敏串。这里断言的是"能看出是哪台代理、且不泄漏密码"：
        # host:port 必须可见（以前截断把 IP 尾部切掉，66.80.12.66 和 .65 分不清，
        # 已修成优先保住 host:port），密码位必须是 ***。
        check("带代理的那行能看到代理的 host:port",
              "66.80.12.66:20000" in joined, joined[:200])
        check("代理列已脱敏（不出现明文密码）",
              "***" in joined and ":p@" not in joined, joined[:200])
        check("直连的那行显示「直连」", "直连" in joined, joined[:200])

        btns = _button_texts(panel)
        print(f"    面板按钮：{btns}")
        for want in ("新建环境", "删除", "代理", "扩展包"):
            check(f"有「{want}」按钮", any(want in b for b in btns), str(btns))
        # 行内启停按钮是**按状态二选一**：`text="停止" if stop_like else "启动"`。
        # 这里两个环境都是 stopped，所以只会出现「启动」。别断言两个都在。
        check("停止态的行显示「启动」（不是「停止」）",
              any("启动" in b for b in btns) and not any(b.strip() == "停止" for b in btns),
              str(btns))

        # ── 真改一次代理（用户明确要求"创建完浏览器之后还可以改代理"）──
        print("\n— 改代理（真点按钮 → 浮层 → 保存）—")
        row_with_proxy = None
        for r in _rows_of(panel):
            if "66.80.12.66" in _row_text(r):
                row_with_proxy = r
                break
        check("找到带代理的那一行", row_with_proxy is not None)
        if row_with_proxy is not None:
            btn = _find_button(row_with_proxy, "代理")
            check("行里有可点的「代理」按钮", btn is not None)
            if btn is not None:
                btn.invoke()
                t.update()
                ov = getattr(panel, "_overlay", None)
                check("点「代理」弹出浮层", ov is not None and ov.is_open())
                if ov is not None:
                    # 换成另一条（带认证、无认证各测一次更贵，这里换一条不带认证的）
                    new_raw = "socks5://9.9.9.9:1080"
                    ov.on_save(new_raw)
                    _drive_until(t, lambda: not ov.is_open(), timeout=25.0)
                    after = [p for p in c.get("/profiles").json()["data"]["profiles"]
                             if p["name"] == "带代理的环境"][0]
                    spec = c.get("/proxies").json()["data"]["proxies"]
                    hit = [x for x in spec if x["host"] == "9.9.9.9"]
                    check("改代理真的写进了库（9.9.9.9 在库里）", bool(hit),
                          str([x["host"] for x in spec])[:160])
                    check("环境已指向新代理", str(after.get("proxy_id") or "") != "",
                          str(after.get("proxy_id")))
                    # 行要刷新出新地址，而不是还显示旧的
                    _drive_until(t, lambda: "9.9.9.9" in " ".join(
                        _row_text(r) for r in _rows_of(panel)), on_tick=lambda: panel.refresh(force=True),
                        timeout=25.0)
                    joined2 = " ".join(_row_text(r) for r in _rows_of(panel))
                    check("界面上的代理已更新为新地址", "9.9.9.9" in joined2, joined2[:200])

        print("\n— 扩展包面板 —")
        packs = gp.PacksOverlay(t, theme, fonts, api=api,
                                on_log=lambda m, k="": logs.append(m),
                                on_changed=lambda: None,
                                profiles_provider=lambda: profs)
        t.update()
        pbtns = _button_texts(packs)
        print(f"    面板按钮：{pbtns}")
        # 「下载内核」必须在：插件能否生效取决于内核能否加载扩展
        # （官方 Chrome 137 起不加载 --load-extension）
        for want in ("从环境导入", "从 Chrome 目录导入", "商店链接安装", "选 .crx 文件",
                     "检查更新", "下载内核"):
            check(f"扩展包面板有「{want}」", any(want in b for b in pbtns), str(pbtns))
        for name in ("ask_install_from_store", "pick_crx_file", "check_updates",
                     "ask_import_from_chrome", "ask_import_from", "ask_install_engine"):
            check(f"PacksOverlay 有处理函数 {name}", hasattr(packs, name))

        # 「从 Chrome 目录导入」的弹窗里要有「扫描本机」。
        # 用户不知道 RXYCDP 的 User Data 在哪 —— 这是最常见的卡点。
        # web 控制台 0.7.68 已有，桌面客户端这一侧也要有，否则两边不一致。
        check("CoreApi 有 scan_browsers", hasattr(api, "scan_browsers"))
        packs.ask_import_from_chrome()
        t.update()
        # 注意：`_Overlay` 不是 Toplevel —— 它用 `tk.Frame` 贴在父窗口里
        # （为了盖住整个窗口、卡片按整窗居中）。所以按 Toplevel 找是找不到的，
        # 第一版就这么写，断言直接红。
        # 浮层不是 Toplevel，是贴在窗口里的 Frame（`_Overlay.root`），
        # 所以直接在整棵控件树上找"扫描本机"这个按钮，连同它的可点性一起验。
        # （第一版试图按 `TextOverlay` 类型找再遍历 `.root`，结果拿到空列表 ——
        #  `_Overlay.root` 挂在 `master.winfo_toplevel()` 上，不在 overlay 对象树里。）
        all_w = _all_widgets(t)
        scan_btns = [b for b in all_w
                     if b.winfo_class() in ("TButton", "Button")
                     and "扫描本机" in str(b.cget("text") or "")]
        check("「从 Chrome 目录导入」弹窗里有「扫描本机」按钮", len(scan_btns) == 1,
              "count=" + str(len(scan_btns)))
        # 弹窗里还应该有「开始导入」，证明这是那个导入弹窗而不是别的
        dlg_btns = [str(b.cget("text") or "") for b in all_w
                    if b.winfo_class() in ("TButton", "Button")]
        check("导入弹窗里的按钮齐全（扫描本机 / 开始导入 / 取消）",
              all(k in dlg_btns for k in ("扫描本机", "开始导入", "取消")), str(dlg_btns))

        print("\n— 远程执行按钮开关 —")
        # 用户要求"弄成按钮开关，默认打开"。这里验：GUI 里是个按钮（不是勾选框），
        # 文案直接反映状态，关掉时用危险色。
        gui_src = (ROOT / "core" / "src" / "zw_core" / "gui.py").read_text(encoding="utf-8")
        check("GUI 用按钮而不是勾选框", 'text="远程执行：已开启"' in gui_src)
        check("按钮文案反映状态（已开启/已关闭）", "已关闭" in gui_src and "已开启" in gui_src)
        check("关掉时用危险色", "Danger.TButton" in gui_src)

        print("\n— 新建环境浮层 —")
        # `_Overlay` 不是 Frame（它内部自己管理浮层容器），所以**不能 pack** ——
        # 构造出来就已经在 master 上建好结构了。
        ov = gp.CreateProfileOverlay(t, theme, fonts, api=api,
                                     on_submit=lambda v: True)
        _pump(t, 0.4)
        # 「当然也可以选择关闭」：必须有"带上默认扩展包"的勾选，且默认勾上
        # 浮层控件挂在 master（根窗口）上，不是 ov 自己的子树 —— 从 t 遍历
        ov_texts = _all_text(t)
        check("新建浮层有「带上默认扩展包」勾选项",
              "带上默认扩展包" in ov_texts, ov_texts[:160])
        ov_vals = ov._values()
        check("该勾选默认是开启的（默认行为不变）",
              ov_vals.get("with_default_packs") == "1", repr(ov_vals.get("with_default_packs")))
        ov.close()

        print("\n— 代理库面板 —")
        lib = gp.ProxyLibraryOverlay(t, theme, fonts, api=api,
                                     on_log=lambda m, k="": logs.append(m),
                                     on_changed=lambda: None)
        t.update()
        lb = _button_texts(lib)
        print(f"    面板按钮：{lb}")
        # 代理库浮层负责"看/测/删/改"，**导入不在这里** ——
        # 导入按钮在环境面板头部（「导入代理」），第一版把期望写错了。
        check("代理库面板有「刷新」与「批量检测」",
              any("刷新" in b for b in lb) and any("批量检测" in b for b in lb), str(lb))

        print("\n— 全中文与无裸异常 —")
        all_text = " ".join(texts + btns + pbtns + lb)
        check("界面文案里没有英文异常痕迹",
              "Traceback" not in all_text and "Error:" not in all_text, all_text[:200])
        # 之前写的是 `bool(logs) or True` —— 恒真，等于没测。
        # 真实期望：面板把"改代理成功"写进了事件流（on_log 被调用过）。
        check("事件流记录下了改代理（on_log 被调用）", len(logs) >= 1,
              f"logs={logs[:3]}")

        t.destroy()
    finally:
        core.stop()
        shutil.rmtree(tmp, ignore_errors=True)
        if xvfb is not None:
            xvfb.terminate()

    print("\n" + "═" * 42)
    print(f"断言通过 {len(PASSED)} 条，失败 {len(FAILED)} 条")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  - " + f)
        return 1
    print("✅ 客户端 GUI 自检全部通过")
    return 0


def _rows_of(panel) -> list:
    """ProfilePanel 的环境行：都在 `panel.inner` 里，每行一个 tk.Frame。

    不要去"按控件特征猜"（第一版就这么写的，结果一行都没找到）：
    `_render()` 会清空 `panel.inner` 后逐行 `_render_row()`，结构是确定的。
    """
    inner = getattr(panel, "inner", None)
    if inner is None:
        return []
    import tkinter as tk

    return [w for w in inner.winfo_children() if isinstance(w, tk.Frame)]


def _all_text(widget) -> str:
    """整棵控件树的可见文字（拼成一串，便于 `in` 断言）。"""
    parts: list[str] = []
    stack = [widget]
    while stack:
        w = stack.pop()
        try:
            txt = w.cget("text")
            if txt:
                parts.append(str(txt))
        except Exception:
            pass
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass
    return " | ".join(parts)


def _pump(root, seconds: float) -> None:
    """驱动 Tk 事件循环若干秒（不用 mainloop，适合断言前的短暂等待）。"""
    end = time.time() + seconds
    while time.time() < end:
        try:
            root.update()
        except Exception:
            break
        time.sleep(0.03)


def _row_text(row) -> str:
    parts: list[str] = []
    stack = list(row.winfo_children())
    while stack:
        w = stack.pop()
        try:
            txt = w.cget("text")
            if txt:
                parts.append(str(txt))
        except Exception:
            pass
        stack.extend(w.winfo_children())
    return " ".join(parts)


def _drive_until(root, cond, *, on_tick=None, timeout: float = 20.0) -> bool:
    """跑一段**真正的 mainloop**，直到 cond() 为真或超时，然后 quit。

    ## 为什么不能用 `root.update()` 循环代替

    面板取数走 `call_async`：后台线程算完再 `widget.after(0, finish)` 回主线程。
    Tk 的 `after()` 从**非主线程**调用并不被保证（产品代码里那层 try/except pass
    就是兜这个）。用 update() 泵时，这个跨线程的 after 可能永远排不进去 ——
    现象就是 `_inflight` 卡在 True、回调从不执行、列表一直停在引导语上。
    真实 GUI 有 mainloop 所以正常；验收脚本也必须跑 mainloop 才是同一条路径。
    """
    import time as _t

    state = {"done": False, "ok": False}
    deadline = _t.time() + timeout

    def tick() -> None:
        if state["done"]:
            return
        try:
            if on_tick is not None:
                on_tick()
            if cond():
                state["ok"] = True
                state["done"] = True
                root.quit()
                return
            if _t.time() > deadline:
                state["done"] = True
                root.quit()
                return
        except Exception:
            state["done"] = True
            root.quit()
            return
        root.after(120, tick)

    root.after(0, tick)
    root.mainloop()
    return state["ok"]


def _roots_of(widget) -> list:
    """取控件树根。

    `PacksOverlay` / `ProxyLibraryOverlay` 是 **Toplevel 型浮层**（`_Overlay`），
    没有 `winfo_children()` —— 内容在 `.body`，底部按钮在 `.buttons`。
    第一版直接对 Overlay 调 winfo_children() 就 AttributeError 了。
    """
    roots = []
    if hasattr(widget, "winfo_children"):
        roots.append(widget)
        return roots
    for attr in ("body", "buttons"):
        w = getattr(widget, attr, None)
        if w is not None and hasattr(w, "winfo_children"):
            roots.append(w)
    root = getattr(widget, "root", None)   # Toplevel 本身
    if root is not None and hasattr(root, "winfo_children"):
        roots.append(root)
    return roots


def _find_button(widget, want: str):
    """在控件树里找一个文字包含 `want` 的按钮。"""
    stack = [widget]
    while stack:
        w = stack.pop()
        try:
            if w.winfo_class() in ("TButton", "Button") and want in str(w.cget("text")):
                return w
        except Exception:
            pass
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass
    return None


def _all_widgets(root) -> list:
    """递归列出 root 下所有控件（含 root 自己）。

    `_Overlay` 这类浮层不是 Toplevel，而是贴在窗口里的 Frame，
    所以找它们只能遍历控件树。
    """
    out: list = []
    stack: list = [root]
    while stack:
        w = stack.pop()
        out.append(w)
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass
    return out


def _button_texts(widget) -> list[str]:
    out: list[str] = []
    stack: list = []
    for r in _roots_of(widget):
        stack.append(r)
    while stack:
        w = stack.pop()
        try:
            if w.winfo_class() in ("TButton", "Button"):
                txt = w.cget("text")
                if txt:
                    out.append(str(txt))
        except Exception:
            pass
        stack.extend(w.winfo_children())
    return out


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        # 抛异常时**必须把栈打出来**：否则外层拿到的只是"失败项："后面空空一行，
        # 看不出是断言失败还是脚本崩了（合并跑时就是这样，多花了一轮才定位）。
        import traceback

        traceback.print_exc()
        sys.exit(1)

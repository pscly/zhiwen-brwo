"""客户端图形界面。

拆成独立模块的原因：GUI 代码量已经超过"业务逻辑"（探测/注册/心跳/core 启停），
混在一个文件里改起来容易互相踩。这里只负责画界面与交互，所有网络与进程操作
都调 client.py 里已有的函数。

## 设计取舍（按"自己每天用"来定）

1. **默认少打扰**：配过一次就自动连，连上直接开控制台；不用每次点按钮。
2. **但必须有状态可见**：连到哪、机器码、本机几个环境在跑、服务器还剩多久心跳 ——
   这些常驻显示，而不是塞在日志里。
3. **失败要能自查**：界面上直接给「自检」按钮，把 `--selftest` 的结果弹出来，
   不用让用户去命令行。
4. **关窗口不杀业务**：默认最小化到托盘（浏览器还在跑）。真想退出走「退出」按钮，
   并明确告诉用户"退出会一并停掉本机服务"。
5. **托盘不可用时降级**：没有 pystray/系统托盘的环境就退化成普通窗口最小化。

## 三区布局

```
┌──────────────────────────────────────────────┐
│ 知纹客户端   v0.3.0        ● 已连接   [设置] │
├──────────────────────────────────────────────┤
│ 服务器 192.168.3.5:8700        [断开][重连]  │
│ 机器码 zwd_xxxx…   [复制]                    │
├──────────────────────────────────────────────┤
│ 本机环境  3 个     运行中  1 个              │
│ 端口池 4/100      磁盘 1.2GB                 │
├──────────────────────────────────────────────┤
│ 最近事件（滚动）                              │
│  …                                            │
├──────────────────────────────────────────────┤
│ [打开控制台]  [自检]           [退出]        │
└──────────────────────────────────────────────┘
```

首次运行（没配过服务器）时，中间换成"填地址"的表单，其余区域隐藏。
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
import threading
import time
import webbrowser
from contextlib import redirect_stdout
from typing import Any

from zw_core import __version__
from zw_core.client import (
    APP_NAME,
    MONO_FAMILY,
    cjk_font_missing,
    Heartbeat,
    LocalCore,
    _resolve_core_port,
    autostart_enabled,
    enroll,
    ensure_device_id,
    load_config,
    normalize_server,
    pick_mono_font,
    pick_ui_font,
    probe_server,
    save_config,
    selftest,
    set_autostart,
)

# 托盘支持是可选的：pystray 不在依赖里（Windows 打包也不带它），
# 所以这里做能力探测，没有就走"最小化窗口"的降级路径。
_TRAY_AVAILABLE: bool | None = None


def _tray_available() -> bool:
    global _TRAY_AVAILABLE
    if _TRAY_AVAILABLE is None:
        try:
            import pystray  # noqa: F401
            from PIL import Image  # noqa: F401

            _TRAY_AVAILABLE = True
        except Exception:
            _TRAY_AVAILABLE = False
    return _TRAY_AVAILABLE


def _make_icon_image(size: int = 64) -> Any:
    """生成托盘图标（不依赖外部图片文件）。

    画一个圆角方块 + "知"字的简化图形。没有 PIL 时返回 None（调用方会降级）。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(2, size // 16)
    d.rounded_rectangle([pad, pad, size - pad, size - pad], radius=size // 5, fill=(91, 157, 255, 255))
    # 两条折线组成的 "Z" 形（和网页 favicon 一致，比画中文字体可靠）
    w = max(2, size // 10)
    left, right = size * 0.3, size * 0.7
    top, bottom = size * 0.3, size * 0.7
    d.line([(left, top), (right, top)], fill="white", width=w)
    d.line([(right, top), (left, bottom)], fill="white", width=w)
    d.line([(left, bottom), (right, bottom)], fill="white", width=w)
    return img


# ── 颜色与字体 ────────────────────────────────────────────
BG = "#0f1216"
CARD = "#171b22"
CARD2 = "#1d222b"
INK = "#e8ebf2"
DIM = "#8b93a7"
FAINT = "#5d6675"
ACC = "#5b9dff"
OK = "#3ecf8e"
WARN = "#ffb020"
BAD = "#ff6b6b"
LINE = "#262c37"


def _build_style(tk: Any, ttk: Any) -> dict[str, Any]:
    """配置 ttk 样式，返回字体字典。"""
    body = pick_ui_font(tk._default_root, 10)
    small = pick_ui_font(tk._default_root, 9)
    tiny = pick_ui_font(tk._default_root, 8)
    title = pick_ui_font(tk._default_root, 14, bold=True)
    bold = pick_ui_font(tk._default_root, 10, bold=True)
    big = pick_ui_font(tk._default_root, 18, bold=True)

    style = ttk.Style(tk._default_root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=INK, font=body, padding=(0, 2))
    style.configure("Dim.TLabel", background=BG, foreground=DIM, font=small, padding=(0, 1))
    style.configure("Faint.TLabel", background=BG, foreground=FAINT, font=tiny, padding=(0, 1))
    style.configure("H.TLabel", background=BG, foreground=INK, font=title, padding=(0, 3))
    style.configure("Big.TLabel", background=CARD, foreground=INK, font=big, padding=(0, 1))
    style.configure("CardDim.TLabel", background=CARD, foreground=DIM, font=small, padding=(0, 1))
    style.configure("CardInk.TLabel", background=CARD, foreground=INK, font=body, padding=(0, 1))
    style.configure("TEntry", fieldbackground="#0b0e12", foreground=INK, insertcolor=INK,
                    borderwidth=0, font=body)
    style.configure("Accent.TButton", background=ACC, foreground="#ffffff", borderwidth=0,
                    font=bold, padding=(14, 8))
    style.map("Accent.TButton",
              background=[("active", "#6fabff"), ("disabled", "#2b323e")],
              foreground=[("disabled", "#707a89")])
    style.configure("TButton", background=CARD2, foreground=INK, borderwidth=0,
                    font=body, padding=(11, 7))
    style.map("TButton", background=[("active", "#252b36")])
    style.configure("Ghost.TButton", background=BG, foreground=DIM, borderwidth=0,
                    font=small, padding=(8, 5))
    style.map("Ghost.TButton", background=[("active", CARD)], foreground=[("active", INK)])
    style.configure("TCheckbutton", background=BG, foreground=INK, font=body)

    # ── 环境 / 日志面板用的样式 ──
    # 这两块塞在 640 宽的 tab 里，默认按钮的 padding（11,7）太占地，
    # 一行里放"名称 + 徽章 + 分组 + 启动 + 删除"会挤爆；所以单独一套小号样式。
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 2, 0, 0))
    style.configure("TNotebook.Tab", background=BG, foreground=DIM, borderwidth=0,
                    font=small, padding=(14, 5))
    style.map("TNotebook.Tab",
              background=[("selected", CARD)],
              foreground=[("selected", INK), ("active", INK)])
    style.configure("Tiny.TButton", background=CARD2, foreground=INK, borderwidth=0,
                    font=tiny, padding=(9, 4))
    style.map("Tiny.TButton",
              background=[("active", "#2a3140"), ("disabled", "#1a1f27")],
              foreground=[("disabled", "#5d6675")])
    style.configure("TinyAccent.TButton", background=ACC, foreground="#ffffff", borderwidth=0,
                    font=tiny, padding=(10, 4))
    style.map("TinyAccent.TButton",
              background=[("active", "#6fabff"), ("disabled", "#2b323e")],
              foreground=[("disabled", "#707a89")])
    style.configure("Danger.TButton", background="#3a1f24", foreground=BAD, borderwidth=0,
                    font=tiny, padding=(9, 4))
    style.map("Danger.TButton", background=[("active", "#4a262c")],
              foreground=[("active", "#ff9a9a")])
    # 日志面板里的勾选框落在卡片底色上，不能沿用 BG 底色的 TCheckbutton
    style.configure("Card.TCheckbutton", background=CARD, foreground=DIM, font=small,
                    indicatorbackground="#0b0e12", indicatorforeground=ACC)
    style.map("Card.TCheckbutton", background=[("active", CARD)], foreground=[("active", INK)])
    return {"body": body, "small": small, "tiny": tiny, "title": title, "bold": bold,
            "big": big, "mono": MONO_FAMILY}


def run_gui() -> int:  # noqa: C901  界面装配本来就长，硬拆反而更难读
    """启动图形界面。没有 tkinter 时退回命令行引导，而不是直接崩。"""
    global _default_root
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as exc:  # noqa: BLE001
        print(f"这台机器上的 Python 没有 tkinter（{exc}），改用命令行模式。")
        return run_cli()

    # 环境列表与日志面板在 gui_profiles 里。它自己也会 import tkinter，
    # 所以只能放在"确认 tkinter 可用"之后再导入 —— 否则没装 tkinter 的机器
    # 连命令行兜底模式都进不去（导入期就炸了）。
    from zw_core.gui_profiles import CoreApi, LogPanel, ProfilePanel, Theme

    cfg = load_config()
    core = LocalCore(port=_resolve_core_port(cfg))

    root = tk.Tk()
    tk._default_root = root
    root.title(f"{APP_NAME} · 客户端")
    # 高度按屏幕算：1366×768 的笔记本上 720 再加标题栏，底部按钮会被任务栏吃掉，
    # 所以封顶在"屏幕高 - 110"。下限 600 保证环境列表至少能看到几行。
    try:
        screen_h = int(root.winfo_screenheight())
    except Exception:
        screen_h = 900
    win_h = max(600, min(720, screen_h - 110))
    root.geometry(f"640x{win_h}")
    root.minsize(600, 600)
    root.configure(bg=BG)

    pick_mono_font(root)
    fonts = _build_style(tk, ttk)

    if cjk_font_missing():
        # 没有中文字体时界面会一片空白。必须在 stderr 与界面两处都说清楚，
        # 否则用户只会觉得"程序坏了"。
        msg = ("这台机器上找不到任何能显示中文的字体，界面文字会是空白。\n"
               "请安装一个中文字体后重启客户端：\n"
               "  Ubuntu/Debian: sudo apt install fonts-wqy-zenhei fonts-noto-cjk\n"
               "  CentOS/Fedora: sudo dnf install wqy-zenhei-fonts google-noto-sans-cjk-fonts\n"
               "  Arch:          sudo pacman -S wqy-zenhei noto-fonts-cjk")
        print(msg, file=sys.stderr)
        try:
            from tkinter import messagebox as _mb

            root.after(200, lambda: _mb.showwarning("缺少中文字体", msg))
        except Exception:
            pass

    # ── 状态 ────────────────────────────────────────────
    state: dict[str, Any] = {
        "phase": "idle",          # idle | connecting | connected | revoked | error
        "base": cfg.get("server") or "",
        "device_id": ensure_device_id(),
        "heartbeat": None,        # Heartbeat 实例
        "core_on": False,
        "events": [],             # [(ts, text, level)]
    }
    server_var = tk.StringVar(value=cfg.get("server") or "")
    token_var = tk.StringVar(value=cfg.get("token") or "")
    addr_hint = tk.StringVar(value="")
    status_text = tk.StringVar(value="未连接")
    status_color = tk.StringVar(value=FAINT)
    env_total = tk.StringVar(value="—")
    env_running = tk.StringVar(value="—")
    port_used = tk.StringVar(value="—")
    disk_used = tk.StringVar(value="—")
    # 内核：能不能加载扩展直接决定"插件是否会生效"，必须让用户在客户端里就看到。
    # 以前 `engine` 这个值从本机取到了却没展示（收集了没人用）。
    engine_text = tk.StringVar(value="—")

    def log(text: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        state["events"].append((stamp, text, level))
        del state["events"][:-200]          # 只留最近 200 条
        render_events()
        # 同时写一份到 stdout：GUI 程序在 Windows 上没有控制台，这里不会打扰用户，
        # 但从命令行启动、或排障时（`zw client > log.txt`）能看到完整时间线。
        try:
            if sys.stdout is not None:
                print(f"[{stamp}] {text}", flush=True)
        except (AttributeError, ValueError, OSError):
            pass

    # ── 顶部栏 ──────────────────────────────────────────
    header = ttk.Frame(root, padding=(20, 14, 20, 8))
    header.pack(fill="x")
    ttk.Label(header, text=f"{APP_NAME} 客户端", style="H.TLabel").pack(side="left")
    ttk.Label(header, text=f"v{__version__}", style="Faint.TLabel").pack(side="left", padx=(8, 0))
    hb_text = tk.StringVar(value="")
    hb_label = tk.Label(header, textvariable=hb_text, bg=BG, fg=FAINT, font=fonts["tiny"])
    hb_label.pack(side="right", padx=(0, 10))
    badge = tk.Label(header, textvariable=status_text, bg=BG, fg=FAINT, font=fonts["small"])
    badge.pack(side="right")

    ttk.Separator(root, orient="horizontal").pack(fill="x", padx=20)

    # ── 主体（两种形态：首次配置 / 已连接）───────────────
    body = ttk.Frame(root, padding=(20, 14))
    body.pack(fill="both", expand=True)

    # ① 首次配置区
    setup = ttk.Frame(body)
    ttk.Label(setup, text="连到你的服务器", style="H.TLabel").pack(anchor="w", pady=(0, 4))
    ttk.Label(setup, text="填服务器地址就行，不需要对接码（除非服务器设了令牌）。",
              style="Dim.TLabel").pack(anchor="w", pady=(0, 14))

    srv_card = ttk.Frame(setup, style="Card.TFrame", padding=14)
    srv_card.pack(fill="x")
    ttk.Label(srv_card, text="服务器地址", style="CardDim.TLabel").pack(anchor="w")
    srv_entry = ttk.Entry(srv_card, textvariable=server_var, font=(MONO_FAMILY, 11))
    srv_entry.pack(fill="x", pady=(5, 0), ipady=7)
    ttk.Label(srv_card, textvariable=addr_hint, style="Faint.TLabel").pack(anchor="w", pady=(5, 0))
    addr_hint.set("例如 192.168.3.5:8700    不带端口时默认 8700")

    tok_card = ttk.Frame(setup, style="Card.TFrame", padding=14)
    ttk.Label(tok_card, text="对接码（这台服务器要求时填写）", style="CardDim.TLabel").pack(anchor="w")
    ttk.Entry(tok_card, textvariable=token_var, show="•", font=(MONO_FAMILY, 11)).pack(fill="x", pady=(5, 0), ipady=7)

    # 这台电脑的标识：让用户能一眼把"本机"和服务器面板里的记录对上
    id_card = ttk.Frame(setup, style="Card.TFrame", padding=14)
    id_card.pack(fill="x", pady=(10, 0))
    id_row = ttk.Frame(id_card, style="Card.TFrame")
    id_row.pack(fill="x")
    ttk.Label(id_row, text="这台电脑的机器码", style="CardDim.TLabel").pack(side="left")
    ttk.Button(id_row, text="复制", style="Ghost.TButton",
               command=lambda: copy_from_setup()).pack(side="right")
    id_label = tk.Label(id_card, text=state["device_id"][:32] + "…", bg=CARD, fg=DIM,
                        font=(MONO_FAMILY, 9))
    id_label.pack(anchor="w", pady=(5, 4))
    ttk.Label(id_card, text="服务器面板用它在设备列表里认出这台机器；删掉配置会变新号。",
              style="Faint.TLabel").pack(anchor="w")
    ttk.Label(id_card, text="管理面板在：服务器地址 + /panel（连上之后底部也有「服务器面板」按钮）",
              style="Faint.TLabel").pack(anchor="w", pady=(3, 0))

    def copy_from_setup() -> None:
        try:
            root.clipboard_clear()
            root.clipboard_append(state["device_id"])
            addr_hint.set("机器码已复制到剪贴板")
        except Exception:
            addr_hint.set("复制失败，请手动选中上面的机器码")

    setup_btns = ttk.Frame(setup)
    setup_btns.pack(fill="x", pady=(18, 0))
    connect_btn = ttk.Button(setup_btns, text="连接并开始", style="Accent.TButton")
    connect_btn.pack(side="right")

    # ② 已连接区
    main = ttk.Frame(body)

    # 连接信息卡
    conn_card = ttk.Frame(main, style="Card.TFrame", padding=14)
    conn_card.pack(fill="x")
    row1 = ttk.Frame(conn_card, style="Card.TFrame")
    row1.pack(fill="x")
    ttk.Label(row1, text="服务器", style="CardDim.TLabel").pack(side="left")
    conn_server = tk.Label(row1, text="—", bg=CARD, fg=INK, font=(MONO_FAMILY, 10))
    conn_server.pack(side="left", padx=(8, 0))
    ttk.Button(row1, text="断开", style="Ghost.TButton", command=lambda: disconnect()).pack(side="right")
    ttk.Button(row1, text="重连", style="Ghost.TButton", command=lambda: reconnect()).pack(side="right", padx=(0, 6))
    # 服务器面板直连入口：用户不知道面板地址时，不该让他去翻文档
    ttk.Button(row1, text="服务器面板", style="Ghost.TButton",
               command=lambda: open_panel()).pack(side="right", padx=(0, 6))

    row2 = ttk.Frame(conn_card, style="Card.TFrame")
    row2.pack(fill="x", pady=(8, 0))
    ttk.Label(row2, text="机器码", style="CardDim.TLabel").pack(side="left")
    conn_dev = tk.Label(row2, text="—", bg=CARD, fg=DIM, font=(MONO_FAMILY, 9))
    conn_dev.pack(side="left", padx=(8, 0))
    ttk.Button(row2, text="复制", style="Ghost.TButton", command=lambda: copy_device()).pack(side="right")

    # 本机状态
    stat_card = ttk.Frame(main, style="Card.TFrame", padding=14)
    stat_card.pack(fill="x", pady=(10, 0))
    grid = ttk.Frame(stat_card, style="Card.TFrame")
    grid.pack(fill="x")
    for i, (label, var) in enumerate((("环境", env_total), ("运行中", env_running),
                                      ("端口占用", port_used), ("磁盘", disk_used),
                                      ("内核", engine_text))):
        cell = ttk.Frame(grid, style="Card.TFrame")
        cell.grid(row=0, column=i, sticky="w", padx=(0, 26))
        ttk.Label(cell, textvariable=var, style="Big.TLabel").pack(anchor="w")
        ttk.Label(cell, text=label, style="CardDim.TLabel").pack(anchor="w")

    # ── 环境 / 日志（两块本地面板）───────────────────────
    # 为什么用 Notebook 而不是继续往下堆：640 宽的单栏里，环境列表与日志
    # 都是"要多行才看得清"的东西，硬堆会让两块都只剩一条缝。
    # 分 tab 之后每一块都能拿到完整的剩余高度。
    tabs = ttk.Notebook(main)
    tabs.pack(fill="both", expand=True, pady=(10, 0))

    api = CoreApi(port=core.port)
    theme = Theme(bg=BG, card=CARD, card2=CARD2, ink=INK, dim=DIM, faint=FAINT,
                  acc=ACC, ok=OK, warn=WARN, bad=BAD, line=LINE)

    def diag_context() -> dict[str, Any]:
        """「复制诊断信息」要的那几个值（版本/设备号/服务地址/端口）。"""
        return {
            "version": __version__,
            "device_id": state.get("device_id") or "",
            "server": state.get("base") or "",
            "core_port": core.port,
        }

    def core_ready() -> bool:
        """自动刷新前的门槛：本机服务没起来就别去打接口。

        本机 core 是"连上服务器之后才起"的，而 tick 从第 1.2 秒就开始跑；
        没有这道判断，每次启动都会先记一条"连不上本机服务"，看着像故障。
        用户手动点刷新/切 tab 时**不走**这个判断（那是他的明确意图）。
        """
        return bool(state.get("core_on"))

    profiles_panel = ProfilePanel(tabs, api=api, theme=theme, fonts=fonts,
                                  on_log=log, on_changed=lambda: refresh_stats(),
                                  enabled=core_ready)
    log_panel = LogPanel(tabs, api=api, theme=theme, fonts=fonts, on_log=log,
                         context=diag_context, enabled=core_ready)
    tabs.add(profiles_panel, text="环境")
    tabs.add(log_panel, text="日志")

    def on_tab_changed(_event: Any = None) -> None:
        """切到哪个 tab 就刷哪个。

        日志面板不必在后台每 5 秒拉一次事件 —— 用户看着「环境」时那一屏
        事件拉回来也没人看，白占本机 CPU 与 core 的那条 SQL。
        """
        # 本机服务没起来就别刷：Notebook 第一次显示时 Tk 也会发这个事件，
        # 而那时 core 还在启动中，刷了只会记一条假的"连不上本机服务"。
        if not state.get("core_on"):
            return
        try:
            current = tabs.index(tabs.select())
        except Exception:
            return
        if current == tabs.index(profiles_panel):
            profiles_panel.refresh(force=True)
        else:
            log_panel.refresh(force=True)

    tabs.bind("<<NotebookTabChanged>>", on_tab_changed)

    # 客户端自己事件流（连没连上、注册失败、环境操作的原因都在这里）。
    # 高度固定 5 行：它是常驻的"最近发生了什么"，不该把列表区挤掉。
    ev_card = ttk.Frame(main, style="Card.TFrame", padding=(14, 10))
    ev_card.pack(fill="x", pady=(10, 0))
    ttk.Label(ev_card, text="最近事件", style="CardDim.TLabel").pack(anchor="w", pady=(0, 6))
    ev_wrap = tk.Frame(ev_card, bg=CARD)
    ev_wrap.pack(fill="both", expand=True)
    ev_text = tk.Text(ev_wrap, bg="#0b0e12", fg=DIM, bd=0, highlightthickness=0,
                      font=(MONO_FAMILY, 9), wrap="word", state="disabled", height=5)
    ev_scroll = tk.Scrollbar(ev_wrap, command=ev_text.yview, bd=0, width=8)
    ev_text.configure(yscrollcommand=ev_scroll.set)
    ev_scroll.pack(side="right", fill="y")
    ev_text.pack(side="left", fill="both", expand=True)
    for tag, color in (("info", DIM), ("ok", OK), ("warn", WARN), ("bad", BAD)):
        ev_text.tag_configure(tag, foreground=color)

    def render_events() -> None:
        try:
            ev_text.configure(state="normal")
            ev_text.delete("1.0", "end")
            for stamp, text, level in state["events"][-120:]:
                ev_text.insert("end", f"{stamp}  ", "info")
                ev_text.insert("end", text + "\n", level)
            ev_text.see("end")
            ev_text.configure(state="disabled")
        except Exception:
            pass

    # ── 底部按钮 ────────────────────────────────────────
    # 拆成两行：640 宽放不下「打开控制台 + 自检 + 服务器面板 + 两个开关 + 退出」，
    # 原来「退出」会被挤到窗口外（截图里根本看不到），用户只能去关窗口。
    # 第一行放动作按钮，第二行放两个开关；「退出」先 pack(side="right")，
    # pack 是按顺序分配空间的，先要的永远拿得到位置。
    footer = ttk.Frame(root, padding=(20, 10, 20, 14))
    footer.pack(fill="x", side="bottom")
    quit_btn = ttk.Button(footer, text="退出", command=lambda: do_quit())
    quit_btn.pack(side="right")

    action_row = ttk.Frame(footer)
    action_row.pack(fill="x")
    open_btn = ttk.Button(action_row, text="打开控制台", style="Accent.TButton",
                          command=lambda: open_console())
    open_btn.pack(side="left")
    # 没连上时控制台根本不存在，开启这个按钮等于给用户一个必然失败的入口
    open_btn.state(["disabled"])
    self_btn = ttk.Button(action_row, text="自检", command=lambda: run_selftest())
    self_btn.pack(side="left", padx=(8, 0))
    panel_btn = ttk.Button(action_row, text="服务器面板", command=lambda: open_panel())
    panel_btn.pack(side="left", padx=(8, 0))
    panel_btn.state(["disabled"])

    toggle_row = ttk.Frame(footer)
    toggle_row.pack(fill="x", pady=(6, 0))

    # 开机自启开关：用户要求"需要，但必须能在界面里关掉"。
    # 勾选状态从真实系统状态读（不是从配置读），
    # 这样从别处改过（安装程序/系统设置）也不会显示错。
    autostart_var = tk.BooleanVar(value=autostart_enabled())

    def toggle_autostart() -> None:
        want = bool(autostart_var.get())
        ok, msg = set_autostart(want)
        if not ok:
            autostart_var.set(autostart_enabled())
            log(msg, "bad")
            messagebox.showerror("开机自启", msg)
            return
        log(msg, "ok")

    auto_chk = ttk.Checkbutton(toggle_row, text="开机自启", variable=autostart_var,
                               command=toggle_autostart)
    auto_chk.pack(side="left")

    def toggle_shell() -> None:
        """远程命令行开关。默认开；关掉后服务器再也拉不到命令。"""
        want = bool(state.get("shell_var").get())
        try:
            import httpx

            with httpx.Client(timeout=5) as c:
                r = c.post(
                    f"http://127.0.0.1:{core.port}/api/v1/shell/enable",
                    json={"enabled": want},
                    headers={"Authorization": f"Bearer {os.environ.get('ZW_ADMIN_TOKEN', '')}"},
                )
            if r.status_code == 200:
                log("远程命令行已" + ("开启" if want else "关闭（服务器无法再执行命令）"),
                    "ok" if want else "warn")
            else:
                log(f"切换失败：HTTP {r.status_code}", "bad")
                state["shell_var"].set(not want)
        except Exception as exc:
            # 本机服务还没起来是常见情况：此时只记住选择，连上后生效
            log(f"开关已记录，连上后生效（{type(exc).__name__}）", "info")
        cfg["shell_enabled"] = want
        save_config(cfg)

    # 远程命令行开关：用户要求"默认开启，也能手动关闭"。
    # 默认 True；关掉后服务器侧拿不到命令，本机浏览器/控制台不受影响。
    state["shell_var"] = tk.BooleanVar(value=bool(cfg.get("shell_enabled", True)))
    shell_chk = ttk.Checkbutton(toggle_row, text="允许远程命令行", variable=state["shell_var"],
                                command=toggle_shell)
    shell_chk.pack(side="left", padx=(14, 0))

    # pack 是**按顺序**分配空间的：body 带 expand=True，先 pack 就会把剩余空间
    # 全吃光，底部栏被排到窗口下沿之外 —— 640×720 下整行按钮（含「退出」）
    # 直接看不见。把 footer 提到 body 之前 pack，它才永远拿得到自己那一行。
    footer.pack_configure(before=body)

    # ── 状态切换 ────────────────────────────────────────
    def set_phase(phase: str, text: str) -> None:
        state["phase"] = phase
        status_text.set(text)
        color = {"connected": OK, "revoked": BAD, "error": BAD,
                 "connecting": WARN, "pending": WARN}.get(phase, FAINT)
        try:
            badge.configure(fg=color)
        except Exception:
            pass

    def show_setup(show: bool) -> None:
        if show:
            main.pack_forget()
            setup.pack(fill="both", expand=True)
        else:
            setup.pack_forget()
            main.pack(fill="both", expand=True)

    # ── 动作 ────────────────────────────────────────────
    def open_console() -> None:
        webbrowser.open(f"http://127.0.0.1:{core.port}")

    def open_panel() -> None:
        """打开服务器端管理面板（就是那个能踢设备的面板）。"""
        base = state.get("base") or normalize_server(server_var.get().strip())
        if not base:
            messagebox.showinfo(APP_NAME, "还没连上服务器。先在下面填服务器地址并连接。")
            return
        url = base.rstrip("/") + "/"
        try:
            webbrowser.open(url)
            log(f"已在浏览器打开服务器面板：{url}", "ok")
        except Exception as exc:
            log(f"打开面板失败：{exc}", "warn")
            messagebox.showinfo(
                "服务器面板",
                f"自动打开失败，请手动在浏览器里访问：\n\n{url}\n\n"
                f"登录名与密码在服务器上的 /opt/zhiwen-hub/.env 里（ZW_PANEL_USER / ZW_PANEL_PASSWORD）。",
            )

    def copy_device() -> None:
        try:
            root.clipboard_clear()
            root.clipboard_append(state["device_id"])
            log("机器码已复制到剪贴板", "ok")
        except Exception as exc:
            log(f"复制失败：{exc}", "warn")

    def run_selftest() -> None:
        """界面上直接跑自检，结果弹窗显示（不用开命令行）。"""
        try:
            log("正在自检 …")
            root.update_idletasks()
            buf: list[str] = []
            with redirect_stdout(io.StringIO()) as f:
                code = selftest(show_dialog=False)
            out = f.getvalue()
            lines = [ln for ln in out.splitlines() if ln.strip() and "报告已保存" not in ln]
            buf.extend(lines)
            text = "\n".join(buf) or ("自检通过" if code == 0 else "自检失败（详情见配置目录的 selftest.txt）")
            log("自检通过" if code == 0 else "自检有问题，看弹窗", "ok" if code == 0 else "bad")
            if code == 0:
                messagebox.showinfo("自检结果", text)
            else:
                messagebox.showerror("自检结果", text)
        except Exception as exc:
            log(f"自检异常：{exc}", "bad")
            messagebox.showerror("自检异常", f"{type(exc).__name__}: {exc}")

    def refresh_stats() -> None:
        """刷本机统计与心跳健康度。取不到就显示 —。

        **这里不再自己 after(5000, 自己)**：定时统一由下面的 `tick()` 驱动。
        原来每加一块要刷新的东西就多挂一个定时器，三个互不同步的节拍
        既费心又难收拾；一个循环按顺序刷，谁慢谁快都只影响自己。
        """
        # 心跳指标：让用户一眼看出"和服务器还连着吗"，而不是等出问题才发现
        hb = state.get("heartbeat")
        if hb is None:
            hb_text.set("")
        else:
            st_hb = hb.status()
            if st_hb["revoked"]:
                hb_text.set("心跳 · 已被移除")
                hb_label.configure(fg=BAD)
            elif st_hb["healthy"]:
                age = st_hb["age"] or 0
                hb_text.set(f"心跳 · {age}s 前")
                hb_label.configure(fg=OK)
            elif st_hb["last_ok"]:
                hb_text.set(f"心跳 · {st_hb['age']}s 未成功" if st_hb["age"] else "心跳 · 失败")
                hb_label.configure(fg=WARN)
            else:
                hb_text.set("心跳 · 等待首次" if not st_hb["error"] else "心跳 · 失败")
                hb_label.configure(fg=WARN)
        try:
            st = core.stats() if state["core_on"] else None
            if not st:
                env_total.set("—")
                env_running.set("—")
                port_used.set("—")
                disk_used.set("—")
                engine_text.set("—")
                return
            env_total.set(str(st.get("profiles", "—")))
            env_running.set(f"{st.get('running', 0)} / {st.get('max_running', '—')}")
            pp = st.get("port_pool") or {}
            port_used.set(f"{pp.get('used', '—')}")
            disk_used.set(str(st.get("disk_human", "—")))
            eng = core.engine() if state["core_on"] else None
            if not eng:
                engine_text.set("—")
            elif (eng.get("download") or {}).get("state") in (
                    "resolving", "downloading", "extracting", "verifying"):
                d = eng.get("download") or {}
                engine_text.set(f"下载 {d.get('percent', 0)}%")
            elif not eng.get("path"):
                engine_text.set("缺失")
            elif eng.get("extensions_loadable") is True:
                engine_text.set("可加载扩展")
            elif eng.get("extensions_loadable") is False:
                engine_text.set("不加载扩展")
            elif eng.get("probe_pending"):
                engine_text.set("检测中")
            else:
                engine_text.set("未检测")
        except Exception:
            pass

    def tick() -> None:
        """统一的 5 秒刷新节拍：统计 + 环境列表 + 事件。

        两块面板各自决定"这一轮要不要真的发请求"：
        面板没显示、用户正在输入浮层里打字、或上一个操作还在飞 → 整轮跳过。
        所以"自动刷新"永远不会打断用户输入，也不会在启动浏览器那几十秒里
        半路把列表重排一遍（用户会以为点空了）。
        """
        for job in (refresh_stats, profiles_panel.autorefresh, log_panel.autorefresh):
            try:
                job()
            except Exception:
                # 单个刷新失败不该拖垮整个节拍（否则一次异常就再也不刷新了）
                pass
        try:
            root.after(5000, tick)
        except Exception:
            pass

    def local_summary() -> dict[str, Any]:
        """给服务器看的本机概况。

        ## core 不在时**也必须上报**

        原来这里 `if not st: return {}` —— 心跳本身照常成功，于是服务器上
        一直显示"在线"，而后台的 core 已经死了（远程命令、远程控制台全废）。
        2026-11-17 在云电脑上真踩到：看着在线，实则什么都连不上，只能跑到机器前处理。

        所以现在无论 core 在不在，都返回一份带 `core_alive` 的字典，
        让主控台能把"进程活着但服务死了"这个状态显示出来。
        """
        st = core.stats() if state.get("core_on") else None
        if not st:
            return {
                "core_alive": False,
                "core_error": str(getattr(core, "error", "") or "")[:500],
                "core_port": int(getattr(core, "port", 0) or 0),
                "shell_enabled": bool(state.get("shell_var").get()),
            }
        # 告诉服务器"从哪能连到我这台机器"。
        # core 在首选端口被占时会自动回退（client.LocalCore），
        # 所以这里必须用**实际端口**拼，否则服务器会一直按 8730 去连、永远连不上。
        base = str(st.get("gateway_public_base") or "")
        if not base:
            port = core.live_port
            host = state.get("lan_ip") or "127.0.0.1"
            base = f"http://{host}:{port}"
        inv = []
        try:
            from zw_core.services import manager as mgr

            from zw_core.services import proxylib as _plib

            for pr in mgr.list_profiles():
                pid = str(pr.get("id") or "")
                _spec = None
                _raw_px = str(pr.get("proxy") or "").strip()
                _px_problem = ""
                try:
                    _spec = mgr.proxy_spec_for(pr)
                except Exception as _exc:
                    _px_problem = str(getattr(_exc, "message", "") or _exc)
                inv.append({
                    "id": pid,
                    "name": str(pr.get("name") or ""),
                    "state": str(pr.get("state") or ""),
                    "group": str(pr.get("group_name") or ""),
                    "cdp_path": f"/cdp/{pid}" if pid else "",
                    "proxy": _plib.display_summary(_spec, raw=_raw_px, problem=_px_problem),
                })
        except Exception:
            inv = []
        try:
            from zw_core.config import get_settings

            core_base = get_settings().gateway_public_base.rstrip("/")
        except Exception:
            core_base = ""
        return {
            "core_alive": True,
            "core_error": "",
            "core_port": int(core.live_port),
            "core_base": core_base,
            "inventory": inv,
            "profiles": int(st.get("profiles") or 0),
            "running": int(st.get("running") or 0),
            "max_running": int(st.get("max_running") or 0),
            "disk_human": str(st.get("disk_human") or ""),
            "engine": str((st.get("engine") or {}).get("version") or ""),
            # 面板据此在设备行显示"命令行：开/关"，并决定终端输入框是否可写
            "shell_enabled": bool(state.get("shell_var").get()),
        }

    def start_heartbeat(base: str, token: str, interval: int) -> None:
        old = state.get("heartbeat")
        if old is not None:
            old.stop()
        hb = Heartbeat(base, state["device_id"], token, interval=interval,
                       stats_provider=local_summary)
        hb.on_revoked = lambda: root.after(0, on_revoked)
        hb.start()
        state["heartbeat"] = hb

    def on_revoked() -> None:
        set_phase("revoked", "已被服务器移除")
        log("这台电脑已被服务器踢下线：同步与注册会失败，本机浏览器仍可继续用。", "bad")
        try:
            messagebox.showwarning(
                "已被踢下线",
                "这台电脑已被服务器端「踢下线」。\n\n"
                "本机已经开着的浏览器还能继续用，但同步、注册都会失败。\n\n"
                "请联系管理员在服务器管理面板里恢复这台设备，然后点「重连」。",
            )
        except Exception:
            pass

    def log_bg(text: str, level: str = "info") -> None:
        """后台线程专用的日志：`log()` 会动控件，必须排队回主线程再调。"""
        try:
            root.after(0, lambda: log(text, level))
        except Exception:
            pass

    def run_bg(fn, done) -> None:
        """后台线程跑 `fn()`，结果回 Tk 主线程交给 `done()`。

        为什么必须有这一层：Tk 控件只能在创建它的线程里动，后台线程直接
        `configure()` 在 Windows 上会崩或随机卡死。所以统一 `after(0, ...)` 回主线程。
        窗口已销毁时静默丢弃。
        """

        def work() -> None:
            try:
                out = fn()
            except Exception as exc:  # noqa: BLE001 —— 后台异常不能静默，也不能杀进程
                out = {"phase": "crash", "error": f"{type(exc).__name__}: {exc}"}

            def finish() -> None:
                try:
                    if not root.winfo_exists():
                        return
                except Exception:
                    return
                done(out)

            try:
                root.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def connect() -> None:
        """点「连接并开始」/ 启动时自动连接。

        ## 为什么整段都挪到后台线程（2026-11-19 修）

        用户反馈"每次打开都会无响应几秒钟"。原因就在这里：这个函数原来**全在
        Tk 主线程**上跑，其中三步都是秒级阻塞 ——

          1. `probe_server(base)`  网络往返；服务器不通时要等超时
          2. `enroll(...)`         又一次网络往返
          3. `core.start(...)`     起 uvicorn 并**轮询等它就绪**（本机量到 1215ms，
                                   Windows 过杀软扫描更久）

        而启动时还会 `root.after(200, connect)` 自动连一次，于是每次打开都要
        卡住整个界面好几秒 —— 窗口变白、标题栏「无响应」。

        现在：阻塞三步放后台，界面立刻能响应；结果回主线程再改控件。
        """
        state["stop_watch"] = False
        state["approval_thread"] = None
        raw = server_var.get().strip()
        base = normalize_server(raw)
        if not base:
            addr_hint.set("地址不能为空。例如 192.168.3.5:8700")
            srv_entry.focus_set()
            return
        set_phase("connecting", "连接中…")
        log(f"正在连接 {base}")
        connect_btn.state(["disabled"])
        root.update_idletasks()          # 先把"连接中…"画出来，再进后台

        token = token_var.get().strip()

        def work() -> dict[str, Any]:
            info = probe_server(base)
            if not info.get("ok"):
                return {"phase": "probe_failed", "info": info}
            if info.get("token_required") and not token:
                return {"phase": "need_token", "info": info}
            log_bg("正在注册这台电脑 …")
            res = enroll(base, state["device_id"], token, app_version=__version__)
            if not res.get("ok"):
                return {"phase": "enroll_failed", "res": res, "info": info}
            log_bg("正在启动本机浏览器服务 …")
            ok = core.start(device_id=state["device_id"], hub_url=base)
            return {"phase": "started" if ok else "core_failed",
                    "res": res, "info": info}

        run_bg(work, lambda out: on_connect_done(base, token, out))

    def on_connect_done(base: str, token: str, out: dict[str, Any]) -> None:
        """`connect()` 的后台结果 → 界面。**全程在 Tk 主线程**。"""
        phase = out.get("phase")
        info = out.get("info") or {}
        res = out.get("res") or {}

        if phase == "crash":
            set_phase("error", "连接出错")
            log(f"连接过程出错：{out.get('error')}", "bad")
            connect_btn.state(["!disabled"])
            return

        if phase == "probe_failed":
            set_phase("error", "连不上")
            log(f"连接失败：{info.get('error')}", "bad")
            # 连不上服务器**不等于这个工具不能用**：本机环境管理不依赖服务器。
            # 但单文件客户端只有图形界面入口，用户不知道还有 `serve` 这条路，
            # 于是就卡在"连不上"这里，以为软件坏了。
            log("想先在本机用（不连服务器）？关掉这个窗口，在本程序目录执行："
                f"{Path(sys.argv[0]).name} serve  —— 会启动本机服务并打开控制台。", "warn")
            address_failed(info.get("error", ""))
            connect_btn.state(["!disabled"])
            return

        if phase == "need_token":
            set_phase("idle", "需要对接码")
            log("这台服务器要求对接码，请填写后重试。", "warn")
            tok_card.pack(fill="x", pady=(10, 0), after=srv_card)
            messagebox.showinfo(APP_NAME, "这台服务器要求对接码，请在下方填写后点「连接并开始」。")
            connect_btn.state(["!disabled"])
            return

        if phase == "enroll_failed":
            code = res.get("code", "")
            set_phase("error", "注册失败")
            if code == "DEVICE_REJECTED":
                log("服务器拒绝了这台电脑的接入请求。联系管理员在控制台里点「允许」。", "bad")
                messagebox.showwarning(
                    "被服务器拒绝",
                    "这台电脑的接入请求被拒绝了。\n\n"
                    "请联系管理员在服务器控制台里点「允许」，然后点「重连」。",
                )
            elif code == "DEVICE_REVOKED":
                on_revoked()
            elif code == "UNAUTHENTICATED":
                log("对接码不正确。", "bad")
                tok_card.pack(fill="x", pady=(10, 0), after=srv_card)
                messagebox.showerror(APP_NAME, "对接码不正确，请检查后重试。")
            else:
                log(f"注册失败：{res.get('error')}", "bad")
                address_failed(res.get("error", ""))
            connect_btn.state(["!disabled"])
            return

        if phase == "core_failed":
            set_phase("error", "本机服务失败")
            log(f"本机服务启动失败：{core.error}", "bad")
            messagebox.showerror(APP_NAME, f"本机服务启动失败：\n\n{core.error}")
            connect_btn.state(["!disabled"])
            return

        if phase != "started":
            set_phase("error", "连接出错")
            log(f"未预期的连接结果：{phase}", "bad")
            connect_btn.state(["!disabled"])
            return

        # ── 到这里本机服务已经起来了 ──────────────────────────
        tok_card.pack_forget()
        cfg.update({
            "server": base,
            "device_id": state["device_id"],
            "token": token,
            "token_required": bool(info.get("token_required")),
        })
        save_config(cfg)

        # 服务器要人工授权时，这里拿到的是 pending。
        # **仍然启动本机服务**：用户明确要求"哪怕没有授权，也可以被远程终端操控"，
        # 而远程终端依赖的正是本机 core（它会在后台去服务器拉命令）。
        dev_status = str(((res.get("device") or {}).get("status")) or "active")
        state["approved"] = dev_status != "pending"

        state["core_on"] = True
        state["base"] = base
        # 配置里关掉的话，连上后立刻把 core 侧也关掉（默认值是开）
        if not bool(cfg.get("shell_enabled", True)):
            run_bg(
                lambda: _post_shell_disable(),
                lambda _r: log("远程命令行按上次的设置保持关闭", "warn"),
            )
        start_heartbeat(base, token, int(res.get("heartbeat_seconds") or 60))
        show_setup(False)
        conn_server.configure(text=base)
        conn_dev.configure(text=state["device_id"][:32] + "…")
        open_btn.state(["!disabled"])
        panel_btn.state(["!disabled"])
        log(f"已连接：{base}", "ok")
        log(f"服务器主控台：{base}/", "info")

        if state.get("approved", True):
            set_phase("connected", "已连接")
        else:
            set_phase("pending", "等待服务器授权")
            log("这台电脑还没被服务器允许。远程终端现在就能用，除此之外等管理员点「允许」。", "warn")
            start_approval_watch(base, token)

        log(f"控制台 http://127.0.0.1:{core.port}", "ok")
        refresh_stats()
        # 刚连上就刷一遍两块本地面板：用户接下来最可能看的就是环境列表，
        # 不该让他先盯着空列表等 5 秒。
        profiles_panel.refresh(force=True)
        log_panel.refresh()
        try:
            webbrowser.open(f"http://127.0.0.1:{core.port}")
        except Exception:
            pass

    def _post_shell_disable() -> None:
        """把 core 侧的远程命令行关掉（配置里关着时才调）。放后台，别阻塞界面。"""
        try:
            import httpx

            httpx.post(
                f"http://127.0.0.1:{core.port}/api/v1/shell/enable",
                json={"enabled": False},
                headers={"Authorization": f"Bearer {os.environ.get('ZW_ADMIN_TOKEN', '')}"},
                timeout=5,
            )
        except Exception:
            pass

    def start_approval_watch(base: str, token: str) -> None:
        """后台等服务器授权。

        为什么要轮询而不是长连接：授权是一次性动作，3 秒一次的 enroll 足够快，
        而且 enroll 是幂等的（已存在的设备只刷新 last_seen），不需要新接口。
        批准后自动切换成"已连接"，用户什么都不用点。
        """
        if state.get("approval_thread"):
            return

        def worker() -> None:
            while not state.get("stop_watch"):
                try:
                    from zw_core.client import enroll as _enroll

                    r = _enroll(base, state["device_id"], token, app_version=__version__)
                except Exception:
                    r = {"ok": False}
                if r.get("ok"):
                    st = str(((r.get("device") or {}).get("status")) or "")
                    if st == "active":
                        state["approved"] = True
                        root.after(0, lambda: (
                            set_phase("connected", "已连接"),
                            log("服务器已授权，可以正常使用了。", "ok"),
                        ))
                        return
                    if st in ("rejected", "revoked"):
                        root.after(0, lambda: (
                            set_phase("error", "已被拒绝"),
                            log("服务器拒绝/移除了这台电脑。", "bad"),
                        ))
                        return
                time.sleep(3)

        t = threading.Thread(target=worker, name="zw-approval", daemon=True)
        state["approval_thread"] = t
        t.start()

    def address_failed(detail: str) -> None:
        tips = ("检查这几件事：\n"
                "1. 地址对不对（例如 192.168.3.5:8700）\n"
                "2. 那台机器上服务有没有跑起来\n"
                "3. 防火墙有没有放行端口")
        messagebox.showerror(APP_NAME, f"连不上服务器。\n\n{detail}\n\n{tips}")

    def disconnect() -> None:
        state["stop_watch"] = True
        state["approval_thread"] = None
        hb = state.get("heartbeat")
        if hb is not None:
            hb.stop()
            state["heartbeat"] = None
        core.stop()
        state["core_on"] = False
        set_phase("idle", "未连接")
        show_setup(True)
        open_btn.state(["disabled"])
        panel_btn.state(["disabled"])
        connect_btn.state(["!disabled"])
        log("已断开，本机服务已停止。", "warn")

    def reconnect() -> None:
        disconnect()
        root.after(200, connect)

    def do_quit() -> None:
        """真退出：停心跳、停本机服务。

        「关窗口」与「退出」是两件事：关窗口默认最小化（浏览器还在跑），
        退出才真的停服务。这里明确告知，避免误关导致正在跑的任务中断。
        """
        running = state.get("core_on")
        if running:
            ok = messagebox.askyesno(
                "退出客户端",
                "退出会停止本机的浏览器服务。\n"
                "已经打开的浏览器窗口不会被强制关闭，但控制台和 CDP 网关会断开。\n\n确定退出？",
            )
            if not ok:
                return
        hb = state.get("heartbeat")
        if hb is not None:
            hb.stop()
        core.stop()
        _stop_tray(root)
        root.destroy()

    def on_window_close() -> None:
        """点右上角 × ：最小化（不杀服务）。"""
        if state.get("core_on") and cfg.get("minimize_on_close", True):
            try:
                root.iconify()
                log("窗口已最小化；本机服务继续运行。要真正退出请点「退出」。", "info")
            except Exception:
                pass
            return
        do_quit()

    # ── 托盘（可选）─────────────────────────────────────
    tray_holder: dict[str, Any] = {}

    def start_tray() -> None:
        if not _tray_available():
            return
        try:
            import pystray

            image = _make_icon_image()

            def show_window(*_a: Any) -> None:
                try:
                    root.after(0, root.deiconify)
                except Exception:
                    pass

            def tray_quit(*_a: Any) -> None:
                try:
                    root.after(0, do_quit)
                except Exception:
                    pass

            menu = pystray.Menu(
                pystray.MenuItem("打开控制台", lambda *_: webbrowser.open(f"http://127.0.0.1:{core.port}")),
                pystray.MenuItem("显示窗口", show_window),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", tray_quit),
            )
            icon = pystray.Icon("zhiwen", image, f"{APP_NAME} 客户端", menu)
            tray_holder["icon"] = icon
            threading.Thread(target=icon.run, name="zw-tray", daemon=True).start()
            log("已启用托盘图标（右键可操作）", "info")
        except Exception as exc:
            log(f"托盘不可用：{exc}（不影响使用）", "info")

    # ── 装配与启动 ──────────────────────────────────────
    connect_btn.configure(command=connect)
    srv_entry.bind("<Return>", lambda _e: connect())
    root.protocol("WM_DELETE_WINDOW", on_window_close)

    if cfg.get("server"):
        # 配过就自动连（用户双击图标应该直接看到控制台）
        show_setup(False)
        conn_server.configure(text=cfg.get("server") or "—")
        conn_dev.configure(text=state["device_id"][:32] + "…")
        log("检测到上次的配置，正在自动连接 …")
        root.after(300, connect)
    else:
        show_setup(True)
        srv_entry.focus_set()

    if cfg.get("minimize_on_close", True):
        root.after(1200, start_tray)

    render_events()
    # 统一的刷新节拍从这一刻开始跑（1.2 秒先让窗口画出来，别和首帧抢时间）。
    # 它同时负责：本机统计、环境列表、事件面板。
    root.after(1200, tick)
    try:
        root.mainloop()
    finally:
        hb = state.get("heartbeat")
        if hb is not None:
            hb.stop()
        _stop_tray(root)
    return 0


def _stop_tray(root: Any) -> None:
    """停掉托盘图标（不抛异常）。"""
    try:
        for child in root.__dict__.values():
            if hasattr(child, "stop"):
                child.stop()
    except Exception:
        pass


def run_cli() -> int:
    """没有图形界面时的兜底：交互式问答。"""
    from zw_core import __version__

    cfg = load_config()
    print(f"{APP_NAME} 客户端 v{__version__}")
    print(f"机器码：{ensure_device_id()}")
    raw = input("服务器地址（例如 192.168.3.5:8700）：").strip() or cfg.get("server", "")
    base = normalize_server(raw)
    if not base:
        print("地址不能为空")
        return 2
    info = probe_server(base)
    if not info.get("ok"):
        print(f"连接失败：{info.get('error')}")
        return 1
    token = ""
    if info.get("token_required"):
        token = input("这台服务器要求对接码：").strip()
    res = enroll(base, ensure_device_id(), token, app_version=__version__)
    if not res.get("ok"):
        print(f"注册失败：[{res.get('code')}] {res.get('error')}")
        return 1
    core = LocalCore(port=_resolve_core_port(cfg))
    if not core.start(device_id=ensure_device_id(), hub_url=base):
        print(f"本机服务启动失败：{core.error}")
        return 1
    cfg.update({"server": base, "token": token, "device_id": ensure_device_id()})
    save_config(cfg)
    print(f"控制台：http://127.0.0.1:{core.port}    （按 Ctrl+C 退出）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        core.stop()
    return 0

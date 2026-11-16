#!/usr/bin/env python3
"""packaging/windows/check-iss.py —— 知纹 Windows 安装包脚本的静态自检。

为什么需要它：Inno Setup 是 Windows 程序，Linux 开发机和 CI 的 ubuntu job 都
没法编译 .iss。等到 Linux 上改完脚本、推到 tag 才发现 ISCC 报错，一轮发布就废了。
这个脚本把"能在任何平台查的东西"全查一遍：段落齐不齐、必需指令在不在、
AppId 是不是固定 GUID、源文件路径对不对、该默认勾的勾了没有、中文有没有乱码。

它**不检查**（也说不出对错的地方，别指望它）：
  · Pascal 的 [Code] 段语法（要真编译器）
  · ISCC 的语义级校验（比如某个标志名在当前 Inno 版本里是否已存在）
  · 安装程序跑起来是什么样（这只能 Windows 上实测）

用法：
    python packaging/windows/check-iss.py                     # 用仓库里的 .iss
    python packaging/windows/check-iss.py --version 0.3.0     # 顺便校验版本号格式
    python packaging/windows/check-iss.py --list-checks       # 只列出检查项
    python packaging/windows/check-iss.py \\
        --default-isl "C:/Program Files (x86)/Inno Setup 6/Default.isl"

    # 想拿 runner 上真实的 Default.isl 交叉校验 [Messages] 键名（CI 里就这么跑）：
    # 不给 --default-isl 时退回仓库里抓好的名字快照
    # packaging/windows/inno-default-isl-messages.txt

退出码：有任何 FAIL → 1；只有 WARN/SKIP → 0。
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ── 常量：本项目的硬约定，改这些等于改契约 ────────────────────────────────
APP_NAME = "知纹客户端"
APP_PUBLISHER = "pscly"
MAIN_EXE = "zhiwen-client.exe"
# .iss 里必须写成的源路径形式（相对 packaging/windows/）
EXPECTED_EXE_SOURCE = r"..\..\dist\zhiwen-client.exe"
OUTPUT_BASENAME = "zhiwen-client-Windows-x64-setup"
RUN_KEY_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_KEY_NAME = "ZhiwenClient"

REQUIRED_SECTIONS = [
    "Setup",
    "Files",
    "Icons",
    "Run",
    "Registry",
    "UninstallDelete",
    "Tasks",
    "Languages",
]
# 中文界面靠这两段覆盖实现（不用额外的 ChineseSimplified.isl），所以也属于必须
REQUIRED_SECTIONS_EXTRA = ["CustomMessages", "Messages"]
REQUIRED_SETUP_KEYS = [
    "AppName",
    "AppVersion",
    "DefaultDirName",
    "OutputBaseFilename",
    "PrivilegesRequired",  # 需求里说的 "Privileges" 就是这条
    "AppId",
]

# 允许出现在"可写路径"位置上的 Inno 常量/前缀（其余一律当写死处理）
ALLOWED_PATH_CONSTS = (
    "{app}",
    "{autopf}",
    "{group}",
    "{autodesktop}",
    "{uninstallexe}",
    "{userappdata}",
    "{localappdata}",
    "{tmp}",
    "{src}",
)
ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]:[\\/])")
ENV_PATH_RE = re.compile(r"%[A-Za-z_]+%")
GUID_RE = re.compile(
    r"^\{\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$"
)
CM_USE_RE = re.compile(r"\{cm:([A-Za-z0-9_]+)\}")
DEFINE_USE_RE = re.compile(r"\{#\s*([A-Za-z0-9_]+)\s*\}")
SEMVER_RE = re.compile(r"^\d+(\.\d+){1,3}([+-][0-9A-Za-z.\-]+)?$")
MOJIBAKE_MARKERS = ("\ufffd", "ï¿½", "Ã¤", "Ã©", "ã€", "â€")

# 用 = 写键值的段；其余段用 "Key: Value; Key: Value" 形式
EQUALS_SECTIONS = {"Setup", "Messages", "CustomMessages"}
# 完全不解析内容的段
RAW_SECTIONS = {"Code"}


# ── 结果收集 ──────────────────────────────────────────────────────────────
@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    warned: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def add(self, check_id: str, title: str, detail: str = "") -> None:
        self.passed.append(_fmt(check_id, title, detail))

    def fail(self, check_id: str, title: str, detail: str = "") -> None:
        self.failed.append(_fmt(check_id, title, detail))

    def warn(self, check_id: str, title: str, detail: str = "") -> None:
        self.warned.append(_fmt(check_id, title, detail))

    def skip(self, check_id: str, title: str, detail: str = "") -> None:
        self.skipped.append(_fmt(check_id, title, detail))


def _fmt(check_id: str, title: str, detail: str) -> str:
    return f"({check_id}) {title}" + (f" — {detail}" if detail else "")


# ── .iss 解析 ─────────────────────────────────────────────────────────────
@dataclass
class IssScript:
    path: Path
    text: str                       # 原始全文
    defines: dict[str, str]         # ISPP #define（带上 #ifndef 分支里的默认值）
    sections: dict[str, list[str]]  # 段名 -> 该段的行（已展开预处理、去掉注释行）
    setup: dict[str, str]           # [Setup] 的 Key -> Value（已展开 {#...}）
    messages: dict[str, str]        # [Messages]
    custom: dict[str, str]          # [CustomMessages]
    entries: dict[str, list[dict[str, str]]]  # 其余段的 Key: Value 条目

    def raw_setup(self, key: str) -> str:
        """未展开预处理的值（用来判断是不是写成 {#MyAppVersion} 这种宏）。"""
        for line in self.sections.get("Setup", []):
            if "=" in line:
                k, _, v = line.partition("=")
                if k.strip().lower() == key.lower():
                    return v.strip()
        return ""

    def param(self, section: str, index: int, key: str) -> str:
        try:
            return self.entries[section][index].get(key.lower(), "")
        except (KeyError, IndexError):
            return ""


def _logical_lines(text: str) -> list[str]:
    """Inno 的续行符是行末的反斜杠。"""
    out: list[str] = []
    buf = ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        out.append(buf + line)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _strip_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    return v


def _unquote_entry(val: str) -> str:
    """去掉条目值外面的引号，并把 Inno 里用双重引号转义的引号还原。

    Inno 的 `Key: 值` 参数外层引号由解析器吃掉；引号内部的 `""` 表示一个字面引号。
    例：ValueData: 三重引号 + {app} 路径 + 三重引号 → 首尾各留一个引号的路径串。
    """
    v = val.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1].replace('""', '"')
    return v


def parse_iss(path: Path) -> IssScript:
    text = path.read_text(encoding="utf-8")
    defines: dict[str, str] = {}
    sections: dict[str, list[str]] = {}
    current: str | None = None
    # 预处理：#ifndef / #ifdef / #else / #endif / #define
    active = True
    stack: list[bool] = []

    for line in _logical_lines(text):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            token = s.split()[0].lower()
            arg = s[len(token):].strip()
            name = arg.split()[0] if arg else ""
            if token in ("#ifndef", "#ifdef", "#if"):
                stack.append(active)
                defined = name in defines
                active = active and (defined if token == "#ifdef" else not defined)
                if token == "#if":  # 本脚本不用 #if；保守当作"未定义"
                    active = active and False
                continue
            if token == "#else":
                parent = stack[-1] if stack else True
                active = parent and not active
                continue
            if token == "#endif":
                active = stack.pop() if stack else True
                continue
            if token == "#define" and active:
                parts = arg.split(None, 1)
                if parts:
                    defines[parts[0]] = _strip_quotes(parts[1]) if len(parts) > 1 else ""
                continue
            # #include / #pragma / #error 等：与本项目无关，忽略
            continue
        if s.startswith(";"):
            continue
        if not active:
            continue
        if s.startswith("[") and s.endswith("]") and len(s) > 2:
            current = s[1:-1]
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        sections.setdefault(current, []).append(line.rstrip())

    def expand(value: str, depth: int = 0) -> str:
        if depth > 8:
            return value
        changed = False

        def sub(m: re.Match[str]) -> str:
            nonlocal changed
            name = m.group(1)
            if name in defines:
                changed = True
                return expand(defines[name], depth + 1)
            return m.group(0)

        out = DEFINE_USE_RE.sub(sub, value)
        return out if not changed else expand(out, depth + 1)

    # 拆分 "Key: Value; Key: Value"（引号内的分号不算分隔符）
    def split_entry(line: str) -> dict[str, str]:
        parts: list[str] = []
        buf = ""
        in_quote = False
        for ch in line:
            if ch == '"':
                in_quote = not in_quote
                buf += ch
            elif ch == ";" and not in_quote:
                parts.append(buf)
                buf = ""
            else:
                buf += ch
        parts.append(buf)
        entry: dict[str, str] = {}
        for part in parts:
            if ":" not in part:
                continue
            key, _, val = part.partition(":")
            entry[key.strip().lower()] = expand(_unquote_entry(val.strip()))
        return entry

    setup: dict[str, str] = {}
    messages: dict[str, str] = {}
    custom: dict[str, str] = {}
    entries: dict[str, list[dict[str, str]]] = {}

    for name, lines in sections.items():
        if name in RAW_SECTIONS:
            continue
        if name in EQUALS_SECTIONS:
            target = {"Setup": setup, "Messages": messages, "CustomMessages": custom}[name]
            for line in lines:
                if line.strip().startswith(";"):
                    continue
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                target[k.strip()] = expand(v.strip())
        else:
            entries[name] = [split_entry(ln) for ln in lines if ":" in ln and not ln.strip().startswith(";")]

    return IssScript(
        path=path,
        text=text,
        defines=defines,
        sections=sections,
        setup=setup,
        messages=messages,
        custom=custom,
        entries=entries,
    )


# ── 检查项 ────────────────────────────────────────────────────────────────
def check_encoding(iss: IssScript, rep: Report) -> None:
    cid = "C01"
    title = "编码：UTF-8 无 BOM、可严格解码、中文没乱码"
    raw = iss.path.read_bytes()
    problems: list[str] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("文件带 UTF-8 BOM（本项目约定无 BOM；Inno 6 能读 BOM，但没必要）")
    try:
        text = raw.decode("utf-8")  # 严格解码：坏字节直接抛
    except UnicodeDecodeError as exc:
        rep.fail(cid, title, f"不是合法 UTF-8：{exc}")
        return
    if "\r\n" in text and text.count("\r\n") not in (0, text.count("\n")):
        problems.append("混用了 CRLF 与 LF")
    for marker in MOJIBAKE_MARKERS:
        if marker in text:
            problems.append(f"发现乱码特征 {marker!r}")
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    if han < 200:
        problems.append(f"中文太少（{han} 个汉字），可能被转成英文或编码损坏")
    if problems:
        rep.fail(cid, title, "；".join(problems))
    else:
        rep.add(cid, title, f"{len(raw)} 字节，UTF-8，{han} 个汉字")


def check_sections(iss: IssScript, rep: Report) -> None:
    cid = "C02"
    have = {k.lower() for k in iss.sections}
    missing = [s for s in REQUIRED_SECTIONS if s.lower() not in have]
    if missing:
        rep.fail(cid, "必需的 Section 都在", "缺：" + ", ".join(f"[{s}]" for s in missing))
        return
    rep.add(cid, "必需的 Section 都在", ", ".join(f"[{s}]" for s in REQUIRED_SECTIONS))

    cid = "C03"
    missing_extra = [s for s in REQUIRED_SECTIONS_EXTRA if s.lower() not in have]
    if missing_extra:
        rep.fail(cid, "中文覆盖所需的 Section 在（[CustomMessages] [Messages]）",
                 "缺：" + ", ".join(f"[{s}]" for s in missing_extra))
    else:
        rep.add(cid, "中文覆盖所需的 Section 在（[CustomMessages] [Messages]）",
                f"Messages {len(iss.messages)} 条，CustomMessages {len(iss.custom)} 条")


def check_setup_keys(iss: IssScript, rep: Report) -> None:
    cid = "C04"
    have = {k.lower() for k in iss.setup}
    missing = [k for k in REQUIRED_SETUP_KEYS if k.lower() not in have]
    if missing:
        rep.fail(cid, "[Setup] 必需指令齐全", "缺：" + ", ".join(missing))
    else:
        rep.add(cid, "[Setup] 必需指令齐全", ", ".join(REQUIRED_SETUP_KEYS))


def check_appid(iss: IssScript, rep: Report) -> None:
    cid = "C05"
    appid = iss.setup.get("AppId", "").strip()
    if not appid:
        rep.fail(cid, "AppId 是固定 GUID 形式（升级能认出同一个应用）", "AppId 为空")
        return
    if GUID_RE.match(appid):
        rep.add(cid, "AppId 是固定 GUID 形式（升级能认出同一个应用）", appid)
    else:
        rep.fail(
            cid,
            "AppId 是固定 GUID 形式（升级能认出同一个应用）",
            f"现在是 {appid!r}；应写成 AppId={{{{8A4F2C31-...}}}} 这种双花括号 + GUID",
        )


def check_identity(iss: IssScript, rep: Report) -> None:
    cid = "C06"
    name = iss.setup.get("AppName", "")
    pub = iss.setup.get("AppPublisher", "")
    if name == APP_NAME and pub == APP_PUBLISHER:
        rep.add(cid, f"应用名/发布者正确（{APP_NAME} / {APP_PUBLISHER}）",
                f"AppName={name}, AppPublisher={pub}")
    else:
        rep.fail(cid, f"应用名/发布者正确（{APP_NAME} / {APP_PUBLISHER}）",
                 f"现在 AppName={name!r}，AppPublisher={pub!r}")


def check_version(iss: IssScript, rep: Report, expected: str | None) -> None:
    cid = "C07"
    raw = iss.raw_setup("AppVersion")
    value = iss.setup.get("AppVersion", "")
    if "{#MyAppVersion}" not in raw:
        rep.fail(cid, "版本号由命令行 /D 注入（而不是写死）",
                 f"AppVersion 写成了 {raw!r}；应写 AppVersion={{#MyAppVersion}}")
    elif "MyAppVersion" not in iss.defines:
        rep.fail(cid, "版本号由命令行 /D 注入（而不是写死）",
                 "脚本里没有 #ifndef MyAppVersion / #define MyAppVersion 的兜底默认值")
    else:
        rep.add(cid, "版本号由命令行 /D 注入（而不是写死）",
                f"AppVersion={{#MyAppVersion}}，默认值 {iss.defines['MyAppVersion']}")

    cid = "C08"
    # 注意：CI 用 /D 覆盖版本号，所以脚本里展开出来的是"本地默认值"（0.3.0）。
    # 拿它和 tag 硬比会在每次发版时误报失败 —— 这里只校验格式，
    # 默认值与 tag 不一致时给 WARN 提醒（这是正常的，/D 会覆盖）。
    if expected:
        norm = expected[1:] if expected.startswith("v") else expected
        if not SEMVER_RE.match(norm):
            rep.fail(cid, "命令行传入的版本号格式合法（1~4 段数字）", f"--version={expected!r}")
            return
        if value == norm:
            rep.add(cid, "版本号与期望一致", f"tag={norm}，脚本默认值也一致")
        else:
            rep.warn(cid, "版本号与期望一致（不一致不算错：CI 用 /D 覆盖）",
                     f"脚本默认 {value}，tag {norm}；实际编译会是 {norm}")
    elif not SEMVER_RE.match(value):
        rep.fail(cid, "解析出的版本号格式合法（1~4 段数字）", f"当前 {value!r}")
    else:
        rep.add(cid, "解析出的版本号格式合法（1~4 段数字）", value)


def check_dir_and_output(iss: IssScript, rep: Report) -> None:
    cid = "C09"
    ddn = iss.setup.get("DefaultDirName", "")
    if ddn.startswith("{autopf}\\") and ddn.endswith("\\" + APP_NAME):
        rep.add(cid, f"默认装到 {{autopf}}\\{APP_NAME}（按权限自适应）", ddn)
    else:
        rep.fail(cid, f"默认装到 {{autopf}}\\{APP_NAME}（按权限自适应）",
                 f"现在是 {ddn!r}；{'{autopf}'} 在管理员模式=Program Files，普通用户=%LOCALAPPDATA%\\Programs")

    cid = "C10"
    obf = iss.setup.get("OutputBaseFilename", "")
    if obf != OUTPUT_BASENAME:
        rep.fail(cid, f"产物名是 {OUTPUT_BASENAME}", f"现在是 {obf!r}")
    elif not obf.isascii():
        rep.fail(cid, "产物名纯 ASCII（中文名在 artifact 传递链上会被截断）", obf)
    else:
        rep.add(cid, f"产物名是 {OUTPUT_BASENAME} 且纯 ASCII", obf)

    cid = "C11"
    outdir = iss.setup.get("OutputDir", "")
    if outdir.replace("/", "\\").rstrip("\\").lower() == r"..\..\dist":
        rep.add(cid, "输出目录是仓库根的 dist\\", outdir)
    else:
        rep.warn(cid, "输出目录是仓库根的 dist\\",
                 f"现在是 {outdir!r}；CI 会按 dist/ 与脚本目录两处兜底找，但不一致容易踩坑")

    cid = "C12"
    priv = iss.setup.get("PrivilegesRequired", "")
    if priv.lower() in ("lowest", "poweruser", "admin"):
        rep.add(cid, "PrivilegesRequired 合法且免管理员（lowest）" if priv.lower() == "lowest"
                else f"PrivilegesRequired 合法（{priv}）", priv)
    else:
        rep.fail(cid, "PrivilegesRequired 合法（lowest/admin/poweruser）", f"现在是 {priv!r}")


def check_source_path(iss: IssScript, rep: Report, repo_root: Path) -> None:
    cid = "C13"
    files = iss.entries.get("Files", [])
    sources = [e.get("source", "") for e in files]
    if EXPECTED_EXE_SOURCE not in sources:
        rep.fail(cid, f"[Files] 用相对路径 {EXPECTED_EXE_SOURCE} 引用主程序",
                 f"实际的 Source：{sources}")
        return
    rep.add(cid, f"[Files] 用相对路径 {EXPECTED_EXE_SOURCE} 引用主程序")

    cid = "C14"
    # 源路径相对 .iss 所在目录解析
    base = iss.path.parent
    resolved = (base / EXPECTED_EXE_SOURCE.replace("\\", "/")).resolve()
    if not resolved.exists():
        rep.skip(cid, "源文件存在时路径能解析对（Windows 构建机上才有 dist\\zhiwen-client.exe）",
                 f"{resolved} 不存在（本机没构建 exe，正常）；路径解析是 {iss.path.parent.name}/ + "
                 f"{EXPECTED_EXE_SOURCE} → {resolved.relative_to(repo_root) if resolved.is_relative_to(repo_root) else resolved}")
        return
    raw = resolved.read_bytes()[:2]
    size = resolved.stat().st_size
    if raw != b"MZ":
        rep.fail(cid, "源文件存在时路径能解析对", f"{resolved} 不是 PE 文件（头两字节 {raw!r}）")
    elif size < 100_000:
        rep.fail(cid, "源文件存在时路径能解析对", f"{resolved} 只有 {size} 字节，不像打包好的 exe")
    else:
        rep.add(cid, "源文件存在时路径能解析对",
                f"{resolved} 存在、{size} 字节、PE 头正确")

    cid = "C15"
    missing = [s for s in sources
               if s and not s.startswith("{") and not (base / s.replace("\\", "/")).resolve().exists()]
    if missing:
        # 构建机上必须全在；开发机上 dist/ 没有属于正常，只警告
        rep.warn(cid, "[Files] 引用的其他源文件（构建时应存在）",
                 "当前不存在：" + ", ".join(missing) + "（本机没有 dist/ 时正常）")
    else:
        rep.add(cid, "[Files] 引用的其他源文件都存在", ", ".join(sources))


def check_no_hardcoded_paths(iss: IssScript, rep: Report) -> None:
    cid = "C16"
    bad: list[str] = []
    for section, items in iss.entries.items():
        for entry in items:
            for key in ("destdir", "destname", "name", "valuedata", "filename", "workingdir"):
                val = entry.get(key, "")
                if not val or val.startswith("{"):
                    continue
                if ABS_PATH_RE.search(val) or ENV_PATH_RE.search(val):
                    bad.append(f"[{section}] {key}={val}")
    for key in ("DefaultDirName", "DefaultGroupName"):
        val = iss.setup.get(key, "")
        if ABS_PATH_RE.search(val) or ENV_PATH_RE.search(val):
            bad.append(f"[Setup] {key}={val}")
    if bad:
        rep.fail(cid, "可写路径都用 Inno 常量（{app} 等），没写死盘符",
                 "；".join(bad))
    else:
        rep.add(cid, "可写路径都用 Inno 常量（{app} 等），没写死盘符",
                 "允许的常量：" + ", ".join(ALLOWED_PATH_CONSTS))


def check_icons(iss: IssScript, rep: Report) -> None:
    cid = "C17"
    icons = iss.entries.get("Icons", [])
    start = [e for e in icons if e.get("name", "").lower().startswith("{group}\\")]
    if start:
        rep.add(cid, "开始菜单里有主程序快捷方式", start[0].get("name", ""))
    else:
        rep.fail(cid, "开始菜单里有主程序快捷方式", "{group}\\ 下没有 [Icons] 条目")

    cid = "C18"
    desk = [e for e in icons
            if e.get("name", "").lower().startswith("{autodesktop}\\")
            and "desktopicon" in e.get("tasks", "").lower()]
    if desk:
        rep.add(cid, "桌面快捷方式可选（Tasks: desktopicon）", desk[0].get("name", ""))
    else:
        rep.fail(cid, "桌面快捷方式可选（Tasks: desktopicon）",
                 "缺 {autodesktop}\\ 条目或没挂 Tasks: desktopicon")

    cid = "C19"
    unin = [e for e in icons if e.get("filename", "").lower() == "{uninstallexe}"]
    if unin:
        rep.add(cid, "开始菜单里有卸载入口", unin[0].get("name", ""))
    else:
        rep.fail(cid, "开始菜单里有卸载入口", "没有 Filename: {uninstallexe} 的条目")


def check_tasks(iss: IssScript, rep: Report) -> None:
    tasks = {e.get("name", "").lower(): e for e in iss.entries.get("Tasks", [])}

    cid = "C20"
    desk = tasks.get("desktopicon")
    if not desk:
        rep.fail(cid, "桌面快捷方式任务默认勾上", "[Tasks] 里没有 desktopicon")
    elif "checkedonce" not in desk.get("flags", "").lower():
        rep.fail(cid, "桌面快捷方式任务默认勾上",
                 "desktopicon 缺 Flags: checkedonce（否则默认不勾）")
    else:
        rep.add(cid, "桌面快捷方式任务默认勾上", "Flags: checkedonce")

    cid = "C21"
    auto = tasks.get("autostart")
    if not auto:
        rep.fail(cid, "开机自启任务默认不勾", "[Tasks] 里没有 autostart")
        return
    flags = auto.get("flags", "").lower()
    if "checkedonce" in flags or ("checked" in flags and "unchecked" not in flags):
        rep.fail(cid, "开机自启任务默认不勾", f"autostart 的 Flags 是 {flags!r}，会默认勾上")
    else:
        rep.add(cid, "开机自启任务默认不勾", f"Description={auto.get('description', '')!r}")

    cid = "C22"
    for name, entry in tasks.items():
        if not entry.get("description") or not entry.get("groupdescription"):
            rep.fail(cid, "每个任务都有中文描述与分组", f"{name} 缺 Description/GroupDescription")
            return
    rep.add(cid, "每个任务都有中文描述与分组", ", ".join(tasks))


def check_registry(iss: IssScript, rep: Report) -> None:
    cid = "C23"
    regs = iss.entries.get("Registry", [])
    runs = [e for e in regs
            if e.get("subkey", "").lower() == RUN_KEY_SUBKEY.lower()
            and e.get("valuedata", "")]
    if len(runs) != 1:
        rep.fail(cid, f"只写一条开机自启 Run 键（HKCU\\{RUN_KEY_SUBKEY}）",
                 f"找到 {len(runs)} 条")
        return
    e = runs[0]
    problems = []
    if e.get("root", "").upper() != "HKCU":
        problems.append(f"Root 是 {e.get('root')!r}，应为 HKCU")
    if e.get("valuename", "") != RUN_KEY_NAME:
        problems.append(f"ValueName 是 {e.get('valuename')!r}，应为 {RUN_KEY_NAME}（ASCII，别用中文）")
    if "uninsdeletevalue" not in e.get("flags", "").lower():
        problems.append("缺 Flags: uninsdeletevalue（卸载时删不掉这条注册表值）")
    if "autostart" not in e.get("tasks", "").lower():
        problems.append("缺 Tasks: autostart（会无条件写，用户取消勾选也照写）")
    # 解析器已经把外层引号去掉，剩下的是"路径被一对引号包着" —— 路径含空格时才能正确启动
    expected_data = '"{app}\\' + MAIN_EXE + '"'
    if e.get("valuedata", "").lower() != expected_data.lower():
        problems.append("ValueData 应为 " + expected_data + "（用引号包住 exe 路径，路径含空格也能启动），"
                        f"现在是 {e.get('valuedata')!r}")
    if problems:
        rep.fail(cid, "Run 键写法正确（HKCU + uninsdeletevalue + Tasks: autostart + 引号包路径）",
                 "；".join(problems))
    else:
        rep.add(cid, "Run 键写法正确（HKCU + uninsdeletevalue + Tasks: autostart + 引号包路径）",
                f"{RUN_KEY_NAME} = {e.get('valuedata')}")

def check_code(iss: IssScript, rep: Report) -> None:
    """[Code] 段（Pascal）—— 只能查最外层结构，语义交给 ISCC。"""
    cid = "C24"
    code_lines = iss.sections.get("Code")
    if not code_lines:
        rep.add(cid, "[Code] 段的 begin/end 配平", "没有 [Code] 段")
        return
    text = "\n".join(code_lines)
    text = re.sub(r"//[^\n]*", "", text)   # 行注释
    text = re.sub(r"\{[^}]*\}", "", text)  # 花括号注释
    begins = len(re.findall(r"\bbegin\b", text, re.I))
    ends = len(re.findall(r"\bend\b", text, re.I))
    if begins != ends:
        rep.fail(cid, "[Code] 段的 begin/end 配平", f"begin {begins} 个、end {ends} 个")
        return
    has_proc = re.search(
        r"\bprocedure\s+CurStepChanged\s*\(\s*CurStep\s*:\s*TSetupStep\s*\)", text, re.I
    )
    if not has_proc:
        rep.fail(cid, "[Code] 段的 begin/end 配平",
                 "用了 ssPostInstall 却没有 procedure CurStepChanged(CurStep: TSetupStep)")
        return
    rep.add(cid, "[Code] 段的 begin/end 配平、事件函数签名正确",
            f"begin/end 各 {begins} 个，CurStepChanged(CurStep: TSetupStep) 在")

    cid = "C34"
    if re.search(r"\)\s*<>\s*0", text) or re.search(r"=\s*0\s+then\s+//", text):
        rep.fail(cid, "Pascal 里没把 Boolean 函数与数字比较",
                 "Inno 的 Reg* 函数返回 Boolean，写成 `<> 0` 是类型错误，ISCC 会直接拒编")
    else:
        rep.add(cid, "Pascal 里没把 Boolean 函数与数字比较（Reg* 返回 Boolean）")

    cid = "C35"
    rep.warn(cid, "[Code] 段的语义只能在 Windows 上用 ISCC 验证",
             "本机没有 Pascal 编译器；上面只查了配平、事件函数签名和 Boolean 误用")


def check_runs(iss: IssScript, rep: Report) -> None:
    cid = "C25"
    runs = iss.entries.get("Run", [])
    auto = [e for e in runs if "postinstall" in e.get("flags", "").lower()]
    if not auto:
        rep.fail(cid, "安装完可以「立即运行」（postinstall，默认勾上）",
                 "没有 postinstall 的 [Run] 条目")
        return
    e = auto[0]
    flags = e.get("flags", "").lower()
    problems = []
    if "nowait" not in flags:
        problems.append("缺 nowait（会让安装程序卡住等你关客户端）")
    if "unchecked" in flags:
        problems.append("标了 unchecked，默认不勾")
    if not e.get("description", ""):
        problems.append("缺 Description（完成页上会是一行空白勾选框）")
    if e.get("filename", "").lower() != "{app}\\" + MAIN_EXE.lower():
        problems.append(f"Filename 应为 {{app}}\\{MAIN_EXE}，现在是 {e.get('filename')!r}")
    if problems:
        rep.fail(cid, "安装完可以「立即运行」（postinstall，默认勾上）", "；".join(problems))
    else:
        rep.add(cid, "安装完可以「立即运行」（postinstall，默认勾上）",
                f"Description={e.get('description')!r}")


def check_uninstall_cleanup(iss: IssScript, rep: Report) -> None:
    cid = "C26"
    dels = iss.entries.get("UninstallDelete", [])
    app_dir = [e for e in dels
               if e.get("name", "").lower() == "{app}"
               and "filesandordirs" in e.get("type", "").lower()]
    if app_dir:
        rep.add(cid, "卸载时清安装目录（Type: filesandordirs; Name: {app}）")
    else:
        rep.fail(cid, "卸载时清安装目录（Type: filesandordirs; Name: {app}）",
                 f"现有条目：{dels}")
    cid = "C27"
    group = [e for e in dels if e.get("name", "").lower() == "{group}"]
    desk = [e for e in dels if e.get("name", "").lower().startswith("{autodesktop}\\")]
    if group or desk:
        rep.add(cid, "卸载时兜底清开始菜单/桌面项",
                ", ".join(e.get("name", "") for e in group + desk))
    else:
        rep.warn(cid, "卸载时兜底清开始菜单/桌面项",
                 "只有 {app}；卸载程序一般也会删自己建的快捷方式，但兜底更稳")


def check_languages(iss: IssScript, rep: Report) -> None:
    cid = "C28"
    langs = iss.entries.get("Languages", [])
    files = [e.get("messagesfile", "").lower() for e in langs]
    if not files:
        rep.fail(cid, "[Languages] 用编译器自带的 Default.isl（不依赖额外 .isl）",
                 "[Languages] 段为空")
        return
    external = [f for f in files if "chinesesimplified" in f]
    if external:
        rep.fail(cid, "[Languages] 用编译器自带的 Default.isl（不依赖额外 .isl）",
                 f"引用了官方发行包里没有的语言文件：{external}")
        return
    if not any(f.startswith("compiler:") for f in files):
        rep.fail(cid, "[Languages] 用编译器自带的 Default.isl（不依赖额外 .isl）",
                 f"没有 compiler: 前缀的 MessagesFile：{files}")
        return
    rep.add(cid, "[Languages] 用编译器自带的 Default.isl（不依赖额外 .isl）", ", ".join(files))


def check_custom_messages(iss: IssScript, rep: Report) -> None:
    cid = "C29"
    used = set(CM_USE_RE.findall(iss.text))
    defined = set(iss.custom)
    missing = sorted(used - defined)
    if missing:
        rep.fail(cid, "用到的 {cm:...} 都在 [CustomMessages] 里定义",
                 "缺：" + ", ".join(missing))
    else:
        rep.add(cid, "用到的 {cm:...} 都在 [CustomMessages] 里定义",
                f"{len(used)} 个：" + ", ".join(sorted(used)))
    cid = "C30"
    unused = sorted(defined - used)
    if unused:
        rep.warn(cid, "[CustomMessages] 没有多余的死条目", "未使用：" + ", ".join(unused))
    else:
        rep.add(cid, "[CustomMessages] 没有多余的死条目", f"{len(defined)} 条全用上了")


def check_messages(iss: IssScript, rep: Report, isl: Path, source: str) -> None:
    cid = "C31"
    try:
        names = {
            line.strip()
            for line in isl.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip() and not line.strip().startswith(("#", ";"))
        }
        # 语言文件里也可能是 Key=Value
        names |= {n.split("=", 1)[0].strip() for n in names if "=" in n}
    except OSError as exc:
        rep.fail(cid, "每个 [Messages] 键都是 Inno 内置消息名", f"读不到 {isl}：{exc}")
        return
    unknown = sorted(set(iss.messages) - names)
    if unknown:
        rep.fail(cid, "每个 [Messages] 键都是 Inno 内置消息名（写错 ISCC 会报 Unknown message name）",
                 f"未知键：{', '.join(unknown)}")
    else:
        rep.add(cid, "每个 [Messages] 键都是 Inno 内置消息名（写错 ISCC 会报 Unknown message name）",
                f"{len(iss.messages)} 条全部命中（名单来源：{source}）")

    cid = "C32"
    han = len(re.findall(r"[\u4e00-\u9fff]", "".join(iss.messages.values())))
    if han < 100:
        rep.fail(cid, "内置文案确实是中文（不是忘了翻译）", f"只找到 {han} 个汉字")
    else:
        rep.add(cid, "内置文案确实是中文（不是忘了翻译）", f"{han} 个汉字")


def check_arch(iss: IssScript, rep: Report) -> None:
    cid = "C33"
    allowed = iss.setup.get("ArchitecturesAllowed", "")
    mode = iss.setup.get("ArchitecturesInstallIn64BitMode", "")
    ok_tokens = {"x86", "x64", "x64compatible", "arm64", "ia64"}
    toks = set(allowed.lower().split())
    bad = toks - ok_tokens
    problems = []
    if bad:
        problems.append(f"ArchitecturesAllowed 有未知取值 {sorted(bad)}")
    if "x64" not in allowed.lower() and "x64compatible" not in allowed.lower():
        problems.append("没限制到 x64，32 位 Windows 上会装出跑不起来的程序")
    if "x64" not in mode.lower():
        problems.append("ArchitecturesInstallIn64BitMode 没设成 x64，{autopf} 会解析成 Program Files (x86)")
    if problems:
        rep.fail(cid, "架构限制到 x64 且装在 64 位目录", "；".join(problems))
    else:
        note = "（x64compatible 需要 Inno Setup ≥ 6.3）" if "x64compatible" in allowed.lower() else ""
        rep.add(cid, "架构限制到 x64 且装在 64 位目录",
                f"ArchitecturesAllowed={allowed}, InstallIn64BitMode={mode}{note}")


# ── 主流程 ────────────────────────────────────────────────────────────────
CHECKS_HELP = [
    ("C01", "编码：UTF-8 无 BOM、可严格解码、中文没乱码"),
    ("C02", "必需的 8 个 Section 都在（任务书里的硬要求）"),
    ("C03", "[CustomMessages] [Messages] 在（中文界面的实现手段）"),
    ("C04", "[Setup] 必需指令：AppName/AppVersion/DefaultDirName/OutputBaseFilename/PrivilegesRequired/AppId"),
    ("C05", "AppId 是固定 GUID 形式（双花括号 + GUID）"),
    ("C06", f"AppName={APP_NAME}、AppPublisher={APP_PUBLISHER}"),
    ("C07", "版本号走 /D 注入的 {#MyAppVersion}，不是写死"),
    ("C08", "版本号格式合法；与 --version 不一致只告警（/D 会覆盖）"),
    ("C09", "DefaultDirName={autopf}\\知纹客户端"),
    ("C10", f"OutputBaseFilename={OUTPUT_BASENAME} 且纯 ASCII"),
    ("C11", "OutputDir 指向仓库根 dist\\"),
    ("C12", "PrivilegesRequired 合法（lowest = 免管理员）"),
    ("C13", "[Files] 用 ..\\..\\dist\\zhiwen-client.exe 相对路径引用主程序"),
    ("C14", "该相对路径能解析到真文件；存在则校验 PE 头与体积"),
    ("C15", "[Files] 其他源文件（LICENSE）在构建时应存在"),
    ("C16", "可写路径全用 Inno 常量，没写死 C:\\ 或 %USERPROFILE%"),
    ("C17", "开始菜单有主程序快捷方式"),
    ("C18", "桌面快捷方式挂在 desktopicon 任务上"),
    ("C19", "开始菜单有卸载入口（{uninstallexe}）"),
    ("C20", "desktopicon 默认勾（checkedonce）"),
    ("C21", "autostart 默认不勾"),
    ("C22", "每个任务都有中文 Description/GroupDescription"),
    ("C23", "Run 键：HKCU + uninsdeletevalue + Tasks: autostart + 引号包路径"),
    ("C24", "[Code] 段 begin/end 配平、CurStepChanged 事件函数签名"),
    ("C25", "[Run] 有 postinstall + nowait 的「立即运行」"),
    ("C26", "[UninstallDelete] 清安装目录"),
    ("C27", "[UninstallDelete] 兜底清开始菜单/桌面项"),
    ("C28", "[Languages] 只用 compiler:Default.isl"),
    ("C29", "用到的 {cm:...} 都已定义"),
    ("C30", "[CustomMessages] 没有死条目"),
    ("C31", "[Messages] 键名都是真实存在的内置消息名"),
    ("C32", "内置文案确实是中文"),
    ("C33", "架构限制 x64 + 64 位安装模式"),
    ("C34", "Pascal 里没把 Boolean 函数当数字比较"),
    ("C35", "[Code] 段的语义只能 Windows 上验证（提示项）"),
]


def find_default_isl(explicit: str | None, script_dir: Path) -> tuple[Path, str]:
    if explicit:
        return Path(explicit), "命令行 --default-isl"
    candidates = [
        Path(r"C:\Program Files (x86)\Inno Setup 6\Default.isl"),
        Path(r"C:\Program Files\Inno Setup 6\Default.isl"),
    ]
    for cand in candidates:
        if cand.exists():
            return cand, "本机 Inno Setup 安装目录"
    snapshot = script_dir / "inno-default-isl-messages.txt"
    if snapshot.exists():
        return snapshot, snapshot.name
    raise SystemExit("找不到 Default.isl 也找不到消息名快照，无法校验 [Messages] 键名")


def main(argv: list[str] | None = None) -> int:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent.parent
    ap = argparse.ArgumentParser(
        description="静态检查 packaging/windows/zhiwen-client.iss（不需要 Windows）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--iss", default=str(script_dir / "zhiwen-client.iss"),
                    help="要检查的 .iss 路径（默认 packaging/windows/zhiwen-client.iss）")
    ap.add_argument("--default-isl", default=None,
                    help="Inno Setup 的 Default.isl 路径，用来校验 [Messages] 键名；"
                         "不给则退回仓库里的名字快照")
    ap.add_argument("--version", default=None,
                    help="期望的版本号（CI 传 tag，如 0.3.0 或 v0.3.0），用来校验 AppVersion 是否对得上")
    ap.add_argument("--list-checks", action="store_true", help="只列出检查项，不检查")
    ap.add_argument("--quiet", action="store_true", help="只打印 FAIL/WARN/SKIP")
    args = ap.parse_args(argv)

    if args.list_checks:
        print(f"check-iss.py 共 {len(CHECKS_HELP)} 项检查：")
        for cid, title in CHECKS_HELP:
            print(f"  ({cid}) {title}")
        return 0

    iss_path = Path(args.iss)
    if not iss_path.exists():
        print(f"FAIL (C00) 找不到 {iss_path}")
        return 1

    print(f"检查 {iss_path}（仓库根 {repo_root}）")
    iss = parse_iss(iss_path)
    rep = Report()

    check_encoding(iss, rep)
    check_sections(iss, rep)
    check_setup_keys(iss, rep)
    check_appid(iss, rep)
    check_identity(iss, rep)
    check_version(iss, rep, args.version)
    check_dir_and_output(iss, rep)
    check_source_path(iss, rep, repo_root)
    check_no_hardcoded_paths(iss, rep)
    check_icons(iss, rep)
    check_tasks(iss, rep)
    check_registry(iss, rep)
    check_code(iss, rep)
    check_runs(iss, rep)
    check_uninstall_cleanup(iss, rep)
    check_languages(iss, rep)
    check_custom_messages(iss, rep)
    isl, isl_source = find_default_isl(args.default_isl, script_dir)
    check_messages(iss, rep, isl, isl_source)
    check_arch(iss, rep)

    if not args.quiet:
        print()
        for line in rep.passed:
            print(f"  PASS {line}")
    for line in rep.warned:
        print(f"  WARN {line}")
    for line in rep.skipped:
        print(f"  SKIP {line}")
    for line in rep.failed:
        print(f"  FAIL {line}")

    total = len(rep.passed) + len(rep.failed) + len(rep.warned) + len(rep.skipped)
    print()
    print(f"结果：{len(rep.passed)} 通过 / {len(rep.failed)} 失败 / "
          f"{len(rep.warned)} 警告 / {len(rep.skipped)} 跳过（共 {total} 项）")
    if rep.failed:
        print("结论：不通过 —— 上面每条 FAIL 都要改掉，否则 ISCC 在 Windows 上大概率编译不过。")
        return 1
    print("结论：通过（静态检查能覆盖的部分；Pascal 段与真实安装效果仍需 Windows 实机验证）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

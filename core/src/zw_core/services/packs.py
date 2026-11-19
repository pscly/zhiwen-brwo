"""扩展包（Extension Packs）：把"一整套扩展 + 它们的存储数据"当成一个可复用的整体。

## 为什么需要它（而不是复用扩展库）

`extensions` 表管的是**单个扩展的代码**：装一次、按 slug 绑定到环境、启动时
`--load-extension` 注入。它解决不了用户真正抱怨的那件事：

> "把 RXYCDP 浏览器的插件及其数据一起打包，以后新建浏览器默认就带上。"

"及其数据"是关键。一个扩展装进新环境后是**白板状态**：YesCaptcha 要重新填 key、
代理认证插件要重填账号、翻译插件要重选语言。这些状态存在
`Default/Local Extension Settings/<扩展 ID>/` 里（leveldb 格式），跟扩展代码是两回事。

所以包 = **代码 + 该扩展的 Local Extension Settings**，一次性搬到任意新环境。

## 目录结构（就是"可以直接复制进 udd"的形状）

```
<数据目录>/packs/<pack_id>/
  pack.json                     元数据（不含任何凭据，纯清单）
  extensions/<ext_key>/         扩展代码根（这一层里就有 manifest.json）
  data/<ext_key>/               复制自 udd 的 Default/Local Extension Settings/<扩展 ID>/
```

`ext_key` 用扩展真实 ID（从源环境导出时能拿到），拿不到时退回 slug。
两边同名，是为了让"代码 ↔ 数据"的对应关系在目录名上就看得出来。

## 三条设计取舍（都是有原因的）

1. **不搬 `Local Storage/leveldb`。**
   那个库是**所有源混在一个 leveldb 里**（按 origin 分 key），没有"属于某个扩展"
   的边界。想只挑出来某个扩展的部分，得解析 leveldb 的键编码 —— 那是在赌
   Chromium 的内部格式，版本一变就炸，而且挑错了等于把别的站的登录态一起搬走。
   扩展自己的状态在 `Local Extension Settings/`，够用。

2. **不搬 `Sync Extension Settings/`。**
   那是 `chrome.storage.sync` 的本地缓存镜像，权威在 Google 账号云端。搬过去既不会
   被当成真数据用（会等云端覆盖），又可能带着别人的账号痕迹。

3. **数据落盘位置要按 Chromium 的"扩展 ID"算。**
   见 `chromium_unpacked_id()` 的注释：未打包扩展的 ID 由**加载路径**推导，
   所以扩展在本机的安装路径一变，数据目录名也得跟着变——否则数据放对了地方
   但浏览器去另一个目录找，等于没带。

## 与 cross-machine bundle 的关系（先读 bundle.py 顶部注释）

包本身**不加密、不含 cookie**，只搬扩展代码与扩展自己的设置。搬机器时它跟着
`<数据目录>/packs/` 一起拷就行；`Default/Cookies`（DPAPI 加密）依然走 bundle + CDP 那套。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..db import get_db

logger = logging.getLogger("zw.packs")

META_FILE = "pack.json"
CODE_DIR = "extensions"
DATA_DIR = "data"

# Chromium 里扩展状态目录的名字（相对 udd）
LES_REL = Path("Default") / "Local Extension Settings"
EXT_REL = Path("Default") / "Extensions"

# 单个包的上限：防止有人把整个 UDD 当扩展目录导进来把磁盘塞满
MAX_PACK_BYTES = 512 * 1024 * 1024


class PackError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ── 小工具 ────────────────────────────────────────────────
def packs_root() -> Path:
    return get_settings().packs_dir


def _pack_dir(pack_id: str) -> Path:
    # pack_id 会进路径，必须挡住 ../ 这类东西
    safe = "".join(ch for ch in str(pack_id) if ch.isalnum() or ch in "-_")
    if not safe:
        raise PackError("PACK_NOT_FOUND", f"包标识不合法：{pack_id!r}", 404)
    return packs_root() / safe


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{n}B"


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _slugify(text: str, fallback: str = "ext") -> str:
    """把 manifest 里的名字压成能当目录名/库 slug 的形状（中文会被丢掉）。"""
    raw = (text or "").strip().lower().replace(" ", "-")
    out = "".join(ch for ch in raw if ch.isalnum() or ch in "-_.")
    return (out or fallback)[:64]


def _read_manifest(folder: Path) -> dict[str, Any] | None:
    mf = folder / "manifest.json"
    if not mf.is_file():
        return None
    try:
        # **必须用 utf-8-sig**：有些扩展的 manifest.json 带 UTF-8 BOM
        # （Windows 上的编辑器、以及某些构建工具会加）。用 `utf-8` 读进来会多一个
        # \ufeff，`json.loads` 直接抛 JSONDecodeError —— 结果是"这个扩展被静默跳过"，
        # 用户只会看到「里面没有可导入的扩展」，完全想不到是 BOM 的问题。
        # 实测踩到：用 PowerShell `Set-Content -Encoding UTF8` 造的文件就带 BOM。
        data = json.loads(mf.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _tree_sha256(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            try:
                h.update(p.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


def _copy_tree(src: Path, dst: Path) -> None:
    """覆盖式复制。dst 已存在时保留它里面 src 没有的文件——扩展的 storage 目录
    可能有正在写的临时文件，整体删掉再建反而更容易丢数据。"""
    if not src.is_dir():
        raise PackError("PACK_INVALID_SOURCE", f"不是一个目录：{src}")
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)


def _write_meta(pack_dir: Path, meta: dict[str, Any]) -> None:
    """原子写：先写 .tmp 再 rename，避免半个 JSON 落盘（读的时候直接崩）。"""
    target = pack_dir / META_FILE
    tmp = pack_dir / (META_FILE + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _load_meta(pack_id: str) -> dict[str, Any]:
    d = _pack_dir(pack_id)
    f = d / META_FILE
    if not f.is_file():
        raise PackError("PACK_NOT_FOUND", f"找不到扩展包：{pack_id}", 404)
    try:
        meta = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackError("PACK_BAD_MANIFEST", f"包的 {META_FILE} 读不出来：{exc}", 400) from exc
    if not isinstance(meta, dict):
        raise PackError("PACK_BAD_MANIFEST", f"包的 {META_FILE} 形状不对", 400)
    meta.setdefault("id", pack_id)
    meta.setdefault("extensions", [])
    return meta


# ── Chromium 扩展 ID ──────────────────────────────────────
def chromium_unpacked_id(load_path: Path) -> str:
    """算"未打包扩展"（--load-extension 的那种）的扩展 ID。

    ## 为什么必须算它

    Chromium 把扩展状态存在 `Default/Local Extension Settings/<扩展 ID>/`。
    已打包的 CRX 扩展 ID 来自 manifest 里的公钥；**未打包**扩展（我们这种）
    没有公钥，ID 由**绝对路径**推导：SHA-256(路径) 取前 32 个十六进制字符，
    再把每个十六进制位映射成 a-p 的字母（0→a, 1→b, … f→p）。

    所以同一个扩展，装在 `/root/…/extensions/x-1.0` 和 `/home/…/extensions/x-1.0`，
    ID 完全不同。数据目录必须按**本机实际加载路径**算出来的 ID 命名，否则
    数据放对了位置、浏览器却去另一个目录找，等于白搬。

    ## 诚实标注：这条算法是社区文档里的规则，我没能在本机实测确认

    实测环境是 Google Chrome 149，它已经**忽略命令行 `--load-extension`**
    （137+ 起被 `DisableLoadExtensionCommandLineSwitch` 特性关掉了），
    扩展根本没被加载，因此拿不到"Chromium 自己算出来的 ID"来对比。
    详情与影响见 `docs/06-同步与加密.md` 的说明与本文件的 `apply_to_profile`：
    导出时会把**源环境里实测到的 ID**一起存进包里，应用时两个 ID 各写一份数据，
    以 Chromium 实际生效的那个为准 —— 算错了只会多一个没人读的目录，不会毁数据。
    """
    text = str(Path(load_path).resolve())
    digest = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


def observed_extension_ids(udd: Path) -> set[str]:
    """从 udd 里"看"出真实用过的扩展 ID（看一眼而已，不解析任何二进制）。

    Chromium 会为每个**真被加载过**的扩展建一个 `Local Extension Settings/<id>/`
    目录，也会在 `Default/Preferences` 的 `extensions.settings` 里记一份。
    两个都读，取并集——这样导出的包能带上"源环境里到底叫什么 ID"这个权威事实。
    """
    ids: set[str] = set()
    les = Path(udd) / LES_REL
    if les.is_dir():
        ids.update(p.name for p in les.iterdir() if p.is_dir())
    prefs = Path(udd) / "Default" / "Preferences"
    if prefs.is_file():
        try:
            data = json.loads(prefs.read_text(encoding="utf-8", errors="replace"))
            ids.update(((data.get("extensions") or {}).get("settings") or {}).keys())
        except (OSError, json.JSONDecodeError):
            pass
    return {i for i in ids if i and len(i) <= 64}


# ── 导入 ──────────────────────────────────────────────────
def _find_extension_dirs(root: Path) -> list[Path]:
    """在一个目录里找"扩展代码根"：含 manifest.json 的那一层。

    zip 里最常见的两种形状：直接是扩展（根上有 manifest.json）、
    或者多套了一层目录（`myext/manifest.json`）。两种都认。
    """
    if _read_manifest(root):
        return [root]
    found = [p for p in sorted(root.iterdir()) if p.is_dir() and _read_manifest(p)]
    if found:
        return found
    # 再多找一层（有些包是 `ext/dist/manifest.json`）
    deep = [p for p in sorted(root.rglob("manifest.json")) if p.parent != root]
    return [p.parent for p in deep]


def _extract_zip(src: Path, staging: Path) -> Path:
    """安全解压：任何成员带绝对路径或 `..` 就整体拒绝。

    这是防路径穿越——一个恶意 zip 里的 `../../.ssh/authorized_keys` 会让解压
    直接写到数据目录外面去。
    """
    out = staging / "unzipped"
    out.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(src) as zf:
            for name in zf.namelist():
                parts = Path(name).parts
                if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                    raise PackError("PACK_UNSAFE_ARCHIVE", f"zip 里有不安全路径：{name}", 400)
            zf.extractall(out)
    except zipfile.BadZipFile as exc:
        raise PackError("PACK_INVALID_SOURCE", f"不是合法的 zip：{exc}", 400) from exc
    except OSError as exc:
        raise PackError("PACK_INVALID_SOURCE", f"解压失败：{exc}", 400) from exc
    return out


def _resolve_manifest_name(code_dir: Path, manifest: dict[str, Any], key: str) -> str:
    """把 manifest 里的 `name` 解析成人能看的字面值（细节见 `services/extmeta.py`）。"""
    from .extmeta import resolve_manifest_name

    return resolve_manifest_name(code_dir, manifest, fallback=key)


def _ext_meta(code_dir: Path, key: str, ext_id: str = "", data_dir: Path | None = None) -> dict[str, Any]:
    manifest = _read_manifest(code_dir) or {}
    resolved = _resolve_manifest_name(code_dir, manifest, key)
    return {
        "key": key,
        # 源环境里实测到的扩展 ID（没有就空）。apply 时用作"数据双写"的第二位置。
        "ext_id": ext_id,
        "slug": _slugify(resolved, fallback=key),
        "name": resolved,
        "version": str(manifest.get("version") or "0"),
        "code_dir": f"{CODE_DIR}/{key}",
        "data_dir": f"{DATA_DIR}/{key}" if data_dir else "",
        "has_data": bool(data_dir),
        "bytes": _dir_size(code_dir) + (_dir_size(data_dir) if data_dir else 0),
    }


def import_pack(src_path: str = "", name: str = "", from_profile: str = "",
                from_chrome: str = "") -> dict[str, Any]:
    """建一个扩展包。四种来源：

    1. `from_profile`：**从已有的知纹环境导出** —— 扫描它的
       `udd/Default/Extensions/<id>/<ver>/` 拿代码，再把
       `Default/Local Extension Settings/<id>/` 一起打包。
    2. `from_chrome`：**从任意 Chrome / Roxy / 其它指纹浏览器的用户目录导出**。
       用户说的"RXYCDP 浏览器的插件及其数据"就是这种：那个浏览器不是知纹管的，
       但它的扩展就装在标准的 Chrome 用户数据目录里。
       参数可以是 `User Data`、`User Data/Default`、或直接指向 `Default` 的路径。
    3. `src_path` 是扩展目录（含 manifest.json）。
    4. `src_path` 是 zip（内部形状与 3 相同即可）。
    """
    s = get_settings()
    packs_root().mkdir(parents=True, exist_ok=True)

    if from_profile:
        return _import_from_profile(from_profile, name)

    if from_chrome:
        return _import_from_chrome(from_chrome, name)

    if not src_path:
        raise PackError("PACK_INVALID_SOURCE", "要么给 src_path（扩展目录或 zip），要么给 from_profile", 400)

    src = Path(src_path).expanduser()
    if not src.exists():
        raise PackError("PACK_INVALID_SOURCE", f"路径不存在：{src}", 400)

    # **用户很可能把"浏览器的 User Data 目录"当路径传进来**（非常自然：
    # 界面上写的就是"填那个浏览器的用户数据目录"）。
    #
    # 以前这条会走进下面的"扩展目录"分支：它确实能在 `User Data/Default/Extensions`
    # 里翻到扩展并**导入成功**，但 `Local Extension Settings` 里的**数据全丢了**
    # —— 返回 `has_data=False`，而用户以为"插件和数据都带上了"。
    # （2026-11-20 我自己就这么误判过一轮，还以为 API 丢参数。）
    #
    # 所以这里嗅一下浏览器目录结构，命中就自动改走 chrome 导入（带数据）。
    if src.is_dir() and _looks_like_browser_userdata(src):
        logger.info("路径像浏览器用户数据目录，自动按 chrome 导入（含 Local Extension Settings）：%s", src)
        return _import_from_chrome(str(src), name)

    staging = s.staging_dir / f"pack.{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        if src.is_file():
            if not zipfile.is_zipfile(src):
                raise PackError("PACK_INVALID_SOURCE", "只接受解包后的扩展目录，或 .zip 压缩包", 400)
            root = _extract_zip(src, staging)
            source_kind = "zip"
        else:
            root = src
            source_kind = "dir"

        code_dirs = _find_extension_dirs(root)
        if not code_dirs:
            raise PackError(
                "PACK_NO_EXTENSION",
                f"{src.name} 里找不到 manifest.json —— 不像是解包后的扩展目录",
                400,
            )

        pack_id = uuid.uuid4().hex[:12]
        pdir = packs_root() / pack_id
        (pdir / CODE_DIR).mkdir(parents=True, exist_ok=True)
        exts: list[dict[str, Any]] = []
        # 包里的数据目录：`data/<key>/…`（core 导出的 zip 就是这个形状）。
        #
        # **不能当成"没有数据可带"**（2026-11-20 实测踩到）：本项目的包格式
        # 本来就带数据目录，以前 zip 分支只找 `code/` 下的扩展、完全没看 `data/`，
        # 于是"从控制台拉下来的包"扩展在、登录态没了 —— 而 hub 那边明明
        # 记录着 `has_data=True`。用户要的是"插件**及其数据**"，这条必须带。
        data_root = root / DATA_DIR if (root / DATA_DIR).is_dir() else None
        for i, code_src in enumerate(code_dirs):
            manifest = _read_manifest(code_src) or {}
            key = _slugify(str(manifest.get("name") or code_src.name), fallback=f"ext{i}") or f"ext{i}"
            # 同包里两个扩展撞名时加序号，避免后一个覆盖前一个
            if any(e["key"] == key for e in exts):
                key = f"{key}-{i}"
            _copy_tree(code_src, pdir / CODE_DIR / key)

            data_dst = None
            if data_root is not None:
                # 包里的数据目录名是**源环境的扩展 ID / key**，可能与本机算出的 key
                # 不同。按"原始目录名"逐个尝试匹配：先试 code_src 的目录名，
                # 再试 manifest 里的 name。命中就整棵拷过来。
                for cand in (code_src.name, str(manifest.get("name") or "")):
                    if not cand:
                        continue
                    src_data = data_root / cand
                    if src_data.is_dir() and any(src_data.iterdir()):
                        data_dst = pdir / DATA_DIR / key
                        _copy_tree(src_data, data_dst)
                        break

            exts.append(_ext_meta(code_src, key, data_dir=data_dst))

        meta = {
            "id": pack_id,
            "name": (name or _default_pack_name(exts)).strip()[:64],
            "created_at": time.time(),
            "is_default": 0,
            "source": {"kind": source_kind, "path": str(src)},
            "extensions": exts,
        }
        _write_meta(pdir, meta)
        total = _dir_size(pdir)
        if total > MAX_PACK_BYTES:
            shutil.rmtree(pdir, ignore_errors=True)
            raise PackError("PACK_TOO_LARGE", f"包体积 {_human(total)} 超过上限 {_human(MAX_PACK_BYTES)}", 413)
        get_db().audit("pack.import", detail={"pack": pack_id, "kind": source_kind, "extensions": len(exts)})
        logger.info("导入扩展包 name=%s 来源=%s 扩展数=%s", meta["name"], source_kind, len(exts))
        return _summary(meta)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _default_pack_name(exts: list[dict[str, Any]]) -> str:
    if not exts:
        return "未命名扩展包"
    if len(exts) == 1:
        return str(exts[0].get("name") or exts[0]["key"])
    return f"{exts[0].get('name') or exts[0]['key']} 等 {len(exts)} 个扩展"


def scan_browser_profiles(max_depth: int = 4, roots: list[Path] | None = None) -> list[dict[str, Any]]:
    """扫本机常见的"Chromium 系浏览器用户数据目录"，给用户挑。

    ## 为什么需要这个

    导入扩展包要用户填一个 `User Data` 的**绝对路径**，但没人知道它在哪 ——
    RXYCDP / Roxy 这类浏览器装在哪、叫什么、Profile 是 `Default` 还是 `Profile 1`，
    全靠猜。用户卡在这一步会直接放弃，功能等于没做。

    所以这里把常见位置都扫一遍，返回"有扩展的那些"，让用户在界面上点一下就行。

    ## 扫哪些地方

    - Windows：`%LOCALAPPDATA%`、`%APPDATA%`、`%LOCALAPPDATA%\\Programs`、
      `C:\\Program Files`、`C:\\Program Files (x86)`（各向下若干层找 `User Data`）
    - Linux/macOS：`~/.config`、`~/Library/Application Support`

    只找**目录名里带 User Data 的**，或者本身就有 `Extensions` 子目录的；
    然后只在**至少有一个扩展**时才收进结果（空目录列出来只会让人更困惑）。
    """
    import os as _os

    if roots is None:
        roots = _default_scan_roots()
    roots = [Path(r) for r in roots]

    def _walk_for_userdata(root: Path, depth: int) -> list[Path]:
        """BFS 找 `User Data` 目录（限深度，避免扫整个磁盘）。"""
        out: list[Path] = []
        level = [root]
        for _ in range(depth):
            nxt: list[Path] = []
            for d in level:
                try:
                    for child in d.iterdir():
                        if not child.is_dir():
                            continue
                        if child.name.lower() == "user data":
                            out.append(child)
                            continue          # 不再往里钻，避免重复
                        nxt.append(child)
                except (OSError, PermissionError):
                    continue
            level = nxt
            if not level:
                break
        return out

    seen: set[str] = set()
    found: list[dict[str, Any]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for ud in _walk_for_userdata(root, max_depth):
            # `User Data` 下可能有 Default / Profile 1 / Profile 2 …
            for prof in sorted(ud.iterdir()) if ud.is_dir() else []:
                if not prof.is_dir():
                    continue
                if not (prof.name == "Default" or prof.name.startswith("Profile")):
                    continue
                try:
                    n = _count_importable(prof / "Extensions")
                except OSError:
                    continue
                if n <= 0:
                    continue
                key = str(prof.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                found.append({
                    "path": str(prof),
                    "user_data": str(ud),
                    "profile": prof.name,
                    # 用上一层目录名猜浏览器（如 `...\\RXYCDP\\User Data` → RXYCDP）
                    "browser": ud.parent.name or ud.name,
                    "extension_count": n,
                })
    found.sort(key=lambda x: (-int(x["extension_count"]), str(x["path"])))
    return found


def _default_scan_roots() -> list[Path]:
    """默认扫描位置：按平台给常见浏览器配置根目录。"""
    import os as _os

    out: list[Path] = []
    if sys.platform == "win32":
        for env in ("LOCALAPPDATA", "APPDATA"):
            v = _os.environ.get(env)
            if v:
                out.append(Path(v))
        la = _os.environ.get("LOCALAPPDATA")
        if la:
            out.append(Path(la) / "Programs")
        for base in ("Program Files", "Program Files (x86)"):
            p = Path("C:/") / base
            if p.is_dir():
                out.append(p)
    else:
        out.append(Path.home() / ".config")
        out.append(Path.home() / "Library" / "Application Support")
    return out


def _count_importable(ext_root: Path) -> int:
    """数一个 `Extensions` 目录下有多少个**能导入**的扩展（与导入逻辑同判据）。"""
    if not ext_root.is_dir():
        return 0
    n = 0
    for id_dir in ext_root.iterdir():
        if not id_dir.is_dir() or id_dir.name.startswith("_"):
            continue
        if _read_manifest(id_dir):
            n += 1
            continue
        try:
            for ver in id_dir.iterdir():
                if ver.is_dir() and _read_manifest(ver):
                    n += 1
                    break
        except OSError:
            continue
    return n


def _looks_like_browser_userdata(p: Path) -> bool:
    """这个目录是不是浏览器的 User Data（或其中的 Default / Profile N）？

    判据：自己或某个直接子目录里同时有 `Extensions/`，或者有
    `Local Extension Settings/`。后者单独出现也算 —— 有的浏览器会把扩展
    以未打包方式加载、`Extensions/` 是空的，但数据目录仍然有。
    """
    def _has_markers(d: Path) -> bool:
        try:
            return (d / "Extensions").is_dir() or (d / "Local Extension Settings").is_dir()
        except OSError:
            return False

    if _has_markers(p):
        return True
    try:
        children = [c for c in p.iterdir() if c.is_dir()]
    except OSError:
        return False
    return any(_has_markers(c) for c in children if c.name == "Default" or c.name.startswith("Profile"))


def _resolve_chrome_default(raw: str) -> Path:
    """把用户给的路径规整成"chrome 的 Default 目录"。

    接受这几种写法（用户很难搞清楚该给哪一层，所以都认）：
      .../User Data                  → 取 Default
      .../User Data/Default          → 就是它
      .../User Data/Profile 1        → 也认（多用户目录）
      .../Default                    → 就是它
    """
    p = Path(raw).expanduser()
    if not p.exists():
        raise PackError("PACK_INVALID_SOURCE", f"路径不存在：{p}", 400)
    if not p.is_dir():
        raise PackError("PACK_INVALID_SOURCE", f"不是目录：{p}", 400)
    # 已经是 Default / Profile N
    if (p / "Extensions").is_dir() or (p / "Local Extension Settings").is_dir():
        return p
    # User Data 层：优先 Default，其次任意 Profile N（选扩展最多的那个）
    cand = p / "Default"
    if cand.is_dir():
        return cand
    profs = [d for d in p.iterdir() if d.is_dir() and (d.name == "Default" or d.name.startswith("Profile"))]
    for d in sorted(profs):
        if (d / "Extensions").is_dir():
            return d
    raise PackError(
        "PACK_NO_EXTENSION",
        f"{p} 里找不到 Chrome 的 Extensions 目录。请给 `User Data` 或 `User Data/Default` 这一层",
        400,
    )


def _import_from_chrome(raw_path: str, name: str = "") -> dict[str, Any]:
    """从任意 Chrome 系浏览器的用户目录导出扩展 + 该扩展的本地数据。

    和 `_import_from_profile` 的区别只在"代码从哪来"：这里直接读标准
    Chrome 布局（`Extensions/<id>/<ver>/` + `Local Extension Settings/<id>/`），
    不依赖知纹自己的 profiles 目录 —— 因为 Roxy / RXYCDP 这类浏览器不是知纹启的。
    """
    default_dir = _resolve_chrome_default(raw_path)
    ext_root = default_dir / "Extensions"
    settings_root = default_dir / "Local Extension Settings"

    found: list[tuple[str, Path]] = []
    if ext_root.is_dir():
        for id_dir in sorted(ext_root.iterdir()):
            if not id_dir.is_dir():
                continue
            # Chrome 有时会把用户装的扩展放在 _metadata 之类的辅助目录里，跳过
            if id_dir.name.startswith("_"):
                continue
            if _read_manifest(id_dir):
                found.append((id_dir.name, id_dir))
                continue
            for ver_dir in sorted(id_dir.iterdir()):
                if ver_dir.is_dir() and _read_manifest(ver_dir):
                    found.append((id_dir.name, ver_dir))
                    break

    if not found:
        raise PackError(
            "PACK_NO_EXTENSION",
            f"{default_dir} 里没有可导入的扩展（Extensions 目录为空或只有内置组件）。"
            "如果那个浏览器还没装扩展，先在它里面装好再导入",
            400,
        )

    return _materialize_pack(
        found,
        settings_root,
        name=name or f"{default_dir.parent.name or 'chrome'}-扩展",
        source_kind="chrome",
        source_path=str(default_dir),
    )



def _materialize_pack(
    found: list[tuple[str, Path]],
    les_root: Path | None,
    *,
    name: str,
    source_kind: str,
    source_path: str = "",
    audit_ref: str = "",
    profile_name: str = "",
) -> dict[str, Any]:
    """把"已找到的扩展列表"落成一个包。

    `found` 是 [(扩展ID, 代码目录)]；`les_root` 是 `Local Extension Settings` 的父目录
    （可能不存在）。"从环境导出"和"从 Chrome 目录导出"最后都走这里，
    差别只在怎么找到这些目录。
    """
    pack_id = uuid.uuid4().hex[:12]
    pdir = packs_root() / pack_id
    (pdir / CODE_DIR).mkdir(parents=True, exist_ok=True)
    exts: list[dict[str, Any]] = []
    for ext_id, code_src in found:
        key = ext_id
        _copy_tree(code_src, pdir / CODE_DIR / key)
        data_dst = None
        if les_root is not None:
            les_src = les_root / ext_id
            if les_src.is_dir() and any(les_src.iterdir()):
                data_dst = pdir / DATA_DIR / key
                _copy_tree(les_src, data_dst)
        exts.append(_ext_meta(code_src, key, ext_id=ext_id, data_dir=data_dst))

    meta = {
        "id": pack_id,
        "name": (name or "扩展包").strip()[:64],
        "created_at": time.time(),
        "is_default": 0,
        "source": {"kind": source_kind, "path": source_path, "profile": profile_name},
        "extensions": exts,
    }
    _write_meta(pdir, meta)
    total = _dir_size(pdir)
    if total > MAX_PACK_BYTES:
        shutil.rmtree(pdir, ignore_errors=True)
        raise PackError("PACK_TOO_LARGE", f"包体积 {_human(total)} 超过上限 {_human(MAX_PACK_BYTES)}", 413)
    get_db().audit(
        "pack.import",
        audit_ref or pack_id,
        detail={"pack": pack_id, "kind": source_kind,
                "extensions": len(exts), "with_data": sum(1 for e in exts if e["has_data"])},
    )
    logger.info(
        "导入扩展包 来源=%s 包=%s 扩展数=%s 带数据=%s",
        source_kind, pack_id, len(exts), sum(1 for e in exts if e["has_data"]),
    )
    return _summary(meta)


def _import_from_profile(profile_ref: str, name: str = "") -> dict[str, Any]:
    """从已有环境导出：代码从 `Default/Extensions/<id>/<ver>/` 取，数据从
    `Default/Local Extension Settings/<id>/` 取。"""
    from . import manager  # 延迟导入：manager 在 create_profile 里反向用到本模块

    p = manager.get_profile(profile_ref)
    udd = manager.udd_path(p["id"])
    ext_root = udd / EXT_REL
    if not ext_root.is_dir():
        raise PackError(
            "PACK_NO_EXTENSION",
            f"{p['name']} 的数据目录里没有 Default/Extensions —— 这个环境还没装过扩展，"
            "或者扩展是通过 --load-extension 从外部目录加载的（那种情况代码不在 udd 里）",
            400,
        )

    # 每个 <id>/<ver>/ 是一份扩展；也给 <id>/manifest.json 这种"没有版本层"的留位置
    found: list[tuple[str, Path]] = []
    for id_dir in sorted(ext_root.iterdir()):
        if not id_dir.is_dir() or id_dir.name in ("Temp", "Cached"):
            continue
        if _read_manifest(id_dir):
            found.append((id_dir.name, id_dir))
            continue
        for ver_dir in sorted(id_dir.iterdir()):
            if ver_dir.is_dir() and _read_manifest(ver_dir):
                found.append((id_dir.name, ver_dir))

    if not found:
        raise PackError(
            "PACK_NO_EXTENSION",
            f"{p['name']} 的 Default/Extensions 里没有可识别的扩展（缺 manifest.json）",
            400,
        )

    return _materialize_pack(
        found,
        udd / LES_REL,
        name=name or f"{p['name']} 的扩展",
        source_kind="profile",
        source_path=str(udd),
        audit_ref=p["id"],
        profile_name=p["name"],
    )


# ── 查询 ──────────────────────────────────────────────────
def _resolve_pack_name(meta: dict[str, Any]) -> str:
    """包的显示名。存的是 `__MSG_xxx__` 占位符时，**现场再解析一次**。

    ## 为什么需要（2026-11-19 云电脑实测）

    早期版本建包时扩展名还没做 `_locales` 解析，于是包名被存成
    `__MSG_extName__` / `__MSG_brandName__` / `__MSG_scriptcat__` / `__MSG_name__` ——
    界面上就直接显示这些占位符，用户莫名其妙。

    修名字有两条路：迁移数据库，或**读取时解析**。选后者：
    存量包自动变好，不用写迁移，也不怕旧版本又写坏一次。

    只对"整体就是一个 `__MSG_xxx__`"的包名生效，正常名字一律原样返回。
    """
    name = str(meta.get("name") or "").strip()
    if not name.startswith("__MSG_"):
        return name
    exts = meta.get("extensions") or []
    for e in exts:
        code = packs_root() / str(meta.get("id") or "") / str(e.get("code_dir") or "")
        manifest = _read_manifest(code) or {}
        # 走模块内的包装函数（`resolve_manifest_name` 是函数内导入的，
        # 这里直接调会 NameError —— 写完先跑一遍就知道）
        resolved = _resolve_manifest_name(code, manifest, "")
        if resolved:
            return resolved
        alt = str(e.get("name") or "").strip()
        if alt and not alt.startswith("__MSG_"):
            return alt
    return name          # 实在解析不出来就原样返回，不编造


def _summary(meta: dict[str, Any]) -> dict[str, Any]:
    pdir = packs_root() / str(meta.get("id") or "")
    size = _dir_size(pdir) if pdir.is_dir() else 0
    src = meta.get("source") or {}
    return {
        "id": meta.get("id", ""),
        # 显示名走解析（存量包的 `__MSG_xxx__` 会在这里被还原成真名）
        "name": _resolve_pack_name(meta),
        "name_raw": meta.get("name", ""),
        "path": str(pdir),
        "created_at": meta.get("created_at", 0),
        "is_default": bool(meta.get("is_default")),
        "source": src.get("kind", ""),
        "source_profile": src.get("profile", ""),
        "extension_count": len(meta.get("extensions") or []),
        "with_data": sum(1 for e in (meta.get("extensions") or []) if e.get("has_data")),
        "size_bytes": size,
        "size_human": _human(size),
        "extensions": meta.get("extensions") or [],
    }


def list_packs() -> list[dict[str, Any]]:
    root = packs_root()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not (d / META_FILE).is_file():
            continue
        try:
            out.append(_summary(_load_meta(d.name)))
        except PackError as exc:
            # 单独一个包坏了不该让整个列表挂掉
            logger.warning("跳过坏掉的包 %s：%s", d.name, exc.message)
    out.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
    return out


def get_pack(pack_id: str) -> dict[str, Any]:
    return _summary(_load_meta(pack_id))


def default_pack_ids() -> list[str]:
    return [p["id"] for p in list_packs() if p.get("is_default")]


def set_default(pack_id: str, enabled: bool) -> dict[str, Any]:
    meta = _load_meta(pack_id)
    meta["is_default"] = 1 if enabled else 0
    _write_meta(_pack_dir(pack_id), meta)
    get_db().audit("pack.set_default", detail={"pack": pack_id, "enabled": bool(enabled)})
    logger.info("扩展包 %s 默认带上=%s", meta.get("name"), bool(enabled))
    return _summary(meta)


def delete_pack(pack_id: str) -> dict[str, Any]:
    """删包。已经应用过的环境**不受影响**——apply 时扩展代码已经复制进
    `<数据目录>/extensions/`，包只是"出厂模板"，不是运行时的依赖。"""
    d = _pack_dir(pack_id)
    if not d.is_dir():
        raise PackError("PACK_NOT_FOUND", f"找不到扩展包：{pack_id}", 404)
    meta = _load_meta(pack_id)
    shutil.rmtree(d, ignore_errors=True)
    get_db().audit("pack.delete", detail={"pack": pack_id, "name": meta.get("name", "")})
    logger.warning("删除扩展包 name=%s id=%s", meta.get("name"), pack_id)
    return {"deleted": True, "id": pack_id, "name": meta.get("name", "")}


# ── 应用（把包装进某个环境）──────────────────────────────
def _ensure_in_library(code_src: Path, ext: dict[str, Any]) -> Path:
    """把包里的扩展代码放进"扩展库"，返回本机真实的加载路径。

    为什么不直接从包里 `--load-extension`：包是要能整体删掉的（删了不该把正在用
    该扩展的环境一起弄坏）。复制进 `<数据目录>/extensions/<slug>-<version>/`
    以后，环境只依赖库里那份；同时库里登记的 slug 也让 `profiles.extensions`
    保持"可跨机搬运"的形状（存 slug 而不是绝对路径）。
    """
    s = get_settings()
    s.ext_dir.mkdir(parents=True, exist_ok=True)
    dest = s.ext_dir / f"{ext['slug']}-{ext['version']}"
    _copy_tree(code_src, dest)
    digest = _tree_sha256(dest)
    with get_db().conn() as c:
        c.execute(
            "INSERT INTO extensions(slug,version,path,name,sha256,added_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(slug,version) DO UPDATE SET path=excluded.path, name=excluded.name, sha256=excluded.sha256",
            (ext["slug"], ext["version"], str(dest), ext.get("name", ext["slug"]), digest, time.time()),
        )
    return dest


def apply_to_profile(profile_ref: str, pack_ids: list[str] | None = None) -> dict[str, Any]:
    """把包里的扩展（代码 + 存储数据）装进某个环境。

    `pack_ids` 为空 = 所有"默认带上"的包（与 `create_profile` 的行为一致）。

    环境正在运行时也允许应用，但会返回 `needs_restart` —— `--load-extension`
    只在启动时读一次，改完必须重启才生效。
    """
    from . import manager

    p = manager.get_profile(profile_ref)
    ids = list(pack_ids) if pack_ids is not None else default_pack_ids()
    if not ids:
        return {
            "profile": p,
            "applied": [],
            "packs": [],
            "warnings": ["没有指定包，而且库里也没有标记为默认的包"],
            "needs_restart": [],
        }

    s = get_settings()
    udd = manager.udd_path(p["id"])
    udd.mkdir(parents=True, exist_ok=True)
    les_root = udd / LES_REL

    refs: list[str] = []
    for x in p.get("extensions") or []:
        refs.append(str(x.get("slug") if isinstance(x, dict) else x))
    warnings: list[str] = []
    applied: list[dict[str, Any]] = []

    for pack_id in ids:
        meta = _load_meta(pack_id)
        pdir = _pack_dir(pack_id)
        for ext in meta.get("extensions") or []:
            code_src = pdir / str(ext.get("code_dir") or f"{CODE_DIR}/{ext['key']}")
            if not _read_manifest(code_src):
                warnings.append(f"包 {meta.get('name')} 里的 {ext.get('key')} 没有 manifest.json，已跳过")
                continue
            dest = _ensure_in_library(code_src, ext)
            if ext["slug"] not in refs:
                refs.append(ext["slug"])

            data_rel = str(ext.get("data_dir") or "")
            data_src = pdir / data_rel if data_rel else None
            data_written: list[str] = []
            if data_src and data_src.is_dir() and any(data_src.iterdir()):
                computed = chromium_unpacked_id(dest)
                targets = [computed]
                observed = str(ext.get("ext_id") or "")
                # 双写：算出来的 ID 是"本机加载路径推导"的结果（算法见
                # chromium_unpacked_id 的注释，本机没能实测确认），源环境实测到的
                # ID 是另一份事实。两个位置都放一份数据，谁被 Chromium 读到就用谁，
                # 另一个只是多占几十 KB——比"猜错了数据就白搬"划算得多。
                if observed and observed != computed:
                    targets.append(observed)
                for name in targets:
                    _copy_tree(data_src, les_root / name)
                    data_written.append(name)
                if observed and observed != computed:
                    warnings.append(
                        f"{ext.get('name')} 的数据按本机路径哈希放进 {computed}，"
                        f"并按源环境实测 ID 另放一份到 {observed}（两者哪个生效取决于内核实际加载路径）"
                    )

            applied.append(
                {
                    "pack": meta.get("name", pack_id),
                    "pack_id": pack_id,
                    "slug": ext["slug"],
                    "name": ext.get("name", ""),
                    "version": ext.get("version", ""),
                    "path": str(dest),
                    "data_written": data_written,
                }
            )

    if applied:
        # extensions 只存 slug：可跨机，且不泄漏本机绝对路径
        manager._update(p["id"], extensions=refs)
        get_db().audit(
            "pack.apply",
            p["id"],
            detail={"packs": ids, "extensions": [a["slug"] for a in applied]},
        )
        logger.info("应用扩展包 环境=%s 包=%s 扩展=%s", p["name"], ids, [a["slug"] for a in applied])

    ext_ok, ext_note = True, ""
    if applied:
        from . import engines as _engines

        try:
            # 引擎路径的解析统一走 engines.find_chrome()：
            # 配置项叫 `chrome_bin`（不是 `engine_path`），profile 里那一列才叫
            # `engine_path` 且可能为空 —— 我第一版写成 `s.engine_path`，
            # Windows 上 profile.engine_path 为空时直接 AttributeError。
            bin_path = Path(str(p["engine_path"])) if p.get("engine_path") else _engines.find_chrome(s)
            ext_ok, ext_note = _engines.extensions_loadable(bin_path)
        except _engines.EngineError as exc:
            # **找不到引擎**不是"判断出错"，它等价于"不知道这是哪个内核" ——
            # 按"不能加载"告警，但文案要说清是版本未知，而不是把 EngineError
            # 抖出来当主因（Windows CI 上就是这样：ZW_CHROME_BIN 指向 Linux 路径）。
            ext_ok, ext_note = False, (
                f"找不到浏览器可执行文件（{exc.message}），无法确认扩展会被加载；"
                "扩展的代码与数据已带进这个环境，但浏览器可能不会加载它。"
            )
        except Exception as exc:  # noqa: BLE001
            # 不能"出错就放行"：那正是"插件没加载但没人告警"的来源。
            ext_ok, ext_note = False, (
                f"判断扩展能否被加载时出错（{type(exc).__name__}: {exc}），已按不加载处理")

    fresh = manager.get_profile(p["id"])
    return {
        "profile": fresh,
        "applied": applied,
        "packs": ids,
        "warnings": warnings,
        "needs_restart": ["extensions"] if (applied and p.get("state") == "running") else [],
        # 扩展**能不能真的被加载**是另一件事：Chrome 137+ 已移除命令行加载能力。
        # 数据与代码照样带过去了（换内核/换版本的机器上仍然有用），但必须明确告诉
        # 调用方"这个环境里的插件不会出现在浏览器里"，否则用户会以为是自己填错了。
        "extensions_active": ext_ok,
        "extensions_note": ext_note,
    }


def apply_default_packs(profile_id: str) -> dict[str, Any]:
    """给刚建好的环境带上所有默认包。由 `manager.create_profile` 末尾调用。

    这个函数**不应该抛**：建环境成功、带包失败是可接受的降级（用户还能手动应用），
    反过来则不行。所以调用方都套了 try，这里只保证逻辑本身单一。
    """
    s = get_settings()
    if not s.default_packs:
        return {"skipped": "ZW_DEFAULT_PACKS 已关闭", "applied": [], "packs": []}
    ids = default_pack_ids()
    if not ids:
        return {"skipped": "没有标记为默认的扩展包", "applied": [], "packs": []}
    return apply_to_profile(profile_id, ids)

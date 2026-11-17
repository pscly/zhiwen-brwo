"""快照打包（bundle）：把一个环境需要跨机搬运的东西收进一个加密文件。

## 为什么不能只拷目录

Chromium 的 `Cookies` 与 `Login Data` 用操作系统密钥加密（Windows DPAPI / macOS Keychain）。
把一个环境的目录直接拷到另一台机器，那两样**必然解不开**。更糟的是：
Chromium 解不开时不会说"密钥不可用"，它说"**损坏**"，然后把整个 eTLD+1 组的 cookie 删干净。
所以跨机搬运必须：

1. **排除** `Cookies` / `Network/Cookies` / `Login Data`；
2. 用 CDP 把运行时 cookie 以**明文**取出来，单独放进 `cookies.json`；
3. 到目标机后再用 CDP 写回去。

这个文件实现 1 和 2 的打包部分；写回在 `cookies.py`。

## 包格式（v1）

```
偏移  长度  字段
  0    4   魔数 "ZWBN"
  4    1   格式版本
  5    1   标志位（bit0 = 正文已 zstd 压缩）
  6    2   保留
  8    4   revision（大端）
 12   16   salt
 28    4   argon m_cost（KiB，大端）
 32    4   argon t_cost（大端）
 36    1   argon parallelism
 37    1   保留
 38    8   key_fp（密钥指纹原始 8 字节）
 46   24   nonce
 70    8   正文长度（大端）
 78    N   正文 = XChaCha20-Poly1305(key, nonce, aad=前 46 字节, tar.zst)
```

header 作为 AAD 参与认证 —— 防止有人改了 revision 或 seed 还让包"看起来合法"。

## v0.2 的补充

manifest 里多了一个 `profile` 字典，装着**环境自身的设置**（分组、标签、代理、
扩展 slug、窗口尺寸、指纹参数……）。0.1.0 只搬了目录，导入到新机器后环境是"裸"的：
代理丢了、扩展丢了、窗口尺寸回到默认 —— 用户得手动重建一遍。现在一并搬。
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import zstandard as zstd

from . import crypto

MAGIC = b"ZWBN"
FORMAT_VERSION = 1
HEADER_LEN = 78  # 固定头部总长：前 46 字节为 AAD + 24 nonce + 8 长度

# ── 打包清单 ──────────────────────────────────────────────
# 必须包含：这些是"账号身份"的载体
INCLUDE = [
    "Default/Local Storage",
    "Default/Session Storage",
    "Default/IndexedDB",
    "Default/Web Data",
    "Default/Preferences",
    "Default/Secure Preferences",
    "Default/Bookmarks",
    "Default/Extension State",
    "Default/Extensions",
    "Default/Local Extension Settings",
    "Default/Service Worker/Database",
    "Default/Storage",
    "Default/Session Storage",
    "Default/Favicons",
    "Default/History",
    "Local State",
]

# 永远排除：带上去只会坏事
SKIP_ALWAYS = [
    # 运行痕迹
    "SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile", "DevToolsActivePort",
    # 缓存
    "Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "DawnCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "download_cache", "CacheStorage",
    "Default/Service Worker/CacheStorage", "Default/Service Worker/ScriptCache",
    # 崩溃与日志
    "Crashpad", "Safe Browsing", "CertificateRevocation",
    # Chromium 组件与词典
    "WasmTtsEngine", "OnDeviceHeadSuggestModel", "OptimizationHints", "ZxcvbnData",
    "hyphen-data", "WidevineCdm", "MEIPreload", "FileTypePolicies",
    "component_crx_cache", "extensions_crx_cache", "GraphiteDawnCache",
]

# 跨机时必须排除：OS 密钥加密，搬过去必挂（改用 cookies.json + CDP 写回）
SKIP_CROSS_MACHINE = [
    "Default/Cookies",
    "Default/Network",
    "Default/Login Data",
    "Default/Login Data For Account",
    "Local State",   # 里面装着 os_crypt 的加密密钥，跨机搬运会连带把"旧密钥"搬过去
]

# 单目录体积上限（超出则跳过并在 manifest 里记 skipped）
SIZE_LIMITS = {
    "Default/IndexedDB": 64 * 1024 * 1024,
    "Default/Service Worker": 32 * 1024 * 1024,
    "Default/Extensions": 64 * 1024 * 1024,
    "Default/Storage": 64 * 1024 * 1024,
}


class BundleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class BundleHeader:
    format_version: int
    revision: int
    salt: bytes
    kdf: dict[str, int]
    key_fp: str
    nonce: bytes = b""
    body_len: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "revision": self.revision,
            "kdf": self.kdf,
            "key_fp": self.key_fp,
        }


@dataclass
class Manifest:
    """包内清单，人类可读，排错时第一眼看这个。"""

    profile_id: str
    name: str
    revision: int
    created_at: float
    source_node: str
    engine_version: str
    fingerprint: dict[str, Any]
    cookie_count: int = 0
    storage_origins: list[str] = field(default_factory=list)
    extensions: list[dict[str, str]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    cross_machine: bool = True
    profile: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "profile_id": self.profile_id,
                "name": self.name,
                "revision": self.revision,
                "created_at": self.created_at,
                "source_node": self.source_node,
                "engine_version": self.engine_version,
                "fingerprint": self.fingerprint,
                "cookie_count": self.cookie_count,
                "storage_origins": self.storage_origins,
                "extensions": self.extensions,
                "files": self.files,
                "skipped": self.skipped,
                "cross_machine": self.cross_machine,
                "profile": self.profile,
            },
            ensure_ascii=False,
            indent=2,
        )


# ── 路径过滤 ──────────────────────────────────────────────
def _norm(rel: str) -> str:
    return rel.replace("\\", "/").strip("/")


def is_excluded(rel: str, cross_machine: bool = True) -> bool:
    """判断相对路径是否应被排除。"""
    rel = _norm(rel)
    patterns = list(SKIP_ALWAYS) + (list(SKIP_CROSS_MACHINE) if cross_machine else [])
    for pat in patterns:
        if rel == pat or rel.startswith(pat + "/"):
            return True
    name = rel.rsplit("/", 1)[-1]
    # 注意：LevelDB 的 `000003.log` 是 localStorage 的真正数据文件，绝不能当作日志排除。
    # 只排除 Chromium 自己写的调试日志与 sqlite 的回滚日志。
    if name.startswith("chrome_debug.log") or name.endswith(".bdic") or name.endswith("-journal"):
        return True
    if name.endswith(".log") and "/Local Storage/" not in f"/{rel}" and "/Session Storage/" not in f"/{rel}":
        return True
    return False


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def collect_files(udd: Path, cross_machine: bool = True) -> tuple[list[Path], list[dict[str, str]]]:
    """按清单收集要入包的文件，返回 (文件列表, 跳过说明)。"""
    udd = Path(udd)
    files: list[Path] = []
    skipped: list[dict[str, str]] = []
    seen: set[Path] = set()

    for rel in INCLUDE:
        target = udd / rel
        if not target.exists():
            continue
        # 体积上限检查
        limit = next((v for k, v in SIZE_LIMITS.items() if _norm(rel).startswith(_norm(k))), None)
        if limit and target.is_dir() and _dir_size(target) > limit:
            skipped.append({"path": rel, "reason": f"超过体积上限 {limit // (1024 * 1024)}MB"})
            continue
        if target.is_file():
            if not is_excluded(rel, cross_machine) and target not in seen:
                files.append(target)
                seen.add(target)
            continue
        for root, dirs, names in os.walk(target):
            rel_root = Path(root).relative_to(udd).as_posix()
            # 跳过被排除的子目录，避免白遍历
            dirs[:] = [d for d in dirs if not is_excluded(f"{rel_root}/{d}", cross_machine)]
            for fname in names:
                full = Path(root) / fname
                rel_path = full.relative_to(udd).as_posix()
                if is_excluded(rel_path, cross_machine):
                    continue
                if full in seen:
                    continue
                files.append(full)
                seen.add(full)
    return files, skipped


# ── 打包 ──────────────────────────────────────────────────
def pack(
    udd: Path,
    profile_id: str,
    name: str,
    revision: int,
    password: str,
    fingerprint: dict[str, Any],
    cookies: list[dict[str, Any]] | None = None,
    storage: dict[str, Any] | None = None,
    engine_version: str = "",
    source_node: str = "",
    extensions: list[Any] | None = None,
    cross_machine: bool = True,
    kdf: crypto.KdfParams | None = None,
    profile_meta: dict[str, Any] | None = None,
) -> tuple[bytes, Manifest]:
    """把一个环境的必要文件 + 明文 cookie 打成一个加密包。返回 (blob, manifest)。"""
    udd = Path(udd)
    if not udd.exists():
        raise BundleError("PROFILE_DIR_MISSING", f"环境目录不存在：{udd}")

    files, skipped = collect_files(udd, cross_machine=cross_machine)

    ext_list: list[dict[str, str]] = []
    for e in extensions or []:
        if isinstance(e, dict):
            ext_list.append({str(k): str(v) for k, v in e.items()})
        else:
            ext_list.append({"slug": str(e), "path": ""})

    manifest = Manifest(
        profile_id=profile_id,
        name=name,
        revision=revision,
        created_at=time.time(),
        source_node=source_node,
        engine_version=engine_version,
        fingerprint=fingerprint,
        cookie_count=len(cookies or []),
        storage_origins=sorted((storage or {}).keys()),
        extensions=ext_list,
        cross_machine=cross_machine,
        profile=profile_meta or {},
    )

    # tar → zstd → 加密
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        # 先放元数据
        for label, payload in (
            ("cookies.json", json.dumps(cookies or [], ensure_ascii=False).encode()),
            ("storage.json", json.dumps(storage or {}, ensure_ascii=False).encode()),
        ):
            info = tarfile.TarInfo(name=label)
            info.size = len(payload)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(payload))

        for f in files:
            rel = f.relative_to(udd).as_posix()
            try:
                info = tar.gettarinfo(str(f), arcname=f"udd/{rel}")
                with open(f, "rb") as fh:
                    tar.addfile(info, fh)
                manifest.files.append({"path": rel, "size": f.stat().st_size})
            except (OSError, PermissionError) as exc:
                skipped.append({"path": rel, "reason": f"读取失败：{exc}"})

        # manifest 最后放，但它的内容在这里已经算好（files 已填满）
        manifest.skipped = skipped
        manifest_bytes = manifest.to_json().encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(manifest_bytes))

    raw = tar_buf.getvalue()
    compressed = zstd.ZstdCompressor(level=10).compress(raw)

    salt = crypto.new_salt()
    params = kdf or crypto.KdfParams()
    key = crypto.derive_key(password, salt, params)
    fp = crypto.key_fingerprint(key)

    header_prefix = (
        MAGIC
        + bytes([FORMAT_VERSION, 0x01])  # flags: bit0 = zstd
        + b"\x00\x00"
        + revision.to_bytes(4, "big")
        + salt
        + params.m_cost_kib.to_bytes(4, "big")
        + params.t_cost.to_bytes(4, "big")
        + bytes([params.parallelism, 0])
        + bytes.fromhex(fp)
    )
    assert len(header_prefix) == 46, f"header 前缀长度异常：{len(header_prefix)}"

    nonce, ct = crypto.encrypt(key, compressed, aad=header_prefix)
    blob = header_prefix + nonce + len(ct).to_bytes(8, "big") + ct
    return blob, manifest


# ── 解包 ──────────────────────────────────────────────────
def read_header(blob: bytes) -> BundleHeader:
    if len(blob) < HEADER_LEN:
        raise BundleError("BUNDLE_TRUNCATED", f"包长度 {len(blob)} 不足最小头部 {HEADER_LEN}")
    if blob[:4] != MAGIC:
        raise BundleError("BAD_MAGIC", f"不是知纹快照包（魔数应为 ZWBN，实际 {blob[:4]!r}）")
    if blob[4] != FORMAT_VERSION:
        raise BundleError("UNSUPPORTED_VERSION", f"包格式版本 {blob[4]} 不被支持（本程序支持 {FORMAT_VERSION}）")
    return BundleHeader(
        format_version=blob[4],
        revision=int.from_bytes(blob[8:12], "big"),
        salt=blob[12:28],
        kdf={
            "m_cost_kib": int.from_bytes(blob[28:32], "big"),
            "t_cost": int.from_bytes(blob[32:36], "big"),
            "parallelism": blob[36],
        },
        key_fp=blob[38:46].hex(),
        nonce=blob[46:70],
        body_len=int.from_bytes(blob[70:78], "big"),
    )


def unpack(
    blob: bytes,
    password: str,
    expected_key_fp: str | None = None,
) -> tuple[bytes, Manifest]:
    """解密并返回 (tar 内容, manifest)。**不做任何磁盘写入**，落盘由调用方决定。"""
    header = read_header(blob)
    if expected_key_fp and header.key_fp != expected_key_fp:
        raise BundleError(
            "KEY_FINGERPRINT_MISMATCH",
            f"包内密钥指纹 {header.key_fp} 与本机 {expected_key_fp} 不一致。已中止，未改动任何本地文件。",
        )
    key = crypto.derive_key(password, header.salt, crypto.KdfParams(**header.kdf))
    fp = crypto.key_fingerprint(key)
    if fp != header.key_fp:
        raise BundleError(
            "KEY_FINGERPRINT_MISMATCH",
            f"主密码派生出的密钥指纹 {fp} 与包内 {header.key_fp} 不一致。已中止，未改动任何本地文件。",
        )

    header_prefix = blob[:46]
    ct = blob[78:]
    if len(ct) != header.body_len:
        raise BundleError("BUNDLE_TRUNCATED", f"正文长度 {len(ct)} 与头部声明 {header.body_len} 不符")
    compressed = crypto.decrypt(key, header.nonce, ct, aad=header_prefix)
    raw = zstd.ZstdDecompressor().decompress(compressed, max_output_size=1024 * 1024 * 1024)

    manifest: Manifest | None = None
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        for member in tar.getmembers():
            if member.name == "manifest.json":
                fh = tar.extractfile(member)
                data = json.loads(fh.read() if fh else b"{}")
                manifest = Manifest(**{k: data[k] for k in data if k in Manifest.__annotations__})
                break
    if manifest is None:
        raise BundleError("MANIFEST_MISSING", "包内没有 manifest.json，包可能不完整")
    return raw, manifest


def extract_to(raw_tar: bytes, dest: Path, udd_name: str = "udd") -> Path:
    """把 tar 里的 `udd/**` 解到 `dest/udd`，`cookies.json` / `storage.json` / `manifest.json` 解到 `dest/` 根。

    安全：拒绝路径穿越（`..`）与绝对路径。
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    udd_root = dest / udd_name
    udd_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r") as tar:
        for member in tar.getmembers():
            name = _norm(member.name)
            if name.startswith("/") or ".." in Path(name).parts:
                raise BundleError("UNSAFE_PATH", f"包内含有不安全路径：{member.name}")
            if name.startswith("udd/"):
                member.name = name[len("udd/"):]
                tar.extract(member, path=udd_root, filter="data")
            else:
                tar.extract(member, path=dest, filter="data")
    return udd_root


def read_side_files(dest: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """从解包目录读回 cookies / storage / manifest。"""
    dest = Path(dest)
    out: dict[str, Any] = {"cookies": [], "storage": {}, "manifest": {}}
    for name, key, default in (
        ("cookies.json", "cookies", []),
        ("storage.json", "storage", {}),
        ("manifest.json", "manifest", {}),
    ):
        p = dest / name
        if p.exists():
            try:
                out[key] = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                out[key] = default
    return out["cookies"], out["storage"], out["manifest"]


def inspect_blob(blob: bytes) -> dict[str, Any]:
    """不解密，只读头部信息 —— 用于"这个包我能解吗"的快速判断。"""
    h = read_header(blob)
    return {
        "format_version": h.format_version,
        "revision": h.revision,
        "key_fp": h.key_fp,
        "kdf": h.kdf,
        "body_len": h.body_len,
        "total_len": len(blob),
    }

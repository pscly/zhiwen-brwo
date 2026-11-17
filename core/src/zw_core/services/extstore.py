"""从 Chrome 应用商店的链接直接安装扩展（以及手动拖入 .crx/.zip）。

## 用户要求（原话）

> 扩展安装相关的，我希望也是可以直接输入谷歌的扩展链接，例如
> https://chromewebstore.google.com/detail/cookiemanager-cookie-edit/hdhngoamekjhmnpenphenpaiindoinpo?hl=zh-CN
> 就可添加，如果更新的话，他也可以自己更新，也可以自己手动拖动一个扩展放上去，他就自动帮我弄好

## 为什么要**在客户端**下载，而不是服务器

- 服务器（hub）常常没有外网，也没有用户代理；客户端才有
- 所以这个模块只在 core 侧用，hub 只负责保存打包结果

## CRX 是怎么解开的

Chrome 商店给的是 **CRX3**：

```
偏移 0   'Cr24'            4 字节魔数
偏移 4   version=3         uint32 小端
偏移 8   header_len        uint32 小端（protobuf 头长度，含签名信息）
偏移 12  protobuf header   header_len 字节
偏移 12+header_len  ZIP    ← 我们要的就是这一段（PK\\x03\\x04 开头）
```

所以"解 CRX"就是把头部切掉：读 header_len，从 `12+header_len` 切到结尾，
剩下就是一个标准 ZIP。**不需要**验证签名（我们只是要代码文件；
真要执行时由浏览器自己按扩展规则处理）。

实测：cookiemanager 的 CRX = 164717 字节，header_len=1310，ZIP 从 1322 开始。
"""
from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger("zw.extstore")

# Chrome 商店的扩展 ID：32 个小写字母 a-p
_ID_RE = re.compile(r"^[a-p]{32}$")
# 链接里 ID 出现的两种位置：/detail/<slug>/<id>  或  ?id=<id>
_DETAIL_RE = re.compile(r"/detail/[^/]+/([a-p]{32})")
_QUERY_ID_RE = re.compile(r"[?&]id=([a-p]{32})")

UPDATE_URL = "https://clients2.google.com/service/update2/crx"
# prodversion 只影响商店给哪个兼容版本；给个较新的稳定值即可。
# 用太老的值，商店可能返回旧版扩展（manifest v2），反而装不上。
DEFAULT_PRODVERSION = "130.0.0.0"


class StoreError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def parse_extension_id(text: str) -> str:
    """从商店链接 / 裸 ID 里取出扩展 ID。

    接受的形态：
      - `https://chromewebstore.google.com/detail/<slug>/<id>?hl=zh-CN`
      - `https://chrome.google.com/webstore/detail/<slug>/<id>`
      - `https://chromewebstore.google.com/detail/<id>`（无 slug）
      - `...?id=<id>`
      - 直接给 32 位 ID
    """
    t = (text or "").strip()
    if not t:
        raise StoreError("EXT_STORE_INVALID_URL", "请填写 Chrome 应用商店的扩展链接或扩展 ID")
    if _ID_RE.match(t):
        return t
    m = _DETAIL_RE.search(t) or _QUERY_ID_RE.search(t)
    if m:
        return m.group(1)
    # 有些分享链接把 id 放在路径最后一段
    tail = unquote(urlparse(t).path.rstrip("/").rsplit("/", 1)[-1])
    if _ID_RE.match(tail):
        return tail
    raise StoreError(
        "EXT_STORE_INVALID_URL",
        f"没能从这串里认出扩展 ID：{t[:80]}。"
        "请用商店详情页链接（…/detail/<名字>/<32位ID>）或直接填 32 位扩展 ID",
    )


def crx_url(ext_id: str, prodversion: str = DEFAULT_PRODVERSION) -> str:
    """商店的 CRX 下载地址。

    `x=id%3D<id>%26uc` 里 `%26` 是 `&` 的转义 —— 整个 `x` 参数是
    "id=<id>&uc"，商店按这个格式解析。写错会 400。
    """
    return (f"{UPDATE_URL}?response=redirect&prodversion={prodversion}"
            f"&acceptformat=crx2,crx3&x=id%3D{ext_id}%26uc")


def strip_crx_header(blob: bytes) -> bytes:
    """CRX → 纯 ZIP 字节。不用 CRX 的直接返回原样。"""
    if not blob:
        raise StoreError("EXT_STORE_BAD_CRX", "下载到的是空文件（商店可能限流了，稍后重试）")
    if blob[:4] != b"Cr24":
        # 有些镜像直接给 zip；也可能商店返回了 HTML 错误页
        if blob[:2] == b"PK":
            return blob
        head = blob[:200].decode("utf-8", "replace")
        raise StoreError(
            "EXT_STORE_BAD_CRX",
            f"拿到的不是 CRX/ZIP（开头是 {head[:60]!r}）。多半是商店限流或需要代理",
        )
    if len(blob) < 12:
        raise StoreError("EXT_STORE_BAD_CRX", "CRX 文件被截断（不足 12 字节头）")
    version = int.from_bytes(blob[4:8], "little")
    if version == 2:  # CRX2：魔数后是公钥/签名长度，再是 ZIP
        pub_len = int.from_bytes(blob[8:12], "little")
        sig_len = int.from_bytes(blob[12:16], "little")
        off = 16 + pub_len + sig_len
    elif version == 3:
        header_len = int.from_bytes(blob[8:12], "little")
        off = 12 + header_len
    else:
        raise StoreError("EXT_STORE_BAD_CRX", f"不认识的 CRX 版本：{version}")
    if off >= len(blob) or blob[off:off + 2] != b"PK":
        raise StoreError("EXT_STORE_BAD_CRX", "CRX 头之后没有 ZIP 数据（文件可能损坏）")
    return blob[off:]


def download_crx(ext_id: str, *, timeout: float = 60.0,
                 prodversion: str = DEFAULT_PRODVERSION) -> bytes:
    """从商店下载 CRX 并返回 ZIP 字节。"""
    url = crx_url(ext_id, prodversion)
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as c:
            r = c.get(url)
    except Exception as exc:  # noqa: BLE001
        raise StoreError(
            "EXT_STORE_UNREACHABLE",
            f"连不上 Chrome 应用商店（{type(exc).__name__}: {exc}）。"
            "这台机器需要能访问 clients2.google.com；国内网络请先给客户端配好代理",
            502,
        ) from exc
    if r.status_code != 200:
        raise StoreError(
            "EXT_STORE_HTTP_ERROR",
            f"商店返回 HTTP {r.status_code}（扩展 ID 可能写错，或该扩展已下架）",
            502 if r.status_code >= 500 else 400,
        )
    return strip_crx_header(r.content)


def read_manifest(zip_bytes: bytes) -> dict[str, Any]:
    """从 ZIP 里读 manifest.json（用来拿到名字/版本，判断是否已装过）。"""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if name.rstrip("/") == "manifest.json" or name.endswith("/manifest.json"):
                    if name.count("/") > 1:      # 只要根层或一层内的
                        continue
                    with zf.open(name) as f:
                        import json

                        return json.loads(f.read().decode("utf-8", "replace"))
    except (zipfile.BadZipFile, OSError) as exc:
        raise StoreError("EXT_STORE_BAD_CRX", f"ZIP 解不开：{exc}") from exc
    raise StoreError("EXT_STORE_NO_MANIFEST", "这个包里没有 manifest.json，不像一个扩展")


def extract_to(zip_bytes: bytes, dest_dir, *, strip_prefix: str = "") -> None:
    """把扩展解到目录（防路径穿越）。"""
    from pathlib import Path

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            name = info.filename
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                raise StoreError("EXT_STORE_UNSAFE_ARCHIVE", f"压缩包里有不安全路径：{name}")
            rel = name
            if strip_prefix and rel.startswith(strip_prefix):
                rel = rel[len(strip_prefix):]
            if not rel or rel.endswith("/"):
                continue
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as dst:
                dst.write(src.read())


def fetch_info(ext_id: str, *, timeout: float = 60.0) -> dict[str, Any]:
    """只查"商店上现在是什么版本"，用来判断要不要更新（不落盘）。"""
    blob = download_crx(ext_id, timeout=timeout)
    man = read_manifest(blob)
    return {
        "id": ext_id,
        "name": str(man.get("name") or ""),
        "version": str(man.get("version") or ""),
        "manifest_version": man.get("manifest_version"),
        "bytes": len(blob),
        "zip_bytes": blob,
    }


# ── 装进"扩展库"（与环境解耦，可被任意环境引用）────────────────
def install(
    *,
    url: str = "",
    crx_path: str = "",
    name: str = "",
    update: bool = True,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """从商店链接或本地 .crx/.zip 装一个扩展到本机扩展库。

    三种来源：
      - `url`：商店详情页链接或 32 位扩展 ID → 下载最新版
      - `crx_path`：用户手动拖进来的 .crx / .zip
      - 两者都给：以 `url` 为准（链接更能保证是官方最新）

    **更新语义**（用户要求"如果更新的话，他也可以自己更新"）：
      - 目标目录按 `<slug>-<version>` 命名（复用 packs 的库布局）
      - `update=True` 时如果商店上的版本比库里已有的新，就把新版装进来
      - 旧版目录**保留**：正在用旧版的环境不会被突然换掉；
        想切换由"应用扩展包/环境引用"决定，这里不替用户做破坏性动作
    """
    from pathlib import Path

    from . import packs

    if not (url or crx_path):
        raise StoreError("EXT_STORE_NO_SOURCE", "要么给商店链接，要么拖一个 .crx/.zip 文件")

    zip_bytes: bytes | None = None
    ext_id = ""
    if url:
        ext_id = parse_extension_id(url)
        zip_bytes = download_crx(ext_id, timeout=timeout)
        logger.info("从商店下载扩展 %s（%s 字节 ZIP）", ext_id, len(zip_bytes))
    else:
        p = Path(crx_path).expanduser()
        if not p.is_file():
            raise StoreError("EXT_STORE_NO_SOURCE", f"文件不存在：{p}")
        raw = p.read_bytes()
        zip_bytes = strip_crx_header(raw)
        logger.info("从本地文件装扩展 %s（%s 字节）", p.name, len(zip_bytes))

    manifest = read_manifest(zip_bytes)
    slug = packs._slugify(str(manifest.get("name") or (ext_id or "ext"))) or "ext"
    version = str(manifest.get("version") or "0")
    s = packs.get_settings()
    s.ext_dir.mkdir(parents=True, exist_ok=True)
    dest = s.ext_dir / f"{slug}-{version}"

    existed = dest.is_dir()
    if existed and not update:
        return {
            "ok": True, "skipped": "这个版本已经在库里了",
            "slug": slug, "version": version, "path": str(dest), "ext_id": ext_id,
        }

    import tempfile

    staging = Path(tempfile.mkdtemp(prefix="zw-ext-"))
    try:
        extract_to(zip_bytes, staging)
        # 有些 CRX 里所有文件套了一层目录
        root = staging
        if not (root / "manifest.json").is_file():
            subs = [d for d in root.iterdir() if d.is_dir() and (d / "manifest.json").is_file()]
            if len(subs) == 1:
                root = subs[0]
        if not (root / "manifest.json").is_file():
            raise StoreError("EXT_STORE_NO_MANIFEST", "解包后找不到 manifest.json")
        packs._copy_tree(root, dest)
        # 记下商店扩展 ID：check_updates() 靠它回商店比对版本。
        # 不写的话更新功能只能靠猜（而猜错就是下载了别人的扩展）。
        if ext_id:
            try:
                (dest / ".zw-ext-id").write_text(ext_id, encoding="utf-8")
            except OSError:
                logger.debug("写扩展 ID 标记失败", exc_info=True)
    finally:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)

    logger.info("扩展已入库 slug=%s version=%s path=%s（已存在=%s）",
                slug, version, dest, existed)
    return {
        "ok": True,
        "slug": slug,
        "name": str(manifest.get("name") or slug),
        "version": version,
        "manifest_version": manifest.get("manifest_version"),
        "path": str(dest),
        "ext_id": ext_id,
        "already_existed": existed,
        "bytes": len(zip_bytes),
    }


def check_updates(slugs: list[str] | None = None) -> list[dict[str, Any]]:
    """查库里已装的扩展有没有新版（用户要求"可以自己更新"）。

    只查**能对上扩展 ID** 的：库里目录名是 `<slug>-<version>`，不含 ID；
    所以这里额外读库内 `ext-id` 标记文件（install 时会写）。
    没有标记的（手工导入的包）跳过 —— 宁可漏查，也不要瞎猜 ID 去下载别人的扩展。
    """
    from . import packs

    s = packs.get_settings()
    if not s.ext_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in sorted(s.ext_dir.iterdir()):
        if not d.is_dir():
            continue
        mark = d / ".zw-ext-id"
        if not mark.is_file():
            continue
        ext_id = mark.read_text(encoding="utf-8").strip()
        if not _ID_RE.match(ext_id):
            continue
        cur = d.name.rsplit("-", 1)[-1]
        try:
            info = fetch_info(ext_id)
        except StoreError as exc:
            out.append({"path": str(d), "ext_id": ext_id, "current": cur,
                        "latest": "", "update_available": False, "error": exc.message})
            continue
        latest = str(info.get("version") or "")
        out.append({
            "path": str(d), "ext_id": ext_id, "current": cur, "latest": latest,
            "update_available": bool(latest and latest != cur),
            "error": "",
        })
    return out

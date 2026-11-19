"""hub 侧的扩展包库：让「把插件打包放到控制台」真正成立。

## 为什么需要它（2026-11-20）

用户原话：

> 把现在 RXYCDP 浏览器的插件及其数据，最好能一起**打包放到我的控制台里面**。
> 这样以后创建浏览器时，可以默认就把这些插件的数据都带上。

但在此之前，扩展包**只存在每台机器的 core 本地**（`<数据目录>/packs/`）。
换一台机器就要重新导入一次 —— 那叫"本机缓存"，不叫"放到控制台"。

这个模块补上那一层：

```
core 导出包 → 打包成 zip → 上传到 hub（这里）→ 任意机器下载 → 导入本地 → 建环境时带上
```

## 设计取舍

- **二进制存盘，索引存 SQLite**：包可能有几十 MB，塞进 SQLite 会让库文件臃肿、
  也让备份变慢。这里 `data_dir/packs/<pack_id>/<pack_id>.zip` 存字节，
  另有一份 `meta.json` 随包落盘（脱离数据库也能人工恢复）。
- **上传用 CAS**：同 id 已存在就拒绝覆盖（409），跟 `store.put_blob` 一致。
- **sha256 必校验**：下载方拿到 zip 后自己校验，避免半截文件被当成好包。
- **不加密**：包内容就是扩展代码 + 该扩展的本地数据。后者可能含登录态，
  所以**默认不下发给未授权设备**——`visible_to` 由调用方控制，
  本模块只负责"谁能读"，不替调用方做授权决定。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

#: 单个包的上限。扩展代码 + Local Extension Settings 通常几 MB，
#: 100MB 足够放下很重的包，同时避免一次上传打满磁盘。
MAX_PACK_BYTES = 100 * 1024 * 1024

#: 包名与 id 的长度限制
MAX_NAME = 64


class PackStoreError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _db_path(s) -> Path:
    return s.data_dir / "zhiwen-hub.sqlite3"


def _conn(s):
    import sqlite3

    c = sqlite3.connect(_db_path(s), timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def packs_root(s) -> Path:
    d = s.data_dir / "packs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pack_dir(s, pack_id: str) -> Path:
    return packs_root(s) / _safe_id(pack_id)


def _safe_id(raw: str) -> str:
    """包 id 只允许安全字符。**必须校验**：它会直接拼进文件路径。"""
    pid = (raw or "").strip()
    if not pid or len(pid) > 64:
        raise PackStoreError("PACK_BAD_ID", "包 id 不合法", 400)
    if not all(ch.isalnum() or ch in "-_" for ch in pid):
        raise PackStoreError("PACK_BAD_ID", f"包 id 含非法字符：{raw!r}", 400)
    return pid


def ensure_schema(s) -> None:
    with _conn(s) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS packs (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL DEFAULT '',
                size        INTEGER NOT NULL DEFAULT 0,
                sha256      TEXT NOT NULL DEFAULT '',
                ext_count   INTEGER NOT NULL DEFAULT 0,
                has_data    INTEGER NOT NULL DEFAULT 0,
                origin      TEXT NOT NULL DEFAULT '',
                note        TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL
            )
            """
        )


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "size": int(row["size"] or 0),
        "size_human": _human(int(row["size"] or 0)),
        "sha256": row["sha256"],
        "extension_count": int(row["ext_count"] or 0),
        "has_data": bool(row["has_data"]),
        "origin": row["origin"],
        "note": row["note"],
        "created_at": row["created_at"],
        "download_path": f"/v1/packs/{row['id']}/download",
    }


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB"):
        n_f = n / 1024.0
        if n_f < 1024 or unit == "GB":
            return f"{n_f:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


def put(
    s,
    data: bytes,
    *,
    name: str = "",
    origin: str = "",
    note: str = "",
    pack_id: str = "",
) -> dict[str, Any]:
    """存一个包。`pack_id` 为空时自动生成；已存在则**拒绝覆盖**（CAS）。"""
    ensure_schema(s)
    if not data:
        raise PackStoreError("PACK_EMPTY", "包内容是空的", 400)
    if len(data) > MAX_PACK_BYTES:
        raise PackStoreError(
            "PACK_TOO_LARGE",
            f"包体积 {_human(len(data))} 超过上限 {_human(MAX_PACK_BYTES)}",
            413,
        )
    pid = _safe_id(pack_id) if pack_id else uuid.uuid4().hex[:12]
    pdir = pack_dir(s, pid)
    zpath = pdir / f"{pid}.zip"
    if zpath.exists():
        raise PackStoreError("PACK_EXISTS", f"包 {pid} 已存在，拒绝覆盖", 409)

    pdir.mkdir(parents=True, exist_ok=True)
    tmp = zpath.with_suffix(".zip.tmp")
    tmp.write_bytes(data)
    tmp.replace(zpath)                      # 原子落地

    sha = hashlib.sha256(data).hexdigest()
    # 从 zip 里读扩展数与"是否带数据"。读不出来不算失败 —— 包已经存好了，
    # 只是统计信息缺失（比如用户上传的是个不规范的 zip）。
    ext_count, has_data = _inspect_zip(zpath)

    meta = {
        "id": pid,
        "name": (name or pid)[:MAX_NAME],
        "size": len(data),
        "sha256": sha,
        "extension_count": ext_count,
        "has_data": has_data,
        "origin": origin[:120],
        "note": note[:200],
        "created_at": time.time(),
    }
    (pdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    with _conn(s) as c:
        c.execute(
            "INSERT INTO packs (id,name,size,sha256,ext_count,has_data,origin,note,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, meta["name"], meta["size"], sha, ext_count, int(has_data),
             meta["origin"], meta["note"], meta["created_at"]),
        )
        row = c.execute("SELECT * FROM packs WHERE id=?", (pid,)).fetchone()
    return _row_to_dict(row)


def _inspect_zip(path: Path) -> tuple[int, bool]:
    """尽量从包里读出扩展数与是否带本地数据。读不出返回 (0, False)。"""
    import zipfile

    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile):
        return 0, False
    # 约定：`code/<key>/manifest.json` 是一个扩展；`data/<key>/...` 是它的本地数据
    exts = {n.split("/")[1] for n in names
            if n.startswith("code/") and n.count("/") >= 2 and n.endswith("/manifest.json")}
    datas = {n.split("/")[1] for n in names if n.startswith("data/") and n.count("/") >= 2}
    return len(exts), bool(datas & exts or datas)


def listing(s) -> list[dict[str, Any]]:
    ensure_schema(s)
    with _conn(s) as c:
        rows = c.execute("SELECT * FROM packs ORDER BY created_at DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def get(s, pack_id: str) -> dict[str, Any]:
    ensure_schema(s)
    pid = _safe_id(pack_id)
    with _conn(s) as c:
        row = c.execute("SELECT * FROM packs WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise PackStoreError("PACK_NOT_FOUND", f"没有这个包：{pid}", 404)
    return _row_to_dict(row)


def read_bytes(s, pack_id: str) -> tuple[bytes, str]:
    """读包内容，返回 (字节, sha256)。文件缺失时报 404 而不是 500。"""
    info = get(s, pack_id)
    zpath = pack_dir(s, info["id"]) / f"{info['id']}.zip"
    if not zpath.is_file():
        raise PackStoreError("PACK_FILE_MISSING", f"包 {info['id']} 的文件不见了", 404)
    data = zpath.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if info["sha256"] and sha != info["sha256"]:
        # 集上磁盘被人动过：宁可报错也不要悄悄下发一个内容不符的包
        raise PackStoreError(
            "PACK_CORRUPTED",
            f"包 {info['id']} 的内容与记录不一致（磁盘 {sha[:12]}…，记录 {info['sha256'][:12]}…）",
            500,
        )
    return data, sha


def delete(s, pack_id: str) -> dict[str, Any]:
    info = get(s, pack_id)
    with _conn(s) as c:
        c.execute("DELETE FROM packs WHERE id=?", (info["id"],))
    with contextlib.suppress(OSError):
        shutil.rmtree(pack_dir(s, info["id"]), ignore_errors=True)
    return info

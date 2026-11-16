"""文件系统存储层。

目录布局（`ZW_DATA_DIR`）：
```
blobs/<profile_id>/v<N>.blob      加密后的快照块
blobs/<profile_id>/v<N>.meta.json 块元信息（清晰可读，便于排错）
latest/<profile_id>.json          当前最新版本指针
leases/<profile_id>.json          跨机租约
logs/                             访问日志
```
所有写操作走"临时文件 + 原子 rename"，避免半写状态。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


class HubError(Exception):
    """带 HTTP 语义的业务错误。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def valid_id(value: str) -> str:
    if not ID_RE.match(value or ""):
        raise HubError(400, "INVALID_ID", f"非法标识：{value!r}（只允许字母数字下划线短横，4-64 位）")
    return value


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class BlobMeta:
    profile_id: str
    revision: int
    size: int
    sha256: str
    key_fp: str
    created_at: float
    source_node: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Store:
    def __init__(self, root: Path, keep_revisions: int = 10) -> None:
        self.root = root
        self.blobs = root / "blobs"
        self.latest = root / "latest"
        self.leases = root / "leases"
        self.keep = keep_revisions
        for d in (self.blobs, self.latest, self.leases):
            d.mkdir(parents=True, exist_ok=True)

    # ── 块 ────────────────────────────────────────────────
    def _blob_path(self, pid: str, rev: int) -> Path:
        return self.blobs / pid / f"v{rev}.blob"

    def _meta_path(self, pid: str, rev: int) -> Path:
        return self.blobs / pid / f"v{rev}.meta.json"

    def put_blob(
        self,
        pid: str,
        rev: int,
        data: bytes,
        key_fp: str,
        source_node: str = "",
        note: str = "",
        cas: bool = True,
    ) -> BlobMeta:
        valid_id(pid)
        if rev < 1:
            raise HubError(400, "INVALID_REVISION", "revision 必须 ≥ 1")
        path = self._blob_path(pid, rev)
        if cas and path.exists():
            raise HubError(409, "REVISION_EXISTS", f"{pid} 的 v{rev} 已存在，拒绝覆盖（如需覆盖请显式 cas=false）")

        meta = BlobMeta(
            profile_id=pid,
            revision=rev,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            key_fp=key_fp,
            created_at=time.time(),
            source_node=source_node,
            note=note,
        )
        atomic_write(path, data)
        atomic_write(self._meta_path(pid, rev), json.dumps(meta.to_dict(), ensure_ascii=False, indent=2).encode())
        self._prune(pid)
        return meta

    def get_blob(self, pid: str, rev: int) -> bytes:
        path = self._blob_path(valid_id(pid), rev)
        if not path.exists():
            raise HubError(404, "BLOB_NOT_FOUND", f"{pid} 的 v{rev} 不存在")
        return path.read_bytes()

    def get_meta(self, pid: str, rev: int) -> BlobMeta:
        path = self._meta_path(valid_id(pid), rev)
        if not path.exists():
            raise HubError(404, "BLOB_NOT_FOUND", f"{pid} 的 v{rev} 不存在")
        return BlobMeta(**json.loads(path.read_text(encoding="utf-8")))

    def list_revisions(self, pid: str) -> list[int]:
        d = self.blobs / valid_id(pid)
        if not d.exists():
            return []
        revs: list[int] = []
        for p in d.glob("v*.meta.json"):
            m = re.match(r"v(\d+)\.meta\.json$", p.name)
            if m:
                revs.append(int(m.group(1)))
        return sorted(revs)

    def delete_blob(self, pid: str, rev: int) -> None:
        for p in (self._blob_path(pid, rev), self._meta_path(pid, rev)):
            if p.exists():
                p.unlink()

    def _prune(self, pid: str) -> None:
        if self.keep <= 0:
            return
        revs = self.list_revisions(pid)
        for rev in revs[: max(0, len(revs) - self.keep)]:
            self.delete_blob(pid, rev)

    # ── latest 指针 ───────────────────────────────────────
    def set_latest(self, pid: str, rev: int, force: bool = False) -> dict[str, Any]:
        valid_id(pid)
        ptr = self.latest / f"{pid}.json"
        current: dict[str, Any] | None = None
        if ptr.exists():
            current = json.loads(ptr.read_text(encoding="utf-8"))
            if current.get("revision", 0) >= rev and not force:
                raise HubError(
                    409,
                    "REVISION_NOT_MONOTONIC",
                    f"当前 latest 是 v{current.get('revision')}，不接受更小或相等的 v{rev}（强制请显式 force=true）",
                )
        meta = self.get_meta(pid, rev)
        payload = {
            "profile_id": pid,
            "revision": rev,
            "etag": f'"v{rev}"',
            "size": meta.size,
            "sha256": meta.sha256,
            "key_fp": meta.key_fp,
            "updated_at": time.time(),
        }
        atomic_write(ptr, json.dumps(payload, ensure_ascii=False, indent=2).encode())
        return payload

    def get_latest(self, pid: str) -> dict[str, Any]:
        ptr = self.latest / f"{valid_id(pid)}.json"
        if not ptr.exists():
            raise HubError(404, "NO_LATEST", f"{pid} 还没有任何已发布的版本")
        return json.loads(ptr.read_text(encoding="utf-8"))

    # ── 租约 ──────────────────────────────────────────────
    def _lease_path(self, pid: str) -> Path:
        return self.leases / f"{valid_id(pid)}.json"

    def acquire_lease(self, pid: str, owner: str, kind: str, ttl: int) -> dict[str, Any]:
        valid_id(pid)
        now = time.time()
        path = self._lease_path(pid)
        if path.exists():
            cur = json.loads(path.read_text(encoding="utf-8"))
            alive = now - float(cur.get("heartbeat", 0)) < float(cur.get("ttl", 0))
            if alive and cur.get("owner") != owner:
                raise HubError(
                    409,
                    "PROFILE_BUSY",
                    f"{pid} 正被 {cur.get('owner')} 持有（{cur.get('kind')}，剩余 "
                    f"{int(float(cur.get('ttl', 0)) - (now - float(cur.get('heartbeat', 0))))} 秒）",
                )
        payload = {
            "profile_id": pid,
            "owner": owner,
            "kind": kind,
            "ttl": ttl,
            "acquired_at": now,
            "heartbeat": now,
        }
        atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2).encode())
        return payload

    def heartbeat_lease(self, pid: str, owner: str) -> dict[str, Any]:
        path = self._lease_path(pid)
        if not path.exists():
            raise HubError(404, "LEASE_NOT_FOUND", f"{pid} 没有租约")
        cur = json.loads(path.read_text(encoding="utf-8"))
        if cur.get("owner") != owner:
            raise HubError(409, "LEASE_OWNER_MISMATCH", f"{pid} 的租约属于 {cur.get('owner')}")
        cur["heartbeat"] = time.time()
        atomic_write(path, json.dumps(cur, ensure_ascii=False, indent=2).encode())
        return cur

    def release_lease(self, pid: str, owner: str) -> None:
        path = self._lease_path(pid)
        if not path.exists():
            return
        cur = json.loads(path.read_text(encoding="utf-8"))
        if cur.get("owner") not in (owner, "*"):
            raise HubError(409, "LEASE_OWNER_MISMATCH", f"{pid} 的租约属于 {cur.get('owner')}")
        path.unlink()

    def get_lease(self, pid: str) -> dict[str, Any] | None:
        path = self._lease_path(pid)
        if not path.exists():
            return None
        cur = json.loads(path.read_text(encoding="utf-8"))
        cur["alive"] = (time.time() - float(cur.get("heartbeat", 0))) < float(cur.get("ttl", 0))
        return cur

    # ── 索引 ──────────────────────────────────────────────
    def index(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for d in sorted(self.blobs.iterdir()) if self.blobs.exists() else []:
            if not d.is_dir():
                continue
            pid = d.name
            revs = self.list_revisions(pid)
            if not revs:
                continue
            latest: dict[str, Any] | None
            try:
                latest = self.get_latest(pid)
            except HubError:
                latest = None
            total = sum((self._blob_path(pid, r).stat().st_size for r in revs if self._blob_path(pid, r).exists()), 0)
            out.append(
                {
                    "profile_id": pid,
                    "revisions": len(revs),
                    "latest_revision": latest.get("revision") if latest else None,
                    "latest_size": latest.get("size") if latest else None,
                    "key_fp": latest.get("key_fp") if latest else None,
                    "updated_at": latest.get("updated_at") if latest else None,
                    "total_bytes": total,
                    "lease": self.get_lease(pid),
                }
            )
        return out

    def stats(self) -> dict[str, Any]:
        idx = self.index()
        return {
            "profiles": len(idx),
            "total_bytes": sum(i["total_bytes"] for i in idx),
            "revisions": sum(i["revisions"] for i in idx),
            "data_dir": str(self.root),
        }

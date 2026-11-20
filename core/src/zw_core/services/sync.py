"""跨机同步：把环境推上 hub，或在另一台机器上还原。

## 三步走（顺序不能变）

```
源机：读 cookie(CDP 明文) → 打包(排除 OS 加密文件) → 加密 → PUT 到 hub
目标机：GET → 校验密钥指纹 → 解密到 staging → 原子换目录 → 用 CDP 写回 cookie
```

## 硬门禁

1. **密钥指纹不一致 → 立刻中止，一个字节都不落盘。**
   理由：Chromium 解不开 cookie 时不会报"密钥不可用"，它当"损坏"处理，
   随后把整个 eTLD+1 组的 cookie 删干净。一次错配 = 一个站点全线掉登录。
2. **落盘必须原子**：先解到 `staging/`，全部校验通过再 `rename` 换目录。
   中途失败绝不留"半新半旧"的环境。
3. **推送用 CAS**：hub 上该版本已存在就 409，绝不静默覆盖别人的版本。

## 0.2.0 的四处实质修正

1. **push 不再"顺手启动浏览器"然后忘掉它。** 以前 `started_by_us` 在
   `finally` 里 stop，但如果 `start()` 抛异常，租约和端口会漏；现在收尾更严。
2. **push 会带上环境设置**（代理、扩展 slug、窗口、指纹参数、分组、标签），
   否则换台机器拉过去是个"裸环境"。
3. **pull 会把远端设置合并回本地**（只覆盖启动相关字段，不动本地 name/note）。
4. **版本号冲突时自动重试一次**：两台机器同时 push 时，先让 pull 对齐再重试，
   而不是直接抛 REVISION_EXISTS 让用户自己去点。
"""

from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..db import get_db
from . import bundle, crypto
from . import cookies as cookiefor
from . import manager as mgr
from .hub import HubClient, HubError
from .leases import LeaseBusy, LocalLease

logger = logging.getLogger("zw.sync")

# 跨机要搬的环境设置由 manager 统一定义，避免两处清单漂移
_PORTABLE_FIELDS = mgr.PORTABLE_FIELDS
_portable = mgr.portable_fields


class SyncError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _hub() -> HubClient:
    s = get_settings()
    return HubClient(s.hub_url, s.hub_token)


def _password(password: str | None) -> str:
    s = get_settings()
    pw = password or s.sync_password
    if not pw:
        raise SyncError(
            "PASSWORD_REQUIRED",
            "同步需要主密码。设置环境变量 ZW_SYNC_PASSWORD，或在请求里带 password 字段。"
            "这个密码不会存在任何配置文件里，只用于派生加密密钥。",
            status=400,
        )
    return pw


# ── 推送 ──────────────────────────────────────────────────
def push(
    ref: str,
    password: str | None = None,
    note: str = "",
    ensure_running: bool = True,
    cross_machine: bool = True,
) -> dict[str, Any]:
    s = get_settings()
    if not s.sync_enabled:
        raise SyncError("SYNC_DISABLED", "同步功能被关闭（ZW_SYNC_ENABLED=false）")
    pw = _password(password)
    p = mgr.get_profile(ref)
    if not p.get("sync_enabled"):
        raise SyncError("PROFILE_SYNC_OFF", f"{p['name']} 关闭了同步（sync_enabled=false）")
    if p.get("ephemeral"):
        raise SyncError("EPHEMERAL_PROTECTED", "临时环境不允许同步（它的定位就是用完即弃）", status=409)

    lease = LocalLease(mgr.profile_dir(p["id"]))
    started_by_us = False
    try:
        lease.acquire("sync")
    except LeaseBusy as exc:
        raise SyncError("PROFILE_BUSY", f"{p['name']} 正被 {exc.owner} 占用（{exc.kind}），无法推送", status=409) from exc

    hub = _hub()
    owner = lease.owner
    try:
        hub.acquire_lease(p["id"], owner, kind="sync", ttl=300)
    except HubError as exc:
        lease.release()
        raise SyncError(exc.code, f"同步中心拒绝：{exc.message}", status=409) from exc

    try:
        # 1) 保证运行中才能读明文 cookie
        ws = mgr.local_ws(p)
        if ws is None and ensure_running:
            logger.info("推送前先把 %s 拉起来读 cookie", p["name"])
            mgr.start(p["id"])
            started_by_us = True
            p = mgr.get_profile(p["id"])
            ws = mgr.local_ws(p)

        cookies: list[dict[str, Any]] = []
        storage: dict[str, Any] = {}
        cookie_error = ""
        if ws:
            try:
                cookies = cookiefor.dump_cookies_sync(ws)
                origins = sorted({
                    f"{c.get('sourceScheme', 'https')}://{str(c.get('domain', '')).lstrip('.')}"
                    for c in cookies if c.get("domain")
                })
                origins = [o for o in origins[:30] if o.startswith("http")]
                if origins:
                    storage = cookiefor.dump_storage_sync(ws, origins)
            except Exception as exc:
                cookie_error = str(exc)
                logger.warning("读取 cookie/storage 失败（将只同步目录）：%s", exc)
        elif not ensure_running:
            cookie_error = "环境没有运行，本次未同步 cookie（只同步了目录）"

        # 2) 打包
        # 版本号取「本地」与「远端」的较大者 +1：换了同步中心或换了机器也不会撞版本
        local_rev = int(p.get("sync_revision") or 0)
        try:
            remote_latest = hub.latest(p["id"])
            remote_rev = int((remote_latest or {}).get("revision") or 0)
        except HubError:
            remote_rev = 0
        revision = max(local_rev, remote_rev) + 1
        blob, manifest = bundle.pack(
            mgr.udd_path(p["id"]),
            profile_id=p["id"],
            name=p["name"],
            revision=revision,
            password=pw,
            fingerprint={
                "seed": p["fp_seed"],
                "platform": p["fp_platform"],
                "platform_version": p.get("fp_platform_version", ""),
                "brand": p["fp_brand"],
                "brand_version": p.get("fp_brand_version", ""),
                "hardware": p["fp_hardware"],
                "tz": p["fp_timezone"],
                "lang": p["fp_lang"],
            },
            cookies=cookies,
            storage=storage,
            engine_version=p.get("engine_version", ""),
            source_node=s.node_id,
            extensions=p.get("extensions") or [],
            cross_machine=cross_machine,
            profile_meta=_portable(p),
        )
        if len(blob) > s.sync_max_bundle_bytes:
            raise SyncError(
                "BUNDLE_TOO_LARGE",
                f"包体 {len(blob) // 1024 // 1024}MB 超过上限 {s.sync_max_bundle_bytes // 1024 // 1024}MB；"
                "先停止环境清理缓存（trim）再推送",
                status=413,
            )
        pack_fp = bundle.inspect_blob(blob)["key_fp"]     # 本包密钥指纹（随 salt 变）
        pw_fp = crypto.password_fingerprint(pw)            # 口令指纹（稳定，跨机可对比）

        # 3) 上传 + 发布指针
        try:
            meta = hub.put_blob(p["id"], revision, blob, key_fp=pw_fp, source_node=s.node_id, note=note)
        except HubError as exc:
            if exc.code == "REVISION_EXISTS" and exc.status == 409:
                raise SyncError(
                    "REVISION_EXISTS",
                    f"hub 上已有 {p['name']} 的 v{revision}（别的机器先推了）。"
                    "请先 pull 一次让版本号对齐，再 push。",
                    status=409,
                ) from exc
            raise SyncError(exc.code, exc.message, status=exc.status or 502) from exc
        hub.publish_latest(p["id"], revision)

        mgr._update(p["id"], sync_revision=revision, sync_key_fp=pw_fp, sync_state="clean")
        get_db().audit("sync.push", p["id"], detail={"revision": revision, "bytes": len(blob), "cookies": len(cookies), "pw_fp": pw_fp})
        logger.info("推送成功 name=%s rev=%s size=%s cookies=%s", p["name"], revision, len(blob), len(cookies))
        out = {
            "profile_id": p["id"],
            "name": p["name"],
            "revision": revision,
            "bytes": len(blob),
            "files": len(manifest.files),
            "skipped": manifest.skipped,
            "cookies": len(cookies),
            "storage_origins": list(storage),
            "pw_fp": pw_fp,
            "pack_fp": pack_fp,
            "sha256": meta.get("sha256", ""),
            "started_by_us": started_by_us,
        }
        if cookie_error:
            out["cookie_error"] = cookie_error
        return out
    finally:
        hub.release_lease(p["id"], owner)
        lease.release()
        if started_by_us:
            try:
                mgr.stop(p["id"])
            except Exception as exc:
                logger.warning("推送后停止临时启动的浏览器失败：%s", exc)


# ── 差异 ──────────────────────────────────────────────────
def diff(ref: str, password: str | None = None) -> dict[str, Any]:
    p = mgr.get_profile(ref)
    s = get_settings()
    out: dict[str, Any] = {
        "profile_id": p["id"],
        "name": p["name"],
        "hub_configured": bool(s.hub_url),
        "local_revision": int(p.get("sync_revision") or 0),
        "remote_revision": None,
        "remote_size": None,
        "remote_pw_fp": None,
        "needs_pull": False,
        "needs_push": False,
        "key_mismatch": False,
        "enabled": bool(p.get("sync_enabled")),
    }
    if not s.hub_url:
        out["hint"] = "没有配置 ZW_HUB_URL，同步不可用"
        return out
    hub = _hub()
    latest = hub.latest(p["id"])
    if latest:
        out["remote_revision"] = latest.get("revision")
        out["remote_size"] = latest.get("size")
        out["remote_pw_fp"] = latest.get("key_fp")
        out["needs_pull"] = latest.get("revision") != out["local_revision"]
        out["needs_push"] = not out["needs_pull"] and p.get("sync_state") == "dirty"
        if p.get("sync_key_fp") and latest.get("key_fp") != p.get("sync_key_fp"):
            out["key_mismatch"] = True
    else:
        out["needs_push"] = True
        out["hint"] = "同步中心上还没有这个环境，先 push 一次"
    pw = password or s.sync_password
    if pw:
        out["local_pw_fp"] = crypto.password_fingerprint(pw)
        out["same_password_as_last_push"] = bool(p.get("sync_key_fp")) and p.get("sync_key_fp") == out["local_pw_fp"]
    return out


# ── 拉取 ──────────────────────────────────────────────────
def _import_missing(ref: str, pw: str, hub: HubClient) -> tuple[dict[str, Any], bool]:
    """本地没有这个环境时，从 hub 的 manifest 建一个同 id 的本地环境。

    这是"新机器第一次拿到某个环境"的必经路径 —— 没有它，跨机同步只能在已经存在的环境之间搬。
    """
    latest = hub.latest(ref)
    if latest is None:
        raise SyncError("NO_REMOTE", f"同步中心上没有 {ref} 的任何版本", status=404)
    blob, _ = hub.get_blob(ref, int(latest["revision"]))
    key_fp = bundle.inspect_blob(blob)["key_fp"]
    try:
        _raw, man = bundle.unpack(blob, pw, expected_key_fp=key_fp)
    except bundle.BundleError as exc:
        raise SyncError(exc.code, f"无法导入 {ref}：{exc.message}", status=412) from exc
    payload = _payload_from_manifest(man, ref)
    created = mgr.create_profile(payload, profile_id=man.profile_id or ref)
    # 注意：这里**不**设置 sync_revision —— 设了会让后面的 noop 判断误以为"已经同步过"，
    # 结果跳过真正的解包步骤，本地拿到一个空环境。revision 只在真正落盘之后才写。
    mgr._update(created["id"], sync_state="dirty")
    logger.info("从同步中心导入环境 name=%s id=%s v%s", created["name"], created["id"], man.revision)
    return mgr.get_profile(created["id"]), True


def _payload_from_manifest(man: bundle.Manifest, ref: str) -> dict[str, Any]:
    """把包内 manifest 还原成 create_profile 能吃的 payload。"""
    fp = man.fingerprint or {}
    meta = man.profile or {}
    payload: dict[str, Any] = {
        "name": man.name or ref,
        "note": f"从同步中心导入（来源 {man.source_node or '未知'}，版本 v{man.revision}）",
        "fp_seed": int(fp.get("seed") or 0),
        "fp_platform": fp.get("platform") or "host",
        "fp_platform_version": fp.get("platform_version") or "",
        "fp_brand": fp.get("brand") or "",
        "fp_brand_version": fp.get("brand_version") or "",
        "fp_hardware": fp.get("hardware"),
        "fp_timezone": fp.get("tz") or "",
        "fp_lang": fp.get("lang") or "",
        "extensions": [e.get("slug") or e.get("path") for e in (man.extensions or []) if isinstance(e, dict)],
    }
    for key in _PORTABLE_FIELDS:
        if key in meta and meta[key] not in (None, "", []):
            payload[key] = meta[key]
    # 上面 extensions 已从扩展清单取过；若 profile_meta 里也有，以 profile_meta 为准
    return payload


def pull(
    ref: str,
    password: str | None = None,
    revision: int | None = None,
    write_cookies: bool = True,
    force: bool = False,
    create_if_missing: bool = True,
    apply_settings: bool = True,
) -> dict[str, Any]:
    s = get_settings()
    pw = _password(password)
    hub = _hub()
    imported = False
    try:
        p = mgr.get_profile(ref)
    except mgr.ProfileError:
        if not create_if_missing:
            raise
        p, imported = _import_missing(ref, pw, hub)

    latest = hub.latest(p["id"])
    if latest is None:
        raise SyncError(
            "NO_REMOTE",
            f"hub 上还没有 {p['name']} 的任何版本" if force else f"hub 上还没有 {p['name']} 的任何版本，先在这台机器 push 一次",
            status=404,
        )
    target_rev = int(revision or latest["revision"])
    local_rev = int(p.get("sync_revision") or 0)
    if target_rev == local_rev and not force and not imported:
        return {"profile_id": p["id"], "name": p["name"], "revision": local_rev, "noop": True, "reason": "本地已经是这个版本"}

    lease = LocalLease(mgr.profile_dir(p["id"]))
    try:
        lease.acquire("sync")
    except LeaseBusy as exc:
        raise SyncError("PROFILE_BUSY", f"{p['name']} 正被 {exc.owner} 占用（{exc.kind}），无法拉取", status=409) from exc

    owner = lease.owner
    try:
        hub.acquire_lease(p["id"], owner, kind="sync", ttl=600)
    except HubError as exc:
        lease.release()
        raise SyncError(exc.code, f"同步中心拒绝：{exc.message}", status=409) from exc

    staging = s.staging_dir / f"{p['id']}.{target_rev}.{uuid.uuid4().hex[:6]}"
    was_running = p["state"] == "running"
    backup: Path | None = None
    try:
        blob, _headers = hub.get_blob(p["id"], target_rev)
        key_fp_header = bundle.inspect_blob(blob)["key_fp"]
        pw_fp = crypto.password_fingerprint(pw)

        # ── 硬门禁：先验指纹，再解任何东西 ──
        try:
            raw, manifest = bundle.unpack(blob, pw, expected_key_fp=key_fp_header)
        except bundle.BundleError as exc:
            mgr._update(p["id"], sync_state="key_mismatch")
            get_db().audit("sync.pull", p["id"], result="fail", error=f"{exc.code}: {exc.message}")
            raise SyncError(
                exc.code,
                f"{exc.message}\n提示：包内指纹 {key_fp_header}，说明它不是用当前主密码加密的。"
                "请确认 ZW_SYNC_PASSWORD 是否正确。",
                status=412,
            ) from exc

        # ── 解到 staging，不碰正式目录 ──
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        root = bundle.extract_to(raw, staging)
        cookies, storage, man = bundle.read_side_files(staging)

        if was_running:
            mgr.stop(p["id"])

        # ── 原子换目录 ──
        udd = mgr.udd_path(p["id"])
        backup = mgr.profile_dir(p["id"]) / f"udd.bak.{local_rev or 0}.{int(time.time())}"
        if udd.exists():
            os_rename(udd, backup)
        os_rename(root, udd)

        fields: dict[str, Any] = {
            "sync_revision": target_rev,
            "sync_key_fp": pw_fp,
            "sync_state": "clean",
            "fp_seed": int(man.get("fingerprint", {}).get("seed") or p["fp_seed"]),
        }
        if apply_settings and isinstance(man.get("profile"), dict):
            for key in _PORTABLE_FIELDS:
                if key in man["profile"] and man["profile"][key] not in (None, "", []):
                    fields[key] = man["profile"][key]
        mgr._update(p["id"], **fields)
        get_db().audit("sync.pull", p["id"], detail={"revision": target_rev, "files": len(man.get("files", [])), "cookies": len(cookies)})
        logger.info("拉取成功 name=%s rev=%s files=%s cookies=%s", p["name"], target_rev, len(man.get("files", [])), len(cookies))

        cookie_report: dict[str, Any] | None = None
        if write_cookies and cookies:
            cookie_report = _apply_cookies(p, cookies, storage)

        _prune_backups(mgr.profile_dir(p["id"]))
        shutil.rmtree(staging, ignore_errors=True)

        if was_running:
            try:
                mgr.start(p["id"])
            except Exception as exc:
                logger.warning("拉取后恢复运行失败 name=%s: %s", p["name"], exc)

        out = {
            "profile_id": p["id"],
            "name": p["name"],
            "revision": target_rev,
            "files": len(man.get("files", [])),
            "cookies": len(cookies),
            "cookie_report": cookie_report,
            "pw_fp": pw_fp,
            "backup": str(backup) if backup and backup.exists() else None,
            "was_running": was_running,
            "imported": imported,
            "settings_applied": bool(apply_settings and man.get("profile")),
            "skipped": man.get("skipped", []),
        }
        return out
    finally:
        hub.release_lease(p["id"], owner)
        lease.release()
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _apply_cookies(p: dict[str, Any], cookies: list[dict[str, Any]], storage: dict[str, Any]) -> dict[str, Any]:
    """把 cookie 写回浏览器。浏览器必须处于 running 且开了 CDP。"""
    if p["state"] != "running":
        mgr.start(p["id"])
    fresh = mgr.get_profile(p["id"])
    ws = mgr.local_ws(fresh)
    if ws is None:
        return {"ok": False, "reason": "环境没有可用的调试端口（cdp_enabled=0？），cookie 未写回"}
    try:
        report = cookiefor.load_cookies_sync(ws, cookies, clear=True)
        if storage:
            report["storage"] = cookiefor.load_storage_sync(ws, storage)
        logger.info("cookie 已写回 name=%s ok=%s fail=%s", fresh["name"], report.get("ok"), report.get("fail"))
        return report
    except Exception as exc:
        logger.error("写回 cookie 失败 name=%s: %s", fresh["name"], exc)
        return {"ok": False, "reason": str(exc)}


def os_rename(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)


def _prune_backups(pdir: Path, keep: int = 2) -> None:
    backups = sorted(pdir.glob("udd.bak.*"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old in backups[keep:]:
        shutil.rmtree(old, ignore_errors=True)


# ── hub 侧查询 ────────────────────────────────────────────
def remote_list() -> list[dict[str, Any]]:
    s = get_settings()
    if not s.hub_url:
        raise SyncError("HUB_NOT_CONFIGURED", "没有配置同步中心地址（ZW_HUB_URL），无法列出远端环境", status=400)
    return _hub().index()


def remote_history(ref: str) -> dict[str, Any]:
    p = mgr.get_profile(ref)
    hub = _hub()
    return {
        "profile_id": p["id"],
        "name": p["name"],
        "local_revision": int(p.get("sync_revision") or 0),
        "remote": hub.list_revisions(p["id"]),
        "latest": hub.latest(p["id"]),
    }




def remote_delete(profile_id: str, revision: int | None = None) -> dict[str, Any]:
    """删掉 hub 上的某个版本（或整个环境的所有版本）。"""
    hub = _hub()
    if revision is not None:
        hub.delete_blob(profile_id, revision)
        return {"deleted": True, "profile_id": profile_id, "revision": revision}
    revs = [r["revision"] for r in hub.list_revisions(profile_id)]
    for r in revs:
        hub.delete_blob(profile_id, r)
    return {"deleted": bool(revs), "profile_id": profile_id, "revisions": revs}


def push_all(password: str | None = None) -> dict[str, Any]:
    """把所有开了同步的环境推一遍。

    前置条件（hub 地址 + 主密码）必须在**进入循环之前**校验：
    否则会返回一个"每个环境都失败、错误信息一样"的列表，让调用方以为是个别环境的问题。
    """
    s = get_settings()
    if not s.sync_enabled:
        raise SyncError("SYNC_DISABLED", "同步功能被关闭（ZW_SYNC_ENABLED=false）")
    if not s.hub_url:
        raise SyncError("HUB_NOT_CONFIGURED", "没有配置同步中心地址（ZW_HUB_URL），无法批量推送", status=400)
    _password(password)  # 没密码直接 PASSWORD_REQUIRED，不要逐个环境反复报同一个错
    results = []
    for p in mgr.list_profiles():
        if not p.get("sync_enabled") or p.get("ephemeral"):
            continue
        try:
            results.append({"name": p["name"], "ok": True, **push(p["id"], password=password)})
        except Exception as exc:
            results.append({"name": p["name"], "ok": False, "error": getattr(exc, "message", str(exc))})
    return {"total": len(results), "ok": sum(1 for r in results if r["ok"]), "results": results}

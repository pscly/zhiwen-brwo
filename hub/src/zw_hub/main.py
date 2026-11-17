"""zw-hub HTTP 服务。

所有错误统一返回：
```json
{"ok": false, "code": "PROFILE_BUSY", "message": "……", "detail": {...}}
```
所有成功返回：
```json
{"ok": true, "data": {...}}
```
这个形状是刻意统一的 —— AI 客户端只需要学一次就能解析全部响应。

## 0.2.0 的加固

1. **网段白名单真的生效了。** 0.1.0 里 `allowed_cidrs` 配置项存在但从未被检查过，
   等于一个摆设：任何人只要拿到令牌就能连（而令牌常常被贴进脚本里）。
2. **`/v1/leases/{pid}` 的 owner 从查询串改成 body-free 的 `X-ZW-Owner` 头** ——
   以前 owner 里有主机名，中文主机名会让 `?owner=` 变成非法 URL。
3. **新增 `/v1/blobs/{pid}/{rev}` 的 ETag 与 If-None-Match**，客户端重复拉同一个版本
   不再传全量字节。
4. **`/healthz` 保持不查磁盘**（给 k8s/docker 探针用），
   `/readyz` 才做写盘探测 —— 探针过重会把 hub 自己拖死。
"""

from __future__ import annotations

import hashlib
import contextlib
import hmac
import ipaddress
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .config import get_settings
from .devices import DeviceError, DeviceStore
from .shell import ShellError, ShellQueue
from .termrelay import relay as term_relay
from .store import HubError, Store

logger = logging.getLogger("zw.hub")

app = FastAPI(
    title="知纹同步中心（zw-hub）",
    version=__version__,
    description="局域网内的加密 profile 快照存储与租约协调服务。只存密文，解不开也不需要解。",
)

_store: Store | None = None
_devices: DeviceStore | None = None
_shell_queue: ShellQueue | None = None
# 进程启动时刻，主控台用它算"服务器已运行多久"
_STARTED_AT = time.time()
_shell_queue_lock = threading.Lock()


def store() -> Store:
    global _store
    if _store is None:
        s = get_settings()
        _store = Store(s.data_dir, keep_revisions=s.keep_revisions)
    return _store


def devices() -> DeviceStore:
    global _devices
    if _devices is None:
        s = get_settings()
        _devices = DeviceStore(s.data_dir / "devices.db")
    return _devices


def shell_queue() -> ShellQueue:
    """远程命令队列（懒加载单例）。

    和其它两个单例不同，这里必须双检加锁：ShellQueue 的**状态在内存里**，
    如果两个并发首请求各自造一个实例，投递进来的命令只有其中一个看得见，
    设备另一侧就会永远空转。多一把锁比"偶发丢命令"便宜得多。
    """
    global _shell_queue
    if _shell_queue is None:
        with _shell_queue_lock:
            if _shell_queue is None:
                s = get_settings()
                _shell_queue = ShellQueue(
                    s.data_dir / "shell.db",
                    max_output=s.shell_max_output,
                    queue_limit=s.shell_queue_limit,
                )
    return _shell_queue


# ── 响应包装 ──────────────────────────────────────────────
def ok(data: Any = None) -> dict[str, Any]:
    return {"ok": True, "data": data}


@app.exception_handler(HubError)
async def _hub_error_handler(_: Request, exc: HubError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"ok": False, "code": exc.code, "message": exc.message})


@app.exception_handler(DeviceError)
async def _device_error_handler(_: Request, exc: DeviceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"ok": False, "code": exc.code, "message": exc.message})


@app.exception_handler(ShellError)
async def _shell_error_handler(_: Request, exc: ShellError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"ok": False, "code": exc.code, "message": exc.message})


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "code": "INTERNAL", "message": f"{type(exc).__name__}: {exc}"},
    )


# ── 鉴权 ──────────────────────────────────────────────────
def _verify_signature(request: Request, raw: bytes, headers: dict[str, str]) -> None:
    s = get_settings()
    if not s.require_signature:
        return
    ts = headers.get("x-zw-ts", "")
    sig = headers.get("x-zw-sign", "")
    if not ts or not sig:
        raise HubError(401, "SIGNATURE_REQUIRED", "服务端要求请求签名，但请求未带 X-ZW-Ts / X-ZW-Sign")
    try:
        skew = abs(time.time() - float(ts))
    except ValueError as exc:
        raise HubError(401, "SIGNATURE_INVALID", "X-ZW-Ts 不是合法时间戳") from exc
    if skew > 300:
        raise HubError(401, "SIGNATURE_EXPIRED", "请求时间戳超过 ±300 秒，拒绝（防重放）")
    body_hash = hashlib.sha256(raw).hexdigest()
    msg = f"{request.method}\n{request.url.path}\n{ts}\n{body_hash}".encode()
    expect = hmac.new(s.signature_secret.encode(), msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        raise HubError(401, "SIGNATURE_INVALID", "请求签名不匹配")


def _check_cidr(request: Request) -> None:
    """来源网段白名单。

    0.1.0 里有这个配置项但**从来没有实现**，所以"只允许局域网"的意图其实没有生效。
    现在是 fail-closed：配了名单就只放行名单内的地址，解析不出 IP 也拒。
    """
    s = get_settings()
    if not s.allowed_cidrs:
        return
    ip_text = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError as exc:
        raise HubError(403, "CIDR_FORBIDDEN", f"来源地址无法解析（{ip_text!r}），已按白名单拒绝") from exc
    for cidr in s.allowed_cidrs:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return
        except ValueError:
            logger.warning("ZW_ALLOWED_CIDRS 里有非法网段：%s", cidr)
    raise HubError(403, "CIDR_FORBIDDEN", f"来源 {ip_text} 不在允许网段内：{', '.join(s.allowed_cidrs)}")


async def auth(request: Request, authorization: str = Header(default="")) -> None:
    _check_cidr(request)
    s = get_settings()
    if s.token:
        token = authorization.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token, s.token):
            raise HubError(401, "UNAUTHENTICATED", "访问令牌不正确（请求头需为 Authorization: Bearer <token>）")
    raw = await request.body()
    _verify_signature(request, raw, {k.lower(): v for k, v in request.headers.items()})
    # 设备校验：客户端会带 X-ZW-Device。带了就校验它是否被踢；
    # 不带也放行（curl/脚本/面板之外的老用法保持兼容，且它们本来就要过令牌）。
    dev = request.headers.get("x-zw-device", "").strip()
    if dev:
        devices().require_active(dev)


Auth = Depends(auth)


# ── 健康与元信息 ──────────────────────────────────────────
@app.get("/healthz", summary="存活探针（不查磁盘）")
def healthz() -> dict[str, Any]:
    return ok({"status": "ok", "version": __version__, "ts": time.time()})


@app.get("/readyz", summary="就绪探针（查磁盘可写）")
def readyz() -> Any:
    s = get_settings()
    probe = s.data_dir / ".ready"
    try:
        probe.write_text(str(time.time()))
        probe.unlink()
        writable = True
    except Exception:
        writable = False
    if not writable:
        return JSONResponse(status_code=503, content={"ok": False, "code": "STORAGE_READONLY", "message": "数据目录不可写"})
    return ok({"status": "ready", "storage": str(s.data_dir), "stats": store().stats()})


@app.get("/v1/meta", dependencies=[Auth], summary="服务元信息")
def meta() -> dict[str, Any]:
    s = get_settings()
    return ok(
        {
            "name": "zhiwen-hub",
            "version": __version__,
            "data_dir": str(s.data_dir),
            "token_required": bool(s.token),
            "signature_required": s.require_signature,
            "allowed_cidrs": s.allowed_cidrs,
            "keep_revisions": s.keep_revisions,
            "lease_ttl": s.lease_ttl,
            "max_blob_bytes": s.max_blob_bytes,
            "shell_enabled": s.shell_enabled,
            "shell_max_output": s.shell_max_output,
            "server_time": time.time(),
        }
    )


@app.get("/v1/ai/manifest", dependencies=[Auth], summary="给 AI 的自描述清单")
def ai_manifest() -> dict[str, Any]:
    """AI 一进来读这个就知道：这是什么、有哪些能力、怎么调、错误怎么解。

    这是"AI 原生"的第一层：不要求模型先读文档，端点本身就是文档。
    """
    return ok(
        {
            "service": "zhiwen-hub",
            "role": "同步中心：托管加密 profile 快照、协调跨机租约",
            "version": __version__,
            "concepts": {
                "profile_id": "环境标识，与 core 侧的 profile id 一致",
                "revision": "单调递增的快照版本号，从 1 开始",
                "key_fp": "客户端主密码派生密钥的指纹（16 位 hex），hub 用它判断快照能否被某台机器解开",
                "lease": "跨机互斥锁，防止两台机器同时使用同一个环境",
                "shell_job": "远程命令任务：管理员投递 → 设备长轮询取走 → 设备回传结果（协议见 docs/13）",
                "machine": "一台装了客户端的电脑（device）。机器上线会注册，心跳带上环境数与浏览器清单",
                "browser": "机器上的一个浏览器环境（profile）。有 cdp_http / cdp_ws 就能远程接管",
            },
            "workflows": {
                "推送": "PUT /v1/blobs/{pid}/{rev} → PUT /v1/latest/{pid}",
                "拉取": "GET /v1/latest/{pid} → GET /v1/blobs/{pid}/{rev}",
                "占用环境": "POST /v1/leases/{pid} → 定期 POST /v1/leases/{pid}/heartbeat → DELETE /v1/leases/{pid}?owner=",
                "远程命令": "POST /v1/devices/{device_id}/shell → 设备 GET /v1/devices/shell/next → 设备 POST /v1/devices/shell/{job_id}/result",
                "先看有什么": "GET /v1/console/overview → 拿到在线机器、每台的浏览器清单与 CDP 地址；再决定调 shell 还是连 CDP",
                "允许一台新机器接入": "控制台里点「允许」；接口是 POST /v1/console/machines/{device_id}/approve。未允许前它不能同步，但**远程终端可用**",
                "接管某个浏览器": "从 overview 里取该浏览器的 cdp_ws，用 Playwright 的 connectOverCDP 连上去；core_base 不可达时改用远程终端在那台机器上操作",
            },
            "errors": {
                "REVISION_EXISTS": "版本号已被占用，换下一个 revision 或先 GET 看最新",
                "REVISION_NOT_MONOTONIC": "latest 指针只允许前进，确认是否漏了 revision",
                "PROFILE_BUSY": "环境被别的机器持有，等租约过期或让对方释放",
                "NO_LATEST": "该环境还没有成功推送过",
                "CIDR_FORBIDDEN": "调用方地址不在 ZW_ALLOWED_CIDRS 里",
                "SHELL_DISABLED": "服务器端关掉了远程命令行（ZW_SHELL_ENABLED=false）",
                "SHELL_QUEUE_FULL": "该设备排队中的命令到顶（默认 200 条），等它执行完再投",
                "SHELL_JOB_NOT_FOUND": "没有这条命令，或它属于别的设备",
                "DEVICE_PENDING": "设备已注册但管理员还没点「允许」。同步会被拒，心跳与远程终端仍可用",
                "DEVICE_REJECTED": "管理员拒绝了这台设备的接入请求",
            },
            "安全模型": "hub 只存 AEAD 密文；主密码不进 hub；传输为局域网 HTTP，完整性由密文自身保证。"
                        "主控台与 /v1/console/* 需要面板会话或 Bearer 令牌；不要暴露到公网",
            "endpoints": [
                {"method": "GET", "path": "/healthz", "desc": "存活"},
                {"method": "GET", "path": "/readyz", "desc": "就绪 + 存储统计"},
                {"method": "GET", "path": "/v1/meta", "desc": "元信息"},
                {"method": "GET", "path": "/v1/index", "desc": "所有环境的版本索引"},
                {"method": "GET", "path": "/v1/stats", "desc": "容量统计"},
                {"method": "PUT", "path": "/v1/blobs/{pid}/{rev}", "desc": "上传加密块（默认 CAS）"},
                {"method": "GET", "path": "/v1/blobs/{pid}/{rev}", "desc": "下载加密块"},
                {"method": "GET", "path": "/v1/blobs/{pid}", "desc": "列出版本"},
                {"method": "DELETE", "path": "/v1/blobs/{pid}/{rev}", "desc": "删版本"},
                {"method": "GET", "path": "/v1/latest/{pid}", "desc": "最新版本指针"},
                {"method": "PUT", "path": "/v1/latest/{pid}", "desc": "推进最新指针"},
                {"method": "POST", "path": "/v1/leases/{pid}", "desc": "抢占租约"},
                {"method": "POST", "path": "/v1/leases/{pid}/heartbeat", "desc": "续租"},
                {"method": "DELETE", "path": "/v1/leases/{pid}", "desc": "释放租约"},
                {"method": "GET", "path": "/v1/leases/{pid}", "desc": "查租约"},
                # ── 远程命令行（设备拉取 + 长轮询，协议见 docs/13）──
                {"method": "GET", "path": "/v1/devices/shell/next", "desc": "设备侧：长轮询取一条待执行命令"},
                {"method": "POST", "path": "/v1/devices/shell/{job_id}/result", "desc": "设备侧：回传执行结果"},
                {"method": "POST", "path": "/v1/devices/{device_id}/shell", "desc": "管理员：投递命令"},
                {"method": "GET", "path": "/v1/devices/{device_id}/shell/{job_id}", "desc": "管理员：查命令状态（可长轮询等终态）"},
                {"method": "GET", "path": "/v1/devices/{device_id}/shell/history", "desc": "管理员：命令历史"},
                {"method": "POST", "path": "/v1/devices/{device_id}/shell/{job_id}/cancel", "desc": "管理员：取消还没下发的命令"},
                # ── 主控台（机器 / 浏览器 / 令牌，契约见 docs/15）──
                {"method": "GET", "path": "/v1/console/overview", "desc": "机器 + 浏览器 + 总数（AI 想知道有哪些机器/浏览器就调这个）"},
                {"method": "GET", "path": "/v1/console/secrets", "desc": "令牌区（明文，敏感，仅管理员）"},
                {"method": "POST", "path": "/v1/console/machines/{device_id}/approve", "desc": "允许一台设备接入（pending/rejected → active）"},
                {"method": "POST", "path": "/v1/console/machines/{device_id}/reject", "desc": "拒绝一台设备的接入请求（记录保留，可再允许）"},
                {"method": "GET", "path": "/", "desc": "主控台页面（人看；AI 用 /v1/console/overview）"},
            ],
        }
    )


@app.get("/v1/index", dependencies=[Auth], summary="全部环境的版本索引")
def index() -> dict[str, Any]:
    return ok({"profiles": store().index()})


@app.get("/v1/stats", dependencies=[Auth], summary="容量统计")
def stats() -> dict[str, Any]:
    return ok(store().stats())


# ── 快照块 ────────────────────────────────────────────────
@app.put("/v1/blobs/{pid}/{rev}", dependencies=[Auth], summary="上传加密快照块")
async def put_blob(
    pid: str,
    rev: int,
    request: Request,
    x_zw_key_fp: str = Header(default=""),
    x_zw_source_node: str = Header(default=""),
    x_zw_note: str = Header(default=""),
    x_zw_no_cas: str = Header(default=""),
) -> dict[str, Any]:
    s = get_settings()
    data = await request.body()
    if not data:
        raise HubError(400, "EMPTY_BODY", "请求体为空，请上传加密后的快照块")
    if len(data) > s.max_blob_bytes:
        raise HubError(413, "BLOB_TOO_LARGE", f"块大小 {len(data)} 超过上限 {s.max_blob_bytes}")
    # 客户端会把中文备注做 URL 编码，这里还原（未编码的旧客户端也能正常工作）
    from urllib.parse import unquote

    decoded = request.headers.get("x-zw-encoded", "") == "url"
    source_node = unquote(x_zw_source_node) if decoded else x_zw_source_node
    note = unquote(x_zw_note) if decoded else x_zw_note
    meta = store().put_blob(
        pid,
        rev,
        data,
        key_fp=x_zw_key_fp or "unknown",
        source_node=source_node,
        note=note,
        cas=not (x_zw_no_cas in ("1", "true")),
    )
    logger.info("上传快照 pid=%s rev=%s size=%s sha=%s", pid, rev, meta.size, meta.sha256[:12])
    return ok(meta.to_dict())


@app.get("/v1/blobs/{pid}/{rev}", dependencies=[Auth], summary="下载加密快照块")
def get_blob(pid: str, rev: int, if_none_match: str = Header(default="")) -> Response:
    meta = store().get_meta(pid, rev)
    etag = f'"v{rev}-{meta.sha256[:12]}"'
    if if_none_match and if_none_match == etag:
        # 客户端已经有这个版本了，不必再传一遍（环境包常有几 MB）
        return Response(status_code=304, headers={"ETag": etag})
    data = store().get_blob(pid, rev)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-ZW-Sha256": meta.sha256,
            "X-ZW-Key-Fp": meta.key_fp,
            "ETag": etag,
        },
    )


@app.get("/v1/blobs/{pid}/{rev}/meta", dependencies=[Auth], summary="读取块元信息")
def blobs_meta(pid: str, rev: int) -> dict[str, Any]:
    return ok(store().get_meta(pid, rev).to_dict())


@app.get("/v1/blobs/{pid}", dependencies=[Auth], summary="列出某环境的全部版本")
def list_blobs(pid: str) -> dict[str, Any]:
    revs = store().list_revisions(pid)
    items = []
    for r in revs:
        m = store().get_meta(pid, r)
        items.append({"revision": r, "size": m.size, "sha256": m.sha256, "key_fp": m.key_fp, "created_at": m.created_at})
    return ok({"profile_id": pid, "revisions": items})


@app.delete("/v1/blobs/{pid}/{rev}", dependencies=[Auth], summary="删除某个版本")
def delete_blob(pid: str, rev: int) -> dict[str, Any]:
    store().delete_blob(pid, rev)
    logger.warning("删除快照 pid=%s rev=%s", pid, rev)
    return ok({"deleted": True, "profile_id": pid, "revision": rev})


# ── latest 指针 ───────────────────────────────────────────
class LatestBody(BaseModel):
    revision: int = Field(ge=1, description="要指向的版本号")
    force: bool = Field(default=False, description="允许回退或覆盖（谨慎）")


@app.get("/v1/latest/{pid}", dependencies=[Auth], summary="读取最新版本指针")
def get_latest(pid: str, if_none_match: str = Header(default="")) -> JSONResponse:
    payload = store().get_latest(pid)
    etag = payload.get("etag", "")
    if if_none_match and if_none_match == etag:
        return JSONResponse(status_code=304, content=None, headers={"ETag": etag})
    return JSONResponse(content=ok(payload), headers={"ETag": etag})


@app.put("/v1/latest/{pid}", dependencies=[Auth], summary="推进最新版本指针")
def put_latest(pid: str, body: LatestBody) -> dict[str, Any]:
    payload = store().set_latest(pid, body.revision, force=body.force)
    return ok(payload)


# ── 租约 ──────────────────────────────────────────────────
class LeaseBody(BaseModel):
    owner: str = Field(min_length=3, description="持有者标识，建议 '<主机名>:<pid>:<随机8位>'")
    kind: str = Field(default="sync", description="用途：run | sync")
    ttl: int = Field(default=0, description="有效期秒数，0 表示用服务端默认")


@app.post("/v1/leases/{pid}", dependencies=[Auth], summary="抢占租约")
def acquire(pid: str, body: LeaseBody) -> dict[str, Any]:
    ttl = body.ttl or get_settings().lease_ttl
    payload = store().acquire_lease(pid, body.owner, body.kind, ttl)
    return ok(payload)


@app.post("/v1/leases/{pid}/heartbeat", dependencies=[Auth], summary="续租")
def heartbeat(pid: str, body: LeaseBody) -> dict[str, Any]:
    return ok(store().heartbeat_lease(pid, body.owner))


@app.delete("/v1/leases/{pid}", dependencies=[Auth], summary="释放租约")
def release(pid: str, owner: str = "") -> dict[str, Any]:
    store().release_lease(pid, owner)
    return ok({"released": True, "profile_id": pid, "owner": owner})


@app.get("/v1/leases/{pid}", dependencies=[Auth], summary="查询租约")
def lease(pid: str) -> dict[str, Any]:
    return ok(store().get_lease(pid))


# ══════════════════════════════════════════════════════════
#  设备注册表（客户端只填 IP 就能用，服务器仍能踢下线）
# ══════════════════════════════════════════════════════════
class EnrollBody(BaseModel):
    device_id: str = Field(min_length=8, max_length=128, description="客户端首次启动生成的随机 ID")
    hostname: str = ""
    platform: str = ""
    arch: str = ""
    app_version: str = ""
    label: str = ""


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@app.get("/v1/enroll-info", summary="客户端首次连接前探测：这台服务器要不要对接码")
def enroll_info() -> dict[str, Any]:
    """客户端在"只填 IP"之后先打这个，就知道后续该怎么做。

    这是"默认不要对接码"的落地：客户端不需要预先知道任何令牌，
    连上就问服务器"我需要什么"，服务器说了算。想收紧就设
    ZW_HUB_TOKEN 并把 ZW_DEVICE_AUTO_APPROVE=false。
    """
    s = get_settings()
    return ok(
        {
            "service": "zhiwen-hub",
            "version": __version__,
            "enroll_endpoint": "/v1/devices/enroll",
            "token_required": bool(s.token),
            "auto_approve": s.auto_approve,
            "heartbeat_seconds": 60,
            "shell_enabled": s.shell_enabled,
            "hint": "先 POST /v1/devices/enroll 带上 device_id；之后所有请求带 X-ZW-Device 头",
        }
    )


@app.post("/v1/devices/enroll", summary="注册设备（或刷新已在册设备）")
def enroll(body: EnrollBody, request: Request) -> dict[str, Any]:
    """设备注册。

    注意这条**故意不做 Bearer 校验**：客户端第一次连上来时手里什么都没有
    （用户只填了 IP）。设备身份靠 device_id 本身，而能不能用由
    `status` 决定 —— 也就是服务器侧的踢下线开关。
    如果部署者设了 ZW_HUB_TOKEN，则注册仍需令牌（客户端里可以填对接码）。
    """
    s = get_settings()
    if s.token:
        auth = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(auth, s.token):
            raise HubError(401, "UNAUTHENTICATED", "这台服务器要求对接码（群里给你的那串），请填到客户端里")
    rec = devices().enroll(
        body.device_id,
        hostname=body.hostname,
        platform=body.platform,
        arch=body.arch,
        app_version=body.app_version,
        label=body.label,
        ip=_client_ip(request),
        approve=bool(get_settings().auto_approve),
    )
    if rec["status"] == "revoked":
        raise DeviceError(403, "DEVICE_REVOKED", "这台设备已被服务器端踢下线，请联系管理员恢复")
    return ok(
        {
            "device": rec,
            "auto_approve": s.auto_approve,
            "token_required": bool(s.token),
            "heartbeat_seconds": 60,
        }
    )


@app.post("/v1/devices/heartbeat", summary="心跳（顺带校验设备是否被踢）")
def device_heartbeat(
    request: Request,
    x_zw_device: str = Header(default=""),
    body: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    """心跳。可捎带本机概况（profiles / running / disk_human / engine），
    让服务器面板能显示"这台机器上有几个环境在跑"，而不只是"在线"。"""
    dev_id = x_zw_device or request.headers.get("x-zw-device", "")
    # 用 require_known 而不是 require_active：未授权的设备也要能上报状态
    # （控制台要显示"它在等授权"、并上报 shell_enabled 与浏览器清单）。
    devices().require_known(dev_id)
    stats = body if isinstance(body, dict) else None
    rec = devices().touch(dev_id, ip=_client_ip(request), stats=stats)
    return ok({"device": rec, "server_time": time.time()})


# ══════════════════════════════════════════════════════════
#  管理面板（浏览器登录用，与 API 令牌是两套东西）
# ══════════════════════════════════════════════════════════
PANEL_COOKIE = "zw_panel"


def _panel_token() -> str:
    """会话 cookie 的值：对 panel_user 做一次带密钥的 HMAC，不需要额外存储。"""
    s = get_settings()
    return hmac.new(s.panel_secret.encode(), f"panel:{s.panel_user}".encode(), hashlib.sha256).hexdigest()


def panel_auth(request: Request) -> str:
    """校验面板会话。返回登录名。"""
    s = get_settings()
    got = request.cookies.get(PANEL_COOKIE, "")
    if not got or not hmac.compare_digest(got, _panel_token()):
        raise HubError(401, "PANEL_UNAUTHENTICATED", "面板未登录或会话已过期")
    return s.panel_user


PanelAuth = Depends(panel_auth)


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """内联图标，避免浏览器因为 404 在 console 里刷错误（看着像 bug）。"""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#5b9dff"/>'
        '<path d="M9 10h14l-8 6 8 6H9" fill="none" stroke="#fff" stroke-width="2.4" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/", include_in_schema=False)
def console_page() -> Response:
    """主控台（见 docs/15-主控台契约.md）。

    `/` 是唯一入口：未登录显示登录框，登录后是机器 / 浏览器 / 令牌 / 事件四个视图。
    旧名 `panel.html` 留着只是兜底 —— 万一 console.html 没打进去，也别 500。
    """
    for name in ("console.html", "panel.html"):
        html = Path(__file__).parent / "web" / name
        if html.exists():
            return HTMLResponse(html.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>知纹主控台</h1><p>页面文件缺失（web/console.html）。</p>", status_code=500)


@app.head("/", include_in_schema=False)
def console_page_head() -> Response:
    """HEAD `/`：只回 200，不返回页面。

    为什么单独写一条：FastAPI 的 `@app.get` **不会**自动支持 HEAD，
    所以 HEAD `/` 原本是 405。而 3.5 上的服务导航页（u.pscly.cn）是用
    `HEAD /__probe/http/8700` → nginx 反代到 `HEAD http://127.0.0.1:8700/`
    来判断服务是否在线的 —— 405 会让那个状态点永远是灰的，看起来像挂了。
    """
    return Response(status_code=200)


@app.get("/panel", include_in_schema=False)
def panel_page() -> Response:
    """旧地址：永久 302 到 `/`，老书签不能断（契约 §一）。"""
    return RedirectResponse("/", status_code=302)


@app.post("/panel/api/login", include_in_schema=False)
def panel_login(body: LoginBody, response: Response, request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    """面板登录。

    这里也走 `_check_cidr`，而不是只在 `/v1/*` 上检查。
    原因：面板密码是"人记的短口令"，一旦泄漏，LAN 之外的人本来可以直接登进来；
    配上 `ZW_ALLOWED_CIDRS` 之后，即使密码被知道，非白名单来源也进不来。
    """
    if request is not None:
        _check_cidr(request)
    s = get_settings()
    ok_user = hmac.compare_digest(body.username.strip(), s.panel_user)
    ok_pass = hmac.compare_digest(body.password, s.panel_password)
    if not (ok_user and ok_pass):
        # 统一错误信息，不告诉对方是用户名错还是密码错
        raise HubError(401, "PANEL_BAD_CREDENTIALS", "登录名或密码不正确")
    response.set_cookie(
        PANEL_COOKIE,
        _panel_token(),
        max_age=s.panel_session_ttl,
        httponly=True,
        samesite="lax",
        path="/",
    )
    logger.info("面板登录成功 user=%s ip=%s", body.username, "-")
    return ok({"user": s.panel_user, "ttl": s.panel_session_ttl})


@app.post("/panel/api/logout", include_in_schema=False)
def panel_logout(response: Response) -> dict[str, Any]:
    response.delete_cookie(PANEL_COOKIE, path="/")
    return ok({"logged_out": True})


# ── 代理库（服务器与用户机共享同一份）────────────────────────
# 用户要求：「代理可以在服务器里面进行添加管控，也可以在用户机上添加管控，
# 都是一样的…用户机上添加过的代理，都会自动同步到服务器里面，也可以在服务器
# 里面删除，或者隐藏」。
#
# 所以它不是"服务器下发配置"，而是一份**共享库**：两边都能加，
# 客户端加了会自动推上来；服务器加了，客户端下次同步会拉下去。


class ProxyBody(BaseModel):
    scheme: str = "http://"
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""      # 空串 = 不改已有密码（编辑时不必每次重填）
    hop: str = ""
    label: str = ""
    note: str = ""


class ProxyHideBody(BaseModel):
    hidden: bool = True


class DeviceProxySyncBody(BaseModel):
    """设备上报本机代理库。`password` 允许为空（已在服务器上的不必每次重传）。"""

    items: list[dict[str, Any]] = []


def _proxy_payload(item: dict[str, Any]) -> dict[str, Any]:
    """内部 dict → 接口形状（把 ProxyError 变成 HTTP）。"""
    return item


@app.get("/v1/proxies", dependencies=[PanelAuth])
def list_proxies(include_hidden: bool = False) -> dict[str, Any]:
    """服务器侧代理库。默认不显示被隐藏的条目。"""
    from . import proxies as px

    s = get_settings()
    items = px.list_all(s, include_hidden=include_hidden)
    return ok({"proxies": items, "count": len(items), "include_hidden": include_hidden})


@app.post("/v1/proxies", dependencies=[PanelAuth])
def create_or_update_proxy(body: ProxyBody) -> dict[str, Any]:
    from . import proxies as px

    s = get_settings()
    if not body.host.strip():
        raise HubError(400, "PROXY_INVALID", "主机不能为空")
    if not (0 < int(body.port) < 65536):
        raise HubError(400, "PROXY_INVALID_PORT", "端口应在 1-65535")
    try:
        item = px.upsert(
            s, scheme=px.norm_scheme(body.scheme),
            host=body.host.strip(), port=int(body.port),
            username=body.username, password=body.password,
            hop=body.hop, label=body.label, note=body.note, origin="panel",
        )
    except px.ProxyError as exc:
        raise HubError(exc.status, exc.code, exc.message) from exc
    logger.info("服务器侧新增/更新代理 id=%s upstream=%s", item["id"], item["upstream"])
    return ok(item)


@app.patch("/v1/proxies/{proxy_id}", dependencies=[PanelAuth])
def patch_proxy(proxy_id: int, body: ProxyBody) -> dict[str, Any]:
    """**就地修改**一条代理（编辑用）。

    ## 为什么需要这个路由（而不是继续用 POST）

    `POST /v1/proxies` 走 `upsert()`，去重键是 `scheme+host+port+username+hop`。
    界面上"编辑"改主机/端口时，新键在库里不存在 → 结果**多出一条新记录**，
    旧那条还在，环境仍指向旧的，新那条还因为编辑框留空而没有密码。
    也就是说"编辑"实际变成了"新增"。

    这个路由按 `id` 定位、原地更新，并保留 `origin` 与未填写的密码；
    新键若与另一条记录冲突则返回 409 `PROXY_DUPLICATE`，不做静默合并。
    """
    from . import proxies as px

    s = get_settings()
    if not body.host.strip():
        raise HubError(400, "PROXY_INVALID", "主机不能为空")
    if not (0 < int(body.port) < 65536):
        raise HubError(400, "PROXY_INVALID_PORT", "端口应在 1-65535")
    try:
        item = px.update_by_id(
            s, proxy_id,
            scheme=body.scheme, host=body.host.strip(), port=int(body.port),
            username=body.username, password=body.password,
            hop=body.hop, label=body.label, note=body.note,
        )
    except px.ProxyError as exc:
        raise HubError(exc.status, exc.code, exc.message) from exc
    return ok(item)


@app.post("/v1/proxies/{proxy_id}/hide", dependencies=[PanelAuth])
def hide_proxy(proxy_id: int, body: ProxyHideBody) -> dict[str, Any]:
    """隐藏/取消隐藏。不影响已经在用它的环境（删除才会）。"""
    from . import proxies as px

    s = get_settings()
    try:
        item = px.set_hidden(s, proxy_id, bool(body.hidden))
    except px.ProxyError as exc:
        raise HubError(exc.status, exc.code, exc.message) from exc
    return ok(item)


@app.delete("/v1/proxies/{proxy_id}", dependencies=[PanelAuth])
def delete_proxy(proxy_id: int) -> dict[str, Any]:
    from . import proxies as px

    s = get_settings()
    try:
        return ok(px.delete(s, proxy_id))
    except px.ProxyError as exc:
        raise HubError(exc.status, exc.code, exc.message) from exc


@app.post("/v1/devices/proxies")
def device_push_proxies(body: DeviceProxySyncBody, request: Request) -> dict[str, Any]:
    """设备把本机代理库推上来（用户机加了代理 → 自动同步到服务器）。

    只接受"这台设备"自己上报，origin 记成它的 device_id，
    面板上就能看出这条是哪个机器加的。
    """
    from . import proxies as px

    s = get_settings()
    dev = _device_id_of(request)
    devices().require_known(dev)      # 被踢/被拒绝的设备不许写库
    try:
        res = px.merge_from_device(s, body.items, origin=dev)
    except px.ProxyError as exc:
        raise HubError(exc.status, exc.code, exc.message) from exc
    logger.info("设备 %s 同步代理 %s 条（新增 %s / 更新 %s）",
                dev[:16], res["total"], res["created"], res["updated"])
    return ok(res)


@app.get("/v1/devices/proxies")
def device_pull_proxies(request: Request, with_secrets: bool = True) -> dict[str, Any]:
    """设备拉取服务器侧代理库（服务器加了代理 → 用户机下次同步就能用）。

    `with_secrets=true` 时把密码一起给它 —— 这就是"服务器加的代理用户机也能用"
    必须的一步。只发给**已注册且未撤销**的设备（`RequireKnown`），
    并且只走这条设备专用的接口，列表接口永远不带明文。
    """
    from . import proxies as px

    s = get_settings()
    dev = _device_id_of(request)
    devices().require_known(dev)      # 明文密码只发给已注册且未撤销的设备
    items = px.list_all(s, include_hidden=False)
    if with_secrets:
        for it in items:
            try:
                it["password"] = px.secret_of(s, int(it["id"])) if it.get("has_password") else ""
            except px.ProxyError as exc:
                # 单条解不开不能拖垮整次同步：标记出来，让客户端保留本地密码
                it["password"] = ""
                it["secret_error"] = exc.message
    logger.debug("设备 %s 拉取代理 %s 条", dev[:16], len(items))
    return ok({"proxies": items, "count": len(items), "with_secrets": with_secrets})


@app.get("/panel/api/meta", dependencies=[PanelAuth], include_in_schema=False)
def panel_meta(request: Request) -> dict[str, Any]:
    _check_cidr(request)
    s = get_settings()
    # 仍然用出厂默认凭据时，面板顶部会挂一条提醒 —— 不拦操作，但要让人看见。
    # 默认值是 admin/1234，公开可查，只适合"先跑起来"。
    return ok({
        "version": __version__,
        "data_dir": str(s.data_dir),
        "auto_approve": s.auto_approve,
        "token_required": bool(s.token),
        "user": s.panel_user,
        "default_credentials": (s.panel_user == "admin" and s.panel_password == "1234"),
    })


@app.get("/panel/api/whoami", include_in_schema=False)
def panel_whoami(request: Request) -> dict[str, Any]:
    """未登录也返回 200（logged_in=false）。

    为什么不用 stats 探活：那会在浏览器 console 里留下一条 401，
    看着像页面出错了。这里明确区分"没登录"（正常状态）和"接口挂了"。
    """
    got = request.cookies.get(PANEL_COOKIE, "")
    logged = bool(got) and hmac.compare_digest(got, _panel_token())
    return ok({"logged_in": logged})


# ══ 主控台接口（见 docs/15-主控台契约.md）══════════════════════
@app.get("/v1/console/overview", summary="主控台首屏：机器 + 浏览器 + 总数")
def console_overview(request: Request, probe: bool = True) -> dict[str, Any]:
    """一次拉完主控台要展示的东西。

    AI 也用它：`GET /v1/console/overview` 就知道有哪些机器在线、每台上有哪些
    浏览器、CDP 地址是什么，再决定下一步调 shell 还是连 CDP。

    `probe=true` 时会**尽力**探测每台设备的 `core_base` 是否可达
    （300ms 超时，失败只标 false，不影响返回）。NAT 后面的机器探测必然失败，
    这是预期结果，不是错误 —— 所以这个字段是 true/false/null 三态。
    """
    who = admin_auth(request)
    s = get_settings()
    now = time.time()
    devs = devices().listing()

    machine_rows: list[dict[str, Any]] = []
    total_browsers = 0
    total_running = 0
    for d in devs:
        inv = d.get("inventory") or []
        base = str(d.get("core_base") or "")
        reachable: bool | None = None
        if probe and base:
            reachable = _probe_core(base)
        browsers = []
        for it in inv:
            path = str(it.get("cdp_path") or "")
            http_url = (base.rstrip("/") + path) if (base and path) else ""
            browsers.append({
                "id": it.get("id", ""),
                "name": it.get("name", "") or "（未命名）",
                "state": it.get("state", ""),
                "group": it.get("group", ""),
                "cdp_http": http_url,
                # ws 地址要连上 /json/version 才知道 uuid，这里给的是
                # "浏览器级 ws 的入口前缀"，前端提示用户点开 http 拿真实 ws。
                "cdp_ws": (http_url.replace("http://", "ws://").replace("https://", "wss://") + "/devtools/browser/") if http_url else "",
                # 代理摘要（已脱敏，见 devices._clean_proxy）。控制台的「代理」列用它。
                "proxy": it.get("proxy") or {"set": False},
            })
        total_browsers += len(browsers)
        total_running += int(d.get("running") or 0)
        machine_rows.append({
            "device_id": d.get("device_id", ""),
            "label": d.get("label", ""),
            "hostname": d.get("hostname", ""),
            "platform": d.get("platform", ""),
            "arch": d.get("arch", ""),
            "app_version": d.get("app_version", ""),
            "status": d.get("status", ""),
            "online": bool(d.get("online")),
            "last_seen_ago": d.get("last_seen_ago"),
            "last_ip": d.get("last_ip", ""),
            "shell_enabled": bool(d.get("shell_enabled")),
            "profiles": int(d.get("profiles") or 0),
            "running": int(d.get("running") or 0),
            "max_running": int(d.get("max_running") or 0) or None,
            "disk_human": d.get("disk_human", ""),
            "engine": d.get("engine", ""),
            "core_base": base,
            "core_reachable": reachable,
            # 终端是否就绪：设备最近有没有来拉过命令
            "shell_last_poll_ago": shell_poll_age(d.get("device_id", "")),
            "shell_polling": _shell_polling(d.get("device_id", "")),
            # 实时通道（WebSocket）是否连着 —— 和上面的"在拉队列"是两回事。
            # 控制台据此显示「实时」还是「队列」，AI 也能据此判断要不要走 WS。
            "realtime": term_relay().online(d.get("device_id", "")),
            # 进程活着 ≠ 本机服务活着。core 死了，远程命令和控制台都不可用，
            # 必须在界面上看出来（2026-11-17 云电脑上真踩到：看着"在线"却什么都连不上）。
            # 三态：True=活 / False=死 / None=旧客户端没上报
            "core_alive": d.get("core_alive"),
            "browsers": browsers,
        })

    st = devices().stats()
    return ok({
        "server": {
            "version": __version__,
            "uptime_seconds": int(now - _STARTED_AT),
            "data_dir": str(s.data_dir),
            "shell_enabled": bool(s.shell_enabled),
            "token_required": bool(s.token),
            "auto_approve": s.auto_approve,
            "allowed_cidrs": list(s.allowed_cidrs),
            "default_credentials": (s.panel_user == "admin" and s.panel_password == "1234"),
            "operator": who,
        },
        "totals": {
            "machines": st.get("total", 0),
            "online": st.get("online", 0),
            "revoked": st.get("revoked", 0),
            "pending": st.get("pending", 0),
            "rejected": st.get("rejected", 0),
            "browsers": total_browsers,
            "running": total_running,
        },
        "machines": machine_rows,
        "events": devices().events(limit=50),
        "shell": shell_queue().stats(),
    })


@app.post("/v1/console/machines/{device_id}/approve", summary="允许一台设备接入")
def console_approve(device_id: str, request: Request) -> dict[str, Any]:
    who = admin_auth(request)
    return ok({"device": devices().approve(device_id, actor=who)})


@app.post("/v1/console/machines/{device_id}/reject", summary="拒绝一台设备的接入请求")
def console_reject(device_id: str, request: Request,
                   body: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    who = admin_auth(request)
    note = str((body or {}).get("note") or "")
    return ok({"device": devices().reject(device_id, actor=who, note=note)})


@app.get("/v1/console/secrets", summary="主控台令牌区（敏感）")
def console_secrets(request: Request) -> dict[str, Any]:
    """返回面板与令牌的**原文**，给主控台的令牌区用。

    为什么真的返回原文：用户明确要求"能看到令牌"。这一层已经受
    「面板密码 + 网段白名单」保护，而能登面板的人本来就能改服务器上的 .env ——
    隐藏它只会让人去 SSH 里翻，不增加任何安全性。

    但**必须**记住它的性质：响应里是明文密钥。
    - 不进访问日志（下面的明文含 token，日志只记"有人调了这个接口"）
    - 不允许跨域（没有 CORS 头，浏览器同源策略兜底）
    """
    who = admin_auth(request)
    s = get_settings()
    logger.info("主控台读取令牌区 by=%s ip=%s", who, _client_ip(request))
    return ok({
        "panel_user": s.panel_user,
        "panel_password": s.panel_password,
        "hub_token": s.token or "",
        "hub_token_set": bool(s.token),
        "allowed_cidrs": list(s.allowed_cidrs),
        "auto_approve": s.auto_approve,
        "shell_enabled": bool(s.shell_enabled),
        # 同步密码是客户端侧的主密码派生，hub 从不持有
        "sync_password_set": False,
        "hint": "这些是服务器 .env 里的值，只在本机/局域网可用，别透到公网",
    })


@app.get("/panel/api/stats", dependencies=[PanelAuth], include_in_schema=False)
def panel_stats() -> dict[str, Any]:
    data = devices().stats()
    # 远程命令行的 KPI（排队 / 在跑 / 完成 / 失败 / 有几台设备在等）
    data["shell"] = shell_queue().stats()
    return ok(data)


@app.get("/panel/api/devices", dependencies=[PanelAuth], include_in_schema=False)
def panel_devices() -> dict[str, Any]:
    return ok({"devices": devices().listing()})


@app.post("/panel/api/devices/{device_id}/revoke", dependencies=[PanelAuth], include_in_schema=False)
def panel_revoke(device_id: str, request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    actor = panel_auth(request)
    note = (body or {}).get("note", "")
    rec = devices().revoke(device_id, actor=actor, note=note)
    logger.warning("面板踢下线 device=%s by=%s", device_id, actor)
    return ok(rec)


@app.post("/panel/api/devices/{device_id}/approve", dependencies=[PanelAuth], include_in_schema=False)
def panel_device_approve(device_id: str, request: Request) -> dict[str, Any]:
    """允许这台设备接入（pending/rejected → active）。"""
    who = get_settings().panel_user
    rec = devices().approve(device_id, actor=who)
    logger.info("面板允许设备接入 device=%s by=%s", device_id, who)
    return ok({"device": rec})


@app.post("/panel/api/devices/{device_id}/reject", dependencies=[PanelAuth], include_in_schema=False)
def panel_device_reject(device_id: str, request: Request, body: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    """拒绝接入。记录保留，随时可以改成允许。"""
    who = get_settings().panel_user
    note = str((body or {}).get("note") or "")
    rec = devices().reject(device_id, actor=who, note=note)
    logger.info("面板拒绝设备接入 device=%s by=%s", device_id, who)
    return ok({"device": rec})


@app.post("/panel/api/devices/{device_id}/restore", dependencies=[PanelAuth], include_in_schema=False)
def panel_restore(device_id: str, request: Request) -> dict[str, Any]:
    rec = devices().restore(device_id, actor=panel_auth(request))
    logger.info("面板恢复设备 device=%s", device_id)
    return ok(rec)


@app.patch("/panel/api/devices/{device_id}", dependencies=[PanelAuth], include_in_schema=False)
def panel_rename(device_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return ok(devices().set_label(device_id, str(body.get("label") or "")[:64]))


@app.delete("/panel/api/devices/{device_id}", dependencies=[PanelAuth], include_in_schema=False)
def panel_delete(device_id: str, request: Request) -> dict[str, Any]:
    deleted = devices().delete(device_id, actor=panel_auth(request))
    return ok({"deleted": deleted, "device_id": device_id})


@app.get("/panel/api/events", dependencies=[PanelAuth], include_in_schema=False)
def panel_events(limit: int = 100) -> dict[str, Any]:
    return ok({"events": devices().events(limit=max(1, min(limit, 500)))})


# ══════════════════════════════════════════════════════════
#  远程命令行（设备拉取 + 长轮询，协议见 docs/13-远程命令行协议.md）
#
#  ## ⚠ 顺序敏感：这一段的路由声明顺序不能随便调
#
#  FastAPI/Starlette 按**声明顺序**逐条匹配，先命中就返回。这个仓库已经被
#  同类问题坑过两次（见 core/src/zw_core/api.py 顶部，以及 api.py 里
#  /profiles/batch 被当成"环境名叫 batch"）。这里的坑有两类：
#
#  1. **固定路径必须排在通配段之前**：
#     `GET /v1/devices/{device_id}/shell/history` 与
#     `GET /v1/devices/{device_id}/shell/{job_id}` 段数完全一样，
#     如果 `{job_id}` 先声明，`.../shell/history` 会被当成"job_id 叫 history"，
#     结果永远 404 SHELL_JOB_NOT_FOUND。所以 history 先声明。
#  2. **设备侧的两条固定路径放在整段最前面**：`/v1/devices/shell/next` 与
#     `/v1/devices/shell/{job_id}/result` 一旦被将来的管理员侧通配段
#     （例如有人加 `/v1/devices/{device_id}/shell/{job_id}` 的变体）先吃掉，
#     设备的轮询就会静默失效 —— 加新路由时请继续把固定路径写在上半段。
#
#  已有的 `/v1/devices/enroll`、`/v1/devices/heartbeat` 声明在本段之前
#  （文件上半部分），顺序上天然安全；测试里也有一条用例守着它们不被吃掉。
# ══════════════════════════════════════════════════════════
def _device_id_of(request: Request) -> str:
    """设备身份。与心跳一致：只认 `X-ZW-Device` 头。"""
    return (request.headers.get("x-zw-device") or "").strip()


# device_id -> 最近一次拉命令的时间戳（内存）。重启后清空无妨。
_SHELL_POLLS: dict[str, float] = {}
_SHELL_POLL_LOCK = threading.Lock()


def _mark_shell_poll(device_id: str) -> None:
    with _SHELL_POLL_LOCK:
        _SHELL_POLLS[device_id] = time.time()


def shell_poll_age(device_id: str) -> float | None:
    """这台设备距上次来拉命令过了多少秒；从没拉过返回 None。

    这个数字是排障的核心：用户说"下发命令没反应"时，
      - None 或很大 → 设备侧的命令行代理没在跑（客户端没连上/开关关了/版本太老）
      - 很小       → 设备在拉，问题在别处（命令报错、被别的 job 挡住）
    """
    with _SHELL_POLL_LOCK:
        ts = _SHELL_POLLS.get(device_id)
    return None if ts is None else max(0.0, time.time() - ts)


def _shell_polling(device_id: str, window: float = 90.0) -> bool:
    """设备在最近 window 秒内拉过命令 = 终端通道是通的。

    90 秒是因为设备的长轮询是 25 秒一轮、失败退避最长 30 秒，
    留足冗余：真掉线 90 秒内一定能看出来。
    """
    age = shell_poll_age(device_id)
    return age is not None and age < window


def _require_shell_enabled() -> None:
    """hub 端总开关。关掉后**所有**命令行接口都不干活（面板也不许绕过）。"""
    if not get_settings().shell_enabled:
        raise ShellError(403, "SHELL_DISABLED", "这台服务器关闭了远程命令行（ZW_SHELL_ENABLED=false）")


def _probe_core(base: str, timeout: float = 0.3) -> bool:
    """探一下设备的 core 是否可直连（只探 /healthz）。

    **短超时是刻意的**：主控台一次要展示 N 台机器，每台最多等 300ms，
    20 台也只有 6 秒上限（而且并发由前端控制）。探不到就是 false ——
    NAT 后面的机器本来就连不上，这不是错误。
    """
    import urllib.error
    import urllib.request

    url = base.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310  只探本项目的 core
            return 200 <= int(getattr(r, "status", 200)) < 300
    except (urllib.error.URLError, ValueError, OSError, TimeoutError):
        return False


def admin_auth(request: Request) -> str:
    """管理员鉴权：面板会话 cookie **或** `Authorization: Bearer <ZW_HUB_TOKEN>`。

    返回操作者标识（写审计日志用）。
    - cookie 复用 `panel_auth` 的那套 HMAC，不需要额外存储；
    - Bearer 只在 `ZW_HUB_TOKEN` 非空时接受 —— 留空意味着"这台服务器不要令牌"，
      此时不能拿空串当口令放行；
    - 两者都不对时统一 401，不告诉对方"你有 cookie 但过期了"还是"令牌不对"。
    - 网段白名单（`_check_cidr`）在鉴权之前先过，和面板登录保持一致。
    """
    _check_cidr(request)
    s = get_settings()
    got = request.cookies.get(PANEL_COOKIE, "")
    if got and hmac.compare_digest(got, _panel_token()):
        return s.panel_user
    authz = request.headers.get("authorization", "")
    token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
    if s.token and token and hmac.compare_digest(token, s.token):
        return "api-token"
    raise HubError(401, "UNAUTHENTICATED", "需要管理面板会话（cookie）或 Authorization: Bearer <令牌>")


AdminAuth = Depends(admin_auth)


# ── 顺序敏感①：设备侧固定路径，必须在管理员侧通配段之前 ──────────
@app.get("/v1/devices/shell/next", summary="设备侧：长轮询取一条待执行命令")
def device_shell_next(request: Request, wait: float = 25.0) -> dict[str, Any]:
    """设备出站长轮询。`wait` 秒内没命令就返回 `{"job": null}`（上限 30）。

    设备**只有在本地开关打开时**才会调这个接口 —— 那是"关闭"真正生效的地方。
    hub 端开关关掉时这里也直接 403，免得设备白等 25 秒。
    """
    _require_shell_enabled()
    dev = _device_id_of(request)
    # 未授权（pending）也要能取命令 —— 用户明确要求"没有授权也能被远程终端操控"。
    # 被踢/被拒绝的设备仍然 403（require_known 会拦住）。
    devices().require_known(dev)
    # 记录"这台设备最近一次来拉命令的时间"：控制台据此显示终端是否就绪，
    # 用户抱怨"下发命令没反应"时，一眼能看出是"设备没在拉"还是"命令没投出去"。
    _mark_shell_poll(dev)
    return ok({"job": shell_queue().next_job(dev, wait=wait)})


@app.post("/v1/devices/shell/{job_id}/result", summary="设备侧：回传执行结果")
def device_shell_result(
    job_id: str,
    request: Request,
    body: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    """设备回传结果。

    这里**不查** hub 端开关：结果可能属于关开关之前已经下发的那条命令，
    拒收只会让那条 job 永远停在 running。开关的作用是"不许再下发"。
    """
    dev = _device_id_of(request)
    devices().require_known(dev)
    _mark_shell_poll(dev)
    payload = body if isinstance(body, dict) else {}
    shell_queue().complete(
        job_id,
        dev,
        rc=payload.get("rc", 0),
        stdout=payload.get("stdout", ""),
        stderr=payload.get("stderr", ""),
        duration_ms=payload.get("duration_ms", 0),
        truncated=bool(payload.get("truncated", False)),
        error=payload.get("error", ""),
    )
    return ok({"accepted": True})


# ── 顺序敏感②：管理员侧（带 {device_id} 通配段，固定路径在下面先声明）──
@app.post("/v1/devices/{device_id}/shell", summary="管理员：投递一条命令")
def shell_submit(
    device_id: str,
    request: Request,
    actor: str = AdminAuth,
    body: dict[str, Any] | None = Body(default=None),
) -> dict[str, Any]:
    """投递命令。设备**离线也允许**（排队等它回来），但被踢下线的不给投。

    审计只记"谁、哪台设备、多长的命令"，**不记命令原文**：
    命令行里经常带密码，日志不该成为泄漏渠道（协议 §5.3）。
    """
    _require_shell_enabled()
    # 未授权（pending）的设备也允许被投递命令 —— 与设备侧一致：
    # "授权"管的是同步，不是"能不能被管理员操作"。
    devices().require_known(device_id)
    payload = body if isinstance(body, dict) else {}
    job = shell_queue().submit(
        device_id,
        payload.get("command", ""),
        cwd=payload.get("cwd", ""),
        timeout=payload.get("timeout"),
    )
    logger.info("管理员投递远程命令 device=%s job=%s actor=%s chars=%s",
                device_id, job["id"], actor, len(job["command"]))
    return ok({"job_id": job["id"], "status": job["status"]})


# ⚠ 这条必须在下面的 `/{job_id}` 之前：段数一样，先声明才不会把 history 当 job_id
@app.get("/v1/devices/{device_id}/shell/history", summary="管理员：命令历史")
def shell_history(
    device_id: str,
    request: Request,
    actor: str = AdminAuth,
    limit: int = 50,
) -> dict[str, Any]:
    _require_shell_enabled()
    return ok({"jobs": shell_queue().history(device_id, limit=limit)})


@app.get("/v1/devices/{device_id}/shell/{job_id}", summary="管理员：查命令状态")
def shell_get(
    device_id: str,
    job_id: str,
    request: Request,
    actor: str = AdminAuth,
    wait: float = 30.0,
) -> dict[str, Any]:
    """`wait` 秒内变终态就立刻返回，否则原样返回当前状态（默认 30，上限 30）。"""
    _require_shell_enabled()
    return ok({"job": shell_queue().wait_terminal(job_id, device_id, wait=wait)})


@app.post("/v1/devices/{device_id}/shell/{job_id}/cancel", summary="管理员：取消还没下发的命令")
def shell_cancel(device_id: str, job_id: str, request: Request, actor: str = AdminAuth) -> dict[str, Any]:
    """只有 `queued` 能取消；已经下发给设备的一律返回 `canceled: false`。"""
    _require_shell_enabled()
    canceled = shell_queue().cancel(job_id, device_id)
    if canceled:
        logger.info("管理员取消远程命令 device=%s job=%s actor=%s", device_id, job_id, actor)
    return ok({"canceled": canceled})


# ── 启动 ──────────────────────────────────────────────────
def _setup_logging() -> None:
    s = get_settings()
    fmt = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(logging.FileHandler(s.log_dir / "hub.log", encoding="utf-8"))
    logging.basicConfig(level=getattr(logging, s.log_level, logging.INFO), format=fmt, handlers=handlers)


def run() -> None:
    import uvicorn

    s = get_settings()
    _setup_logging()
    logger.info("知纹同步中心启动：http://%s:%s 数据目录=%s", s.host, s.port, s.data_dir)
    uvicorn.run(
        "zw_hub.main:app",
        host=s.host,
        port=s.port,
        log_level=s.log_level.lower(),
        access_log=True,
    )


if __name__ == "__main__":
    run()


# ══ 实时终端（WebSocket 中继，协议见 core/services/termhub.py 顶部）══════
# 顺序敏感：这两条都是固定前缀下的通配，放在 /v1/devices/* 的普通路由之后没关系
# （WebSocket 与 HTTP 是不同的协议栈，不会互相吞）。
@app.websocket("/v1/devices/ws")
async def device_ws(websocket: WebSocket) -> None:
    """设备（客户端）连上来的实时终端通道。

    鉴权用 `X-ZW-Device`，与心跳/命令行一致；状态用 `require_known`，
    所以**未授权（pending）的设备也能连** —— 与"未授权也能被远程终端操控"一致。
    被踢/被拒绝的设备在握手阶段就被拒（close 1008）。
    """
    dev = (websocket.headers.get("x-zw-device") or "").strip()
    await websocket.accept()
    if not dev:
        await websocket.close(code=1008, reason="device-required")
        return
    try:
        _check_cidr(websocket)  # type: ignore[arg-type]
    except HubError as exc:
        await websocket.close(code=1008, reason=exc.code)
        return
    try:
        devices().require_known(dev)
    except DeviceError as exc:
        await websocket.close(code=1008, reason=exc.code)
        return

    link = await term_relay().attach(dev, websocket)
    logger.info("设备 %s 已建立实时终端通道", dev)
    try:
        while True:
            raw = await websocket.receive_text()
            link.last_seen = time.time()
            try:
                msg = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("t") == "ping":
                await link.send({"t": "pong"})
                continue
            if msg.get("t") == "hello":
                link.shell_enabled = bool(msg.get("shell_enabled", True))
            # 设备发来的业务消息（out/done/exit/err/opened）广播给管理员
            link.fanout(msg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001  设备侧异常不该影响 hub 其它功能
        logger.debug("设备 %s 的终端通道异常：%s", dev, exc)
    finally:
        await term_relay().detach(link)
        logger.info("设备 %s 的实时终端通道已断开", dev)


@app.websocket("/v1/console/term/{device_id}")
async def console_term_ws(websocket: WebSocket, device_id: str) -> None:
    """管理员（浏览器/CLI）接上去操作这台设备的终端。

    鉴权：面板会话 cookie 或 `?token=<ZW_HUB_TOKEN>`。
    设备不在线时给一条明确的 `device_offline` 然后正常关闭 ——
    前端据此回退到 HTTP 队列，而不是报错卡死。
    """
    token = websocket.query_params.get("token", "")
    cookie = websocket.cookies.get(PANEL_COOKIE, "")
    s = get_settings()
    authed = bool(cookie) and hmac.compare_digest(cookie, _panel_token())
    if not authed and s.token and token and hmac.compare_digest(token, s.token):
        authed = True
    if not authed:
        await websocket.accept()
        await websocket.close(code=1008, reason="unauthenticated")
        return
    try:
        _check_cidr(websocket)  # type: ignore[arg-type]
    except HubError as exc:
        await websocket.accept()
        await websocket.close(code=1008, reason=exc.code)
        return

    await websocket.accept()
    if not term_relay().online(device_id):
        await websocket.send_text(json.dumps({
            "t": "device_offline",
            "device_id": device_id,
            "message": "这台设备的实时通道没连上（客户端版本太老、命令行被关掉，或网络不通）",
        }, ensure_ascii=False))
        await websocket.close(code=1000)
        return
    await websocket.send_text(json.dumps({"t": "hello", "device_id": device_id}, ensure_ascii=False))
    try:
        res = await term_relay().relay(device_id, websocket)
        logger.debug("管理员终端会话结束 device=%s reason=%s", device_id, res.get("reason"))
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("管理员终端会话异常 device=%s：%s", device_id, exc)
    finally:
        with contextlib.suppress(Exception):
            await websocket.close(code=1000)

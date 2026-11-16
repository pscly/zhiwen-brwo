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
import hmac
import ipaddress
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .config import get_settings
from .devices import DeviceError, DeviceStore
from .shell import ShellError, ShellQueue
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
            },
            "workflows": {
                "推送": "PUT /v1/blobs/{pid}/{rev} → PUT /v1/latest/{pid}",
                "拉取": "GET /v1/latest/{pid} → GET /v1/blobs/{pid}/{rev}",
                "占用环境": "POST /v1/leases/{pid} → 定期 POST /v1/leases/{pid}/heartbeat → DELETE /v1/leases/{pid}?owner=",
                "远程命令": "POST /v1/devices/{device_id}/shell → 设备 GET /v1/devices/shell/next → 设备 POST /v1/devices/shell/{job_id}/result",
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
            },
            "安全模型": "hub 只存 AEAD 密文；主密码不进 hub；传输为局域网 HTTP，完整性由密文自身保证",
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
    devices().require_active(dev_id)
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


@app.get("/panel", include_in_schema=False)
def panel_page() -> Response:
    html = Path(__file__).parent / "web" / "panel.html"
    if not html.exists():
        return HTMLResponse("<h1>知纹管理面板</h1><p>面板文件缺失（web/panel.html）。</p>", status_code=500)
    return HTMLResponse(html.read_text(encoding="utf-8"))


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


def _require_shell_enabled() -> None:
    """hub 端总开关。关掉后**所有**命令行接口都不干活（面板也不许绕过）。"""
    if not get_settings().shell_enabled:
        raise ShellError(403, "SHELL_DISABLED", "这台服务器关闭了远程命令行（ZW_SHELL_ENABLED=false）")


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
    devices().require_active(dev)          # 被踢的设备在这里 403 DEVICE_REVOKED
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
    devices().require_active(dev)
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
    devices().require_active(device_id)
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

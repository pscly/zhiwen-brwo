"""HTTP API。所有响应形状统一：

成功：`{"ok": true, "data": {...}}`
失败：`{"ok": false, "code": "PROFILE_BUSY", "message": "……", "hint": "……"}`

这个统一形状是**为 AI 设计的**：模型只需要学一次解析，就能对全部端点做判断；
`code` 是稳定的、可编程的；`hint` 是给人看的一句话建议。
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocket

from . import __version__, gateway
from .config import get_settings
from .db import get_db
from .services import bundle, cookies as ck, engines, manager as mgr, sync as syncsvc
from .services.hub import HubError
from .services.leases import LocalLease

logger = logging.getLogger("zw.api")

API_PREFIX = "/api/v1"


# ── 统一响应 ──────────────────────────────────────────────
def ok(data: Any = None) -> dict[str, Any]:
    return {"ok": True, "data": data}


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.hint = hint


def _err(code: str, message: str, status: int = 400, hint: str = "") -> JSONResponse:
    payload: dict[str, Any] = {"ok": False, "code": code, "message": message}
    if hint:
        payload["hint"] = hint
    return JSONResponse(status_code=status, content=payload)


# ── 鉴权 ──────────────────────────────────────────────────
def require_auth(authorization: str = Header(default=""), x_zw_token: str = Header(default="")) -> None:
    s = get_settings()
    if not s.admin_token:
        return  # 未配置令牌 = 本机开发模式（config 已保证非回环时必须有令牌）
    token = authorization.removeprefix("Bearer ").strip() or x_zw_token.strip()
    import hmac

    if not hmac.compare_digest(token, s.admin_token):
        raise ApiError("UNAUTHENTICATED", "令牌不正确", status=401, hint="请求头加 Authorization: Bearer <ZW_ADMIN_TOKEN>")


Auth = Depends(require_auth)


# ── 请求体 ────────────────────────────────────────────────
class ProfileCreate(BaseModel):
    name: str = Field(description="环境名，全局唯一")
    group_name: str = ""
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    fp_seed: int | None = Field(default=None, description="留空自动分配；已分配后不可改")
    fp_platform: str = Field(default="host", description="host 表示跟随宿主机；跨 OS 会被引擎泄漏，慎用")
    fp_brand: str = ""
    fp_brand_version: str = ""
    fp_hardware: int | None = None
    fp_timezone: str = ""
    fp_lang: str = ""
    fp_ua: str = ""
    proxy: str = Field(default="", description="例如 http://127.0.0.1:7890 或 socks5://user:pass@host:port")
    start_urls: list[str] = Field(default_factory=list)
    window_w: int = 1280
    window_h: int = 800
    extensions: list[str] = Field(default_factory=list)
    restart_policy: str = "on-failure"
    headless: bool = False
    ephemeral: bool = False
    trim_on_stop: bool = False
    sync_enabled: bool = True
    cdp_enabled: bool = True


class StartBody(BaseModel):
    urls: list[str] | None = None
    wait: bool = True


class CookieBody(BaseModel):
    cookies: list[dict[str, Any]]
    mode: str = Field(default="replace", description="replace=先清空再写；merge=合并写入")
    start_if_needed: bool = True


class StorageBody(BaseModel):
    mapping: dict[str, dict[str, str]]
    start_if_needed: bool = True


class SyncPushBody(BaseModel):
    password: str | None = None
    note: str = ""
    ensure_running: bool = True
    cross_machine: bool = True


class SyncPullBody(BaseModel):
    password: str | None = None
    revision: int | None = None
    write_cookies: bool = True
    force: bool = False


class ResetFingerprintBody(BaseModel):
    confirm: str


class ExtensionInstall(BaseModel):
    path: str = Field(description="扩展所在目录或 zip 文件的绝对路径")


class ScriptBody(BaseModel):
    expression: str
    profile: str


# ── 路由 ──────────────────────────────────────────────────
def build_app() -> FastAPI:
    app = FastAPI(
        title="知纹核心（zw-core）",
        version=__version__,
        description="指纹浏览器机群的编排层、CDP 网关与同步客户端。所有响应统一为 {ok, data} / {ok, code, message, hint}。",
    )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _err(exc.code, exc.message, exc.status, exc.hint)

    @app.exception_handler(mgr.ProfileError)
    async def _profile_error(_: Request, exc: mgr.ProfileError) -> JSONResponse:
        return _err(exc.code, exc.message, exc.status)

    @app.exception_handler(syncsvc.SyncError)
    async def _sync_error(_: Request, exc: syncsvc.SyncError) -> JSONResponse:
        return _err(exc.code, exc.message, exc.status)

    @app.exception_handler(HubError)
    async def _hub_error(_: Request, exc: HubError) -> JSONResponse:
        return _err(exc.code, exc.message, status=exc.status or 502, hint="检查 ZW_HUB_URL 与 ZW_HUB_TOKEN 是否正确")

    @app.exception_handler(bundle.BundleError)
    async def _bundle_error(_: Request, exc: bundle.BundleError) -> JSONResponse:
        return _err(exc.code, exc.message, 400)

    @app.exception_handler(engines.EngineError)
    async def _engine_error(_: Request, exc: engines.EngineError) -> JSONResponse:
        return _err(exc.code, exc.message, 422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return _err("INTERNAL", f"{type(exc).__name__}: {exc}", 500, hint="详细堆栈见 <数据目录>/logs/zhiwen.log")

    # ── 健康 ──────────────────────────────────────────────
    @app.get("/healthz", tags=["运维"], summary="存活探针")
    def healthz() -> dict[str, Any]:
        return ok({"status": "ok", "version": __version__, "ts": time.time()})

    @app.get("/readyz", tags=["运维"], summary="就绪探针")
    def readyz() -> Any:
        s = get_settings()
        db_ok = get_db().integrity_ok()
        eng = mgr._engine_summary()
        issues = []
        if not db_ok:
            issues.append("数据库完整性检查失败")
        if not eng["ok"]:
            issues.append("没有找到浏览器内核")
        if issues:
            return JSONResponse(status_code=503, content={"ok": False, "code": "NOT_READY", "message": "；".join(issues), "data": {"engine": eng}})
        return ok({"status": "ready", "engine": eng, "root": str(s.root)})

    # ── 元信息与 AI 自描述 ────────────────────────────────
    @app.get(f"{API_PREFIX}/meta", tags=["运维"], dependencies=[Auth], summary="服务元信息")
    def meta() -> dict[str, Any]:
        s = get_settings()
        return ok(
            {
                "name": "zhiwen-core",
                "version": __version__,
                "node_id": s.node_id,
                "root": str(s.root),
                "host": s.host,
                "port": s.port,
                "gateway_public_base": s.gateway_public_base,
                "auth_required": bool(s.admin_token),
                "hub_url": s.hub_url,
                "hub_configured": bool(s.hub_url),
                "sync_enabled": s.sync_enabled,
                "max_running": s.max_running,
                "port_pool": [s.port_pool_start, s.port_pool_end],
                "server_time": time.time(),
            }
        )

    @app.get(f"{API_PREFIX}/ai/manifest", dependencies=[Auth], tags=["AI"], summary="给 AI 的自描述清单")
    def ai_manifest() -> dict[str, Any]:
        """AI 读完这个就知道全部能力、调用顺序、以及踩坑提示 —— 不需要先读文档。"""
        s = get_settings()
        return ok(
            {
                "service": "zhiwen-core",
                "一句话": "一台机器上的指纹浏览器机群：创建环境、开窗口、把调试端口给局域网用、跨机同步登录态。",
                "你是谁": "调用方通常是 AI 代理或自动化脚本。你不需要懂指纹原理，只需要按下面的顺序调接口。",
                "核心概念": {
                    "profile": "一个隔离的浏览器环境，有自己的指纹种子、cookie、扩展、代理。用 id 或 name 指代。",
                    "fp_seed": "指纹种子（32 位整数）。同一个种子 = 同一台'设备'。**不可修改**，改了等于换设备。",
                    "cdp": "Chrome 调试协议。拿到 gateway_ws_url 就能用 Playwright/Puppeteer 接管页面。",
                    "state": "stopped | starting | running | stopping | crashed | failed",
                    "sync_revision": "同步版本号。push 会 +1；pull 不会改远端。",
                },
                "标准作业流程": {
                    "第一次用": [
                        "GET /api/v1/ai/manifest  ← 你正在读这个",
                        "GET /api/v1/profiles       ← 看有没有现成环境",
                        "POST /api/v1/profiles      ← 没有就建一个（只要 name 必填）",
                    ],
                    "要操作网页": [
                        "POST /api/v1/profiles/{id}/start",
                        "从响应里取 data.cdp.gateway_ws_url",
                        "用 Playwright: chromium.connectOverCDP(gateway_ws_url)",
                        "或用 HTTP: GET /cdp/{id}/json/version 取 webSocketDebuggerUrl",
                    ],
                    "要读/写登录态": [
                        "GET  /api/v1/profiles/{id}/cookies   （环境必须在 running）",
                        "PUT  /api/v1/profiles/{id}/cookies   （body: {cookies: [...]}）",
                    ],
                    "要在另一台机器接着用": [
                        "源机 POST /api/v1/sync/{id}/push  (body: {password: '<主密码>', ensure_running: true})",
                        "目标机 POST /api/v1/sync/{id}/pull  (body: {password: '<主密码>'})",
                    ],
                    "要批量": ["POST /api/v1/profiles/batch (body: {action, ids})"],
                },
                "重要约束": [
                    "cookie 只能从 running 的环境读写。stopped 时读文件是密文，读出来也没用。",
                    "同一个环境同一时刻只能有一个持有者（租约）。第二个 start 会 409。",
                    "push 需要主密码派生密钥；密码错误会在**落盘前**被拦下（code=KEY_FINGERPRINT_MISMATCH）。",
                    "指纹种子与宿主 OS 强绑定：fp_platform=host 最安全，跨 OS 会有字体/GPU 泄漏。",
                    "没有 GPU 的机器上 WebGL 拿不到，指纹会明显异常。要做真人身份请在有显卡的机器上跑。",
                ],
                "错误码速查": {
                    "PROFILE_NOT_FOUND": "id/name 写错了，先 GET /profiles 看看",
                    "PROFILE_BUSY": "别的进程/机器正持有它，等或先 stop",
                    "STATE_CONFLICT": "当前状态不允许这个操作，先看 state",
                    "ENGINE_NOT_FOUND": "没装 Chromium 内核，见 docs/03-部署指南.md",
                    "ENGINE_EXITED": "浏览器起来就退了，读该环境的 profile.log",
                    "LAUNCH_TIMEOUT": "CDP 端口 45 秒没通，通常是内核损坏或被杀软拦",
                    "KEY_FINGERPRINT_MISMATCH": "主密码不对/包不是这台机器加密的。**不要重试**，先确认密码",
                    "PASSWORD_REQUIRED": "没给主密码，设 ZW_SYNC_PASSWORD 或请求里带 password",
                    "REVISION_EXISTS": "远端已有该版本，先 pull 对齐版本号",
                    "NOT_READY": "内核缺失或数据库损坏，看 hint",
                },
                "端点清单": sorted(
                    [
                        {"method": "GET", "path": "/healthz", "说明": "存活"},
                        {"method": "GET", "path": "/readyz", "说明": "就绪"},
                        {"method": "GET", "path": f"{API_PREFIX}/meta", "说明": "元信息"},
                        {"method": "GET", "path": f"{API_PREFIX}/ai/manifest", "说明": "本清单"},
                        {"method": "GET", "path": f"{API_PREFIX}/stats", "说明": "容量与状态统计"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles", "说明": "列环境"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles", "说明": "建环境"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles/{{id}}", "说明": "看单个"},
                        {"method": "PATCH", "path": f"{API_PREFIX}/profiles/{{id}}", "说明": "改"},
                        {"method": "DELETE", "path": f"{API_PREFIX}/profiles/{{id}}", "说明": "删"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/start", "说明": "开"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/stop", "说明": "关"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/restart", "说明": "重启"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles/{{id}}/cdp", "说明": "取 CDP 地址与令牌"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles/{{id}}/cookies", "说明": "读 cookie"},
                        {"method": "PUT", "path": f"{API_PREFIX}/profiles/{{id}}/cookies", "说明": "写 cookie"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles/{{id}}/storage", "说明": "读 localStorage"},
                        {"method": "PUT", "path": f"{API_PREFIX}/profiles/{{id}}/storage", "说明": "写 localStorage"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles/{{id}}/logs", "说明": "看日志尾巴"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/batch", "说明": "批量启停"},
                        {"method": "GET", "path": f"{API_PREFIX}/extensions", "说明": "扩展库"},
                        {"method": "POST", "path": f"{API_PREFIX}/extensions/install", "说明": "装扩展"},
                        {"method": "POST", "path": f"{API_PREFIX}/page/screenshot", "说明": "截图"},
                        {"method": "POST", "path": f"{API_PREFIX}/page/navigate", "说明": "导航"},
                        {"method": "POST", "path": f"{API_PREFIX}/page/evaluate", "说明": "执行 JS"},
                        {"method": "POST", "path": f"{API_PREFIX}/sync/{{id}}/push", "说明": "推送到 hub"},
                        {"method": "POST", "path": f"{API_PREFIX}/sync/{{id}}/pull", "说明": "从 hub 拉取"},
                        {"method": "GET", "path": f"{API_PREFIX}/sync/{{id}}/diff", "说明": "看差异"},
                        {"method": "GET", "path": f"{API_PREFIX}/sync/remote", "说明": "hub 上的索引"},
                        {"method": "GET", "path": f"{API_PREFIX}/audit", "说明": "审计日志"},
                        {"method": "GET", "path": f"{API_PREFIX}/events", "说明": "事件流（SSE 风格轮询）"},
                        {"method": "GET", "path": "/cdp/{{id}}/json/version", "说明": "CDP 发现接口（地址已重写）"},
                        {"method": "WS", "path": "/cdp/{{id}}/...", "说明": "CDP 主通道"},
                    ],
                    key=lambda x: (x["path"], x["method"]),
                ),
                "MCP": {
                    "说明": "同一套能力已包装成 MCP 工具，AI 客户端可直接挂载",
                    "stdio": "zw mcp",
                    "http": "POST /mcp  （JSON-RPC 2.0）",
                },
                "本次运行的偏好": {
                    "gateway_public_base": s.gateway_public_base,
                    "node_id": s.node_id,
                    "max_running": s.max_running,
                    "hub_configured": bool(s.hub_url),
                },
            }
        )

    # ── 统计 ──────────────────────────────────────────────
    @app.get(f"{API_PREFIX}/stats", tags=["运维"], dependencies=[Auth], summary="容量与状态")
    def stats() -> dict[str, Any]:
        return ok(mgr.stats())

    # ── 环境 CRUD ─────────────────────────────────────────
    @app.get(f"{API_PREFIX}/profiles", tags=["环境"], dependencies=[Auth], summary="列出环境")
    def profiles_list(
        state: str | None = None,
        group: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        items = mgr.list_profiles(state=state, group=group, q=q)
        return ok({"profiles": items, "total": len(items)})

    @app.post(f"{API_PREFIX}/profiles", tags=["环境"], dependencies=[Auth], summary="创建环境")
    def profiles_create(body: ProfileCreate) -> dict[str, Any]:
        data = body.model_dump(exclude_none=True)
        return ok(mgr.create_profile(data))

    @app.get(f"{API_PREFIX}/profiles/{{ref}}", tags=["环境"], dependencies=[Auth], summary="查看环境")
    def profiles_get(ref: str) -> dict[str, Any]:
        p = mgr.get_profile(ref)
        lease = LocalLease(mgr.profile_dir(p["id"])).status()
        # 注意：这里**不签发** CDP 令牌。
        # 签发一次就要在库子里留一行，而详情接口会被 UI/轮询反复调用，
        # 每调一次发一个令牌会让 cdp_tokens 表无限增长。要地址请显式调 GET /cdp。
        return ok(
            {
                **p,
                "lease": lease,
                "cdp_ready": p["state"] == "running" and bool(p.get("cdp_port")),
                "cdp_hint": "运行中可以直接 GET /api/v1/profiles/{ref}/cdp 取连接地址（会签发一个短期令牌）",
            }
        )

    @app.patch(f"{API_PREFIX}/profiles/{{ref}}", tags=["环境"], dependencies=[Auth], summary="修改环境")
    def profiles_patch(ref: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return ok(mgr.update_profile(ref, payload))

    @app.delete(f"{API_PREFIX}/profiles/{{ref}}", tags=["环境"], dependencies=[Auth], summary="删除环境")
    def profiles_delete(ref: str, purge: bool = False) -> dict[str, Any]:
        return ok(mgr.delete_profile(ref, purge=purge))

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/start", tags=["环境"], dependencies=[Auth], summary="启动环境")
    def profiles_start(ref: str, body: StartBody = StartBody()) -> dict[str, Any]:
        return ok(mgr.start(ref, urls=body.urls, wait=body.wait))

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/stop", tags=["环境"], dependencies=[Auth], summary="停止环境")
    def profiles_stop(ref: str, force: bool = False) -> dict[str, Any]:
        return ok(mgr.stop(ref, force=force))

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/restart", tags=["环境"], dependencies=[Auth], summary="重启环境")
    def profiles_restart(ref: str, body: StartBody = StartBody()) -> dict[str, Any]:
        return ok(mgr.restart(ref, urls=body.urls))

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/unlock", tags=["环境"], dependencies=[Auth], summary="强制释放租约")
    def profiles_unlock(ref: str) -> dict[str, Any]:
        p = mgr.get_profile(ref)
        lease = LocalLease(mgr.profile_dir(p["id"]))
        lease.force_release()
        get_db().audit("profile.unlock", p["id"], result="ok")
        logger.warning("强制释放租约 name=%s", p["name"])
        return ok({"released": True, "profile_id": p["id"]})

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/fingerprint/reset", tags=["环境"], dependencies=[Auth], summary="重置指纹（危险）")
    def profiles_reset_fp(ref: str, body: ResetFingerprintBody) -> dict[str, Any]:
        return ok(mgr.reset_fingerprint(ref, body.confirm))

    @app.get(f"{API_PREFIX}/profiles/{{ref}}/cdp", tags=["环境"], dependencies=[Auth], summary="取 CDP 地址")
    def profiles_cdp(ref: str, ttl: int | None = Query(default=None, ge=60, le=7200)) -> dict[str, Any]:
        p = mgr.get_profile(ref)
        if p["state"] != "running":
            raise ApiError("STATE_CONFLICT", f"{p['name']} 当前是 {p['state']}，先启动再取 CDP 地址", 409)
        return ok(mgr.cdp_info(p, ttl=ttl))

    @app.get(f"{API_PREFIX}/profiles/{{ref}}/logs", tags=["环境"], dependencies=[Auth], summary="读环境日志")
    def profiles_logs(ref: str, tail: int = Query(default=200, ge=1, le=5000)) -> dict[str, Any]:
        p = mgr.get_profile(ref)
        out: dict[str, Any] = {"profile_id": p["id"], "name": p["name"]}
        for label, name in (("browser", "browser.log"), ("profile", "profile.log")):
            f = mgr.profile_dir(p["id"]) / name
            if f.exists():
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
                out[label] = lines
            else:
                out[label] = []
        return ok(out)

    class BatchBody(BaseModel):
        action: str = Field(description="start | stop | restart")
        ids: list[str]
        concurrency: int = 3

    @app.post(f"{API_PREFIX}/profiles/batch", tags=["环境"], dependencies=[Auth], summary="批量操作")
    def profiles_batch(body: BatchBody) -> dict[str, Any]:
        if body.action not in ("start", "stop", "restart"):
            raise ApiError("INVALID_ACTION", "action 只能是 start / stop / restart")
        results = []
        for ref in body.ids:
            try:
                if body.action == "start":
                    mgr.start(ref)
                elif body.action == "stop":
                    mgr.stop(ref)
                else:
                    mgr.restart(ref)
                results.append({"ref": ref, "ok": True})
            except Exception as exc:
                results.append({"ref": ref, "ok": False, "error": str(exc)})
        return ok({"total": len(results), "ok": sum(1 for r in results if r["ok"]), "results": results})

    # ── cookie / storage ──────────────────────────────────
    def _ws_for(ref: str, start_if_needed: bool = False) -> tuple[dict[str, Any], str]:
        p = mgr.get_profile(ref)
        if p["state"] != "running":
            if not start_if_needed:
                raise ApiError(
                    "STATE_CONFLICT",
                    f"{p['name']} 没有在运行。cookie 只能在运行时读取（停止时文件是 OS 密钥加密的密文）。",
                    409,
                    hint="先 POST /profiles/{id}/start，或在请求里带 start_if_needed=true 让服务端代你启动",
                )
            mgr.start(p["id"])
            p = mgr.get_profile(p["id"])
        port = p.get("cdp_port")
        if not port:
            raise ApiError("CDP_DISABLED", f"{p['name']} 关闭了调试端口（cdp_enabled=false），无法读写 cookie", 409)
        try:
            import httpx

            ws = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=4).json()["webSocketDebuggerUrl"]
        except Exception as exc:
            raise ApiError("CDP_UNAVAILABLE", f"取不到 {p['name']} 的调试地址：{exc}", 502) from exc
        return p, ws

    @app.get(f"{API_PREFIX}/profiles/{{ref}}/cookies", tags=["数据"], dependencies=[Auth], summary="读 cookie")
    def cookies_get(ref: str, fmt: str = Query(default="cdp", pattern="^(cdp|netscape|playwright)$")) -> Any:
        p, ws = _ws_for(ref)
        items = ck.dump_cookies_sync(ws)
        get_db().audit("cookie.read", p["id"], detail={"count": len(items)})
        if fmt == "cdp":
            return ok({"count": len(items), "cookies": items})
        if fmt == "netscape":
            lines = ["# Netscape HTTP Cookie File"]
            for c in items:
                dom = c.get("domain", "")
                domain_field = dom if dom.startswith(".") else dom
                lines.append(
                    "\t".join(
                        [
                            domain_field,
                            "TRUE" if dom.startswith(".") else "FALSE",
                            c.get("path", "/"),
                            "TRUE" if c.get("secure") else "FALSE",
                            str(int(c.get("expires") or 0)),
                            c.get("name", ""),
                            c.get("value", ""),
                        ]
                    )
                )
            return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")
        flat = [
            {
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain"),
                "path": c.get("path", "/"),
                "expires": c.get("expires"),
                "httpOnly": c.get("httpOnly"),
                "secure": c.get("secure"),
                "sameSite": c.get("sameSite"),
            }
            for c in items
        ]
        return ok({"count": len(flat), "cookies": flat})

    @app.put(f"{API_PREFIX}/profiles/{{ref}}/cookies", tags=["数据"], dependencies=[Auth], summary="写 cookie")
    def cookies_put(ref: str, body: CookieBody) -> dict[str, Any]:
        p, ws = _ws_for(ref, start_if_needed=body.start_if_needed)
        report = ck.load_cookies_sync(ws, body.cookies, clear=(body.mode == "replace"))
        get_db().audit("cookie.write", p["id"], detail={"count": len(body.cookies), "mode": body.mode, "report": report})
        return ok({"profile_id": p["id"], **report})

    @app.get(f"{API_PREFIX}/profiles/{{ref}}/storage", tags=["数据"], dependencies=[Auth], summary="读 localStorage")
    def storage_get(ref: str, origin: list[str] = Query(default=[])) -> dict[str, Any]:
        p, ws = _ws_for(ref)
        origins = origin or []
        if not origins:
            raise ApiError("ORIGIN_REQUIRED", "请用 ?origin=https://a.com 指定要读哪个站点（localStorage 是按源隔离的）")
        return ok({"profile_id": p["id"], "storage": ck.dump_storage_sync(ws, origins)})

    @app.put(f"{API_PREFIX}/profiles/{{ref}}/storage", tags=["数据"], dependencies=[Auth], summary="写 localStorage")
    def storage_put(ref: str, body: StorageBody) -> dict[str, Any]:
        p, ws = _ws_for(ref, start_if_needed=body.start_if_needed)
        report = ck.load_storage_sync(ws, body.mapping)
        get_db().audit("storage.write", p["id"], detail={"origins": list(body.mapping)})
        return ok({"profile_id": p["id"], "report": report})

    # ── 页面能力（给 AI 直接用，不必自己接 CDP）────────────
    class NavigateBody(BaseModel):
        profile: str
        url: str

    class EvaluateBody(BaseModel):
        profile: str
        expression: str

    class ScreenshotBody(BaseModel):
        profile: str
        format: str = "png"

    @app.post(f"{API_PREFIX}/page/navigate", tags=["页面"], dependencies=[Auth], summary="导航到网址")
    def page_navigate(body: NavigateBody) -> dict[str, Any]:
        _, ws = _ws_for(body.profile, start_if_needed=True)
        return ok(ck.navigate_sync(ws, body.url))

    @app.post(f"{API_PREFIX}/page/evaluate", tags=["页面"], dependencies=[Auth], summary="执行 JS 表达式")
    def page_evaluate(body: EvaluateBody) -> dict[str, Any]:
        _, ws = _ws_for(body.profile, start_if_needed=True)
        return ok({"result": ck.evaluate_sync(ws, body.expression)})

    @app.post(f"{API_PREFIX}/page/screenshot", tags=["页面"], dependencies=[Auth], summary="页面截图")
    def page_screenshot(body: ScreenshotBody) -> Any:
        _, ws = _ws_for(body.profile, start_if_needed=True)
        data = ck.screenshot_sync(ws, fmt=body.format)
        if body.format == "png":
            return Response(content=data, media_type="image/png")
        return ok({"base64": base64.b64encode(data).decode(), "format": body.format})

    @app.get(f"{API_PREFIX}/page/text", tags=["页面"], dependencies=[Auth], summary="页面可见文字")
    def page_text(profile: str) -> dict[str, Any]:
        _, ws = _ws_for(profile, start_if_needed=True)
        return ok({"text": ck.run(ck.page_text(ws))})

    @app.get(f"{API_PREFIX}/page/targets", tags=["页面"], dependencies=[Auth], summary="列出标签页")
    def page_targets(profile: str) -> dict[str, Any]:
        _, ws = _ws_for(profile)
        return ok({"targets": ck.run(ck.list_targets(ws))})

    # ── 扩展 ──────────────────────────────────────────────
    @app.get(f"{API_PREFIX}/extensions", tags=["扩展"], dependencies=[Auth], summary="扩展库列表")
    def extensions_list() -> dict[str, Any]:
        with get_db().conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM extensions ORDER BY added_at DESC")]
        return ok({"extensions": rows, "total": len(rows)})

    @app.post(f"{API_PREFIX}/extensions/install", tags=["扩展"], dependencies=[Auth], summary="安装扩展到中央库")
    def extensions_install(body: ExtensionInstall) -> dict[str, Any]:
        s = get_settings()
        src = Path(body.path).expanduser()
        if not src.exists():
            raise ApiError("PATH_NOT_FOUND", f"路径不存在：{src}")
        import hashlib
        import shutil
        import zipfile

        if src.is_file() and zipfile.is_zipfile(src):
            target = s.ext_dir / src.stem
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            with zipfile.ZipFile(src) as zf:
                zf.extractall(target)
        else:
            target = s.ext_dir / src.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(src, target)

        manifest_path = target / "manifest.json"
        if not manifest_path.exists():
            raise ApiError("NOT_AN_EXTENSION", f"{target} 里没有 manifest.json，不像是解包后的扩展目录")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        slug = (manifest.get("name") or target.name).strip().lower().replace(" ", "-")[:64]
        version = str(manifest.get("version") or "0")
        digest = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(target.rglob("*")) if p.is_file())).hexdigest()
        with get_db().conn() as c:
            c.execute(
                "INSERT INTO extensions(slug,version,path,name,sha256,added_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(slug,version) DO UPDATE SET path=excluded.path, name=excluded.name, sha256=excluded.sha256",
                (slug, version, str(target), manifest.get("name", slug), digest, time.time()),
            )
        get_db().audit("extension.install", detail={"slug": slug, "version": version, "path": str(target)})
        return ok({"slug": slug, "version": version, "path": str(target), "name": manifest.get("name", slug)})

    @app.delete(f"{API_PREFIX}/extensions/{{slug}}/{{version}}", tags=["扩展"], dependencies=[Auth], summary="删除扩展")
    def extensions_delete(slug: str, version: str) -> dict[str, Any]:
        with get_db().conn() as c:
            row = c.execute("SELECT * FROM extensions WHERE slug=? AND version=?", (slug, version)).fetchone()
            if row is None:
                raise ApiError("EXTENSION_NOT_FOUND", f"扩展库里没有 {slug}@{version}", 404)
            c.execute("DELETE FROM extensions WHERE slug=? AND version=?", (slug, version))
        import shutil

        shutil.rmtree(Path(row["path"]), ignore_errors=True)
        return ok({"deleted": True, "slug": slug, "version": version})

    # ── 同步 ──────────────────────────────────────────────
    @app.get(f"{API_PREFIX}/sync/state", tags=["同步"], dependencies=[Auth], summary="同步总览")
    def sync_state() -> dict[str, Any]:
        s = get_settings()
        items = []
        for p in mgr.list_profiles():
            items.append(
                {
                    "profile_id": p["id"],
                    "name": p["name"],
                    "revision": p.get("sync_revision"),
                    "key_fp": p.get("sync_key_fp"),
                    "state": p.get("sync_state"),
                    "enabled": bool(p.get("sync_enabled")),
                }
            )
        return ok({"hub_url": s.hub_url, "hub_configured": bool(s.hub_url), "profiles": items})

    @app.post(f"{API_PREFIX}/sync/{{ref}}/push", tags=["同步"], dependencies=[Auth], summary="推送到 hub")
    def sync_push(ref: str, body: SyncPushBody = SyncPushBody()) -> dict[str, Any]:
        return ok(syncsvc.push(ref, password=body.password, note=body.note, ensure_running=body.ensure_running, cross_machine=body.cross_machine))

    @app.post(f"{API_PREFIX}/sync/{{ref}}/pull", tags=["同步"], dependencies=[Auth], summary="从 hub 拉取")
    def sync_pull(ref: str, body: SyncPullBody = SyncPullBody()) -> dict[str, Any]:
        return ok(syncsvc.pull(ref, password=body.password, revision=body.revision, write_cookies=body.write_cookies, force=body.force))

    @app.get(f"{API_PREFIX}/sync/{{ref}}/diff", tags=["同步"], dependencies=[Auth], summary="本地与远端差异")
    def sync_diff(ref: str) -> dict[str, Any]:
        return ok(syncsvc.diff(ref))

    @app.get(f"{API_PREFIX}/sync/{{ref}}/history", tags=["同步"], dependencies=[Auth], summary="版本历史")
    def sync_history(ref: str) -> dict[str, Any]:
        return ok(syncsvc.remote_history(ref))

    @app.get(f"{API_PREFIX}/sync/remote", tags=["同步"], dependencies=[Auth], summary="hub 上的索引")
    def sync_remote() -> dict[str, Any]:
        return ok({"profiles": syncsvc.remote_list()})

    @app.post(f"{API_PREFIX}/sync/all/push", tags=["同步"], dependencies=[Auth], summary="全量推送")
    def sync_push_all(body: SyncPushBody = SyncPushBody()) -> dict[str, Any]:
        return ok(syncsvc.push_all(password=body.password))

    # ── 审计与事件 ────────────────────────────────────────
    @app.get(f"{API_PREFIX}/audit", tags=["运维"], dependencies=[Auth], summary="审计日志")
    def audit_list(
        profile_id: str | None = None,
        action: str | None = None,
        limit: int = Query(default=100, ge=1, le=2000),
    ) -> dict[str, Any]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        args: list[Any] = []
        if profile_id:
            sql += " AND profile_id=?"
            args.append(profile_id)
        if action:
            sql += " AND action LIKE ?"
            args.append(f"%{action}%")
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with get_db().conn() as c:
            rows = [dict(r) for r in c.execute(sql, args)]
        for r in rows:
            try:
                r["detail"] = json.loads(r["detail"])
            except Exception:
                pass
        return ok({"items": rows, "total": len(rows)})

    @app.get(f"{API_PREFIX}/events", tags=["运维"], dependencies=[Auth], summary="事件与状态快照")
    def events(since: float = Query(default=0.0)) -> dict[str, Any]:
        """AI 轮询用：一次拿到"最近发生了什么 + 现在什么状态"。"""
        with get_db().conn() as c:
            rows = [
                dict(r)
                for r in c.execute("SELECT * FROM audit_log WHERE ts > ? ORDER BY id DESC LIMIT 200", (since,))
            ]
        for r in rows:
            try:
                r["detail"] = json.loads(r["detail"])
            except Exception:
                pass
        profiles = [{"id": p["id"], "name": p["name"], "state": p["state"], "sync_state": p["sync_state"]} for p in mgr.list_profiles()]
        return ok({"now": time.time(), "events": rows, "profiles": profiles, "gateway_routes": gateway.ROUTER.all()})

    # ── 运维动作 ──────────────────────────────────────────
    @app.post(f"{API_PREFIX}/maintenance/trim", tags=["运维"], dependencies=[Auth], summary="清理所有已停止环境的缓存")
    def maintenance_trim() -> dict[str, Any]:
        total = {"profiles": 0, "freed_bytes": 0}
        for p in mgr.list_profiles(state="stopped"):
            r = engines.trim(mgr.udd_path(p["id"]))
            total["profiles"] += 1
            total["freed_bytes"] += r["freed_bytes"]
        return ok(total)

    @app.post(f"{API_PREFIX}/maintenance/doctor", tags=["运维"], dependencies=[Auth], summary="环境自检")
    def maintenance_doctor() -> dict[str, Any]:
        s = get_settings()
        checks: list[dict[str, Any]] = []
        eng = mgr._engine_summary()
        checks.append({"项": "浏览器内核", "ok": eng["ok"], "详情": eng})
        checks.append({"项": "数据库", "ok": get_db().integrity_ok(), "详情": str(s.db_path)})
        has_token = bool(s.admin_token)
        checks.append(
            {
                "项": "访问令牌",
                "ok": has_token or s.host in ("127.0.0.1", "localhost", "::1"),
                "详情": "已配置" if has_token else "未配置（仅回环可用）",
            }
        )
        hub_ok = False
        hub_detail = "未配置"
        if s.hub_url:
            try:
                from .services.hub import HubClient

                hub_detail = json.dumps(HubClient(s.hub_url, s.hub_token).health(), ensure_ascii=False)
                hub_ok = True
            except Exception as exc:
                hub_detail = str(exc)
        checks.append({"项": "同步中心", "ok": hub_ok or not s.hub_url, "详情": hub_detail})
        checks.append({"项": "数据目录", "ok": Path(s.root).exists(), "详情": str(s.root)})
        st = mgr.stats()
        checks.append({"项": "并发余量", "ok": st["running"] < s.max_running, "详情": f"{st['running']}/{s.max_running}"})
        return ok({"checks": checks, "all_ok": all(c["ok"] for c in checks), "stats": st})

    @app.post(f"{API_PREFIX}/maintenance/reap-orphans", tags=["运维"], dependencies=[Auth], summary="清理孤儿浏览器进程")
    def maintenance_reap() -> dict[str, Any]:
        """清掉"数据库里没有对应环境、或者环境状态不是 running"却还在跑的浏览器。

        典型来源：core 被 kill -9、数据库被重置、手动删了环境目录。
        这些进程会一直占内存和端口，还会让同名环境下次启动撞 SingletonLock。
        """
        return ok(mgr.reap_orphan_browsers())

    @app.post(f"{API_PREFIX}/maintenance/cleanup", tags=["运维"], dependencies=[Auth], summary="清理 staging 与备份")
    def maintenance_cleanup() -> dict[str, Any]:
        s = get_settings()
        removed = 0
        for d in Path(s.staging_dir).glob("*"):
            try:
                if d.is_dir():
                    import shutil

                    shutil.rmtree(d, ignore_errors=True)
                else:
                    d.unlink()
                removed += 1
            except OSError:
                pass
        get_db().purge_cdp_tokens()
        return ok({"staging_removed": removed})

    # ── CDP 网关入口 ──────────────────────────────────────
    @app.get("/cdp/{profile_id}/json/{rest:path}", tags=["CDP 网关"], summary="CDP 发现接口（已重写地址）")
    async def cdp_json(profile_id: str, rest: str, token: str = "") -> Response:
        ok_token = mgr.verify_cdp_token(profile_id, token)
        if not ok_token:
            return JSONResponse(status_code=401, content={"ok": False, "code": "UNAUTHENTICATED", "message": "CDP 令牌无效或已过期，请重新 GET /api/v1/profiles/{id}/cdp"})
        status, body, headers = await gateway.proxy_http(profile_id, f"/json/{rest}", get_settings().gateway_public_base, token)
        return Response(content=body, status_code=status, headers=headers)

    @app.get("/cdp/{profile_id}/json", tags=["CDP 网关"], summary="CDP 目标列表（已重写地址）")
    async def cdp_json_root(profile_id: str, token: str = "") -> Response:
        if not mgr.verify_cdp_token(profile_id, token):
            return JSONResponse(status_code=401, content={"ok": False, "code": "UNAUTHENTICATED", "message": "CDP 令牌无效或已过期"})
        status, body, headers = await gateway.proxy_http(profile_id, "/json", get_settings().gateway_public_base, token)
        return Response(content=body, status_code=status, headers=headers)

    @app.get("/cdp/{profile_id}/json/version", tags=["CDP 网关"], summary="CDP 版本（已重写 ws 地址）")
    async def cdp_version(profile_id: str, token: str = "") -> Response:
        if not mgr.verify_cdp_token(profile_id, token):
            return JSONResponse(status_code=401, content={"ok": False, "code": "UNAUTHENTICATED", "message": "CDP 令牌无效或已过期"})
        status, body, headers = await gateway.proxy_http(profile_id, "/json/version", get_settings().gateway_public_base, token)
        return Response(content=body, status_code=status, headers=headers)

    @app.websocket("/cdp/{profile_id}/{path:path}")
    async def cdp_ws(websocket: WebSocket, profile_id: str, path: str) -> None:
        token = websocket.query_params.get("token", "")
        if not mgr.verify_cdp_token(profile_id, token):
            await websocket.close(code=1008, reason="bad-token")
            return
        await gateway.proxy_ws(websocket, profile_id, f"/{path}", get_settings().cdp_max_conn_per_profile)

    @app.websocket("/cdp/{profile_id}")
    async def cdp_ws_root(websocket: WebSocket, profile_id: str) -> None:
        token = websocket.query_params.get("token", "")
        if not mgr.verify_cdp_token(profile_id, token):
            await websocket.close(code=1008, reason="bad-token")
            return
        await gateway.proxy_ws(websocket, profile_id, "/", get_settings().cdp_max_conn_per_profile)

    # ── Web 控制台 ────────────────────────────────────────
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        """内联一个极简图标，避免控制台因为 404 在 devtools 里报错（看着像 bug）。"""
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
            '<rect width="32" height="32" rx="7" fill="#5b9dff"/>'
            '<path d="M9 10h14l-8 6 8 6H9" fill="none" stroke="#fff" stroke-width="2.4" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        )
        return Response(content=svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        html = Path(__file__).parent / "web" / "index.html"
        return html.read_text(encoding="utf-8") if html.exists() else "<h1>知纹</h1><p>控制台文件缺失。</p>"

    return app

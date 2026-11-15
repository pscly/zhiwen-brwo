"""HTTP API。所有响应形状统一：

成功：`{"ok": true, "data": {...}}`
失败：`{"ok": false, "code": "PROFILE_BUSY", "message": "……", "hint": "……"}`

这个统一形状是**为 AI 设计的**：模型只需要学一次解析，就能对全部端点做判断；
`code` 是稳定的、可编程的；`hint` 是给人看的一句话建议。

## 路由顺序陷阱（改之前先看这个）

FastAPI 按**声明顺序**匹配。`/profiles/{ref}` 这种通配段会吃掉
`/profiles/batch`、`/sync/all/push` 这类固定路径。0.1.0 里
`POST /api/v1/sync/all/push` 就被 `POST /api/v1/sync/{ref}/push` 抢先匹配，
当成"环境名叫 all"处理，于是这条能力实际上从来没有生效过。
规矩：**固定路径必须在通配路径之前声明**，本文件里用注释 `# 顺序敏感` 标出。
"""

from __future__ import annotations

import base64
import ipaddress
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
from .services import bundle, cookies as ck, engines, manager as mgr, proxy as proxysvc, sync as syncsvc
from .services.hub import HubClient, HubError
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


def _client_ip(request: Request) -> str:
    s = get_settings()
    if s.trust_proxy_headers:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def cidr_guard(request: Request) -> None:
    """来源网段白名单。没配 ZW_ALLOWED_CIDRS 就不限制（默认行为与 0.1.0 一致）。"""
    s = get_settings()
    if not s.allowed_cidrs:
        return
    ip_text = _client_ip(request)
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        # 拿不到可解析的来源地址时按拒绝处理：白名单是"默认关闭"的开关，
        # 一旦打开就必须是 fail-closed，否则某个特殊 client.host 就会绕过它。
        # 注意 TestClient / 反向代理转发都可能给出非 IP 的主机名，
        # 部署时请确保 ZW_TRUST_PROXY_HEADERS 与实际拓扑一致。
        raise ApiError(
            "CIDR_FORBIDDEN",
            f"来源地址无法解析为 IP（{ip_text!r}），已按白名单拒绝",
            status=403,
            hint="如果你确实要通过主机名/代理访问，配置 ZW_TRUST_PROXY_HEADERS=true 或放宽 ZW_ALLOWED_CIDRS",
        )
    for cidr in s.allowed_cidrs:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return
        except ValueError:
            continue
    raise ApiError(
        "CIDR_FORBIDDEN",
        f"来源地址 {ip_text} 不在允许网段内",
        status=403,
        hint=f"当前允许：{', '.join(s.allowed_cidrs)}；要放行请改 ZW_ALLOWED_CIDRS",
    )


Cidr = Depends(cidr_guard)


# ── 请求体 ────────────────────────────────────────────────
class ProfileCreate(BaseModel):
    name: str = Field(description="环境名，全局唯一")
    group_name: str = ""
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    fp_seed: int | None = Field(default=None, description="留空自动分配；已分配后不可改")
    fp_platform: str = Field(default="host", description="host 表示跟随宿主机；跨 OS 会被引擎泄漏，慎用")
    fp_platform_version: str = ""
    fp_brand: str = ""
    fp_brand_version: str = ""
    fp_hardware: int | None = None
    fp_timezone: str = ""
    fp_lang: str = ""
    fp_accept_lang: str = ""
    fp_disable_spoofing: str = Field(default="", description="逗号分隔：font,audio,canvas,clientrects,gpu（Chrome 144+）")
    fp_ua: str = ""
    proxy: str = Field(default="", description="例如 http://127.0.0.1:7890 或 socks5://user:pass@host:port")
    proxy_id: int | None = None
    start_urls: list[str] = Field(default_factory=list)
    window_w: int = 1280
    window_h: int = 800
    extensions: list[str] = Field(default_factory=list, description="扩展库里的 slug，或绝对路径")
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
    apply_settings: bool = True


class ResetFingerprintBody(BaseModel):
    confirm: str


class ExtensionInstall(BaseModel):
    path: str = Field(description="扩展所在目录或 zip 文件的绝对路径")


class ExtBindBody(BaseModel):
    slug: str
    version: str | None = None


class NavigateBody(BaseModel):
    profile: str
    url: str
    new_tab: bool = False


class EvaluateBody(BaseModel):
    profile: str
    expression: str


class ScreenshotBody(BaseModel):
    profile: str
    format: str = Field(default="png", pattern="^(png|jpeg|webp)$")
    full_page: bool = False


class BatchBody(BaseModel):
    action: str = Field(description="start | stop | restart")
    ids: list[str]
    concurrency: int = 3


class ExportBody(BaseModel):
    """导出时可显式带主密码（不填就用查询串或服务端环境变量）。"""

    password: str | None = None


class ProxyBody(BaseModel):
    label: str = ""
    scheme: str = "http"
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    country: str = ""


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

    @app.exception_handler(proxysvc.ProxyError)
    async def _proxy_error(_: Request, exc: proxysvc.ProxyError) -> JSONResponse:
        return _err(exc.code, exc.message, 400)

    @app.exception_handler(HubError)
    async def _hub_error(_: Request, exc: HubError) -> JSONResponse:
        return _err(exc.code, exc.message, status=exc.status or 502, hint="检查 ZW_HUB_URL 与 ZW_HUB_TOKEN 是否正确")

    @app.exception_handler(bundle.BundleError)
    async def _bundle_error(_: Request, exc: bundle.BundleError) -> JSONResponse:
        return _err(exc.code, exc.message, 400)

    @app.exception_handler(engines.EngineError)
    async def _engine_error(_: Request, exc: engines.EngineError) -> JSONResponse:
        return _err(exc.code, exc.message, 422)

    @app.exception_handler(ck.CDPError)
    async def _cdp_error(_: Request, exc: ck.CDPError) -> JSONResponse:
        return _err(exc.code, exc.message, 502, hint="浏览器还活着吗？看该环境的日志与状态")

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
            return JSONResponse(
                status_code=503,
                content={"ok": False, "code": "NOT_READY", "message": "；".join(issues), "data": {"engine": eng}},
            )
        return ok({"status": "ready", "engine": eng, "root": str(s.root), "schema_version": get_db().schema_version()})

    # ── 元信息与 AI 自描述 ────────────────────────────────
    @app.get(f"{API_PREFIX}/meta", tags=["运维"], dependencies=[Auth, Cidr], summary="服务元信息")
    def meta() -> dict[str, Any]:
        s = get_settings()
        eng = mgr._engine_summary()
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
                "hub_token_set": bool(s.hub_token),
                "hub_configured": bool(s.hub_url),
                "sync_enabled": s.sync_enabled,
                "sync_password_env": bool(s.sync_password),
                "max_running": s.max_running,
                "port_pool": [s.port_pool_start, s.port_pool_end],
                "allowed_cidrs": s.allowed_cidrs,
                "idle_timeout": s.idle_timeout,
                "engine": eng,
                "schema_version": get_db().schema_version(),
                "features": {
                    "proxy_auth": True,
                    "export_import": True,
                    "fingerprint_probe": True,
                    "groups": True,
                    "mcp_http": True,
                },
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
                    "ephemeral": "临时环境：停止时自动清空数据目录，且禁止导出/同步。",
                    "management_tab": "页面类操作（导航/截图/执行 JS/localStorage）跑在专属管理页，不会劫持你正在看的标签页。",
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
                    "不想接 CDP、只想拿页面结果": [
                        "POST /api/v1/page/navigate  {profile, url}",
                        "POST /api/v1/page/screenshot {profile}  → 直接得到 PNG 字节",
                        "GET  /api/v1/page/text?profile=名字",
                        "POST /api/v1/page/evaluate  {profile, expression}",
                    ],
                    "要读/写登录态": [
                        "GET  /api/v1/profiles/{id}/cookies   （环境没运行会自动启动）",
                        "PUT  /api/v1/profiles/{id}/cookies   （body: {cookies: [...]}）",
                        "POST /api/v1/profiles/{id}/cookies/clear",
                    ],
                    "要在另一台机器接着用": [
                        "源机 POST /api/v1/sync/{id}/push  (body: {password: '<主密码>', ensure_running: true})",
                        "目标机 POST /api/v1/sync/{id}/pull  (body: {password: '<主密码>'})",
                        "只有一台机器时：POST /profiles/{id}/export 下载包；POST /profiles/import 上传包",
                    ],
                    "要批量": ["POST /api/v1/profiles/batch (body: {action, ids, concurrency})"],
                    "要判断指纹靠不靠谱": ["POST /api/v1/profiles/{id}/fingerprint/probe  ← 读真机指纹并给出自洽性警告"],
                },
                "重要约束": [
                    "cookie 只能从 running 的环境读写。stopped 时读文件是密文，读出来也没用。",
                    "同一个环境同一时刻只能有一个持有者（租约）。第二个 start 会 409。",
                    "push 需要主密码派生密钥；密码错误会在**落盘前**被拦下（code=KEY_FINGERPRINT_MISMATCH）。",
                    "指纹种子与宿主 OS 强绑定：fp_platform=host 最安全，跨 OS 会有字体/GPU 泄漏。",
                    "没有 GPU 的机器上 WebGL 拿不到，指纹会明显异常。要做真人身份请在有显卡的机器上跑。",
                    "带用户名密码的代理不能写在 --proxy-server 里，知纹会自动生成认证扩展来接管。",
                    "改了代理/扩展/时区/窗口等启动参数后必须 restart 才生效（响应里的 needs_restart 会告诉你）。",
                ],
                "错误码速查": {
                    "PROFILE_NOT_FOUND": "id/name 写错了，先 GET /profiles 看看",
                    "PROFILE_BUSY": "别的进程/机器正持有它，等或先 stop",
                    "STATE_CONFLICT": "当前状态不允许这个操作，先看 state",
                    "ENGINE_NOT_FOUND": "没装 Chromium 内核，见 docs/03-部署指南.md",
                    "ENGINE_EXITED": "浏览器起来就退了，读该环境的 profile.log",
                    "LAUNCH_TIMEOUT": "CDP 端口 45 秒没通，通常是内核损坏或被杀软拦",
                    "PORT_MISMATCH": "端口上跑的不是我们刚拉起的那个进程；先清孤儿进程",
                    "KEY_FINGERPRINT_MISMATCH": "主密码不对/包不是这台机器加密的。**不要重试**，先确认密码",
                    "PASSWORD_REQUIRED": "没给主密码，设 ZW_SYNC_PASSWORD 或请求里带 password",
                    "REVISION_EXISTS": "远端已有该版本，先 pull 对齐版本号",
                    "PROXY_IN_USE": "代理还被环境引用，先解绑",
                    "EXTENSION_IN_USE": "扩展还被环境绑定，先解绑",
                    "NOT_READY": "内核缺失或数据库损坏，看 hint",
                },
                "端点清单": sorted(
                    [
                        {"method": "GET", "path": "/healthz", "说明": "存活"},
                        {"method": "GET", "path": "/readyz", "说明": "就绪"},
                        {"method": "GET", "path": f"{API_PREFIX}/meta", "说明": "元信息"},
                        {"method": "GET", "path": f"{API_PREFIX}/ai/manifest", "说明": "本清单"},
                        {"method": "GET", "path": f"{API_PREFIX}/stats", "说明": "容量与状态统计"},
                        {"method": "GET", "path": f"{API_PREFIX}/groups", "说明": "分组概览"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles", "说明": "列环境"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles", "说明": "建环境"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles/{{id}}", "说明": "看单个"},
                        {"method": "PATCH", "path": f"{API_PREFIX}/profiles/{{id}}", "说明": "改"},
                        {"method": "DELETE", "path": f"{API_PREFIX}/profiles/{{id}}", "说明": "删"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/start", "说明": "开"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/stop", "说明": "关"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/restart", "说明": "重启"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/unlock", "说明": "强制释放租约"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/fingerprint/reset", "说明": "重置指纹"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/fingerprint/probe", "说明": "指纹体检"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles/{{id}}/cdp", "说明": "取 CDP 地址与令牌"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles/{{id}}/cookies", "说明": "读 cookie"},
                        {"method": "PUT", "path": f"{API_PREFIX}/profiles/{{id}}/cookies", "说明": "写 cookie"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/cookies/clear", "说明": "清空 cookie"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles/{{id}}/storage", "说明": "读 localStorage"},
                        {"method": "PUT", "path": f"{API_PREFIX}/profiles/{{id}}/storage", "说明": "写 localStorage"},
                        {"method": "GET", "path": f"{API_PREFIX}/profiles/{{id}}/logs", "说明": "看日志尾巴"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/export", "说明": "导出单文件"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{id}}/extensions", "说明": "绑定扩展"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/batch", "说明": "批量启停"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/import", "说明": "导入单文件"},
                        {"method": "GET", "path": f"{API_PREFIX}/extensions", "说明": "扩展库"},
                        {"method": "POST", "path": f"{API_PREFIX}/extensions/install", "说明": "装扩展"},
                        {"method": "GET", "path": f"{API_PREFIX}/proxies", "说明": "代理库"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies", "说明": "加代理"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies/check", "说明": "不保存直接测代理出口"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies/{{id}}/check", "说明": "测代理出口"},
                        {"method": "PATCH", "path": f"{API_PREFIX}/proxies/{{id}}", "说明": "改代理"},
                        {"method": "DELETE", "path": f"{API_PREFIX}/proxies/{{id}}", "说明": "删代理"},
                        {"method": "DELETE", "path": f"{API_PREFIX}/extensions/{{slug}}/{{version}}", "说明": "删扩展"},
                        {"method": "POST", "path": f"{API_PREFIX}/page/navigate", "说明": "导航"},
                        {"method": "POST", "path": f"{API_PREFIX}/page/screenshot", "说明": "截图"},
                        {"method": "POST", "path": f"{API_PREFIX}/page/evaluate", "说明": "执行 JS"},
                        {"method": "POST", "path": f"{API_PREFIX}/sync/{{id}}/push", "说明": "推送到 hub"},
                        {"method": "POST", "path": f"{API_PREFIX}/sync/{{id}}/pull", "说明": "从 hub 拉取"},
                        {"method": "GET", "path": f"{API_PREFIX}/sync/{{id}}/diff", "说明": "看差异"},
                        {"method": "POST", "path": f"{API_PREFIX}/sync/push-all", "说明": "全量推送"},
                        {"method": "GET", "path": f"{API_PREFIX}/sync/remote", "说明": "hub 上的索引"},
                        {"method": "GET", "path": f"{API_PREFIX}/audit", "说明": "审计日志"},
                        {"method": "GET", "path": f"{API_PREFIX}/events", "说明": "事件流"},
                        {"method": "GET", "path": "/cdp/{{id}}/json/version", "说明": "CDP 发现接口（地址已重写）"},
                        {"method": "WS", "path": "/cdp/{{id}}/...", "说明": "CDP 主通道"},
                    ],
                    key=lambda x: (x["path"], x["method"]),
                ),
                "MCP": {
                    "说明": "同一套能力已包装成 MCP 工具，AI 客户端可直接挂载",
                    "stdio": "zw mcp",
                    "http": "POST /mcp  （JSON-RPC 2.0，需要与其他接口同样的 Bearer 令牌）",
                },
                "本次运行的偏好": {
                    "gateway_public_base": s.gateway_public_base,
                    "node_id": s.node_id,
                    "max_running": s.max_running,
                    "hub_configured": bool(s.hub_url),
                },
            }
        )

    # ── 统计与分组 ────────────────────────────────────────
    @app.get(f"{API_PREFIX}/stats", tags=["运维"], dependencies=[Auth, Cidr], summary="容量与状态")
    def stats(refresh_size: bool = Query(default=False, description="强制重算磁盘占用（默认走 60 秒缓存）")) -> dict[str, Any]:
        return ok(mgr.stats(refresh_size=refresh_size))

    @app.get(f"{API_PREFIX}/groups", tags=["环境"], dependencies=[Auth], summary="分组概览")
    def groups_list() -> dict[str, Any]:
        return ok({"groups": mgr.groups()})

    # ══ 扩展库 ════════════════════════════════════════════
    # 注意：扩展相关端点必须在 /profiles/{ref} 之前不冲突（路径不同，无冲突），
    # 但 /profiles/import、/profiles/batch 必须在 /profiles/{ref} 之前。见下面顺序敏感标记。
    @app.get(f"{API_PREFIX}/extensions", tags=["扩展"], dependencies=[Auth], summary="扩展库列表")
    def extensions_list() -> dict[str, Any]:
        with get_db().conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM extensions ORDER BY added_at DESC")]
        used: dict[str, list[str]] = {}
        for p in mgr.list_profiles():
            for ref in p.get("extensions") or []:
                key = str(ref.get("slug") if isinstance(ref, dict) else ref)
                used.setdefault(key, []).append(p["name"])
        for r in rows:
            r["bound_to"] = used.get(r["slug"], [])
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
            try:
                with zipfile.ZipFile(src) as zf:
                    # 防路径穿越：任何成员带 .. 或绝对路径就整体拒绝
                    for name in zf.namelist():
                        if name.startswith("/") or ".." in Path(name).parts:
                            raise ApiError("UNSAFE_ARCHIVE", f"zip 里有不安全路径：{name}")
                    zf.extractall(target)
            except zipfile.BadZipFile as exc:
                raise ApiError("BAD_ARCHIVE", f"不是合法的 zip：{exc}") from exc
        elif src.is_dir():
            target = s.ext_dir / src.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(src, target)
        else:
            raise ApiError("NOT_AN_EXTENSION", "只接受解包后的扩展目录，或 .zip 压缩包")

        # 有些扩展下载后多套了一层目录（zip 里含 __MACOSX 或包了一层文件夹）
        manifest_path = target / "manifest.json"
        if not manifest_path.exists():
            nested = [p for p in target.glob("*/manifest.json")]
            if len(nested) == 1:
                inner = nested[0].parent
                moved = target.parent / f"{target.name}.__tmp"
                shutil.move(str(inner), str(moved))
                shutil.rmtree(target, ignore_errors=True)
                shutil.move(str(moved), str(target))
                manifest_path = target / "manifest.json"
        if not manifest_path.exists():
            shutil.rmtree(target, ignore_errors=True)
            raise ApiError("NOT_AN_EXTENSION", f"{src.name} 里没有 manifest.json，不像是解包后的扩展目录")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise ApiError("BAD_MANIFEST", f"manifest.json 不是合法 JSON：{exc}") from exc

        slug = (manifest.get("name") or target.name).strip().lower().replace(" ", "-")[:64]
        slug = "".join(ch for ch in slug if ch.isalnum() or ch in "-_.") or target.name[:64]
        version = str(manifest.get("version") or "0")
        h = hashlib.sha256()
        for p in sorted(target.rglob("*")):
            if p.is_file():
                h.update(p.read_bytes())
        digest = h.hexdigest()
        with get_db().conn() as c:
            c.execute(
                "INSERT INTO extensions(slug,version,path,name,sha256,added_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(slug,version) DO UPDATE SET path=excluded.path, name=excluded.name, sha256=excluded.sha256",
                (slug, version, str(target), manifest.get("name", slug), digest, time.time()),
            )
        get_db().audit("extension.install", detail={"slug": slug, "version": version, "path": str(target)})
        return ok({"slug": slug, "version": version, "path": str(target), "name": manifest.get("name", slug), "sha256": digest})

    @app.delete(f"{API_PREFIX}/extensions/{{slug}}/{{version}}", tags=["扩展"], dependencies=[Auth], summary="删除扩展")
    def extensions_delete(slug: str, version: str) -> dict[str, Any]:
        with get_db().conn() as c:
            row = c.execute("SELECT * FROM extensions WHERE slug=? AND version=?", (slug, version)).fetchone()
        if row is None:
            raise ApiError("EXTENSION_NOT_FOUND", f"扩展库里没有 {slug}@{version}", 404)
        bound = [p["name"] for p in mgr.list_profiles() if slug in [str(x.get("slug") if isinstance(x, dict) else x) for x in (p.get("extensions") or [])]]
        if bound:
            raise ApiError(
                "EXTENSION_IN_USE",
                f"{slug} 还被这些环境使用：{'、'.join(bound)}",
                409,
                hint="先在环境里解绑，再删除扩展",
            )
        with get_db().conn() as c:
            c.execute("DELETE FROM extensions WHERE slug=? AND version=?", (slug, version))
        import shutil

        shutil.rmtree(Path(row["path"]), ignore_errors=True)
        get_db().audit("extension.delete", detail={"slug": slug, "version": version})
        return ok({"deleted": True, "slug": slug, "version": version})

    # ══ 代理库 ════════════════════════════════════════════
    @app.get(f"{API_PREFIX}/proxies", tags=["代理"], dependencies=[Auth], summary="代理库列表")
    def proxies_list() -> dict[str, Any]:
        with get_db().conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM proxies ORDER BY id DESC")]
        used: dict[int, list[str]] = {}
        for p in mgr.list_profiles():
            if p.get("proxy_id"):
                used.setdefault(int(p["proxy_id"]), []).append(p["name"])
        out = []
        for r in rows:
            r.pop("password", None)  # 绝不回显密码
            r["has_password"] = bool(
                (get_db().get_setting(f"proxy_pw_{r['id']}", "") != "") or r.get("username")
            )
            r["upstream"] = f"{r['scheme']}://{r['host']}:{r['port']}"
            r["used_by"] = used.get(r["id"], [])
            out.append(r)
        return ok({"proxies": out, "total": len(out)})

    @app.post(f"{API_PREFIX}/proxies", tags=["代理"], dependencies=[Auth], summary="新增代理")
    def proxies_create(body: ProxyBody) -> dict[str, Any]:
        if not body.host:
            raise ApiError("PROXY_INVALID", "host 不能为空")
        if not (0 < body.port < 65536):
            raise ApiError("PROXY_INVALID", "port 要在 1-65535 之间")
        label = (body.label or f"{body.host}:{body.port}").strip()
        with get_db().conn() as c:
            if c.execute("SELECT 1 FROM proxies WHERE label=?", (label,)).fetchone():
                raise ApiError("LABEL_TAKEN", f"代理标签已存在：{label}", 409)
            cur = c.execute(
                "INSERT INTO proxies(label,scheme,host,port,username,password,country,last_check) VALUES(?,?,?,?,?,?,?,?)",
                (label, body.scheme, body.host, body.port, body.username, body.password, body.country, None),
            )
            pid = cur.lastrowid
        get_db().audit("proxy.create", detail={"id": pid, "label": label})
        return ok({"id": pid, "label": label})

    @app.patch(f"{API_PREFIX}/proxies/{{proxy_id}}", tags=["代理"], dependencies=[Auth], summary="修改代理")
    def proxies_patch(proxy_id: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        allowed = {"label", "scheme", "host", "port", "username", "password", "country"}
        bad = set(body) - allowed
        if bad:
            raise ApiError("UNKNOWN_FIELDS", f"不认识的字段：{'、'.join(sorted(bad))}")
        with get_db().conn() as c:
            row = c.execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
            if row is None:
                raise ApiError("PROXY_NOT_FOUND", f"代理不存在：{proxy_id}", 404)
            fields = dict(body)
            if "password" in fields and fields["password"] == "":
                fields.pop("password")  # 空串 = 不改密码
            if not fields:
                return ok({"updated": False})
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE proxies SET {cols} WHERE id=?", (*fields.values(), proxy_id))
        get_db().audit("proxy.update", detail={"id": proxy_id, "fields": sorted(body)})
        return ok({"updated": True, "id": proxy_id})

    @app.delete(f"{API_PREFIX}/proxies/{{proxy_id}}", tags=["代理"], dependencies=[Auth], summary="删除代理")
    def proxies_delete(proxy_id: int) -> dict[str, Any]:
        bound = [p["name"] for p in mgr.list_profiles() if p.get("proxy_id") and int(p["proxy_id"]) == proxy_id]
        if bound:
            raise ApiError("PROXY_IN_USE", f"代理还被这些环境使用：{'、'.join(bound)}", 409, hint="先在这些环境里清掉 proxy_id")
        with get_db().conn() as c:
            c.execute("DELETE FROM proxies WHERE id=?", (proxy_id,))
        get_db().audit("proxy.delete", detail={"id": proxy_id})
        return ok({"deleted": True, "id": proxy_id})

    @app.post(f"{API_PREFIX}/proxies/check", tags=["代理"], dependencies=[Auth], summary="不保存直接测出口")
    def proxies_check_raw(body: ProxyBody) -> dict[str, Any]:
        raw = f"{body.scheme}://" + (f"{body.username}:{body.password}@" if body.username else "") + f"{body.host}:{body.port}"
        spec = proxysvc.parse_proxy(raw)
        if spec is None:
            raise ApiError("PROXY_INVALID", "没有可检测的代理")
        return ok(proxysvc.check_egress(spec))

    @app.post(f"{API_PREFIX}/proxies/{{proxy_id}}/check", tags=["代理"], dependencies=[Auth], summary="检测代理出口")
    def proxies_check(proxy_id: int) -> dict[str, Any]:
        with get_db().conn() as c:
            row = c.execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
        if row is None:
            raise ApiError("PROXY_NOT_FOUND", f"代理不存在：{proxy_id}", 404)
        raw = f"{row['scheme']}://" + (f"{row['username']}:{row['password']}@" if row["username"] else "") + f"{row['host']}:{row['port']}"
        spec = proxysvc.parse_proxy(raw)
        result = proxysvc.check_egress(spec) if spec else {"ok": False, "error": "代理配置不完整"}
        with get_db().conn() as c:
            c.execute(
                "UPDATE proxies SET last_ok=?, last_check=?, last_exit_ip=?, country=COALESCE(NULLIF(?,''), country) WHERE id=?",
                (1 if result.get("ok") else 0, time.time(), result.get("exit_ip", ""), result.get("country", ""), proxy_id),
            )
        return ok(result)

    # ══ 环境（顺序敏感：固定路径先声明）═══════════════════
    @app.post(f"{API_PREFIX}/profiles/import", tags=["环境"], dependencies=[Auth], summary="从导出包导入环境")
    async def profiles_import(request: Request, password: str | None = Query(default=None)) -> dict[str, Any]:
        raw = await request.body()
        if not raw:
            raise ApiError("IMPORT_INVALID", "请求体为空，请把 .zwp 文件的字节作为 body 上传")
        # 导入是重活（Argon2 + 解包 + 写盘），丢线程池别卡事件循环
        import asyncio

        return ok(await asyncio.to_thread(mgr.import_profile, raw, password))

    @app.post(f"{API_PREFIX}/profiles/batch", tags=["环境"], dependencies=[Auth], summary="批量操作")
    def profiles_batch(body: BatchBody) -> dict[str, Any]:
        action = body.action
        if action not in ("start", "stop", "restart"):
            raise ApiError("INVALID_ACTION", "action 只能是 start / stop / restart")
        if not body.ids:
            raise ApiError("INVALID_ACTION", "ids 必须是非空数组")
        ids = body.ids
        if action == "start":
            return ok(mgr.start_many([str(i) for i in ids], concurrency=body.concurrency))
        results = []
        for ref in ids:
            try:
                if action == "stop":
                    mgr.stop(str(ref))
                else:
                    mgr.restart(str(ref))
                results.append({"ref": str(ref), "ok": True})
            except Exception as exc:
                results.append({"ref": str(ref), "ok": False, "error": getattr(exc, "message", str(exc))})
        return ok({"total": len(results), "ok": sum(1 for r in results if r["ok"]), "results": results})

    @app.get(f"{API_PREFIX}/profiles", tags=["环境"], dependencies=[Auth], summary="列出环境")
    def profiles_list(
        state: str | None = None,
        group: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        items = mgr.list_profiles(state=state, group=group, q=q)
        return ok({"profiles": items, "total": len(items), "groups": [g["name"] for g in mgr.groups()]})

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
    def profiles_start(ref: str, body: StartBody | None = None) -> dict[str, Any]:
        b = body or StartBody()
        return ok(mgr.start(ref, urls=b.urls, wait=b.wait))

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/stop", tags=["环境"], dependencies=[Auth], summary="停止环境")
    def profiles_stop(ref: str, force: bool = False) -> dict[str, Any]:
        return ok(mgr.stop(ref, force=force))

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/restart", tags=["环境"], dependencies=[Auth], summary="重启环境")
    def profiles_restart(ref: str, body: StartBody | None = None) -> dict[str, Any]:
        b = body or StartBody()
        return ok(mgr.restart(ref, urls=b.urls))

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/unlock", tags=["环境"], dependencies=[Auth], summary="强制释放租约")
    def profiles_unlock(ref: str) -> dict[str, Any]:
        p = mgr.get_profile(ref)
        LocalLease(mgr.profile_dir(p["id"])).force_release()
        get_db().audit("profile.unlock", p["id"], result="ok")
        logger.warning("强制释放租约 name=%s", p["name"])
        return ok({"released": True, "profile_id": p["id"]})

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/fingerprint/reset", tags=["环境"], dependencies=[Auth], summary="重置指纹（危险）")
    def profiles_reset_fp(ref: str, body: ResetFingerprintBody) -> dict[str, Any]:
        return ok(mgr.reset_fingerprint(ref, body.confirm))

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/fingerprint/probe", tags=["环境"], dependencies=[Auth], summary="指纹体检")
    def profiles_probe_fp(ref: str, start_if_needed: bool = True) -> dict[str, Any]:
        return ok(mgr.probe_fingerprint(ref, start_if_needed=start_if_needed))

    @app.get(f"{API_PREFIX}/profiles/{{ref}}/cdp", tags=["环境"], dependencies=[Auth], summary="取 CDP 地址")
    def profiles_cdp(ref: str, ttl: int | None = Query(default=None, ge=60, le=7200)) -> dict[str, Any]:
        p = mgr.get_profile(ref)
        if p["state"] != "running":
            raise ApiError("STATE_CONFLICT", f"{p['name']} 当前是 {p['state']}，先启动再取 CDP 地址", 409)
        return ok(mgr.cdp_info(p, ttl=ttl))

    @app.get(f"{API_PREFIX}/profiles/{{ref}}/logs", tags=["环境"], dependencies=[Auth], summary="读环境日志")
    def profiles_logs(ref: str, tail: int = Query(default=200, ge=1, le=5000)) -> dict[str, Any]:
        p = mgr.get_profile(ref)
        out: dict[str, Any] = {"profile_id": p["id"], "name": p["name"], "state": p["state"], "last_error": p.get("last_error", "")}
        for label, name in (("browser", "browser.log"), ("profile", "profile.log")):
            f = mgr.profile_dir(p["id"]) / name
            if f.exists():
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]
                out[label] = lines
            else:
                out[label] = []
        return ok(out)

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/export", tags=["环境"], dependencies=[Auth], summary="导出环境为单文件")
    def profiles_export(
        ref: str,
        body: ExportBody | None = None,
        password: str | None = Query(default=None),
    ) -> Response:
        """导出加密单文件。

        主密码三个来源（优先级从高到低）：JSON body 的 password、查询串的 password、
        服务端环境变量 ZW_SYNC_PASSWORD。**三种都收** 是有意的：
        - 浏览器里点「导出」时前端只能用 fetch 带 JSON body（它不好加查询串）；
        - curl / 脚本更喜欢 `?password=`；
        - 而长期部署的人会把它放在 `.env` 里，不想每次都传。
        只认其中一种就会让另外两种场景莫名其妙地报 PASSWORD_REQUIRED。
        """
        pw = (body.password if body and body.password else None) or password
        blob, filename = mgr.export_profile(ref, password=pw)
        from urllib.parse import quote as _q

        return Response(
            content=blob,
            media_type="application/octet-stream",
            headers={"X-ZW-Filename": _q(filename), "Content-Disposition": f"attachment; filename*=UTF-8''{_q(filename)}"},
        )

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/extensions", tags=["环境"], dependencies=[Auth], summary="绑定扩展")
    def profile_bind_extension(ref: str, body: ExtBindBody) -> dict[str, Any]:
        p = mgr.get_profile(ref)
        with get_db().conn() as c:
            if body.version:
                row = c.execute("SELECT * FROM extensions WHERE slug=? AND version=?", (body.slug, body.version)).fetchone()
            else:
                row = c.execute("SELECT * FROM extensions WHERE slug=? ORDER BY added_at DESC LIMIT 1", (body.slug,)).fetchone()
        if row is None:
            raise ApiError("EXTENSION_NOT_FOUND", f"扩展库里没有 {body.slug}", 404)
        refs = [str(x.get("slug") if isinstance(x, dict) else x) for x in (p.get("extensions") or [])]
        if body.slug not in refs:
            refs.append(body.slug)
        mgr._update(p["id"], extensions=refs)
        return ok({**mgr.get_profile(p["id"]), "needs_restart": ["extensions"] if p["state"] == "running" else []})

    @app.delete(f"{API_PREFIX}/profiles/{{ref}}/extensions/{{slug}}", tags=["环境"], dependencies=[Auth], summary="解绑扩展")
    def profile_unbind_extension(ref: str, slug: str) -> dict[str, Any]:
        p = mgr.get_profile(ref)
        refs = [str(x.get("slug") if isinstance(x, dict) else x) for x in (p.get("extensions") or [])]
        refs = [r for r in refs if r != slug]
        mgr._update(p["id"], extensions=refs)
        return ok({**mgr.get_profile(p["id"]), "needs_restart": ["extensions"] if p["state"] == "running" else []})

    # ══ cookie / storage ══════════════════════════════════
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
        ws = mgr.local_ws(p)
        if ws is None:
            if not p.get("cdp_enabled", True):
                raise ApiError("CDP_DISABLED", f"{p['name']} 关闭了调试端口（cdp_enabled=false），无法读写 cookie", 409)
            raise ApiError("CDP_UNAVAILABLE", f"取不到 {p['name']} 的调试地址（浏览器可能刚崩）", 502)
        return p, ws

    @app.get(f"{API_PREFIX}/profiles/{{ref}}/cookies", tags=["数据"], dependencies=[Auth], summary="读 cookie")
    def cookies_get(ref: str, fmt: str = Query(default="cdp", pattern="^(cdp|netscape|playwright)$")) -> Any:
        p, ws = _ws_for(ref)
        items = ck.dump_cookies_sync(ws)
        mgr.touch(p["id"])
        get_db().audit("cookie.read", p["id"], detail={"count": len(items)})
        if fmt == "cdp":
            return ok({"count": len(items), "cookies": items})
        if fmt == "netscape":
            lines = ["# Netscape HTTP Cookie File"]
            for c in items:
                dom = c.get("domain", "")
                lines.append(
                    "\t".join(
                        [
                            dom if dom.startswith(".") else dom,
                            "TRUE" if dom.startswith(".") else "FALSE",
                            c.get("path", "/"),
                            "TRUE" if c.get("secure") else "FALSE",
                            str(int(c.get("expires") or 0)),
                            c.get("name", ""),
                            c.get("value", ""),
                        ]
                    )
                )
            return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; charset=utf-8")
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
        if body.mode not in ("replace", "merge"):
            raise ApiError("INVALID_MODE", "mode 只能是 replace 或 merge")
        p, ws = _ws_for(ref, start_if_needed=body.start_if_needed)
        report = ck.load_cookies_sync(ws, body.cookies, clear=(body.mode == "replace"))
        mgr.touch(p["id"])
        get_db().audit("cookie.write", p["id"], detail={"count": len(body.cookies), "mode": body.mode, "report": report})
        return ok({"profile_id": p["id"], **report})

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/cookies/clear", tags=["数据"], dependencies=[Auth], summary="清空 cookie")
    def cookies_clear(ref: str, start_if_needed: bool = True) -> dict[str, Any]:
        p, ws = _ws_for(ref, start_if_needed=start_if_needed)
        report = ck.clear_cookies_sync(ws)
        get_db().audit("cookie.clear", p["id"], detail=report)
        logger.warning("清空 cookie name=%s", p["name"])
        return ok({"profile_id": p["id"], **report})

    @app.get(f"{API_PREFIX}/profiles/{{ref}}/storage", tags=["数据"], dependencies=[Auth], summary="读 localStorage")
    def storage_get(ref: str, origin: list[str] = Query(default=[])) -> dict[str, Any]:
        p, ws = _ws_for(ref)
        if not origin:
            raise ApiError("ORIGIN_REQUIRED", "请用 ?origin=https://a.com 指定要读哪个站点（localStorage 是按源隔离的）")
        for o in origin:
            if not o.startswith(("http://", "https://")):
                raise ApiError("ORIGIN_INVALID", f"origin 只接受 http/https：{o}")
        mgr.touch(p["id"])
        return ok({"profile_id": p["id"], "storage": ck.dump_storage_sync(ws, origin)})

    @app.put(f"{API_PREFIX}/profiles/{{ref}}/storage", tags=["数据"], dependencies=[Auth], summary="写 localStorage")
    def storage_put(ref: str, body: StorageBody) -> dict[str, Any]:
        for o in body.mapping:
            if not o.startswith(("http://", "https://")):
                raise ApiError("ORIGIN_INVALID", f"origin 只接受 http/https：{o}")
        p, ws = _ws_for(ref, start_if_needed=body.start_if_needed)
        report = ck.load_storage_sync(ws, body.mapping)
        get_db().audit("storage.write", p["id"], detail={"origins": list(body.mapping)})
        return ok({"profile_id": p["id"], "report": report})

    # ══ 页面能力（给 AI 直接用，不必自己接 CDP）════════════
    @app.post(f"{API_PREFIX}/page/navigate", tags=["页面"], dependencies=[Auth], summary="导航到网址")
    def page_navigate(body: NavigateBody) -> dict[str, Any]:
        p, ws = _ws_for(body.profile, start_if_needed=True)
        mgr.touch(p["id"])
        return ok(ck.navigate_sync(ws, body.url))

    @app.post(f"{API_PREFIX}/page/evaluate", tags=["页面"], dependencies=[Auth], summary="执行 JS 表达式")
    def page_evaluate(body: EvaluateBody) -> dict[str, Any]:
        p, ws = _ws_for(body.profile, start_if_needed=True)
        mgr.touch(p["id"])
        return ok({"result": ck.evaluate_sync(ws, body.expression)})

    @app.post(f"{API_PREFIX}/page/screenshot", tags=["页面"], dependencies=[Auth], summary="页面截图")
    def page_screenshot(body: ScreenshotBody) -> Any:
        p, ws = _ws_for(body.profile, start_if_needed=True)
        mgr.touch(p["id"])
        data = ck.screenshot_sync(ws, fmt=body.format, full_page=body.full_page)
        if body.format == "png":
            return Response(content=data, media_type="image/png", headers={"Cache-Control": "no-store"})
        return ok({"base64": base64.b64encode(data).decode(), "format": body.format, "bytes": len(data)})

    @app.get(f"{API_PREFIX}/page/text", tags=["页面"], dependencies=[Auth], summary="页面可见文字")
    def page_text(profile: str) -> dict[str, Any]:
        p, ws = _ws_for(profile, start_if_needed=True)
        mgr.touch(p["id"])
        return ok({"text": ck.run(ck.page_text(ws))})

    @app.get(f"{API_PREFIX}/page/targets", tags=["页面"], dependencies=[Auth], summary="列出标签页")
    def page_targets(profile: str) -> dict[str, Any]:
        _, ws = _ws_for(profile)
        return ok({"targets": ck.run(ck.list_targets(ws))})

    # ══ 同步（顺序敏感：固定路径先声明）═══════════════════
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
        hub_ok = False
        hub_error = ""
        if s.hub_url:
            try:
                HubClient(s.hub_url, s.hub_token).health()
                hub_ok = True
            except Exception as exc:
                hub_error = str(exc)
        return ok({
            "hub_url": s.hub_url,
            "hub_configured": bool(s.hub_url),
            "hub_ok": hub_ok,
            "hub_error": hub_error,
            "password_env": bool(s.sync_password),
            "profiles": items,
        })

    @app.post(f"{API_PREFIX}/sync/push-all", tags=["同步"], dependencies=[Auth], summary="全量推送")
    def sync_push_all(body: SyncPushBody | None = None) -> dict[str, Any]:
        b = body or SyncPushBody()
        return ok(syncsvc.push_all(password=b.password))

    @app.get(f"{API_PREFIX}/sync/remote", tags=["同步"], dependencies=[Auth], summary="hub 上的索引")
    def sync_remote() -> dict[str, Any]:
        return ok({"profiles": syncsvc.remote_list()})

    @app.post(f"{API_PREFIX}/sync/{{ref}}/push", tags=["同步"], dependencies=[Auth], summary="推送到 hub")
    def sync_push(ref: str, body: SyncPushBody | None = None) -> dict[str, Any]:
        b = body or SyncPushBody()
        return ok(
            syncsvc.push(
                ref, password=b.password, note=b.note,
                ensure_running=b.ensure_running, cross_machine=b.cross_machine,
            )
        )

    @app.post(f"{API_PREFIX}/sync/{{ref}}/pull", tags=["同步"], dependencies=[Auth], summary="从 hub 拉取")
    def sync_pull(ref: str, body: SyncPullBody | None = None) -> dict[str, Any]:
        b = body or SyncPullBody()
        return ok(
            syncsvc.pull(
                ref, password=b.password, revision=b.revision,
                write_cookies=b.write_cookies, force=b.force, apply_settings=b.apply_settings,
            )
        )

    @app.get(f"{API_PREFIX}/sync/{{ref}}/diff", tags=["同步"], dependencies=[Auth], summary="本地与远端差异")
    def sync_diff(ref: str, password: str | None = None) -> dict[str, Any]:
        return ok(syncsvc.diff(ref, password=password))

    @app.get(f"{API_PREFIX}/sync/{{ref}}/history", tags=["同步"], dependencies=[Auth], summary="版本历史")
    def sync_history(ref: str) -> dict[str, Any]:
        return ok(syncsvc.remote_history(ref))

    @app.delete(f"{API_PREFIX}/sync/{{ref}}", tags=["同步"], dependencies=[Auth], summary="删除远端版本")
    def sync_delete(ref: str, revision: int | None = None) -> dict[str, Any]:
        p = mgr.get_profile(ref)
        return ok(syncsvc.remote_delete(p["id"], revision=revision))

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
            mgr._SIZE_CACHE.pop(p["id"], None)
        total["freed_human"] = mgr.human_size(total["freed_bytes"])
        get_db().audit("maintenance.trim", detail=total)
        return ok(total)

    @app.post(f"{API_PREFIX}/maintenance/doctor", tags=["运维"], dependencies=[Auth], summary="环境自检")
    def maintenance_doctor() -> dict[str, Any]:
        s = get_settings()
        checks: list[dict[str, Any]] = []
        eng = mgr._engine_summary()
        checks.append({"项": "浏览器内核", "ok": eng["ok"], "详情": eng})
        if eng["ok"] and not eng.get("fingerprint_supported"):
            checks.append(
                {
                    "项": "指纹内核",
                    "ok": False,
                    "详情": "当前内核不是 fingerprint-chromium，--fingerprint 参数会被忽略，环境之间只剩 cookie 隔离。"
                    "建议换成 fingerprint-chromium（见 docs/01-快速开始.md）",
                }
            )
        else:
            checks.append({"项": "指纹内核", "ok": eng.get("fingerprint_supported", False), "详情": "支持 --fingerprint"})
        checks.append({"项": "数据库", "ok": get_db().integrity_ok(), "详情": {"path": str(s.db_path), "schema": get_db().schema_version()}})
        has_token = bool(s.admin_token)
        checks.append(
            {
                "项": "访问令牌",
                "ok": has_token or s.host in ("127.0.0.1", "localhost", "::1"),
                "详情": "已配置" if has_token else "未配置（仅回环可用）",
            }
        )
        hub_ok = False
        hub_detail: Any = "未配置"
        if s.hub_url:
            try:
                client = HubClient(s.hub_url, s.hub_token)
                hub_detail = {"health": client.health(), "stats": client.stats()}
                hub_ok = True
            except Exception as exc:
                hub_detail = str(exc)
        checks.append({"项": "同步中心", "ok": hub_ok or not s.hub_url, "详情": hub_detail})
        checks.append({"项": "数据目录", "ok": Path(s.root).exists(), "详情": str(s.root)})
        free = _disk_free(s.root)
        checks.append({
            "项": "磁盘余量",
            "ok": free > 2 * 1024**3,
            "详情": f"剩余 {mgr.human_size(free)}（低于 2GB 时浏览器会写盘失败）",
        })
        st = mgr.stats()
        checks.append({"项": "并发余量", "ok": st["running"] < s.max_running, "详情": f"{st['running']}/{s.max_running}"})
        checks.append({"项": "数据目录可写", "ok": _writable(s.root), "详情": str(s.root)})
        return ok({"checks": checks, "all_ok": all(c["ok"] for c in checks), "stats": st})

    @app.post(f"{API_PREFIX}/maintenance/reap-orphans", tags=["运维"], dependencies=[Auth], summary="清理孤儿浏览器进程")
    def maintenance_reap() -> dict[str, Any]:
        """清掉"数据库里没有对应环境、或者环境状态不是 running"却还在跑的浏览器。

        典型来源：core 被 kill -9、数据库被重置、手动删了环境目录。
        这些进程会一直占内存和端口，还会让同名环境下次启动撞 SingletonLock。
        """
        return ok(mgr.reap_orphan_browsers())

    @app.post(f"{API_PREFIX}/maintenance/cleanup", tags=["运维"], dependencies=[Auth], summary="清理 staging / 令牌 / 孤儿目录")
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
        tokens = get_db().purge_cdp_tokens()
        # 没人引用的代理认证扩展目录（里面是明文凭据，不能留着）
        keep: set[str] = set()
        for p in mgr.list_profiles():
            try:
                spec = mgr.proxy_spec_for(p)
            except proxysvc.ProxyError:
                continue
            if spec and spec.needs_auth:
                keep.add(spec.key)
        ext_removed = proxysvc.cleanup_auth_extensions(keep)
        audit_pruned = get_db().prune_audit()
        return ok({"staging_removed": removed, "tokens_purged": tokens, "auth_ext_removed": ext_removed, "audit_pruned": audit_pruned})

    # ── CDP 网关入口 ──────────────────────────────────────
    async def _cdp_guard(profile_id: str, token: str) -> JSONResponse | None:
        if not mgr.verify_cdp_token(profile_id, token):
            return JSONResponse(
                status_code=401,
                content={"ok": False, "code": "UNAUTHENTICATED", "message": "CDP 令牌无效或已过期，请重新 GET /api/v1/profiles/{id}/cdp"},
            )
        return None

    @app.get("/cdp/{profile_id}/json/version", tags=["CDP 网关"], summary="CDP 版本（已重写 ws 地址）")
    async def cdp_version(profile_id: str, token: str = "") -> Response:
        bad = await _cdp_guard(profile_id, token)
        if bad:
            return bad
        status, body, headers = await gateway.proxy_http(profile_id, "/json/version", get_settings().gateway_public_base, token)
        return Response(content=body, status_code=status, headers=headers)

    @app.get("/cdp/{profile_id}/json/list", tags=["CDP 网关"], summary="CDP 目标列表")
    async def cdp_list(profile_id: str, token: str = "") -> Response:
        bad = await _cdp_guard(profile_id, token)
        if bad:
            return bad
        status, body, headers = await gateway.proxy_http(profile_id, "/json/list", get_settings().gateway_public_base, token)
        return Response(content=body, status_code=status, headers=headers)

    @app.get("/cdp/{profile_id}/json", tags=["CDP 网关"], summary="CDP 目标列表（根）")
    async def cdp_json_root(profile_id: str, token: str = "") -> Response:
        bad = await _cdp_guard(profile_id, token)
        if bad:
            return bad
        status, body, headers = await gateway.proxy_http(profile_id, "/json", get_settings().gateway_public_base, token)
        return Response(content=body, status_code=status, headers=headers)

    @app.put("/cdp/{profile_id}/json/new", tags=["CDP 网关"], summary="新建标签页")
    async def cdp_json_new(profile_id: str, token: str = "", url: str = "about:blank") -> Response:
        bad = await _cdp_guard(profile_id, token)
        if bad:
            return bad
        from urllib.parse import quote as _q

        status, body, headers = await gateway.proxy_http(
            profile_id, f"/json/new?{_q(url, safe='')}", get_settings().gateway_public_base, token, method="PUT"
        )
        return Response(content=body, status_code=status, headers=headers)

    @app.get("/cdp/{profile_id}/json/activate/{target_id}", tags=["CDP 网关"], summary="激活标签页")
    async def cdp_json_activate(profile_id: str, target_id: str, token: str = "") -> Response:
        bad = await _cdp_guard(profile_id, token)
        if bad:
            return bad
        status, body, headers = await gateway.proxy_http(
            profile_id, f"/json/activate/{target_id}", get_settings().gateway_public_base, token
        )
        return Response(content=body, status_code=status, headers=headers)

    @app.get("/cdp/{profile_id}/json/close/{target_id}", tags=["CDP 网关"], summary="关闭标签页")
    async def cdp_json_close(profile_id: str, target_id: str, token: str = "") -> Response:
        bad = await _cdp_guard(profile_id, token)
        if bad:
            return bad
        status, body, headers = await gateway.proxy_http(
            profile_id, f"/json/close/{target_id}", get_settings().gateway_public_base, token
        )
        return Response(content=body, status_code=status, headers=headers)

    @app.get("/cdp/{profile_id}/json/{rest:path}", tags=["CDP 网关"], summary="CDP 其余发现接口（兜底）")
    async def cdp_json_rest(profile_id: str, rest: str, token: str = "") -> Response:
        bad = await _cdp_guard(profile_id, token)
        if bad:
            return bad
        status, body, headers = await gateway.proxy_http(profile_id, f"/json/{rest}", get_settings().gateway_public_base, token)
        return Response(content=body, status_code=status, headers=headers)

    @app.websocket("/cdp/{profile_id}/{path:path}")
    async def cdp_ws(websocket: WebSocket, profile_id: str, path: str) -> None:
        token = websocket.query_params.get("token", "")
        if not mgr.verify_cdp_token(profile_id, token):
            await websocket.accept()
            await websocket.close(code=1008, reason="bad-token")
            return
        await gateway.proxy_ws(websocket, profile_id, f"/{path}", get_settings().cdp_max_conn_per_profile)

    @app.websocket("/cdp/{profile_id}")
    async def cdp_ws_root(websocket: WebSocket, profile_id: str) -> None:
        token = websocket.query_params.get("token", "")
        if not mgr.verify_cdp_token(profile_id, token):
            await websocket.accept()
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


def _disk_free(path: Path) -> int:
    import shutil

    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


def _writable(path: Path) -> bool:
    probe = Path(path) / ".write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False

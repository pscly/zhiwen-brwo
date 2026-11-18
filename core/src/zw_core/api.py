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
import contextlib
import hmac
import ipaddress
import json
import logging
import threading
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
from .services import (bundle, cookies as ck, engines, enginesrc, extstore, manager as mgr,
                       packs, proxy as proxysvc, proxylib, proxyrelay as relaysvc,
                       session as sessionsvc, shell as shellsvc, sync as syncsvc)
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
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost", "0.0.0.0")


def is_loopback_request(request: Request) -> bool:
    ip = _client_ip(request)
    return ip in LOOPBACK_HOSTS or ip.startswith("127.")


def require_auth(
    request: Request,
    authorization: str = Header(default=""),
    x_zw_token: str = Header(default=""),
) -> None:
    """控制台鉴权。

    ## 规则（按用户要求改过）

    1. **来自本机（回环）→ 直接放行**。用户原话："客户端打开控制台的话，
       就再也不需要输入什么访问令牌，直接就可以进入。" 本机进程本来就能
       直接读配置文件和数据库，拦它没有意义，只会让人每次多输一次密码。
    2. **来自其它地址 → 必须提供凭据**，二者任一：
       - `设备机器码`（`ZW_DEVICE_ID`，客户端自动生成，36 字符随机串）
       - `ZW_ADMIN_TOKEN`（如果配了的话）
       用户原话："如果远程需要访问客户端的控制台，只需要输入客户端的机器码即可。"

    ⚠️ 机器码就是远程访问的口令，所以**不要把它贴到公开的地方**。
    它同时也是设备在 hub 上的标识，两者一致是刻意的：知道机器码的人本来就能
    在服务器控制台上对这台机器执行命令。
    """
    import hmac

    s = get_settings()
    if is_loopback_request(request):
        return
    token = authorization.removeprefix("Bearer ").strip() or x_zw_token.strip()
    candidates = [c for c in (s.admin_token, s.device_id) if c]
    if not candidates:
        # 既没有令牌也没有机器码（独立跑 core 且没配任何凭据）：
        # config 已保证对外监听时不会出现这种组合，这里只是兜底。
        raise ApiError("UNAUTHENTICATED", "这台机器没有配置任何凭据，已拒绝外部访问", status=401)
    for cand in candidates:
        if token and hmac.compare_digest(token, cand):
            return
    raise ApiError(
        "UNAUTHENTICATED",
        "需要机器码或访问令牌",
        status=401,
        hint="远程打开控制台时输入这台电脑的机器码（形如 zwd_...）；也可以配置 ZW_ADMIN_TOKEN",
    )


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
    """装扩展的入参（四种来源共用一个入口）。

    - `url`：Chrome 商店详情页链接或 32 位扩展 ID → 客户端下载 CRX
    - `path`：`.crx` 文件 / 已解包的扩展目录 / `.zip`
    - `make_pack` / `as_default`：装完是否顺手建扩展包、是否设为默认
      （"以后新建环境默认带上"就是 as_default）
    """

    path: str = Field(default="", description="扩展所在目录 / .zip / .crx 的绝对路径")
    url: str = Field(default="", description="Chrome 应用商店链接或 32 位扩展 ID")
    make_pack: bool = Field(default=True, description="装完自动建成扩展包")
    as_default: bool = Field(default=False, description="把包设为“新建环境默认带上”")


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
class ProxyImportBody(BaseModel):
    """批量导入代理。一行一个，支持 socks5/http 与 host:port:user:pass 等形态。"""

    text: str = Field(min_length=1, max_length=200_000)
    label_prefix: str = ""


class ProxyTestBody(BaseModel):
    ids: list[int] | None = None
    raw: str = ""
    timeout: float = 15.0


class ProfileProxyBody(BaseModel):
    """给某个环境设置/更换代理。

    `proxy_id` 从代理库选；`raw` 直接给串（会自动入库）；两者都空 = 清除代理。
    """

    proxy_id: int | None = None
    raw: str = ""


class PackImportBody(BaseModel):
    """导入扩展包。三种来源（见 services/packs.import_pack）：

    - `from_profile`：从已有的知纹环境导出
    - `from_chrome`：从任意 Chrome/Roxy 的用户数据目录导出（给"把 RXYCDP 的插件带走"用）
    - `path`：扩展目录或 zip
    """

    path: str = ""
    from_profile: str = ""
    from_chrome: str = ""
    name: str = ""
    # 导入后是否顺手设为"新建环境默认带上"。
    #
    # 这个字段以前**只加在了安装 .crx 的模型上**，导入扩展包的模型里没有 ——
    # 于是界面/接口传 `as_default: true` 会被 pydantic 直接忽略（不报错！），
    # 返回的 `is_default` 仍是 false，用户以为开关坏了。
    # 真机复现：POST /packs/import {"as_default": true} → is_default: false。
    as_default: bool = False


class PackDefaultBody(BaseModel):
    enabled: bool = True


class ProfilePacksBody(BaseModel):
    pack_ids: list[str] | None = None


class ShellExecBody(BaseModel):
    """本地执行一条命令。

    注意：这个模型必须在模块层定义。`api.py` 用了
    `from __future__ import annotations`，把模型放进 `build_app()` 内部会让
    类型注解变成字符串而无法解析，FastAPI 会报 422（踩过两次）。
    """

    command: str = Field(min_length=1, max_length=4000)
    cwd: str = ""
    timeout: int | float | None = None


class ShellToggleBody(BaseModel):
    enabled: bool


def _sync_proxies_async(reason: str) -> None:
    """把代理库同步到服务器（后台线程，绝不阻塞也不报错给调用方）。

    用户要求「用户机上添加过的代理，都会自动同步到服务器里面」——
    所以导入/新增代理之后自动推一次。同步失败只记日志：
    代理是本机自己的东西，服务器暂时连不上不该影响本地使用。
    """
    try:
        from .services import proxysync

        def _run() -> None:
            try:
                proxysync.push()
            except Exception:  # noqa: BLE001
                logger.debug("代理库上行失败（%s）", reason, exc_info=True)

        threading.Thread(target=_run, name="zw-proxysync", daemon=True).start()
    except Exception:  # noqa: BLE001
        logger.debug("启动代理同步线程失败", exc_info=True)


def build_app() -> FastAPI:
    app = FastAPI(
        title="知纹核心（zw-core）",
        version=__version__,
        description="指纹浏览器机群的编排层、CDP 网关与同步客户端。所有响应统一为 {ok, data} / {ok, code, message, hint}。",
    )

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _err(exc.code, exc.message, exc.status, exc.hint)

    @app.exception_handler(enginesrc.EngineSourceError)
    async def _engine_src_error(request: Request, exc: enginesrc.EngineSourceError):
        """内核下载/校验/解包失败。

        这类错误带 `hint`（比如"设 ZW_DOWNLOAD_PROXY"、"装 libnss3"），必须原样带给用户，
        否则界面上只剩一句"500 Internal Server Error"。
        """
        return JSONResponse(
            status_code=int(getattr(exc, "status", 500) or 500),
            content={"ok": False, "code": exc.code, "message": exc.message,
                     "hint": getattr(exc, "hint", "") or ""},
        )

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

    # ── 下面这几个以前**没接住**，会冒成 500 + 完整 traceback ────────────
    #
    # 用户/AI 拿到的是原始堆栈而不是 `{ok:false, code, message}`，
    # 既看不懂也没法按错误码处理。是全量扫清单端点时发现的：
    # `POST /packs/import` 参数不对 → 日志里出现 "未处理异常" + 堆栈。
    #
    # 这几个都是业务错误（参数不对 / 找不到），本来就该是 4xx + 可读原因。
    @app.exception_handler(packs.PackError)
    async def _pack_error(_: Request, exc: packs.PackError) -> JSONResponse:
        return _err(exc.code, exc.message, status=getattr(exc, "status", 400) or 400)

    @app.exception_handler(extstore.StoreError)
    async def _store_error(_: Request, exc: extstore.StoreError) -> JSONResponse:
        return _err(exc.code, exc.message,
                    status=getattr(exc, "status", 400) or 400,
                    hint="商店装扩展需要这台机器能访问 clients2.google.com；"
                         "国内网络请先给客户端配好代理")

    @app.exception_handler(shellsvc.ShellError)
    async def _shell_error(_: Request, exc: shellsvc.ShellError) -> JSONResponse:
        return _err(exc.code, exc.message, status=getattr(exc, "status", 400) or 400)

    @app.exception_handler(sessionsvc.SessionError)
    async def _session_error(_: Request, exc: sessionsvc.SessionError) -> JSONResponse:
        return _err(exc.code, exc.message, status=getattr(exc, "status", 400) or 400)

    @app.exception_handler(relaysvc.RelayError)
    async def _relay_error(_: Request, exc: relaysvc.RelayError) -> JSONResponse:
        return _err(exc.code, exc.message, status=getattr(exc, "status", 502) or 502,
                    hint="本地代理中继起不来时，带认证或链式代理会连不上")

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
    def meta(request: Request) -> dict[str, Any]:
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
                # **按这次请求算**，不是"配没配令牌"。
                #
                # 原来写的是 `bool(s.admin_token)`：配了令牌的机器，即使从本机
                # 回环访问（已经被放行、接口返回 200），页面也会因为
                # auth_required=true 而弹出登录框 —— 正是用户抱怨的那个现象。
                # 页面只需要知道"这一次要不要我输令牌"，所以这里直接问
                # 本请求是否来自回环。
                "auth_required": not is_loopback_request(request),
                # 绑定地址被降级时的原因（例如设了 0.0.0.0 但没有凭据）。
                # 以前这个字段只在 config 里被赋值，没有任何出口，
                # 用户只能自己猜"为什么局域网连不上"。
                "host_notice": getattr(s, "host_notice", ""),
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
                        {"method": "GET", "path": f"{API_PREFIX}/shell/status", "说明": "命令行开关与工作目录"},
                        {"method": "POST", "path": f"{API_PREFIX}/shell/exec", "说明": "本地执行一条命令（不受远程开关限制）"},
                        {"method": "POST", "path": f"{API_PREFIX}/shell/enable", "说明": "开关远程命令行（服务器能否执行命令）"},
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
                        {"method": "GET", "path": f"{API_PREFIX}/proxies", "说明": "代理库"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies/import", "说明": "批量导入（一行一个，socks5/http 带认证都支持）"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies/test", "说明": "测一个代理的出口 IP"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies/test-many", "说明": "并发测一批"},
                        {"method": "PUT", "path": f"{API_PREFIX}/profiles/{{ref}}/proxy", "说明": "给环境设置/更换/清除代理（运行中需重启生效）"},
                        {"method": "GET", "path": f"{API_PREFIX}/packs", "说明": "扩展包列表"},
                        {"method": "POST", "path": f"{API_PREFIX}/packs/import", "说明": "导入扩展包（可从一个已有环境导出，含扩展数据）"},
                        {"method": "POST", "path": f"{API_PREFIX}/packs/{{id}}/default", "说明": "设为新建环境默认带上"},
                        {"method": "POST", "path": f"{API_PREFIX}/profiles/{{ref}}/packs/apply", "说明": "把扩展包应用到环境"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies", "说明": "加代理"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies/check", "说明": "不保存直接测代理出口"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies/{{id}}/check", "说明": "测代理出口"},
                        {"method": "PATCH", "path": f"{API_PREFIX}/proxies/{{id}}", "说明": "改代理"},
                        {"method": "DELETE", "path": f"{API_PREFIX}/proxies/{{id}}", "说明": "删代理"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies/{{id}}/hide", "说明": "隐藏/取消隐藏代理（不删除，不影响在用它的环境）"},
                        {"method": "POST", "path": f"{API_PREFIX}/extensions/install", "说明": "装扩展（商店链接 / .crx / 目录 / zip，可自动建包并设为默认）"},
                        {"method": "GET", "path": f"{API_PREFIX}/extensions/updates", "说明": "查已装扩展有无新版（只查带商店 ID 标记的）"},
                        {"method": "POST", "path": f"{API_PREFIX}/proxies/sync", "说明": "代理库与服务器双向对齐"},
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
                        # 注意：这两条不是 f-string，所以写单花括号。
                        # 之前照抄 f-string 的写法写了 {{id}}，渲染出来就是字面量双花括号，
                        # AI 照着拼路径会 404（tests/test_docs_consistency.py 会拦住这种错）。
                        {"method": "GET", "path": "/cdp/{id}/json/version", "说明": "CDP 发现接口（地址已重写）"},
                        {"method": "GET", "path": "/cdp/{id}/json/list", "说明": "CDP 目标列表（地址已重写）"},
                        {"method": "PUT", "path": "/cdp/{id}/json/new", "说明": "新建标签页"},
                        {"method": "WS", "path": "/cdp/{id}/...", "说明": "CDP 主通道"},
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

    # ══ 远程命令行 ════════════════════════════════════════
    @app.get(f"{API_PREFIX}/shell/status", tags=["命令行"], dependencies=[Auth], summary="命令行状态")
    def shell_status() -> dict[str, Any]:
        """当前开关、工作目录、超时，以及远程代理的运行状态。"""
        from zw_core.services import shellagent
        from zw_core.services import shell as shellsvc

        s = get_settings()
        agent = shellagent.get_agent()
        return ok({
            "enabled": bool(s.shell_enabled),
            "cwd": shellsvc.default_cwd(),
            "timeout": int(s.shell_timeout),
            "max_output": int(s.shell_max_output),
            "platform_shell": shellsvc.shell_hint(),
            "device_id": getattr(s, "device_id", "") or "",
            "hub_url": s.hub_url or "",
            "agent": agent.status() if agent else None,
        })

    @app.post(f"{API_PREFIX}/shell/exec", tags=["命令行"], dependencies=[Auth, Cidr], summary="本地执行一条命令")
    def shell_exec(body: ShellExecBody) -> dict[str, Any]:
        """本地执行命令，并返回 rc/stdout/stderr。

        与"远程命令行"是两件事：本地执行**不受远程开关影响**（那是给
        服务器用的闸门），但同样走超时、输出上限与审计日志。
        返回值里带 `local: true` 标明这是本地调用。
        """
        from zw_core.services import shell as shellsvc

        res = shellsvc.execute(body.command, cwd=body.cwd, timeout=body.timeout)
        data = res.to_dict()
        data["local"] = True
        return ok(data)

    @app.post(f"{API_PREFIX}/shell/enable", tags=["命令行"], dependencies=[Auth], summary="开关远程命令行")
    def shell_enable(body: ShellToggleBody) -> dict[str, Any]:
        """运行时切换"是否允许服务器远程执行命令"。

        两件事同时做：
        1. 改内存里的 Settings（本地接口立刻能反映）
        2. 把选择写进客户端配置文件（重启后仍然生效）
        3. 让后台代理立即停/start（开关关掉时根本不去 hub 拉命令）
        """
        from zw_core.services import shellagent

        s = get_settings()
        s.shell_enabled = bool(body.enabled)
        persisted = False
        try:
            from zw_core import client as clientmod

            cfg = clientmod.load_config()
            cfg["shell_enabled"] = bool(body.enabled)
            clientmod.save_config(cfg)
            persisted = True
        except Exception:
            # 源码直接跑 core（没有客户端配置）时不算错误
            persisted = False

        agent = shellagent.get_agent()
        if agent is not None:
            agent.set_enabled(bool(body.enabled))

        # 立刻上报一次，别让面板等最多 60 秒才知道开关变了
        hb = shellagent.get_heartbeat()
        if hb is not None:
            hb.enabled = bool(body.enabled)
            hb.send_now()

        return ok({
            "enabled": bool(body.enabled),
            "persisted": persisted,
            "agent_running": bool(agent and agent.status()["running"]),
        })

    @app.get(f"{API_PREFIX}/groups", tags=["环境"], dependencies=[Auth], summary="分组概览")
    def groups_list() -> dict[str, Any]:
        return ok({"groups": mgr.groups()})

    # ══ 内核（ungoogled-chromium）═════════════════════════
    @app.get(f"{API_PREFIX}/engine", tags=["内核"], dependencies=[Auth],
             summary="当前内核状态与下载进度")
    def engine_status() -> dict[str, Any]:
        from zw_core.services import engines as engsvc
        from zw_core.services import enginesrc

        s = get_settings()
        try:
            bin_path = engsvc.find_chrome(s)
            path, err = str(bin_path), ""
        except Exception as exc:  # noqa: BLE001 —— 没内核不是接口错误，照实返回
            path, err = "", f"{getattr(exc, 'code', 'ENGINE_NOT_FOUND')}: {getattr(exc, 'message', exc)}"
        loadable: bool | None = None
        if path:
            try:
                from zw_core.services import extprobe

                loadable = bool(extprobe.probe(path, timeout=25.0).get("loadable"))
            except Exception:  # noqa: BLE001 —— 探不起责任在环境，不影响状态展示
                loadable = None
        return ok({
            "policy": s.engine_policy,
            "auto_install": bool(s.engine_auto_install),
            "engine_dir": str(s.engine_dir),
            "path": path,
            "version": engsvc.engine_version(bin_path) if path else "",
            "extensions_loadable": loadable,
            "error": err,
            "download": enginesrc.progress(),
        })

    @app.post(f"{API_PREFIX}/engine/install", tags=["内核"], dependencies=[Auth],
              summary="下载并安装 ungoogled-chromium（后台进行）")
    def engine_install(force: bool = False) -> dict[str, Any]:
        from zw_core.services import enginesrc

        return ok(enginesrc.start_background(force=force))

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

    @app.post(f"{API_PREFIX}/extensions/install", tags=["扩展"], dependencies=[Auth],
              summary="安装扩展（商店链接 / .crx / 扩展目录 / zip）")
    def extensions_install(body: ExtensionInstall) -> dict[str, Any]:
        """一个入口，四种来源。

        ## 为什么合并成一个路由（踩过的坑）

        曾经这里有**两个**同路径同方法的装饰器：一个是"从目录/zip 装进中央库"
        （老功能），另一个是我新加的"从商店链接装"。FastAPI 用先注册的那个，
        所以**新的一直没生效** —— 表现为带上 `url` 调过去报
        `body.path Field required`，而 `make_pack`/`as_default` 被完全忽略。
        本地单测直接调 `extstore.install()` 绕过了 HTTP，因此没发现。

        现在按来源分流，两条路都走这一个入口：
          - `url` 非空 → 商店链接/ID（下载 CRX）
          - `path` 是 `.crx` → 本地 CRX 文件
          - `path` 是目录或 `.zip` → 老行为，装进中央库（顺便也能建包）
        """
        s = get_settings()
        src = Path(body.path).expanduser() if body.path else None

        # ① 商店链接 / 本地 .crx → 交给 extstore（它负责 CRX 去头、建包、设默认）
        looks_like_crx = src is not None and src.suffix.lower() == ".crx"
        if (body.url or "").strip() or looks_like_crx:
            from zw_core.services import extstore as _es

            try:
                return ok(_es.install(
                    url=(body.url or "").strip(),
                    crx_path=str(src) if looks_like_crx else "",
                    make_pack=getattr(body, "make_pack", True),
                    as_default=getattr(body, "as_default", False),
                ))
            except _es.StoreError as exc:
                raise ApiError(exc.code, exc.message, status=exc.status) from exc

        if src is None:
            raise ApiError("PATH_NOT_FOUND",
                           "要么给 url（商店链接/扩展 ID），要么给 path（.crx / 扩展目录 / zip）")
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
        """代理库列表。

        ## 必须走 proxylib.list_all()，别在这里自己拼

        这里原来有一段内联实现：直接 SELECT *、pop 掉 password、手工拼 upstream。
        结果它**漏掉了 `is_chain` / `chain`**（链式代理的前置信息），
        界面上就画不出"前置 → 落地"；而且还拿一个根本不存在的
        `proxy_pw_<id>` 设置项去判断有没有密码。
        `_row_to_public()` 才是唯一该维护字段形状的地方。
        """
        items = proxylib.list_all()
        return ok({"proxies": items, "total": len(items)})

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

    # ══ 代理库：批量导入 / 批量检测 ════════════════════════════
    # ── 从 Chrome 应用商店装扩展（用户：输入商店链接就能添加）────────
    @app.get(f"{API_PREFIX}/extensions/updates", tags=["扩展"], dependencies=[Auth],
             summary="检查已装扩展有没有新版")
    def extensions_updates() -> dict[str, Any]:
        """回商店比对版本。只查带 ID 标记的（手工导入的包没有 ID，跳过不瞎猜）。"""
        from zw_core.services import extstore

        items = extstore.check_updates()
        return ok({"items": items,
                   "updatable": [i for i in items if i.get("update_available")]})

    @app.post(f"{API_PREFIX}/proxies/sync", tags=["代理"], dependencies=[Auth],
              summary="代理库与服务器同步（上行 + 下行）")
    def proxies_sync() -> dict[str, Any]:
        """手动触发一次往返同步。

        平时是自动的（导入后上行、core 启动时下行）。这个接口给界面上的
        "立即同步"按钮和排错用 —— 用户要求两边都能加、保持一致的库。
        """
        from zw_core.services import proxysync

        return ok(proxysync.sync())

    @app.get(f"{API_PREFIX}/proxies/sync", tags=["代理"], dependencies=[Auth],
             summary="查看上次同步结果")
    def proxies_sync_status() -> dict[str, Any]:
        from zw_core.services import proxysync

        return ok(proxysync.last_result())

    # 顺序敏感：`/proxies/import` 与 `/proxies/test-many` 都是固定路径，
    # 必须声明在 `/proxies/{proxy_id}` 之前（否则会被当成 id="import" 解析失败）。
    @app.post(f"{API_PREFIX}/proxies/import", tags=["代理"], dependencies=[Auth], summary="批量导入代理")
    def proxies_import(body: ProxyImportBody) -> dict[str, Any]:
        """一行一个代理，返回导入/跳过/失败明细。

        支持的形态（见 services/proxylib.parse_line）：
        `socks5://u:p@h:port`、`http://u:p@h:port`、`h:port:u:p`、
        `u:p@h:port`、`h:port`，行尾可用 `# 备注`。
        """
        from zw_core.services import proxylib

        # import_batch 自己已经返回 {"ok":true,"data":{...}}，
        # 再包一层 ok() 会变成 {"ok":true,"data":{"ok":true,...}}，前端取不到 imported。
        res = proxylib.import_batch(body.text, body.label_prefix)
        _sync_proxies_async("import")
        return res

    @app.post(f"{API_PREFIX}/proxies/test-many", tags=["代理"], dependencies=[Auth, Cidr], summary="批量检测代理出口")
    def proxies_test_many(body: ProxyTestBody) -> dict[str, Any]:
        """并发检测（默认 4 并发），结果写回库里。"""
        from zw_core.services import proxylib

        return ok(proxylib.test_many(body.ids or [], concurrency=4, timeout=body.timeout))

    @app.post(f"{API_PREFIX}/proxies/test", tags=["代理"], dependencies=[Auth, Cidr], summary="测试单个代理")
    def proxies_test(body: ProxyTestBody) -> dict[str, Any]:
        """测库内一条（给 id）或测一条临时串（给 raw）。"""
        from zw_core.services import proxylib

        return ok(proxylib.test_one(body.ids[0] if body.ids else None,
                                    raw=body.raw, timeout=body.timeout))

    @app.put(f"{API_PREFIX}/profiles/{{ref}}/proxy", tags=["代理"], dependencies=[Auth], summary="给环境设置/更换代理")
    def profile_set_proxy(ref: str, body: ProfileProxyBody) -> dict[str, Any]:
        """创建之后随时可以改代理。

        运行中的环境**不允许热改**（Chromium 的 proxy 是启动期参数），
        返回 `restart_required=true`，请先 stop 再 start。
        """
        from zw_core.services import proxylib

        res = proxylib.assign_to_profile(ref, proxy_id=body.proxy_id, raw=body.raw)
        if res.get("ok") and res.get("created"):
            # 只有"顺带入了一条新的"才需要上行，避免无意义请求
            _sync_proxies_async("assign")
        return ok(res)

    # ══ 扩展包 ════════════════════════════════════════════
    @app.get(f"{API_PREFIX}/packs", tags=["扩展"], dependencies=[Auth], summary="扩展包列表")
    def packs_list() -> dict[str, Any]:
        from zw_core.services import packs

        return ok({"packs": packs.list_packs()})

    @app.post(f"{API_PREFIX}/packs/import", tags=["扩展"], dependencies=[Auth], summary="导入扩展包")
    def packs_import(body: PackImportBody) -> dict[str, Any]:
        """从目录/zip 导入，或从某个已有环境导出（`from_profile`）。

        用户要的"把 RXYCDP 浏览器里的插件及其数据一起打包"就是 `from_profile`。
        """
        from zw_core.services import packs

        res = packs.import_pack(body.path, name=body.name,
                                from_profile=body.from_profile,
                                from_chrome=body.from_chrome)
        # `as_default` 以前定义了却**没往下传** —— 界面上勾了「设为默认」，
        # 返回的 `is_default` 仍是 false，用户会以为开关坏了。
        # （真机复现过：POST /packs/import {"as_default": true} → is_default: false）
        if body.as_default and res.get("id"):
            try:
                packs.set_default(str(res["id"]), True)
                res["is_default"] = True
            except Exception as exc:  # noqa: BLE001 —— 设默认失败不该让导入失败
                res["is_default"] = False
                res["default_warning"] = f"包已导入，但设为默认失败：{type(exc).__name__}: {exc}"
        return ok(res)

    @app.get(f"{API_PREFIX}/packs/scan-browsers", tags=["扩展"], dependencies=[Auth],
             summary="扫本机可导入的浏览器用户数据目录")
    def packs_scan_browsers() -> dict[str, Any]:
        """列出本机"有扩展可导入"的 Chromium 系浏览器配置目录。

        存在的理由：导入扩展包需要用户填 `User Data` 的绝对路径，而 RXYCDP /
        Roxy 这类浏览器装在哪、Profile 叫什么，用户根本不知道 —— 卡在这一步
        就放弃了。这里把候选扫出来，界面上点一下就行。
        """
        return ok({"candidates": packs.scan_browser_profiles()})

    @app.post(f"{API_PREFIX}/packs/{{pack_id}}/default", tags=["扩展"], dependencies=[Auth], summary="设为新建环境默认带上")
    def packs_set_default(pack_id: str, body: PackDefaultBody) -> dict[str, Any]:
        from zw_core.services import packs

        return ok(packs.set_default(pack_id, body.enabled))

    @app.delete(f"{API_PREFIX}/packs/{{pack_id}}", tags=["扩展"], dependencies=[Auth], summary="删除扩展包")
    def packs_delete(pack_id: str) -> dict[str, Any]:
        from zw_core.services import packs

        return ok(packs.delete_pack(pack_id))

    @app.post(f"{API_PREFIX}/profiles/{{ref}}/packs/apply", tags=["扩展"], dependencies=[Auth], summary="把扩展包应用到某个环境")
    def profile_apply_packs(ref: str, body: ProfilePacksBody) -> dict[str, Any]:
        from zw_core.services import packs

        return ok(packs.apply_to_profile(ref, body.pack_ids))

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
        urls = b.urls
        if urls is None:
            # 没显式指定就按"恢复上次标签 + 追加检测页"来（用户要求）。
            # 显式传了 urls 就尊重调用方（脚本/AI 想打开指定页面时不该被塞检测页）。
            try:
                from zw_core.services import launcher

                prof = mgr.get_profile(ref)
                urls = launcher.compose_start_urls(prof)
            except Exception:
                urls = None
        return ok(mgr.start(ref, urls=urls, wait=b.wait))

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

    # ══ 实时终端（本机 WebSocket）══════════════════════════════
    # 顺序敏感：必须放在 /cdp/{profile_id} 之前声明吗？不需要 —— 路径完全不同。
    # 但必须放在 `return app` 之前。
    @app.websocket(f"{API_PREFIX}/term")
    async def term_ws(websocket: WebSocket) -> None:
        """本机实时终端。

        浏览器控制台"终端"面板连这里（同一台机器，零延迟）。
        远程要看别人的终端，走 hub 的 `/v1/console/term/{device_id}`，
        由 hub 中继到那台设备的出站连接 —— 不需要对方开端口。

        鉴权与 HTTP 一致：回环直通；非回环要机器码或令牌（query 参数传）。
        """
        import asyncio
        import json as _json

        s = get_settings()
        token = websocket.query_params.get("token", "")
        host = websocket.client.host if websocket.client else ""
        loopback = host in LOOPBACK_HOSTS or host.startswith("127.")
        if not loopback:
            ok_tok = False
            for cand in (s.admin_token, s.device_id):
                if cand and token and hmac.compare_digest(token, cand):
                    ok_tok = True
                    break
            if not ok_tok:
                await websocket.accept()
                await websocket.close(code=1008, reason="unauthenticated")
                return
        if not s.shell_enabled:
            await websocket.accept()
            await websocket.send_text(_json.dumps(
                {"t": "err", "code": "SHELL_DISABLED",
                 "message": "远程命令行被关掉了（客户端界面里可以打开）"}, ensure_ascii=False))
            await websocket.close(code=1000)
            return

        await websocket.accept()
        from zw_core.services import session as sessmod

        registry = _local_sessions()
        send_lock = asyncio.Lock()
        loop = asyncio.get_running_loop()

        async def push(payload: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_text(_json.dumps(payload, ensure_ascii=False))

        def make_cbs(sid: str):
            def on_output(data: str) -> None:
                if data:
                    loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(push({"t": "out", "sid": sid, "data": data})))

            def on_done(rc: int, cwd: str, timed_out: bool = False) -> None:
                sess = registry.get(sid)
                loop.call_soon_threadsafe(lambda: asyncio.create_task(push({
                    "t": "done", "sid": sid, "rc": rc, "cwd": cwd,
                    "truncated": bool(sess.truncated) if sess else False,
                    "timed_out": bool(timed_out),
                })))

            def on_exit(rc: int) -> None:
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(push({"t": "exit", "sid": sid, "rc": rc})))

            return on_output, on_done, on_exit

        await push({"t": "hello", "host": host, "shell_enabled": bool(s.shell_enabled)})
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = _json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = str(msg.get("t") or "")
                sid = str(msg.get("sid") or "local")
                if kind == "ping":
                    await push({"t": "pong"})
                    continue
                if kind == "open":
                    out_cb, done_cb, exit_cb = make_cbs(sid)
                    try:
                        sess = registry.create(sid, cwd=str(msg.get("cwd") or ""),
                                               timeout=float(msg.get("timeout") or 0),
                                               on_output=out_cb, on_done=done_cb, on_exit=exit_cb)
                        await push({"t": "opened", "sid": sid, "cwd": sess.cwd})
                    except sessmod.SessionError as exc:
                        await push({"t": "err", "sid": sid, "code": exc.code, "message": exc.message})
                    continue
                if kind == "run":
                    sess = registry.get(sid)
                    if sess is None or not sess.alive():
                        out_cb, done_cb, exit_cb = make_cbs(sid)
                        try:
                            sess = registry.create(sid, cwd=str(msg.get("cwd") or ""),
                                                   timeout=float(msg.get("timeout") or 0),
                                                   on_output=out_cb, on_done=done_cb, on_exit=exit_cb)
                        except sessmod.SessionError as exc:
                            await push({"t": "err", "sid": sid, "code": exc.code, "message": exc.message})
                            continue
                    try:
                        sess.run(str(msg.get("command") or ""), timeout=msg.get("timeout"))
                    except sessmod.SessionError as exc:
                        await push({"t": "err", "sid": sid, "code": exc.code, "message": exc.message})
                    continue
                if kind == "input":
                    sess = registry.get(sid)
                    if sess is not None:
                        try:
                            sess.write(str(msg.get("data") or ""))
                        except sessmod.SessionError as exc:
                            await push({"t": "err", "sid": sid, "code": exc.code, "message": exc.message})
                    continue
                if kind == "signal":
                    sess = registry.get(sid)
                    if sess is not None and str(msg.get("kind") or "") == "interrupt":
                        sess.interrupt()
                    continue
                if kind == "close":
                    registry.drop(sid)
                    continue
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)

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


_LOCAL_SESSIONS = None


def _local_sessions():
    """本机实时终端共用的会话仓库（进程内单例）。

    刻意不按 WebSocket 连接隔离：浏览器刷新标签页会新建 WS，
    但用户期望"刷新后终端还接着刚才那次会话"（cwd、历史都在）。
    """
    global _LOCAL_SESSIONS
    if _LOCAL_SESSIONS is None:
        from zw_core.services.session import SessionRegistry

        _LOCAL_SESSIONS = SessionRegistry()
    return _LOCAL_SESSIONS

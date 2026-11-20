"""程序入口。

`python -m zw_core` / `zw serve` 启动 HTTP 服务；后台同时跑健康巡检。

## 0.2.0 修的一处安全问题

`/mcp` 以前**完全不鉴权**：任何能访问 8730 端口的人都能通过 MCP 直接
读写 cookie、截图、甚至开浏览器。现在它和 REST 端点走同一套 Bearer 令牌，
并同样受网段白名单约束。

MCP 客户端（Claude Desktop / Codex 等）配置时把令牌写进 header 即可：
```json
{"mcpServers": {"zhiwen": {"url": "http://<host>:8730/mcp",
  "headers": {"Authorization": "Bearer <ZW_ADMIN_TOKEN>"}}}}
```
如果客户端不支持自定义 header（有些只支持 command），用 stdio 方式 `zw mcp`
——它在本机进程内直接跑，不需要令牌也没有网络暴露面。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from . import __version__
from .api import build_app, cidr_guard, require_auth
from .config import get_settings
from .logging_conf import setup_logging
from .mcp_server import handle as mcp_handle
from .services import manager as mgr

logger = logging.getLogger("zw.main")


async def _health_loop() -> None:
    """周期性巡检：发现崩溃的环境并按策略重启。"""
    s = get_settings()
    interval = max(5, s.health_interval)
    while True:
        await asyncio.sleep(interval)
        try:
            # health_tick 里有阻塞调用，丢到线程池，别卡住事件循环
            events = await asyncio.to_thread(mgr.health_tick)
            for ev in events:
                logger.warning("巡检事件：%s", json.dumps(ev, ensure_ascii=False))
            purged = await asyncio.to_thread(mgr.purge_expired_tokens)
            if purged:
                logger.info("清理过期 CDP 令牌 %s 个", purged)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 巡检永远不能把服务带崩
            logger.error("健康巡检异常：%s", exc)


@contextlib.asynccontextmanager
async def _lifespan(app: Any):
    """启动 / 关闭钩子（FastAPI 新版推荐写法，不用已废弃的 on_event）。"""
    s = get_settings()
    logger.info("知纹核心启动 node=%s root=%s port=%s", s.node_id, s.root, s.port)
    try:
        rebuilt = await asyncio.to_thread(mgr.rebuild_gateway_routes)
        if rebuilt["restored"] or rebuilt["crashed"]:
            logger.info("启动时重建网关路由：恢复=%s 崩溃=%s", rebuilt["restored"], rebuilt["crashed"])
    except Exception as exc:
        logger.error("重建网关路由失败：%s", exc)

    eng = mgr._engine_summary()
    if not eng["ok"]:
        logger.warning("没有找到浏览器内核：%s", eng.get("error", ""))

    # 代理库与服务器对齐（用户要求：两边都能加，"都是一样的"）。
    # 放后台线程：服务器暂时连不上绝不能拖慢 core 启动。
    try:
        import threading as _threading

        from zw_core.services import proxysync as _proxysync

        def _sync_bg() -> None:
            try:
                _proxysync.sync()
            except Exception:  # noqa: BLE001
                logger.debug("启动时代理库同步失败", exc_info=True)

        if s.hub_url and getattr(s, "device_id", ""):
            _threading.Thread(target=_sync_bg, name="zw-proxysync-boot", daemon=True).start()
    except Exception:  # noqa: BLE001
        logger.debug("代理库同步启动失败", exc_info=True)

    # 远程命令行代理：只在有 hub 地址 + 设备号时启动。
    # 纯出站长轮询，设备不新开监听端口。
    try:
        from zw_core.services import shellagent

        # 先注册成设备：服务器上直接跑的 core 没有客户端帮它注册，
        # 不补这一步面板里根本看不到它（心跳会 403 DEVICE_UNKNOWN）。
        dev_id = getattr(s, "device_id", "") or ""
        if s.hub_url and dev_id:
            reg = shellagent.ensure_registered(s.hub_url, dev_id, s.hub_token)
            if reg.get("ok"):
                logger.info("已在同步中心注册为设备 %s", dev_id[:24])
            else:
                logger.warning("设备注册失败（%s）：%s", reg.get("code", ""), reg.get("error", ""))

        # 无客户端时的独立心跳：否则注册后 3 分钟就被判离线，
        # 面板里一直是灰的，也没人上报 shell_enabled。
        hb = shellagent.start_heartbeat(
            hub_url=s.hub_url,
            device_id=dev_id,
            token=s.hub_token,
            enabled=bool(s.shell_enabled),
        )
        if hb is not None:
            logger.info("已启动独立心跳（每 %s 秒）", hb.interval)

        # 实时终端：一条到 hub 的持久 WS（设备侧出站，不开监听端口）
        try:
            from zw_core.services import termhub

            tc = termhub.start_client(
                hub_url=s.hub_url,
                device_id=dev_id,
                token=s.hub_token,
                shell_enabled=bool(s.shell_enabled),
            )
            if tc is not None:
                logger.info("实时终端通道已启动（连 %s）", s.hub_url)
        except Exception as exc:  # noqa: BLE001  终端坏了不能拖垮整个 core
            logger.error("实时终端通道启动失败：%s", exc)

        agent = shellagent.start_agent(
            hub_url=s.hub_url,
            device_id=getattr(s, "device_id", "") or "",
            token=s.hub_token,
            enabled=bool(s.shell_enabled),
        )
        if agent is not None:
            logger.info("远程命令行已启用（设备可在客户端里关闭）")
        elif s.shell_enabled:
            logger.debug("跳过程序化远程命令行：没有 hub 地址或设备号")
    except Exception as exc:  # noqa: BLE001  命令行代理坏了不能拖垮整个服务
        logger.error("远程命令行代理启动失败：%s", exc)

    app.state.health_task = asyncio.create_task(_health_loop())
    started = time.time()
    try:
        yield
    finally:
        try:
            from zw_core.services import termhub

            termhub.stop_client()
        except Exception:
            pass
        try:
            from zw_core.services import shellagent

            shellagent.stop_agent()
        except Exception:
            pass
        task = getattr(app.state, "health_task", None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("知纹核心停止（运行 %.0f 秒）", time.time() - started)


def _mcp_authorized(request: Request) -> JSONResponse | None:
    """MCP 与 REST 共用同一套鉴权 + 网段白名单。失败时返回可直接吐给客户端的响应。"""
    try:
        require_auth(
            request,
            authorization=request.headers.get("authorization", ""),
            x_zw_token=request.headers.get("x-zw-token", ""),
        )
        cidr_guard(request)
    except Exception as exc:
        from .api import ApiError

        if isinstance(exc, ApiError):
            return JSONResponse(
                status_code=exc.status,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": f"[{exc.code}] {exc.message}"}},
                headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
            )
        raise
    return None


def create_app() -> Any:
    setup_logging()
    app = build_app()
    app.router.lifespan_context = _lifespan

    # ── MCP over HTTP ────────────────────────────────────
    @app.post("/mcp", include_in_schema=False)
    async def mcp_http(request: Request) -> JSONResponse:
        denied = _mcp_authorized(request)
        if denied is not None:
            return denied
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "不是合法 JSON"}},
            )
        if isinstance(payload, list):
            out = [r for r in (mcp_handle(p) for p in payload) if r is not None]
            return JSONResponse(content=out)
        # 工具调用里会跑 CDP / 文件 IO，丢线程池，别阻塞事件循环
        resp = await asyncio.to_thread(mcp_handle, payload)
        if resp is None:
            return JSONResponse(content={"jsonrpc": "2.0", "id": None, "result": {}})
        return JSONResponse(content=resp)

    @app.get("/mcp", include_in_schema=False)
    def mcp_info(request: Request) -> Any:
        denied = _mcp_authorized(request)
        if denied is not None:
            return denied
        from .mcp_server import TOOLS

        return {
            "ok": True,
            "data": {
                "说明": "MCP over HTTP。用 POST 发 JSON-RPC 2.0（initialize / tools/list / tools/call），需要 Bearer 令牌。",
                "工具数": len(TOOLS),
                "工具": [{"name": n, "description": d} for n, (d, _s, _f) in TOOLS.items()],
            },
        }

    return app


app = create_app()


def run() -> None:
    import uvicorn

    s = get_settings()
    # 降级提示必须在**启动时打出来**。
    #
    # 真踩过：配置层在"绑 0.0.0.0 但既没有 ZW_ADMIN_TOKEN 也没有 ZW_DEVICE_ID"
    # 时会静默降级成只监听 127.0.0.1（安全上是对的），并把原因写进
    # `s.host_notice` —— 但**全项目没人读它**。
    # 于是用户设了 ZW_HOST=0.0.0.0，实际只能本机访问，日志一个字都不说，
    # 只能自己去猜为什么局域网连不上。这里把它打出来（用 WARNING，
    # 因为对用户来说这几乎总是"不是我想要的结果"）。
    if getattr(s, "host_notice", ""):
        logger.warning("%s", s.host_notice)
    logger.info("监听 http://%s:%s  数据目录 %s", s.host, s.port, s.root)

    # 内核准备：policy=ungoogled 且没有 ungoogled 内核时，后台开始下载。
    #
    # 为什么放在启动而不是"用户点启动环境时"：下载 150–200MB 要几分钟，
    # 放到启动时就等于"开客户端之后自己去泡杯茶，回来就能用"。
    # 这里**绝不阻塞启动**，也**绝不因为失败就退出** —— 没内核是能稍后重试的状态，
    # 把整个客户端带下去只会更糟。进度看 GET /api/v1/engine 或 `zw engine status`。
    try:
        from .services import engines as _engines

        info = _engines.prepare_engine_async(s)
        if info.get("started"):
            logger.info("正在后台下载 ungoogled-chromium 内核（进度见 /api/v1/engine）")
        elif info.get("reason"):
            logger.debug("内核准备跳过：%s", info["reason"])
    except Exception:  # noqa: BLE001
        logger.warning("内核准备检查失败（不影响启动）", exc_info=True)
    # log_config=None：不让 uvicorn 用 dictConfig 覆盖我们的日志配置
    # （它的 formatter 会碰 sys.stdout，而无控制台环境下那是 None）。
    # `proxy_headers=False` 是**安全必需**，不是可选优化。
    #
    # uvicorn 默认 `proxy_headers=True` 且 `forwarded_allow_ips="127.0.0.1,::1"`：
    # 只要连过来的是本机地址（例如 nginx / Lucky 反代到 127.0.0.1:8730），
    # 它就会把 `request.client.host` 换成请求头里的 `X-Forwarded-For`。
    # 而"来自回环 = 免令牌"是我们的鉴权规则 —— 于是远程攻击者只要经反代发一个
    # `X-Forwarded-For: 127.0.0.1`，就会被当成"本机"，整个控制台不需要令牌。
    #
    # 我们自己已经有一套显式的转发头策略（`ZW_TRUST_PROXY_HEADERS` +
    # `api._client_ip()`），默认关闭、由运维按实际拓扑打开。所以这里必须让
    # uvicorn 别插手，否则那套开关形同虚设。
    # 自我重启时（见 api.py 的 `/service/takeover`），旧进程可能还没完全放掉端口。
    # 这里按 `ZW_WAIT_PORT_MS` 等一下，避免 `[Errno 98] address already in use`。
    _wait_ms = int(os.environ.pop("ZW_WAIT_PORT_MS", "0") or 0)
    if _wait_ms > 0:
        import socket as _sk
        import time as _t

        # ## 探测端口时**绝对不能设 SO_REUSEADDR**（2026-11-20 审查实测）
        #
        # 第一版设了它，结果：旧进程还 LISTEN 着，`bind` 在 Linux 上**照样成功**
        # → 探测判"端口空了" → 退出循环 → uvicorn 真去绑 → `[Errno 98]`
        # → 新实例秒退，而旧进程已经 `os._exit` 了 → **服务彻底消失**。
        #
        # 这正是仓库自己在 `client.py:_port_bindable` 注释里写过的坑
        # （Windows 上 SO_REUSEADDR 允许抢占），POSIX 上它同样会让"占用中"
        # 探测不出来。不设它，bind 才会因端口被占而抛 OSError。
        #
        # 另外：探测成功后要连做两次采样（间隔 120ms），避免刚好撞上
        # "旧进程正在关闭、还没完全释放"的瞬间。
        _deadline = _t.time() + _wait_ms / 1000.0
        _ok_streak = 0
        while _t.time() < _deadline:
            _free = False
            with _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM) as _probe:
                try:
                    _probe.bind((s.host, s.port))
                    _free = True
                except OSError:
                    _free = False
            _ok_streak = _ok_streak + 1 if _free else 0
            if _ok_streak >= 2:            # 连续两次都空，才认为真的空了
                break
            _t.sleep(0.12)

    uvicorn.run(app, host=s.host, port=s.port, log_level=s.log_level.lower(),
                access_log=False, log_config=None,
                proxy_headers=False, forwarded_allow_ips="")


def main() -> None:
    run()


if __name__ == "__main__":
    main()

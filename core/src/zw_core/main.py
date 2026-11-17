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
    logger.info("监听 http://%s:%s  数据目录 %s", s.host, s.port, s.root)
    # log_config=None：不让 uvicorn 用 dictConfig 覆盖我们的日志配置
    # （它的 formatter 会碰 sys.stdout，而无控制台环境下那是 None）。
    uvicorn.run(app, host=s.host, port=s.port, log_level=s.log_level.lower(),
                access_log=False, log_config=None)


def main() -> None:
    run()


if __name__ == "__main__":
    main()

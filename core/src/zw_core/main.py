"""程序入口。

`python -m zw_core` / `zw serve` 启动 HTTP 服务；后台同时跑健康巡检。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from . import __version__
from .api import build_app
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 巡检永远不能把服务带崩
            logger.error("健康巡检异常：%s", exc)


@contextlib.asynccontextmanager
async def _lifespan(app: Any):
    """启动 / 关闭钩子（FastAPI 新版推荐写法，不用已废弃的 on_event）。"""
    s = get_settings()
    logger.info("知纹核心启动 node=%s root=%s", s.node_id, s.root)
    try:
        rebuilt = await asyncio.to_thread(mgr.rebuild_gateway_routes)
        if rebuilt["restored"] or rebuilt["crashed"]:
            logger.info("启动时重建网关路由：恢复=%s 崩溃=%s", rebuilt["restored"], rebuilt["crashed"])
    except Exception as exc:
        logger.error("重建网关路由失败：%s", exc)

    eng = mgr._engine_summary()
    if not eng["ok"]:
        logger.warning("没有找到浏览器内核：%s", eng.get("error", ""))

    app.state.health_task = asyncio.create_task(_health_loop())
    try:
        yield
    finally:
        task = getattr(app.state, "health_task", None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> Any:
    setup_logging()
    app = build_app()
    app.router.lifespan_context = _lifespan

    # ── MCP over HTTP ────────────────────────────────────
    @app.post("/mcp", include_in_schema=False)
    async def mcp_http(request: Request) -> JSONResponse:
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
        resp = mcp_handle(payload)
        if resp is None:
            return JSONResponse(content={"jsonrpc": "2.0", "id": None, "result": {}})
        return JSONResponse(content=resp)

    @app.get("/mcp", include_in_schema=False)
    def mcp_info() -> dict[str, Any]:
        from .mcp_server import TOOLS

        return {
            "ok": True,
            "data": {
                "说明": "MCP over HTTP。用 POST 发 JSON-RPC 2.0（initialize / tools/list / tools/call）。",
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
    uvicorn.run(app, host=s.host, port=s.port, log_level=s.log_level.lower(), access_log=False)


def main() -> None:
    run()


if __name__ == "__main__":
    main()

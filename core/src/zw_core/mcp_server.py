"""MCP 服务：把知纹的能力包装成 AI 客户端可以直接挂载的工具。

为什么自己实现 JSON-RPC 而不用 `mcp` 包：
1. 少一个依赖，Windows 上 pip 装得越快越不容易出事；
2. 协议本身只有 `initialize` / `tools/list` / `tools/call` 三条消息，手写更可控；
3. 可以让同一份实现同时服务 stdio（Claude Desktop / Codex / Cursor）和 HTTP（远程）。

用法：
```bash
zw mcp                      # 走 stdio，供本机 AI 客户端拉起
curl -X POST /mcp -d '{...}' # 走 HTTP，供远端 AI 调用
```
"""

from __future__ import annotations

import base64
import json
import sys
from typing import Any, Callable

from . import __version__
from .services import cookies as ck
from .services import manager as mgr
from .services import sync as syncsvc

PROTOCOL_VERSION = "2024-11-05"

# 工具表：name -> (说明, JSON Schema, 处理函数)
TOOLS: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], Any]]] = {}


def tool(name: str, desc: str, schema: dict[str, Any]) -> Callable[[Callable[[dict[str, Any]], Any]], Callable[[dict[str, Any]], Any]]:
    def deco(fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
        TOOLS[name] = (desc, schema, fn)
        return fn

    return deco


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


def _ws(ref: str, start: bool = True) -> str:
    p = mgr.get_profile(ref)
    if p["state"] != "running":
        if not start:
            raise RuntimeError(f"{p['name']} 没有在运行（当前 {p['state']}）")
        mgr.start(p["id"])
        p = mgr.get_profile(p["id"])
    port = p.get("cdp_port")
    if not port:
        raise RuntimeError(f"{p['name']} 关闭了调试端口")
    import httpx

    return httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=5).json()["webSocketDebuggerUrl"]


# ── 环境 ──────────────────────────────────────────────────
@tool(
    "zw_list_profiles",
    "列出本机全部浏览器环境（含状态、指纹种子、同步版本）。开始任何操作前先调它。",
    _obj({"state": {"type": "string", "description": "只看某个状态：running/stopped/…"}, "q": {"type": "string", "description": "按名字搜索"}}),
)
def _list_profiles(args: dict[str, Any]) -> Any:
    items = mgr.list_profiles(state=args.get("state"), q=args.get("q"))
    return {"profiles": items, "total": len(items)}


@tool(
    "zw_create_profile",
    "创建一个新的隔离环境（浏览器指纹 + cookie + 扩展 + 代理都独立）。只有 name 必填，指纹种子会自动分配。",
    _obj(
        {
            "name": {"type": "string", "description": "环境名，全局唯一"},
            "group_name": {"type": "string"},
            "note": {"type": "string"},
            "proxy": {"type": "string", "description": "例如 http://127.0.0.1:7890"},
            "start_urls": {"type": "array", "items": {"type": "string"}},
            "window_w": {"type": "integer"},
            "window_h": {"type": "integer"},
        },
        ["name"],
    ),
)
def _create_profile(args: dict[str, Any]) -> Any:
    return mgr.create_profile(args)


@tool(
    "zw_start_profile",
    "启动一个环境。返回里含 gateway_ws_url —— 把它交给 Playwright 的 connectOverCDP 就能接管页面。",
    _obj({"profile": {"type": "string", "description": "环境 id 或名字"}, "urls": {"type": "array", "items": {"type": "string"}}}, ["profile"]),
)
def _start_profile(args: dict[str, Any]) -> Any:
    return mgr.start(args["profile"], urls=args.get("urls"))


@tool(
    "zw_stop_profile",
    "停止一个环境。会先尝试 CDP 优雅关闭，再走信号。",
    _obj({"profile": {"type": "string"}, "force": {"type": "boolean"}}, ["profile"]),
)
def _stop_profile(args: dict[str, Any]) -> Any:
    return mgr.stop(args["profile"], force=bool(args.get("force")))


@tool("zw_restart_profile", "重启一个环境（等价于 stop 后等端口释放再 start）。", _obj({"profile": {"type": "string"}}, ["profile"]))
def _restart_profile(args: dict[str, Any]) -> Any:
    return mgr.restart(args["profile"])


@tool(
    "zw_get_cdp",
    "只取 CDP 连接信息（不启动浏览器）。返回 gateway_ws_url / 原始发现接口 / 现成的 Playwright 代码片段。",
    _obj({"profile": {"type": "string"}, "ttl": {"type": "integer", "description": "令牌有效期秒数，60-7200"}}, ["profile"]),
)
def _get_cdp(args: dict[str, Any]) -> Any:
    p = mgr.get_profile(args["profile"])
    if p["state"] != "running":
        raise RuntimeError(f"{p['name']} 当前 {p['state']}，先 start")
    return mgr.cdp_info(p, ttl=args.get("ttl"))


@tool("zw_profile_stats", "返回环境总量、运行中数量、端口池占用、磁盘占用、内核版本。", _obj({}))
def _stats(_: dict[str, Any]) -> Any:
    return mgr.stats()


# ── 数据 ──────────────────────────────────────────────────
@tool(
    "zw_read_cookies",
    "读取某个环境当前的全部 cookie（明文，含 httpOnly）。环境会自动启动。",
    _obj({"profile": {"type": "string"}, "limit": {"type": "integer", "description": "最多返回多少条，默认全部"}}, ["profile"]),
)
def _read_cookies(args: dict[str, Any]) -> Any:
    ws = _ws(args["profile"])
    items = ck.dump_cookies_sync(ws)
    limit = args.get("limit")
    if limit:
        items = items[: int(limit)]
    return {"count": len(items), "cookies": items}


@tool(
    "zw_write_cookies",
    "把 cookie 写进某个环境（默认先清空再写）。环境会自动启动。",
    _obj(
        {
            "profile": {"type": "string"},
            "cookies": {"type": "array", "items": {"type": "object"}, "description": "CDP 格式的 cookie 数组"},
            "mode": {"type": "string", "enum": ["replace", "merge"]},
        },
        ["profile", "cookies"],
    ),
)
def _write_cookies(args: dict[str, Any]) -> Any:
    ws = _ws(args["profile"])
    return ck.load_cookies_sync(ws, args["cookies"], clear=(args.get("mode", "replace") == "replace"))


@tool("zw_read_storage", "读取某个环境里指定站点的 localStorage。", _obj({"profile": {"type": "string"}, "origin": {"type": "string"}}, ["profile", "origin"]))
def _read_storage(args: dict[str, Any]) -> Any:
    ws = _ws(args["profile"])
    return ck.dump_storage_sync(ws, [args["origin"]])


# ── 页面 ──────────────────────────────────────────────────
@tool("zw_navigate", "在指定环境里打开网址，返回最终 URL 与标题。环境没启动会自动启动。", _obj({"profile": {"type": "string"}, "url": {"type": "string"}}, ["profile", "url"]))
def _navigate(args: dict[str, Any]) -> Any:
    return ck.navigate_sync(_ws(args["profile"]), args["url"])


@tool("zw_evaluate", "在指定环境的当前页面执行一段 JS 并返回结果（同步返回值）。", _obj({"profile": {"type": "string"}, "expression": {"type": "string"}}, ["profile", "expression"]))
def _evaluate(args: dict[str, Any]) -> Any:
    return {"result": ck.evaluate_sync(_ws(args["profile"]), args["expression"])}


@tool("zw_page_text", "拿到当前页面的可见文字（用于阅读页面内容）。", _obj({"profile": {"type": "string"}}, ["profile"]))
def _page_text(args: dict[str, Any]) -> Any:
    return {"text": ck.run(ck.page_text(_ws(args["profile"])))}


@tool("zw_screenshot", "对当前页面截图，返回 base64 PNG。", _obj({"profile": {"type": "string"}}, ["profile"]))
def _screenshot(args: dict[str, Any]) -> Any:
    data = ck.screenshot_sync(_ws(args["profile"]))
    return {"format": "png", "base64": base64.b64encode(data).decode(), "bytes": len(data)}


@tool("zw_click", "点击元素（用 CSS 选择器）或在坐标处点击。", _obj({"profile": {"type": "string"}, "selector": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"}}, ["profile"]))
def _click(args: dict[str, Any]) -> Any:
    return ck.run(ck.click(_ws(args["profile"]), selector=args.get("selector"), x=args.get("x"), y=args.get("y")))


@tool("zw_type", "在元素里逐字符输入文本。", _obj({"profile": {"type": "string"}, "text": {"type": "string"}, "selector": {"type": "string"}}, ["profile", "text"]))
def _type(args: dict[str, Any]) -> Any:
    return ck.run(ck.type_text(_ws(args["profile"]), args["text"], selector=args.get("selector")))


@tool("zw_list_tabs", "列出该环境当前打开的所有标签页。", _obj({"profile": {"type": "string"}}, ["profile"]))
def _list_tabs(args: dict[str, Any]) -> Any:
    return {"targets": ck.run(ck.list_targets(_ws(args["profile"], start=False)))}


# ── 同步 ──────────────────────────────────────────────────
@tool(
    "zw_sync_push",
    "把这个环境加密后推到同步中心。需要主密码（也可由环境变量 ZW_SYNC_PASSWORD 提供）。会先把浏览器拉起来读明文 cookie。",
    _obj({"profile": {"type": "string"}, "password": {"type": "string"}, "note": {"type": "string"}}, ["profile"]),
)
def _sync_push(args: dict[str, Any]) -> Any:
    return syncsvc.push(args["profile"], password=args.get("password"), note=args.get("note", ""))


@tool(
    "zw_sync_pull",
    "从同步中心拉取这个环境的最新版本并还原。主密码不对时会在落盘之前被拦下。",
    _obj({"profile": {"type": "string"}, "password": {"type": "string"}, "revision": {"type": "integer"}}, ["profile"]),
)
def _sync_pull(args: dict[str, Any]) -> Any:
    return syncsvc.pull(args["profile"], password=args.get("password"), revision=args.get("revision"))


@tool("zw_sync_diff", "对比本地与远端的同步版本，判断要不要 push/pull。", _obj({"profile": {"type": "string"}}, ["profile"]))
def _sync_diff(args: dict[str, Any]) -> Any:
    return syncsvc.diff(args["profile"])


@tool("zw_sync_index", "列出同步中心上所有环境的版本索引。", _obj({}))
def _sync_index(_: dict[str, Any]) -> Any:
    return {"profiles": syncsvc.remote_list()}


# ── 排错 ──────────────────────────────────────────────────
@tool("zw_doctor", "自检：内核、数据库、令牌、同步中心、并发余量。出问题先跑这个。", _obj({}))
def _doctor(_: dict[str, Any]) -> Any:
    from .api import build_app  # 避免循环导入

    del build_app
    from .services.hub import HubClient
    from pathlib import Path

    from .config import get_settings
    from .db import get_db

    s = get_settings()
    checks = [
        {"项": "浏览器内核", "ok": mgr._engine_summary()["ok"], "详情": mgr._engine_summary()},
        {"项": "数据库", "ok": get_db().integrity_ok(), "详情": str(s.db_path)},
        {"项": "数据目录", "ok": Path(s.root).exists(), "详情": str(s.root)},
    ]
    if s.hub_url:
        try:
            checks.append({"项": "同步中心", "ok": True, "详情": HubClient(s.hub_url, s.hub_token).health()})
        except Exception as exc:
            checks.append({"项": "同步中心", "ok": False, "详情": str(exc)})
    return {"checks": checks, "all_ok": all(c["ok"] for c in checks)}


@tool("zw_read_logs", "读取某个环境的浏览器日志与事件日志尾部，排错用。", _obj({"profile": {"type": "string"}, "tail": {"type": "integer"}}, ["profile"]))
def _read_logs(args: dict[str, Any]) -> Any:
    p = mgr.get_profile(args["profile"])
    tail = int(args.get("tail") or 120)
    out: dict[str, Any] = {"profile": p["name"], "state": p["state"], "last_error": p.get("last_error", "")}
    for label, fname in (("browser", "browser.log"), ("events", "profile.log")):
        f = mgr.profile_dir(p["id"]) / fname
        out[label] = f.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:] if f.exists() else []
    return out


# ── JSON-RPC 分发 ─────────────────────────────────────────
def _tools_list() -> list[dict[str, Any]]:
    return [
        {"name": name, "description": desc, "inputSchema": schema}
        for name, (desc, schema, _fn) in TOOLS.items()
    ]


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    """处理一条 JSON-RPC 消息。通知（无 id）返回 None。"""
    method = request.get("method")
    rid = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "zhiwen", "version": __version__},
                "instructions": (
                    "知纹（zhiwen）指纹浏览器机群管理器。"
                    "先调 zw_list_profiles 看现状；zw_start_profile 会返回 gateway_ws_url，"
                    "把它交给 Playwright 的 connectOverCDP 就能接管页面。"
                    "跨机搬运登录态用 zw_sync_push / zw_sync_pull，需要主密码。"
                ),
            },
        }

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _tools_list()}}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        entry = TOOLS.get(name)
        if entry is None:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"没有这个工具：{name}"}}
        _desc, _schema, fn = entry
        try:
            result = fn(args)
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)}],
                },
            }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"不支持的方法：{method}"}}


def serve_stdio() -> None:
    """stdio 传输：一行一条 JSON-RPC 消息。"""
    print(json.dumps({"jsonrpc": "2.0", "method": "notifications/ready", "params": {"name": "zhiwen"}}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "不是合法 JSON"}}), flush=True)
            continue
        resp = handle(req)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False, default=str), flush=True)

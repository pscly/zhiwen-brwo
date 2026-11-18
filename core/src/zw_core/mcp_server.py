"""MCP 服务：把知纹的能力包装成 AI 客户端可以直接挂载的工具。

为什么自己实现 JSON-RPC 而不用 `mcp` 包：
1. 少一个依赖，Windows 上 pip 装得越快越不容易出事；
2. 协议本身只有 `initialize` / `tools/list` / `tools/call` 三条消息，手写更可控；
3. 可以让同一份实现同时服务 stdio（Claude Desktop / Codex / Cursor）和 HTTP（远程）。

用法：
```bash
zw mcp                      # 走 stdio，供本机 AI 客户端拉起（进程内直调，无需令牌）
curl -X POST /mcp -d '{...}' -H "Authorization: Bearer $ZW_ADMIN_TOKEN"   # 走 HTTP
```

## 设计取向

工具刻意做成"少而厚"：每个工具都能一步完成一件真实的事，不需要模型自己
串四五个调用。代价是参数多一点，但 MCP 的 schema 会告诉模型每个参数是干什么的。
不做 `delete_profile` / `reset_fingerprint` 这类不可逆操作——那该由人来点。
"""

from __future__ import annotations

import base64
import json
import sys
from typing import Any, Callable

from . import __version__
from .services import cookies as ck
from .services import manager as mgr
from .services import proxy as proxysvc
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
    ws = mgr.local_ws(p)
    if ws is None:
        raise RuntimeError(f"{p['name']} 没有可用的调试端口（cdp_enabled=false？）")
    mgr.touch(p["id"])
    return ws


# ── 环境 ──────────────────────────────────────────────────
@tool(
    "zw_list_profiles",
    "列出本机全部浏览器环境（含状态、指纹种子、同步版本、分组、代理）。"
    "代理看 `proxy_info`：绑了代理库时 `proxy` 字段是空的（库是唯一真相），"
    "`proxy_info.source=library` 且 `proxy_info.upstream` 才是实际地址；"
    "`source=missing` 表示库条目已被删。开始任何操作前先调它。",
    _obj({
        "state": {"type": "string", "description": "只看某个状态：running/stopped/crashed/failed"},
        "group": {"type": "string", "description": "只看某个分组"},
        "q": {"type": "string", "description": "按名字/备注/标签模糊搜"},
    }),
)
def _list_profiles(args: dict[str, Any]) -> Any:
    items = mgr.list_profiles(state=args.get("state"), group=args.get("group"), q=args.get("q"))
    return {"profiles": items, "total": len(items), "groups": mgr.groups()}


@tool(
    "zw_create_profile",
    "创建一个新的隔离环境（浏览器指纹 + cookie + 扩展 + 代理都独立）。只有 name 必填，指纹种子会自动分配。",
    _obj(
        {
            "name": {"type": "string", "description": "环境名，全局唯一"},
            "group_name": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "note": {"type": "string"},
            "proxy": {"type": "string", "description": "例如 http://127.0.0.1:7890；带用户名密码也可以（会自动走认证扩展）"},
            "start_urls": {"type": "array", "items": {"type": "string"}},
            "window_w": {"type": "integer"},
            "window_h": {"type": "integer"},
            "fp_timezone": {"type": "string", "description": "例如 Asia/Shanghai，要和出口 IP 一致"},
            "fp_lang": {"type": "string", "description": "例如 zh-CN"},
            "extensions": {"type": "array", "items": {"type": "string"}, "description": "扩展库里的 slug"},
            "headless": {"type": "boolean"},
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


@tool("zw_restart_profile", "重启一个环境（等价于 stop 后等端口释放再 start）。改过代理/扩展后必须重启才生效。", _obj({"profile": {"type": "string"}}, ["profile"]))
def _restart_profile(args: dict[str, Any]) -> Any:
    return mgr.restart(args["profile"])


@tool(
    "zw_update_profile",
    "修改环境的设置（代理、时区、语言、窗口、扩展、备注等）。改指纹种子会被拒（SEED_IMMUTABLE）。运行中的环境改完需要 restart。",
    _obj(
        {
            "profile": {"type": "string"},
            "fields": {
                "type": "object",
                "description": "要改的字段，例如 {proxy, fp_timezone, fp_lang, note, group_name, extensions, window_w, window_h}",
            },
        },
        ["profile", "fields"],
    ),
)
def _update_profile(args: dict[str, Any]) -> Any:
    return mgr.update_profile(args["profile"], args["fields"] or {})


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


@tool("zw_profile_stats", "返回环境总量、运行中数量、端口池占用、磁盘占用、内核版本、分组分布。", _obj({}))
def _stats(_: dict[str, Any]) -> Any:
    return mgr.stats()


@tool(
    "zw_fingerprint_probe",
    "指纹体检：读真机实际暴露的 UA/平台/时区/WebGL/canvas/字体，并给出自相矛盾的警告。判断环境是否像真人的第一步。",
    _obj({"profile": {"type": "string"}, "start_if_needed": {"type": "boolean"}}, ["profile"]),
)
def _fingerprint_probe(args: dict[str, Any]) -> Any:
    return mgr.probe_fingerprint(args["profile"], start_if_needed=bool(args.get("start_if_needed", True)))


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


@tool("zw_clear_cookies", "清空某个环境的全部 cookie（不可撤销，慎用）。", _obj({"profile": {"type": "string"}}, ["profile"]))
def _clear_cookies(args: dict[str, Any]) -> Any:
    return ck.clear_cookies_sync(_ws(args["profile"]))


@tool(
    "zw_read_storage",
    "读取某个环境里指定站点的 localStorage。注意这个操作会在专属管理标签页里打开该站点，不影响用户正在看的页面。",
    _obj({"profile": {"type": "string"}, "origin": {"type": "string", "description": "例如 https://www.xiaohongshu.com"}}, ["profile", "origin"]),
)
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


@tool("zw_screenshot", "对当前页面截图，返回 base64 PNG。", _obj({"profile": {"type": "string"}, "full_page": {"type": "boolean"}}, ["profile"]))
def _screenshot(args: dict[str, Any]) -> Any:
    data = ck.screenshot_sync(_ws(args["profile"]), full_page=bool(args.get("full_page")))
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


# ── 代理 ──────────────────────────────────────────────────
@tool("zw_list_proxies", "列出代理库（不含密码）与每个代理被哪些环境使用。", _obj({}))
def _list_proxies(_: dict[str, Any]) -> Any:
    from .db import get_db

    with get_db().conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM proxies ORDER BY id DESC")]
    for r in rows:
        r.pop("password", None)
    return {"proxies": rows, "total": len(rows)}


@tool(
    "zw_check_proxy",
    "检测一个代理的出口 IP 与延迟。可以直接测未保存的代理，也可以用 id 测库里已有的。",
    _obj({
        "id": {"type": "integer"},
        "scheme": {"type": "string"},
        "host": {"type": "string"},
        "port": {"type": "integer"},
        "username": {"type": "string"},
        "password": {"type": "string"},
    }),
)
def _check_proxy(args: dict[str, Any]) -> Any:
    from .db import get_db

    if args.get("id"):
        with get_db().conn() as c:
            row = c.execute("SELECT * FROM proxies WHERE id=?", (int(args["id"]),)).fetchone()
        if row is None:
            raise RuntimeError(f"代理不存在：{args['id']}")
        auth = f"{row['username']}:{row['password']}@" if row["username"] else ""
        spec = proxysvc.parse_proxy(f"{row['scheme']}://{auth}{row['host']}:{row['port']}")
    else:
        if not args.get("host") or not args.get("port"):
            raise RuntimeError("要么给 id，要么给 host+port")
        auth = f"{args.get('username','')}:{args.get('password','')}@" if args.get("username") else ""
        spec = proxysvc.parse_proxy(f"{args.get('scheme','http')}://{auth}{args['host']}:{args['port']}")
    return proxysvc.check_egress(spec) if spec else {"ok": False, "error": "代理配置不完整"}


@tool(
    "zw_import_proxies",
    "批量导入代理（一行一个）。支持 socks5://u:p@h:port、http://…、h:port:u:p、行尾 # 备注，"
    "以及链式「前置,落地」。坏行单独返回原因，不影响其它行。"
    "用户可以一次粘几十条，返回里有 imported/skipped/failed 明细。",
    _obj({"text": {"type": "string", "description": "多行文本，一行一个代理"}}, ["text"]),
)
def _import_proxies(args: dict[str, Any]) -> Any:
    from .services import proxylib

    return proxylib.import_batch(str(args.get("text") or ""))


@tool(
    "zw_sync_proxies",
    "把本机代理库与服务器对齐（先上行把本机新增的交上去，再下行补服务器新增的）。"
    "用户要求「两边都能加，自动同步」；平时是自动的，这个工具用于手动补一次。",
    _obj({}),
)
def _sync_proxies(_: dict[str, Any]) -> Any:
    from .services import proxysync

    return proxysync.sync()


# 注：这里**故意没有** `zw_hide_proxy`。
#
# "隐藏"是**服务器侧**的概念（`hub/src/zw_hub/proxies.py` 的 `hidden` 列），
# 本机库 `proxies` 表没有这一列。曾经加过一个 `zw_hide_proxy` 直接
# `UPDATE proxies SET hidden=?`，结果 100% 报 `no such column: hidden` ——
# 一个永远失败的工具比没有这个工具更糟（AI 会反复试、用户以为坏了）。
# 要隐藏代理，请用主控台（`POST /v1/proxies/{id}/hide`）或客户端 GUI。

@tool(
    "zw_install_extension",
    "安装一个 Chrome 扩展：给 url（商店详情页链接或 32 位扩展 ID）或 path（.crx / 扩展目录 / zip）。"
    "默认会顺手建成扩展包；as_default=true 则标成「新建环境自动带上」。"
    "下载在这台机器上做 —— 服务器常常没有外网也没有用户的代理。",
    _obj({
        "url": {"type": "string", "description": "商店链接或 32 位扩展 ID"},
        "path": {"type": "string", "description": ".crx / 扩展目录 / zip 的绝对路径"},
        "as_default": {"type": "boolean", "description": "设为新建环境默认带上"},
        "make_pack": {"type": "boolean", "description": "是否顺手建扩展包（默认 true）"},
    }),
)
def _install_extension(args: dict[str, Any]) -> Any:
    from .services import extstore

    url = str(args.get("url") or "").strip()
    path = str(args.get("path") or "").strip()
    if not (url or path):
        raise RuntimeError("要么给 url（商店链接/ID），要么给 path")
    try:
        return extstore.install(
            url=url, crx_path=path,
            make_pack=bool(args.get("make_pack", True)),
            as_default=bool(args.get("as_default", False)),
        )
    except extstore.StoreError as exc:
        raise RuntimeError(f"[{exc.code}] {exc.message}") from exc


@tool(
    "zw_check_extension_updates",
    "检查已装扩展有没有新版（回 Chrome 商店比对）。只查带商店 ID 标记的；"
    "手工导入的包没有 ID，会跳过 —— 不瞎猜 ID 去下载别人的扩展。",
    _obj({}),
)
def _check_extension_updates(_: dict[str, Any]) -> Any:
    from .services import extstore

    items = extstore.check_updates()
    return {"items": items, "updatable": [i for i in items if i.get("update_available")]}


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


@tool("zw_sync_diff", "对比本地与远端的同步版本，判断要不要 push/pull。", _obj({"profile": {"type": "string"}, "password": {"type": "string"}}, ["profile"]))
def _sync_diff(args: dict[str, Any]) -> Any:
    return syncsvc.diff(args["profile"], password=args.get("password"))


@tool("zw_sync_index", "列出同步中心上所有环境的版本索引。", _obj({}))
def _sync_index(_: dict[str, Any]) -> Any:
    return {"profiles": syncsvc.remote_list()}


# ── 排错 ──────────────────────────────────────────────────
@tool("zw_doctor", "自检：内核、指纹内核、数据库、令牌、同步中心、磁盘、并发余量。出问题先跑这个。", _obj({}))
def _doctor(_: dict[str, Any]) -> Any:
    from pathlib import Path

    from .config import get_settings
    from .db import get_db
    from .services.hub import HubClient

    s = get_settings()
    eng = mgr._engine_summary()
    checks = [
        {"项": "浏览器内核", "ok": eng["ok"], "详情": eng},
        {"项": "指纹内核", "ok": bool(eng.get("fingerprint_supported")), "详情": "支持 --fingerprint" if eng.get("fingerprint_supported") else "当前内核不认 --fingerprint，环境之间只有 cookie 隔离"},
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
                    "不想接 CDP 时直接用 zw_navigate / zw_page_text / zw_screenshot 也能干活。"
                    "跨机搬运登录态用 zw_sync_push / zw_sync_pull，需要主密码。"
                    "怀疑指纹不真实时先跑 zw_fingerprint_probe，它的 warnings 会告诉你哪里自相矛盾。"
                ),
            },
        }

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _tools_list()}}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        entry = TOOLS.get(name)
        if entry is None:
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32602, "message": f"没有这个工具：{name}（可用：{', '.join(sorted(TOOLS))}）"},
            }
        _desc, _schema, fn = entry
        # **按 schema 先校验必填项**。
        #
        # 之前是直接把 `args` 丢给 `fn()`，而各工具内部写的是 `args["profile"]`
        # 这种直取 —— 于是 AI 少给一个参数时，拿到的是裸
        # `{"code": "KeyError", "message": "'profile'"}`：
        # 既不知道**哪个**参数必填，也不知道该怎么填。schema 里其实早就声明了
        # `required`，只是没人用。
        #
        # 现在统一在这里挡一层，报错写成"缺哪个参数 + 它的类型和说明"。
        missing = [k for k in (_schema.get("required") or []) if k not in args]
        if missing:
            props = _schema.get("properties") or {}
            detail = "；".join(
                f"`{k}`（{props.get(k, {}).get('description') or props.get(k, {}).get('type') or '必填'}）"
                for k in missing)
            return {
                "jsonrpc": "2.0", "id": rid,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps({
                        "ok": False, "code": "MCP_MISSING_ARG",
                        "message": f"调用 {name} 缺少必填参数：{detail}",
                        "hint": "参数名见该工具的 inputSchema.required；"
                                "也可以先调 tools/list 看完整定义。",
                    }, ensure_ascii=False)}],
                },
            }
        try:
            result = fn(args)
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]},
            }
        except Exception as exc:
            code = getattr(exc, "code", type(exc).__name__)
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"ok": False, "code": code, "message": getattr(exc, "message", str(exc))},
                                ensure_ascii=False,
                            ),
                        }
                    ],
                },
            }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}}

    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"不支持的方法：{method}"}}


def serve_stdio() -> None:
    """stdio 传输：一行一条 JSON-RPC 消息。

    注意别往 stdout 打任何调试信息 —— 那会被客户端当成协议数据。
    日志走 stderr。
    """
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

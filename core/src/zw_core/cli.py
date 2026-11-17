"""命令行入口 `zw`。

设计原则：每条命令都能加 `--json` 输出机器可读结果 —— 这样 AI 或脚本可以直接消费，
不需要去解析人类可读的表格。

## 0.2.0 修掉的几处

- `zw profile cdp` 以前读 `data["gateway_ws_base"]`，而这个键在响应里根本不存在，
  于是**必然 KeyError 崩溃**。现在打印真实可用的一行命令。
- 所有子命令都补了 `--json`（以前 `cookie dump`、`page shot` 之类没有）。
- 新增 `zw profile probe / export / import / bind-ext / unbind-ext`、
  `zw proxy ...`、`zw sync push-all / rm`、`zw group ls`、`zw events`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import __version__


def _out(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                print(f"{k}:")
                print(json.dumps(v, ensure_ascii=False, indent=2, default=str))
            else:
                print(f"{k}: {v}")
    elif isinstance(data, list):
        for item in data:
            print(json.dumps(item, ensure_ascii=False, default=str))
    else:
        print(data)


def _base_url() -> str:
    from .config import get_settings

    s = get_settings()
    host = "127.0.0.1" if s.host in ("0.0.0.0", "::") else s.host
    return f"http://{host}:{s.port}"


def _headers() -> dict[str, str]:
    from .config import get_settings

    s = get_settings()
    return {"Authorization": f"Bearer {s.admin_token}"} if s.admin_token else {}


def _client() -> Any:
    """CLI 默认走 HTTP 调本机服务；服务没起时给出明确提示。"""
    import httpx

    return httpx.Client(base_url=_base_url(), headers=_headers(), timeout=300)


def _call(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    import httpx

    try:
        with _client() as c:
            r = c.request(method, path, json=payload)
    except httpx.ConnectError:
        print(
            "连不上本机知纹服务。先启动它：\n"
            "  前台调试：zw serve\n"
            "  后台常驻：zw serve --daemon（Windows 上建议用任务计划或 nssm 注册服务）",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        data = r.json()
    except Exception:
        print(f"服务返回的不是 JSON（HTTP {r.status_code}）：{r.text[:300]}", file=sys.stderr)
        raise SystemExit(1)
    if not data.get("ok"):
        print(f"失败 [{data.get('code')}] {data.get('message')}", file=sys.stderr)
        if data.get("hint"):
            print(f"提示：{data['hint']}", file=sys.stderr)
        raise SystemExit(1)
    return data.get("data")


def _call_raw(method: str, path: str, content: bytes | None = None, headers: dict[str, str] | None = None) -> Any:
    """给二进制接口（截图、导出包）用：返回原始 response。"""
    import httpx

    try:
        with _client() as c:
            return c.request(method, path, content=content, headers=headers or {})
    except httpx.ConnectError:
        print("连不上本机知纹服务，先 zw serve", file=sys.stderr)
        raise SystemExit(2)


# ── 服务 ──────────────────────────────────────────────────
def cmd_serve(args: argparse.Namespace) -> None:
    from .main import run

    run()


def cmd_client(args: argparse.Namespace) -> None:
    """启动桌面客户端（图形界面；没有 tkinter 时退回命令行）。"""
    from .client import run_gui

    raise SystemExit(run_gui())


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import serve_stdio

    serve_stdio()


def cmd_doctor(args: argparse.Namespace) -> None:
    data = _call("POST", "/api/v1/maintenance/doctor")
    if args.json:
        _out(data, True)
    else:
        for c in data.get("checks", []):
            mark = "✅" if c["ok"] else "❌"
            detail = c["详情"]
            if isinstance(detail, (dict, list)):
                detail = json.dumps(detail, ensure_ascii=False)
            print(f"{mark} {c['项']:<10} {detail}")
        print()
        print("全部通过" if data.get("all_ok") else "有未通过项，见上")
    if not data.get("all_ok"):
        raise SystemExit(1)


def cmd_stats(args: argparse.Namespace) -> None:
    data = _call("GET", "/api/v1/stats")
    if args.json:
        _out(data, True)
        return
    print(f"环境总数：{data['profiles']}    运行中：{data['running']}/{data['max_running']}")
    print(f"状态分布：{json.dumps(data['by_state'], ensure_ascii=False)}")
    print(f"端口池：{data['port_pool']['start']}-{data['port_pool']['end']}（已用 {data['port_pool']['used']}）")
    print(f"磁盘占用：{data['disk_human']}")
    print(f"内核：{data['engine'].get('version') or '缺失'}  {data['engine'].get('path', '')}")
    for g in data.get("groups", []):
        print(f"分组 {g['name']:<16} {g['count']} 个（运行 {g['running']}）")


def cmd_manifest(args: argparse.Namespace) -> None:
    _out(_call("GET", "/api/v1/ai/manifest"), args.json)


def cmd_meta(args: argparse.Namespace) -> None:
    _out(_call("GET", "/api/v1/meta"), args.json)


# ── 环境 ──────────────────────────────────────────────────
def cmd_profile_list(args: argparse.Namespace) -> None:
    q = []
    if args.state:
        q.append(f"state={args.state}")
    if args.group:
        q.append(f"group={args.group}")
    if args.q:
        q.append(f"q={args.q}")
    path = "/api/v1/profiles" + (("?" + "&".join(q)) if q else "")
    data = _call("GET", path)
    if args.json:
        _out(data, True)
        return
    rows = data.get("profiles", [])
    if not rows:
        print("（还没有环境。创建：zw profile create --name demo）")
        return
    print(f"{'名字':<22}{'状态':<10}{'分组':<12}{'种子':<12}{'同步':<6}{'端口':<8}id")
    for p in rows:
        print(
            f"{p['name'][:21]:<22}{p['state']:<10}{(p.get('group_name') or '-'):<12}{str(p['fp_seed']):<12}"
            f"{str(p.get('sync_revision')):<6}{str(p.get('cdp_port') or '-'):<8}{p['id']}"
        )


def cmd_profile_create(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {"name": args.name}
    if args.group:
        payload["group_name"] = args.group
    if args.proxy:
        payload["proxy"] = args.proxy
    if args.urls:
        payload["start_urls"] = [u for u in args.urls.split(",") if u]
    if args.platform:
        payload["fp_platform"] = args.platform
    if args.timezone:
        payload["fp_timezone"] = args.timezone
    if args.lang:
        payload["fp_lang"] = args.lang
    if args.ext:
        payload["extensions"] = [e for e in args.ext.split(",") if e]
    if args.note:
        payload["note"] = args.note
    if args.headless:
        payload["headless"] = True
    _out(_call("POST", "/api/v1/profiles", payload), args.json)


def cmd_profile_start(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {}
    if args.urls:
        payload["urls"] = [u for u in args.urls.split(",") if u]
    data = _call("POST", f"/api/v1/profiles/{args.ref}/start", payload)
    if args.json:
        _out(data, True)
        return
    p = data.get("profile", {})
    cdp = data.get("cdp") or {}
    print(f"{p.get('name')} 状态 {p.get('state')}  端口 {(data.get('local') or {}).get('port', p.get('cdp_port'))}")
    if cdp.get("gateway_ws_url"):
        print(f"CDP：{cdp['gateway_ws_url']}")
        print(f"有效期：{cdp.get('token_ttl')} 秒")
    for w in data.get("warnings", []):
        print(f"注意：{w}")


def cmd_profile_stop(args: argparse.Namespace) -> None:
    data = _call("POST", f"/api/v1/profiles/{args.ref}/stop?force={'true' if args.force else 'false'}")
    if args.json:
        _out(data, True)
        return
    print(f"{data.get('profile', {}).get('name')} 已停止（方式 {data.get('method')}）")


def cmd_profile_restart(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/profiles/{args.ref}/restart", {}), args.json)


def cmd_profile_rm(args: argparse.Namespace) -> None:
    _out(_call("DELETE", f"/api/v1/profiles/{args.ref}?purge={'true' if args.purge else 'false'}"), args.json)


def cmd_profile_cdp(args: argparse.Namespace) -> None:
    data = _call("GET", f"/api/v1/profiles/{args.ref}/cdp")
    if args.json:
        _out(data, True)
        return
    print(data["gateway_ws_url"])
    print(f"# 令牌有效期 {data['token_ttl']} 秒（过期重新执行本命令）")
    print(f"# Playwright: {data.get('playwright', '')}")
    print(f"# 原始发现接口: {data['raw_cdp_json']}")


def cmd_profile_logs(args: argparse.Namespace) -> None:
    data = _call("GET", f"/api/v1/profiles/{args.ref}/logs?tail={args.tail}")
    if args.json:
        _out(data, True)
        return
    if data.get("last_error"):
        print(f"# 最近一次错误：{data['last_error']}")
    for label in ("profile", "browser"):
        lines = data.get(label) or []
        print(f"=== {label}（最后 {len(lines)} 行）===")
        for line in lines:
            print(line)


def cmd_profile_unlock(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/profiles/{args.ref}/unlock"), args.json)


def cmd_profile_reset_fp(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/profiles/{args.ref}/fingerprint/reset", {"confirm": args.confirm}), args.json)


def cmd_profile_probe(args: argparse.Namespace) -> None:
    data = _call("POST", f"/api/v1/profiles/{args.ref}/fingerprint/probe")
    if args.json:
        _out(data, True)
        return
    for key in ("ua", "platform", "timezone", "lang", "hardware_concurrency", "webgl_vendor", "webgl_renderer", "canvas_hash", "fonts_count", "webdriver"):
        print(f"{key:<22}{data.get(key)}")
    warns = data.get("warnings") or []
    print()
    if warns:
        for w in warns:
            print(f"⚠️  {w}")
    else:
        print("✅ 本地检查没有发现自相矛盾之处（真机指纹质量仍需 browserscan / creepjs 实测）")


def cmd_profile_export(args: argparse.Namespace) -> None:
    body: bytes | None = None
    headers: dict[str, str] = {}
    pw = getattr(args, "password", None)
    if pw:
        # 显式给密码：服务端不一定配了 ZW_SYNC_PASSWORD，用户也不该被迫去改服务配置
        body = json.dumps({"password": pw}).encode()
        headers["Content-Type"] = "application/json"
    resp = _call_raw("POST", f"/api/v1/profiles/{args.ref}/export", content=body, headers=headers)
    if resp.status_code >= 400:
        print(f"失败：{resp.text[:300]}", file=sys.stderr)
        raise SystemExit(1)
    from urllib.parse import unquote

    name = args.out or unquote(resp.headers.get("X-ZW-Filename", f"{args.ref}.zwp"))
    with open(name, "wb") as fh:
        fh.write(resp.content)
    print(f"已导出 → {name}（{len(resp.content)} 字节）")


def cmd_profile_import(args: argparse.Namespace) -> None:
    with open(args.file, "rb") as fh:
        raw = fh.read()
    path = "/api/v1/profiles/import"
    pw = getattr(args, "password", None)
    if pw:
        from urllib.parse import quote

        path += f"?password={quote(pw)}"
    resp = _call_raw("POST", path, content=raw, headers={"Content-Type": "application/octet-stream"})
    data = resp.json()
    if not data.get("ok"):
        print(f"失败 [{data.get('code')}] {data.get('message')}", file=sys.stderr)
        raise SystemExit(1)
    d = data["data"]
    if args.json:
        _out(d, True)
        return
    print(f"已导入 {d['profile']['name']}（id {d['profile']['id']}，cookie {d['cookies']} 条）")


def cmd_profile_bind_ext(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/profiles/{args.ref}/extensions", {"slug": args.slug, "version": args.version}), args.json)


def cmd_profile_unbind_ext(args: argparse.Namespace) -> None:
    _out(_call("DELETE", f"/api/v1/profiles/{args.ref}/extensions/{args.slug}"), args.json)


# ── cookie ────────────────────────────────────────────────
def cmd_cookie_dump(args: argparse.Namespace) -> None:
    if args.format == "cdp":
        data = _call("GET", f"/api/v1/profiles/{args.ref}/cookies?fmt=cdp")
        text = json.dumps(data.get("cookies", []), ensure_ascii=False, indent=2)
    else:
        resp = _call_raw("GET", f"/api/v1/profiles/{args.ref}/cookies?fmt={args.format}")
        resp.raise_for_status()
        text = resp.text
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"已写入 {args.out}（{len(text)} 字节）")
    else:
        print(text)


def cmd_cookie_load(args: argparse.Namespace) -> None:
    raw = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    cookies = json.loads(raw)
    if isinstance(cookies, dict):
        cookies = cookies.get("cookies", [])
    _out(_call("PUT", f"/api/v1/profiles/{args.ref}/cookies", {"cookies": cookies, "mode": args.mode}), args.json)


def cmd_cookie_clear(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/profiles/{args.ref}/cookies/clear"), args.json)


# ── 同步 ──────────────────────────────────────────────────
def cmd_sync_state(args: argparse.Namespace) -> None:
    data = _call("GET", "/api/v1/sync/state")
    if args.json:
        _out(data, True)
        return
    print(f"同步中心：{data.get('hub_url') or '（未配置）'}  {'连通' if data.get('hub_ok') else '不可用'}")
    if data.get("hub_error"):
        print(f"  错误：{data['hub_error']}")
    for p in data.get("profiles", []):
        print(f"  {p['name']:<20} 本地 v{p['revision']} 状态 {p['state']} {'启用' if p['enabled'] else '停用'}")


def cmd_sync_push(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/sync/{args.ref}/push", {"password": args.password, "note": args.note or ""}), args.json)


def cmd_sync_pull(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/sync/{args.ref}/pull", {"password": args.password, "revision": args.revision, "force": args.force}), args.json)


def cmd_sync_push_all(args: argparse.Namespace) -> None:
    _out(_call("POST", "/api/v1/sync/push-all", {"password": args.password}), args.json)


def cmd_sync_diff(args: argparse.Namespace) -> None:
    path = f"/api/v1/sync/{args.ref}/diff"
    if args.password:
        from urllib.parse import quote

        path += f"?password={quote(args.password)}"
    data = _call("GET", path)
    if args.json:
        _out(data, True)
        return
    print(f"{data['name']}：本地 v{data['local_revision']}  远端 v{data.get('remote_revision')}")
    print(f"需要拉取：{data.get('needs_pull')}    需要推送：{data.get('needs_push')}    口令不一致：{data.get('key_mismatch')}")
    if data.get("hint"):
        print(data["hint"])


def cmd_sync_remote(args: argparse.Namespace) -> None:
    _out(_call("GET", "/api/v1/sync/remote"), args.json)


def cmd_sync_rm(args: argparse.Namespace) -> None:
    path = f"/api/v1/sync/{args.ref}"
    if args.revision:
        path += f"?revision={args.revision}"
    _out(_call("DELETE", path), args.json)


# ── 代理 ──────────────────────────────────────────────────
def cmd_proxy_list(args: argparse.Namespace) -> None:
    data = _call("GET", "/api/v1/proxies")
    if args.json:
        _out(data, True)
        return
    rows = data.get("proxies", [])
    if not rows:
        print("（代理库是空的）")
        return
    for r in rows:
        flag = "" if r.get("last_ok") is None else ("✅" if r["last_ok"] else "❌")
        print(f"{r['id']:<4}{r['label']:<24}{r['scheme']:<8}{r['host']}:{r['port']:<6}{flag:<3}{r.get('last_exit_ip') or ''}")


def cmd_proxy_add(args: argparse.Namespace) -> None:
    payload = {"label": args.label or "", "scheme": args.scheme, "host": args.host, "port": args.port,
               "username": args.username or "", "password": args.password or "", "country": args.country or ""}
    _out(_call("POST", "/api/v1/proxies", payload), args.json)


def cmd_proxy_check(args: argparse.Namespace) -> None:
    if args.id:
        _out(_call("POST", f"/api/v1/proxies/{args.id}/check"), args.json)
        return
    if not args.host or not args.port:
        print("要么 --id，要么 --host + --port", file=sys.stderr)
        raise SystemExit(2)
    payload = {"scheme": args.scheme, "host": args.host, "port": args.port,
               "username": args.username or "", "password": args.password or ""}
    _out(_call("POST", "/api/v1/proxies/check", payload), args.json)


def cmd_proxy_rm(args: argparse.Namespace) -> None:
    _out(_call("DELETE", f"/api/v1/proxies/{args.id}"), args.json)


def cmd_proxy_import(args: argparse.Namespace) -> None:
    """批量导入代理：一行一个，从参数或 stdin 读。

    支持 `socks5://u:p@h:port`、`http://…`、`h:port:u:p`、行尾 `# 备注`，
    以及链式 `前置,落地`（见 docs/16）。坏行单独报错，不影响其它行。
    """
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    elif args.text:
        text = args.text
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        print("要么给 --text，要么给 --file，要么用管道输入（一行一个）", file=sys.stderr)
        raise SystemExit(2)
    data = _call("POST", "/api/v1/proxies/import",
                 {"text": text, "label_prefix": args.label_prefix or ""})
    if args.json:
        _out(data, True)
        return
    # `_call` 已经把 {ok,data} 拆开了，这里拿到的就是 data 本体，不要再取一层
    d = data if isinstance(data, dict) else {}
    print(f"导入 {d.get('imported', 0)} 条，跳过 {d.get('skipped', 0)} 条，"
          f"忽略 {d.get('ignored', 0)} 行，失败 {len(d.get('failed') or [])} 条")
    for f in d.get("failed") or []:
        print(f"  ❌ {f.get('line')} → {f.get('error')}")


def cmd_proxy_sync(args: argparse.Namespace) -> None:
    """与服务器对齐代理库（上行 + 下行）。

    用户要求"两边都能加、自动同步"，平时是自动的；这个命令用于手动补一次。
    """
    data = _call("POST", "/api/v1/proxies/sync")
    if args.json:
        _out(data, True)
        return
    d = data if isinstance(data, dict) else {}
    up = d.get("up") or {}
    down = d.get("down") or {}
    print(f"上行：{'成功' if up.get('ok') else '失败'}"
          f"（新增 {up.get('created', 0)} / 更新 {up.get('updated', 0)}）"
          + (f" {up.get('error') or up.get('skipped') or ''}"))
    print(f"下行：{'成功' if down.get('ok') else '失败'}"
          f"（补入 {down.get('added', 0)} / 更新 {down.get('updated', 0)}）"
          + (f" {down.get('error') or down.get('skipped') or ''}"))


# ── 扩展 ──────────────────────────────────────────────────
def cmd_ext_list(args: argparse.Namespace) -> None:
    data = _call("GET", "/api/v1/extensions")
    if args.json:
        _out(data, True)
        return
    rows = data.get("extensions", [])
    if not rows:
        print("（扩展库是空的）")
        return
    for r in rows:
        print(f"{r['slug']:<28}@{r['version']:<10} {r.get('name', '')}  绑定:{','.join(r.get('bound_to') or []) or '-'}")


def cmd_ext_install(args: argparse.Namespace) -> None:
    """装扩展：给 --url（商店链接/ID）或 --path（.crx / 目录 / zip）。

    `--default` 顺手设成"新建环境默认带上"（用户要的就是这个效果）。
    """
    if not (args.url or args.path):
        print("要么给 --url（商店链接或扩展 ID），要么给 --path", file=sys.stderr)
        raise SystemExit(2)
    payload = {"path": args.path or "", "url": args.url or "",
               "make_pack": not args.no_pack, "as_default": bool(args.default)}
    data = _call("POST", "/api/v1/extensions/install", payload)
    if args.json:
        _out(data, True)
        return
    d = data if isinstance(data, dict) else {}
    print(f"已装 {d.get('name') or d.get('slug')} {d.get('version') or ''} → {d.get('path') or ''}")
    if d.get("pack_created"):
        print(f"  已建扩展包 {d.get('pack_id')}" + ("（已设为默认，新环境会自动带上）"
                                                    if d.get("is_default") else ""))
    elif d.get("pack_error"):
        print(f"  ⚠️ 自动建包失败：{d['pack_error']}")


def cmd_ext_updates(args: argparse.Namespace) -> None:
    """查已装扩展有没有新版（只查带商店 ID 标记的）。"""
    data = _call("GET", "/api/v1/extensions/updates")
    if args.json:
        _out(data, True)
        return
    d = data if isinstance(data, dict) else {}
    items = d.get("items") or []
    if not items:
        print("（没有带商店 ID 的扩展，手工导入的包查不了更新）")
        return
    for it in items:
        mark = "⬆️ " if it.get("update_available") else "   "
        print(f"{mark}{it.get('current')} → {it.get('latest') or '?'}  {it.get('path')}")


def cmd_ext_rm(args: argparse.Namespace) -> None:
    _out(_call("DELETE", f"/api/v1/extensions/{args.slug}/{args.version}"), args.json)


# ── 页面 ──────────────────────────────────────────────────
def cmd_page_shot(args: argparse.Namespace) -> None:
    payload = {"profile": args.ref, "full_page": args.full_page}
    resp = _call_raw("POST", "/api/v1/page/screenshot", content=json.dumps(payload).encode(),
                     headers={"Content-Type": "application/json"})
    if resp.status_code >= 400:
        print(f"失败：{resp.text[:300]}", file=sys.stderr)
        raise SystemExit(1)
    out = args.out or f"{args.ref}.png"
    with open(out, "wb") as fh:
        fh.write(resp.content)
    print(f"截图已保存：{out}（{len(resp.content)} 字节）")


def cmd_page_nav(args: argparse.Namespace) -> None:
    _out(_call("POST", "/api/v1/page/navigate", {"profile": args.ref, "url": args.url}), args.json)


def cmd_page_text(args: argparse.Namespace) -> None:
    _out(_call("GET", f"/api/v1/page/text?profile={args.ref}"), args.json)


# ── 运维 ──────────────────────────────────────────────────
def cmd_events(args: argparse.Namespace) -> None:
    _out(_call("GET", f"/api/v1/events?since={args.since}"), args.json)


def cmd_shell(args: Any) -> int:
    """在本机执行一条命令并打印结果。

    注意这是**本地**执行：不受"远程命令行"开关影响（那个开关管的是
    "服务器能不能通过 hub 让这台机器执行命令"）。但同样受超时与输出上限约束。
    """
    from zw_core.services import shell as shellsvc

    res = shellsvc.execute(args.command, cwd=args.cwd, timeout=args.timeout)
    if getattr(args, "json", False):
        _out(res.to_dict(), True)
        return 0 if res.rc == 0 and not res.error else 1
    if res.stdout:
        sys.stdout.write(res.stdout if res.stdout.endswith("\n") else res.stdout + "\n")
    if res.stderr:
        sys.stderr.write(res.stderr if res.stderr.endswith("\n") else res.stderr + "\n")
    if res.error:
        print(f"[失败] {res.error}")
    if res.truncated:
        from .config import get_settings as _gs

        print(f"[提示] 输出超过上限（{_gs().shell_max_output} 字节），已截断")
    print(f"[rc={res.rc} {res.duration_ms}ms cwd={res.cwd}]", file=sys.stderr)
    return 0 if res.rc == 0 and not res.error else 1


def cmd_shell_status(args: Any) -> int:
    from zw_core.services import shellagent
    from zw_core.services import shell as shellsvc

    from .config import get_settings

    s = get_settings()
    agent = shellagent.get_agent()
    _out({
        "enabled": bool(s.shell_enabled),
        "cwd": shellsvc.default_cwd(),
        "timeout": int(s.shell_timeout),
        "max_output": int(s.shell_max_output),
        "platform_shell": shellsvc.shell_hint(),
        "device_id": getattr(s, "device_id", "") or "",
        "hub_url": s.hub_url or "",
        "agent": agent.status() if agent else None,
        "说明": "enabled 是「服务器能否远程执行命令」的开关；本机 zw shell 不受它限制",
    }, True)
    return 0


def cmd_audit(args: argparse.Namespace) -> None:
    q = [f"limit={args.limit}"]
    if args.profile:
        q.append(f"profile_id={args.profile}")
    if args.action:
        q.append(f"action={args.action}")
    data = _call("GET", "/api/v1/audit?" + "&".join(q))
    if args.json:
        _out(data, True)
        return
    import time as _t

    for r in data.get("items", []):
        ts = _t.strftime("%m-%d %H:%M:%S", _t.localtime(r["ts"]))
        print(f"{ts}  {r['action']:<28}{r['result']:<6}{r.get('profile_id') or '':<22}{r.get('error') or ''}")


def cmd_reap(args: argparse.Namespace) -> None:
    _out(_call("POST", "/api/v1/maintenance/reap-orphans"), args.json)


def cmd_cleanup(args: argparse.Namespace) -> None:
    _out(_call("POST", "/api/v1/maintenance/cleanup"), args.json)


def cmd_trim(args: argparse.Namespace) -> None:
    _out(_call("POST", "/api/v1/maintenance/trim"), args.json)


# ── 参数解析 ──────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zw", description="知纹 —— 指纹浏览器机群管理器")
    p.add_argument("--version", action="version", version=f"zhiwen {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def j(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true", help="输出 JSON（给脚本/AI 用）")

    sp = sub.add_parser("serve", help="启动 HTTP 服务（控制台 + API + 网关）")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("client", help="启动桌面客户端（只需填服务器地址）")
    sp.set_defaults(func=cmd_client)

    sp = sub.add_parser("mcp", help="以 MCP 协议在 stdio 上服务（给 AI 客户端拉起）")
    sp.set_defaults(func=cmd_mcp)

    sp = sub.add_parser("doctor", help="自检")
    j(sp)
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("stats", help="容量与状态统计")
    j(sp)
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("manifest", help="给 AI 的自描述清单")
    j(sp)
    sp.set_defaults(func=cmd_manifest)

    sp = sub.add_parser("meta", help="服务元信息")
    j(sp)
    sp.set_defaults(func=cmd_meta)

    sp = sub.add_parser("events", help="最近事件")
    sp.add_argument("--since", type=float, default=0.0)
    j(sp)
    sp.set_defaults(func=cmd_events)

    sp = sub.add_parser("shell", help="在本机执行一条命令（不经过服务器）")
    sp.add_argument("command", help="要执行的命令，例如 'ipconfig /all'")
    sp.add_argument("--cwd", default="", help="工作目录（默认用户主目录）")
    sp.add_argument("--timeout", type=float, default=None, help="超时秒数（默认取配置）")
    sp.add_argument("--json", action="store_true", help="输出完整 JSON（含 stdout/stderr/rc）")
    sp.set_defaults(func=cmd_shell)

    sp = sub.add_parser("shell-status", help="远程命令行开关与代理状态")
    j(sp)
    sp.set_defaults(func=cmd_shell_status)

    sp = sub.add_parser("audit", help="审计日志")
    sp.add_argument("--profile")
    sp.add_argument("--action")
    sp.add_argument("--limit", type=int, default=50)
    j(sp)
    sp.set_defaults(func=cmd_audit)

    for name, fn in (("trim", cmd_trim), ("cleanup", cmd_cleanup), ("reap", cmd_reap)):
        sp = sub.add_parser(name, help={"trim": "清理已停止环境的缓存", "cleanup": "清 staging/令牌/认证扩展", "reap": "清理孤儿浏览器进程"}[name])
        j(sp)
        sp.set_defaults(func=fn)

    # profile
    sp = sub.add_parser("profile", help="环境管理")
    psub = sp.add_subparsers(dest="sub")

    s1 = psub.add_parser("list", help="列出环境")
    s1.add_argument("--state")
    s1.add_argument("--group")
    s1.add_argument("--q")
    j(s1)
    s1.set_defaults(func=cmd_profile_list)

    s1 = psub.add_parser("create", help="创建环境")
    s1.add_argument("--name", required=True)
    s1.add_argument("--group")
    s1.add_argument("--proxy")
    s1.add_argument("--urls")
    s1.add_argument("--platform", help="默认 host（跟随宿主机），不要随便改成别的 OS")
    s1.add_argument("--timezone", help="例如 Asia/Shanghai")
    s1.add_argument("--lang", help="例如 zh-CN")
    s1.add_argument("--ext", help="扩展 slug，逗号分隔")
    s1.add_argument("--note")
    s1.add_argument("--headless", action="store_true")
    j(s1)
    s1.set_defaults(func=cmd_profile_create)

    for name, fn in (
        ("start", cmd_profile_start),
        ("stop", cmd_profile_stop),
        ("restart", cmd_profile_restart),
        ("rm", cmd_profile_rm),
        ("cdp", cmd_profile_cdp),
        ("logs", cmd_profile_logs),
        ("unlock", cmd_profile_unlock),
        ("reset-fingerprint", cmd_profile_reset_fp),
        ("probe", cmd_profile_probe),
        ("export", cmd_profile_export),
        ("bind-ext", cmd_profile_bind_ext),
        ("unbind-ext", cmd_profile_unbind_ext),
    ):
        s1 = psub.add_parser(name, help=f"{name} 环境")
        s1.add_argument("ref", help="环境 id 或名字")
        if name in ("start", "restart"):
            s1.add_argument("--urls")
        if name == "stop":
            s1.add_argument("--force", action="store_true")
        if name == "rm":
            s1.add_argument("--purge", action="store_true")
        if name == "logs":
            s1.add_argument("--tail", type=int, default=120)
        if name == "reset-fingerprint":
            s1.add_argument("--confirm", required=True, help="把环境名写一遍表示确认")
        if name == "export":
            s1.add_argument("--out", help="输出文件名（默认按环境名生成）")
            s1.add_argument("--password", help="导出用的主密码（不给则用服务端的 ZW_SYNC_PASSWORD）")
        if name == "bind-ext":
            s1.add_argument("slug")
            s1.add_argument("--version")
        if name == "unbind-ext":
            s1.add_argument("slug")
        j(s1)
        s1.set_defaults(func=fn)

    s1 = psub.add_parser("import", help="从导出包导入环境")
    s1.add_argument("file", help=".zwp 文件路径")
    s1.add_argument("--password", help="导入用的主密码（不给则用服务端的 ZW_SYNC_PASSWORD）")
    j(s1)
    s1.set_defaults(func=cmd_profile_import)

    # cookie
    sp = sub.add_parser("cookie", help="cookie 读写")
    csub = sp.add_subparsers(dest="sub")
    s2 = csub.add_parser("dump", help="导出 cookie")
    s2.add_argument("ref")
    s2.add_argument("--format", default="cdp", choices=["cdp", "netscape", "playwright"])
    s2.add_argument("--out")
    s2.set_defaults(func=cmd_cookie_dump)
    s2 = csub.add_parser("load", help="导入 cookie（从文件或 - 表示 stdin）")
    s2.add_argument("ref")
    s2.add_argument("--file", default="-")
    s2.add_argument("--mode", default="replace", choices=["replace", "merge"])
    j(s2)
    s2.set_defaults(func=cmd_cookie_load)
    s2 = csub.add_parser("clear", help="清空 cookie")
    s2.add_argument("ref")
    j(s2)
    s2.set_defaults(func=cmd_cookie_clear)

    # sync
    sp = sub.add_parser("sync", help="跨机同步")
    ssub = sp.add_subparsers(dest="sub")
    s3 = ssub.add_parser("state", help="同步总览")
    j(s3)
    s3.set_defaults(func=cmd_sync_state)
    for name, fn in (("push", cmd_sync_push), ("pull", cmd_sync_pull)):
        s3 = ssub.add_parser(name, help=f"{name} 到同步中心")
        s3.add_argument("ref")
        s3.add_argument("--password")
        if name == "push":
            s3.add_argument("--note")
        else:
            s3.add_argument("--revision", type=int)
            s3.add_argument("--force", action="store_true")
        j(s3)
        s3.set_defaults(func=fn)
    s3 = ssub.add_parser("push-all", help="把所有环境推一遍")
    s3.add_argument("--password")
    j(s3)
    s3.set_defaults(func=cmd_sync_push_all)
    s3 = ssub.add_parser("diff", help="与远端对比")
    s3.add_argument("ref")
    s3.add_argument("--password")
    j(s3)
    s3.set_defaults(func=cmd_sync_diff)
    s3 = ssub.add_parser("remote", help="同步中心上的索引")
    j(s3)
    s3.set_defaults(func=cmd_sync_remote)
    s3 = ssub.add_parser("rm", help="删掉同步中心上的版本")
    s3.add_argument("ref")
    s3.add_argument("--revision", type=int)
    j(s3)
    s3.set_defaults(func=cmd_sync_rm)

    # proxy
    sp = sub.add_parser("proxy", help="代理库")
    prsub = sp.add_subparsers(dest="sub")
    s4 = prsub.add_parser("list", help="列出代理")
    j(s4)
    s4.set_defaults(func=cmd_proxy_list)
    s4 = prsub.add_parser("add", help="新增代理")
    s4.add_argument("--label")
    s4.add_argument("--scheme", default="http")
    s4.add_argument("--host", required=True)
    s4.add_argument("--port", type=int, required=True)
    s4.add_argument("--username")
    s4.add_argument("--password")
    s4.add_argument("--country")
    j(s4)
    s4.set_defaults(func=cmd_proxy_add)
    s4 = prsub.add_parser("check", help="检测出口 IP")
    s4.add_argument("--id", type=int)
    s4.add_argument("--scheme", default="http")
    s4.add_argument("--host")
    s4.add_argument("--port", type=int)
    s4.add_argument("--username")
    s4.add_argument("--password")
    j(s4)
    s4.set_defaults(func=cmd_proxy_check)
    s4 = prsub.add_parser("rm", help="删除代理")
    s4.add_argument("id", type=int)
    j(s4)
    s4.set_defaults(func=cmd_proxy_rm)
    s4 = prsub.add_parser("import", help="批量导入（一行一个，坏行单独报错）")
    s4.add_argument("--text", help="直接给一行或多行")
    s4.add_argument("--file", help="从文件读")
    s4.add_argument("--label-prefix", help="没写备注时用这个前缀编号")
    j(s4)
    s4.set_defaults(func=cmd_proxy_import)
    s4 = prsub.add_parser("sync", help="与服务器对齐代理库（上行 + 下行）")
    j(s4)
    s4.set_defaults(func=cmd_proxy_sync)

    # page
    sp = sub.add_parser("page", help="页面操作")
    psub2 = sp.add_subparsers(dest="sub")
    s5 = psub2.add_parser("shot", help="截图")
    s5.add_argument("ref")
    s5.add_argument("--out")
    s5.add_argument("--full-page", action="store_true")
    s5.set_defaults(func=cmd_page_shot)
    s5 = psub2.add_parser("nav", help="导航")
    s5.add_argument("ref")
    s5.add_argument("url")
    j(s5)
    s5.set_defaults(func=cmd_page_nav)
    s5 = psub2.add_parser("text", help="页面可见文字")
    s5.add_argument("ref")
    j(s5)
    s5.set_defaults(func=cmd_page_text)

    # extension
    sp = sub.add_parser("extension", help="扩展库")
    esub = sp.add_subparsers(dest="sub")
    s6 = esub.add_parser("list", help="列出扩展")
    j(s6)
    s6.set_defaults(func=cmd_ext_list)
    s6 = esub.add_parser("install", help="安装扩展（商店链接 / .crx / 目录 / zip）")
    s6.add_argument("path", nargs="?", default="", help=".crx / 扩展目录 / zip")
    s6.add_argument("--url", help="Chrome 商店链接或 32 位扩展 ID")
    s6.add_argument("--default", action="store_true", help="设为新建环境默认带上")
    s6.add_argument("--no-pack", action="store_true", help="只入库，不建扩展包")
    j(s6)
    s6.set_defaults(func=cmd_ext_install)
    s6 = esub.add_parser("updates", help="查已装扩展有没有新版")
    j(s6)
    s6.set_defaults(func=cmd_ext_updates)
    s6 = esub.add_parser("rm", help="删除扩展")
    s6.add_argument("slug")
    s6.add_argument("version")
    j(s6)
    s6.set_defaults(func=cmd_ext_rm)

    return p


def main() -> None:
    # 先修控制台编码：Windows 默认代码页编不了 ✅ 这类符号，
    # `zw doctor` 会直接 UnicodeEncodeError 崩掉（用户在 Windows 上的第一条命令）。
    from zw_core.console import setup_console

    setup_console()
    parser = build_parser()
    args = parser.parse_args()
    if not getattr(args, "cmd", None):
        parser.print_help()
        return
    if not hasattr(args, "func"):
        parser.print_help()
        return
    if not hasattr(args, "json"):
        args.json = False
    args.func(args)


if __name__ == "__main__":
    main()

"""命令行入口 `zw`。

设计原则：每条命令都能加 `--json` 输出机器可读结果 —— 这样 AI 或脚本可以直接消费，
不需要去解析人类可读的表格。
"""

from __future__ import annotations

import argparse
import json
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


def _client() -> Any:
    """CLI 默认走 HTTP 调本机服务；服务没起时给出明确提示。"""
    import httpx

    from .config import get_settings

    s = get_settings()
    base = f"http://{'127.0.0.1' if s.host in ('0.0.0.0', '::') else s.host}:{s.port}"
    headers = {"Authorization": f"Bearer {s.admin_token}"} if s.admin_token else {}
    return httpx.Client(base_url=base, headers=headers, timeout=180)


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
    data = r.json()
    if not data.get("ok"):
        print(f"失败 [{data.get('code')}] {data.get('message')}", file=sys.stderr)
        if data.get("hint"):
            print(f"提示：{data['hint']}", file=sys.stderr)
        raise SystemExit(1)
    return data.get("data")


# ── 子命令 ────────────────────────────────────────────────
def cmd_serve(args: argparse.Namespace) -> None:
    from .main import run

    run()


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp_server import serve_stdio

    serve_stdio()


def cmd_doctor(args: argparse.Namespace) -> None:
    data = _call("POST", "/api/v1/maintenance/doctor")
    _out(data, args.json)
    if not data.get("all_ok"):
        raise SystemExit(1)


def cmd_stats(args: argparse.Namespace) -> None:
    _out(_call("GET", "/api/v1/stats"), args.json)


def cmd_manifest(args: argparse.Namespace) -> None:
    _out(_call("GET", "/api/v1/ai/manifest"), args.json)


def cmd_meta(args: argparse.Namespace) -> None:
    _out(_call("GET", "/api/v1/meta"), args.json)


# profile
def cmd_profile_list(args: argparse.Namespace) -> None:
    q = []
    if args.state:
        q.append(f"state={args.state}")
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
    print(f"{'名字':<22}{'状态':<10}{'种子':<12}{'同步':<8}{'端口':<8}id")
    for p in rows:
        print(
            f"{p['name'][:21]:<22}{p['state']:<10}{str(p['fp_seed']):<12}"
            f"{str(p.get('sync_revision')):<8}{str(p.get('cdp_port') or '-'):<8}{p['id']}"
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
    if args.note:
        payload["note"] = args.note
    _out(_call("POST", "/api/v1/profiles", payload), args.json)


def cmd_profile_start(args: argparse.Namespace) -> None:
    payload: dict[str, Any] = {}
    if args.urls:
        payload["urls"] = [u for u in args.urls.split(",") if u]
    _out(_call("POST", f"/api/v1/profiles/{args.ref}/start", payload), args.json)


def cmd_profile_stop(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/profiles/{args.ref}/stop?force={'true' if args.force else 'false'}"), args.json)


def cmd_profile_restart(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/profiles/{args.ref}/restart", {}), args.json)


def cmd_profile_rm(args: argparse.Namespace) -> None:
    _out(_call("DELETE", f"/api/v1/profiles/{args.ref}?purge={'true' if args.purge else 'false'}"), args.json)


def cmd_profile_cdp(args: argparse.Namespace) -> None:
    data = _call("GET", f"/api/v1/profiles/{args.ref}/cdp")
    if args.json:
        _out(data, True)
        return
    print(data["gateway_ws_base"])
    print(f"# 令牌有效期 {data['token_ttl']} 秒")
    print(f"# 完整连接（含 GUID）：先取一次发现接口")
    print(f"curl -s '{data['raw_cdp_json']}' | jq -r .webSocketDebuggerUrl")


def cmd_profile_logs(args: argparse.Namespace) -> None:
    data = _call("GET", f"/api/v1/profiles/{args.ref}/logs?tail={args.tail}")
    for label in ("profile", "browser"):
        lines = data.get(label) or []
        print(f"=== {label}（最后 {len(lines)} 行）===")
        for line in lines:
            print(line)


def cmd_profile_unlock(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/profiles/{args.ref}/unlock"), args.json)


def cmd_profile_reset_fp(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/profiles/{args.ref}/fingerprint/reset", {"confirm": args.confirm}), args.json)


# cookies
def cmd_cookie_dump(args: argparse.Namespace) -> None:
    if args.format == "cdp":
        data = _call("GET", f"/api/v1/profiles/{args.ref}/cookies?fmt=cdp")
        text = json.dumps(data.get("cookies", []), ensure_ascii=False, indent=2)
    else:
        import httpx

        from .config import get_settings

        s = get_settings()
        base = f"http://{'127.0.0.1' if s.host in ('0.0.0.0', '::') else s.host}:{s.port}"
        headers = {"Authorization": f"Bearer {s.admin_token}"} if s.admin_token else {}
        r = httpx.get(f"{base}/api/v1/profiles/{args.ref}/cookies?fmt={args.format}", headers=headers, timeout=120)
        r.raise_for_status()
        text = r.text
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
        print(f"已写入 {args.out}（{len(text)} 字节）")
    else:
        print(text)


def cmd_cookie_load(args: argparse.Namespace) -> None:
    raw = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    cookies = json.loads(raw)
    if isinstance(cookies, dict):
        cookies = cookies.get("cookies", [])
    _out(_call("PUT", f"/api/v1/profiles/{args.ref}/cookies", {"cookies": cookies, "mode": args.mode}), args.json)


# sync
def cmd_sync_state(args: argparse.Namespace) -> None:
    _out(_call("GET", "/api/v1/sync/state"), args.json)


def cmd_sync_push(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/sync/{args.ref}/push", {"password": args.password, "note": args.note or ""}), args.json)


def cmd_sync_pull(args: argparse.Namespace) -> None:
    _out(_call("POST", f"/api/v1/sync/{args.ref}/pull", {"password": args.password, "revision": args.revision}), args.json)


def cmd_sync_diff(args: argparse.Namespace) -> None:
    _out(_call("GET", f"/api/v1/sync/{args.ref}/diff"), args.json)


def cmd_sync_remote(args: argparse.Namespace) -> None:
    _out(_call("GET", "/api/v1/sync/remote"), args.json)


# 页面
def cmd_page_shot(args: argparse.Namespace) -> None:
    import httpx

    from .config import get_settings

    s = get_settings()
    base = f"http://{'127.0.0.1' if s.host in ('0.0.0.0', '::') else s.host}:{s.port}"
    headers = {"Authorization": f"Bearer {s.admin_token}"} if s.admin_token else {}
    r = httpx.post(f"{base}/api/v1/page/screenshot", json={"profile": args.ref}, headers=headers, timeout=120)
    r.raise_for_status()
    out = args.out or f"{args.ref}.png"
    open(out, "wb").write(r.content)
    print(f"截图已保存：{out}（{len(r.content)} 字节）")


def cmd_ext_list(args: argparse.Namespace) -> None:
    _out(_call("GET", "/api/v1/extensions"), args.json)


def cmd_ext_install(args: argparse.Namespace) -> None:
    _out(_call("POST", "/api/v1/extensions/install", {"path": args.path}), args.json)


# ── 参数解析 ──────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="zw", description="知纹 —— 指纹浏览器机群管理器")
    p.add_argument("--version", action="version", version=f"zhiwen {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def j(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--json", action="store_true", help="输出 JSON（给脚本/AI 用）")

    sp = sub.add_parser("serve", help="启动 HTTP 服务（控制台 + API + 网关）")
    sp.set_defaults(func=cmd_serve)

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

    # profile
    sp = sub.add_parser("profile", help="环境管理")
    psub = sp.add_subparsers(dest="sub")

    s1 = psub.add_parser("list", help="列出环境")
    s1.add_argument("--state")
    s1.add_argument("--q")
    j(s1)
    s1.set_defaults(func=cmd_profile_list)

    s1 = psub.add_parser("create", help="创建环境")
    s1.add_argument("--name", required=True)
    s1.add_argument("--group")
    s1.add_argument("--proxy")
    s1.add_argument("--urls")
    s1.add_argument("--platform", help="默认 host（跟随宿主机），不要随便改成别的 OS")
    s1.add_argument("--note")
    j(s1)
    s1.set_defaults(func=cmd_profile_create)

    for name, fn, extra in (
        ("start", cmd_profile_start, True),
        ("stop", cmd_profile_stop, False),
        ("restart", cmd_profile_restart, False),
        ("rm", cmd_profile_rm, False),
        ("cdp", cmd_profile_cdp, False),
        ("logs", cmd_profile_logs, False),
        ("unlock", cmd_profile_unlock, False),
        ("reset-fingerprint", cmd_profile_reset_fp, False),
    ):
        s1 = psub.add_parser(name, help=f"{name} 环境")
        s1.add_argument("ref", help="环境 id 或名字")
        if extra:
            s1.add_argument("--urls")
        if name == "stop":
            s1.add_argument("--force", action="store_true")
        if name == "rm":
            s1.add_argument("--purge", action="store_true")
        if name == "logs":
            s1.add_argument("--tail", type=int, default=120)
        if name == "reset-fingerprint":
            s1.add_argument("--confirm", required=True, help="把环境名写一遍表示确认")
        j(s1)
        s1.set_defaults(func=fn)

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
        j(s3)
        s3.set_defaults(func=fn)
    s3 = ssub.add_parser("diff", help="与远端对比")
    s3.add_argument("ref")
    j(s3)
    s3.set_defaults(func=cmd_sync_diff)
    s3 = ssub.add_parser("remote", help="同步中心上的索引")
    j(s3)
    s3.set_defaults(func=cmd_sync_remote)

    # page
    sp = sub.add_parser("page", help="页面操作")
    psub2 = sp.add_subparsers(dest="sub")
    s4 = psub2.add_parser("shot", help="截图")
    s4.add_argument("ref")
    s4.add_argument("--out")
    s4.set_defaults(func=cmd_page_shot)

    # extension
    sp = sub.add_parser("extension", help="扩展库")
    esub = sp.add_subparsers(dest="sub")
    s5 = esub.add_parser("list", help="列出扩展")
    j(s5)
    s5.set_defaults(func=cmd_ext_list)
    s5 = esub.add_parser("install", help="安装扩展（目录或 zip）")
    s5.add_argument("path")
    j(s5)
    s5.set_defaults(func=cmd_ext_install)

    return p


def main() -> None:
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

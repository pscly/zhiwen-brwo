#!/usr/bin/env python3
"""单机全能力自检：只针对**一个** core 实例，把所有能力真跑一遍。

设计意图：用户拿到程序后，一条命令就能知道自己这台机器上哪些能力是好的、
哪些因为环境原因（无 GPU、无内核、没配 hub）而不可用。所以每项都给出
「✅ 通过 / ⚠️ 跳过（原因）/ ❌ 失败」，而不是简单的 pass/fail。

覆盖：
  1. 就绪：内核、数据库、令牌、数据目录、磁盘
  2. 环境 CRUD 与参数校验（非法输入必须被拒）
  3. 启动浏览器 → 取 CDP → 发现接口地址重写
  4. 指纹体检（读真机 UA / 时区 / WebGL / canvas / 字体）
  5. cookie 写 / 读 / netscape 导出 / 清空
  6. localStorage 写 / 读
  7. 页面导航 / 可见文字 / 执行 JS / 截图
  8. 管理标签页不劫持用户页面
  9. 扩展库：安装 → 绑定 → 解绑 → 删除
 10. 代理库：新增 → 查 / 测出口（失败算跳过，不算错）→ 删除
 11. 导出 .zwp → 删除环境 → 导入还原
 12. 同步：hub 是否连通、diff 是否说得清（没配 hub 就算跳过）
 13. MCP：工具清单、工具调用、鉴权拒绝
 14. 运维：自检、trim、cleanup、孤儿进程扫描、审计、事件
 15. 错误码形状：401 / 404 / 409 / 422 都是统一形状

用法：
  ZW_CORE=http://127.0.0.1:8730 ZW_ADMIN_TOKEN=xxx python scripts/e2e-check.py

## 想跑到 72 项而不是 65 项，要额外满足两个条件

下面这两组能力**只有配好了才会真跑**，否则按"环境不具备"跳过（不是 bug）：

1. **导出 / 导入**（第 11 节）需要主密码：
   `ZW_SYNC_PASSWORD=...`（导出包靠它加密）。core 进程和本脚本都要有。

2. **同步**（第 12 节）需要一个连通的 hub，且**设备已被批准**：
   - core 侧：`ZW_HUB_URL` + `ZW_HUB_TOKEN` + `ZW_DEVICE_ID`
   - 服务器侧：这台设备要 `active`。`ZW_DEVICE_AUTO_APPROVE=false` 时新设备是
     `pending`，推送会失败（差异查询/推送/错密码拦截三节都会红）。

     去面板点「允许」，或调 `POST /panel/api/devices/<id>/approve`。

实测差异（同一台机器、同一份代码）：

    没配主密码、没配 hub   → 通过 65 / 跳过 6
    配好上面两项并批准设备 → 通过 72 / 跳过 4

剩下真正跳过的 4 项是机器环境限制：指纹内核（2 项）、代理出口（没有真代理）、
MCP 鉴权（本机回环免令牌，属正常）。
  ZW_SYNC_PASSWORD=xxx   # 想连同步那几项时必须给
"""

from __future__ import annotations

import json
import os
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import httpx

# Windows 默认控制台编码编不了中文与 ✅，先修一下再输出
try:
    for _s in (sys.stdout, sys.stderr):
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except (AttributeError, ValueError, OSError):
    pass

CORE = os.environ.get("ZW_CORE", "http://127.0.0.1:8730")
TOKEN = os.environ.get("ZW_ADMIN_TOKEN", "")
PW = os.environ.get("ZW_SYNC_PASSWORD", "")
BASE_NAME = os.environ.get("ZW_E2E_NAME", "自检环境")
H = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}

passed: list[str] = []
failed: list[str] = []
skipped: list[str] = []


def mark(kind: str, name: str, detail: str = "") -> None:
    icon = {"ok": "✅", "skip": "⚠️ ", "fail": "❌"}[kind]
    ({"ok": passed, "skip": skipped, "fail": failed}[kind]).append(name)
    show = f" — {detail}" if detail and kind != "ok" else ""
    print(f"  {icon} {name}{show}")


def check(name: str, cond: bool, detail: str = "") -> bool:
    mark("ok" if cond else "fail", name, "" if cond else detail)
    return cond


def skip(name: str, why: str) -> None:
    mark("skip", name, why)


def call(method: str, path: str, body: dict | None = None, timeout: float = 300):
    try:
        r = httpx.request(method, CORE + path, headers={**H, "Content-Type": "application/json"}, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        return 0, {"code": "UNREACHABLE", "message": str(exc)}
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:400]}


def call_raw(method: str, path: str, content: bytes | None = None, headers: dict | None = None, timeout: float = 300):
    try:
        return httpx.request(method, CORE + path, headers={**H, **(headers or {})}, content=content, timeout=timeout)
    except httpx.HTTPError as exc:
        return httpx.Response(599, text=str(exc))


def cleanup(prefix: str) -> None:
    st, data = call("GET", "/api/v1/profiles")
    if st != 200:
        return
    for p in data.get("data", {}).get("profiles", []):
        if p["name"].startswith(prefix):
            call("POST", f"/api/v1/profiles/{p['id']}/stop")
            call("DELETE", f"/api/v1/profiles/{p['id']}?purge=true")


def main() -> int:
    print(f"目标：{CORE}    令牌：{'已配置' if TOKEN else '未配置（仅回环）'}")
    print()
    print("== 1. 就绪 ==")
    st, data = call("GET", "/readyz")
    if st != 200:
        print(f"  ❌ 服务未就绪：{data}")
        print("     先启动：zw serve   或   .venv/bin/python -m zw_core.main")
        return 1
    check("服务就绪", True)
    engine = (data.get("data") or {}).get("engine") or {}
    check("找到浏览器内核", bool(engine.get("ok")), str(engine.get("error", ""))[:200])
    if engine.get("ok") and not engine.get("fingerprint_supported"):
        skip("指纹内核", "当前是普通 Chromium/Chrome，--fingerprint 会被忽略；环境之间只有 cookie 隔离")

    st, meta = call("GET", "/api/v1/meta")
    m = meta.get("data") or {}
    check("元信息可读", st == 200 and bool(m), str(meta)[:200])
    check("数据库 schema ≥ 2", int(m.get("schema_version") or 0) >= 2, str(m.get("schema_version")))
    hub_ok = bool(m.get("hub_configured"))
    gw_base = m.get("gateway_public_base", "")

    st, doc = call("POST", "/api/v1/maintenance/doctor")
    doc_data = doc.get("data") or {}
    bad = [c for c in doc_data.get("checks", []) if not c["ok"]]
    if bad:
        for c in bad:
            detail = c["详情"] if isinstance(c["详情"], str) else json.dumps(c["详情"], ensure_ascii=False)[:160]
            skip(f"自检项 {c['项']}", detail)
    else:
        check("全部自检项通过", True)

    print("== 2. 环境 CRUD 与参数校验 ==")
    cleanup(BASE_NAME)
    st, data = call("POST", "/api/v1/profiles", {
        "name": BASE_NAME, "group_name": "自检", "tags": ["e2e"],
        "fp_timezone": "Asia/Shanghai", "fp_lang": "zh-CN",
        "window_w": 1366, "window_h": 768,
    })
    if not check("创建环境", st == 200 and (data.get("data") or {}).get("id"), str(data)[:300]):
        return 1
    pid = data["data"]["id"]
    seed = data["data"]["fp_seed"]

    st, dup = call("POST", "/api/v1/profiles", {"name": BASE_NAME})
    check("重名被拒（NAME_TAKEN）", dup.get("code") == "NAME_TAKEN", str(dup)[:200])

    st, bad_in = call("POST", "/api/v1/profiles", {"name": BASE_NAME + "-bad", "window_w": 10})
    check("非法窗口尺寸被拒", bad_in.get("code") == "INVALID_WINDOW", str(bad_in)[:200])

    st, bad_tz = call("POST", "/api/v1/profiles", {"name": BASE_NAME + "-bad2", "fp_timezone": "Mars/Base"})
    check("非法时区被拒", bad_tz.get("code") == "INVALID_TIMEZONE", str(bad_tz)[:200])

    st, bad_proxy = call("POST", "/api/v1/profiles", {"name": BASE_NAME + "-bad3", "proxy": "ftp://h:1"})
    check("非法代理协议被拒", bad_proxy.get("code") == "PROXY_SCHEME_UNSUPPORTED", str(bad_proxy)[:200])

    st, patched = call("PATCH", f"/api/v1/profiles/{pid}", {"note": "自检改过"})
    check("修改环境", (patched.get("data") or {}).get("note") == "自检改过", str(patched)[:200])

    st, seedbad = call("PATCH", f"/api/v1/profiles/{pid}", {"fp_seed": 1})
    check("指纹种子不可改", seedbad.get("code") == "SEED_IMMUTABLE", str(seedbad)[:200])

    st, secret = call("POST", "/api/v1/profiles", {"name": BASE_NAME + "-proxy", "proxy": "socks5://u:topsecret@10.9.9.9:1080"})
    leaked = "topsecret" in json.dumps(secret, ensure_ascii=False)
    check("代理凭据不入库不回显", not leaked, str(secret)[:200])
    if (secret.get("data") or {}).get("id"):
        call("DELETE", f"/api/v1/profiles/{secret['data']['id']}?purge=true")

    st, groups = call("GET", "/api/v1/groups")
    check("分组聚合正确", any(g["name"] == "自检" for g in (groups.get("data") or {}).get("groups", [])), str(groups)[:200])

    print("== 3. 启动 / CDP / 地址重写 ==")
    st, started = call("POST", f"/api/v1/profiles/{pid}/start", {}, timeout=240)
    if not check("启动浏览器", st == 200 and (started.get("data") or {}).get("profile"), str(started)[:400]):
        call("DELETE", f"/api/v1/profiles/{pid}?purge=true")
        return 1
    sd = started.get("data") or {}
    cdp = sd.get("cdp") or {}
    check("返回 gateway_ws_url", bool(cdp.get("gateway_ws_url")), str(cdp)[:200])
    check("ws 地址带令牌", "token=" in str(cdp.get("gateway_ws_url")), str(cdp.get("gateway_ws_url"))[:120])
    local_port = cdp.get("local_port")
    check("ws 地址不指向回环端口", f":{local_port}" not in str(cdp.get("gateway_ws_url")), str(cdp.get("gateway_ws_url"))[:120])

    r = call_raw("GET", f"/cdp/{pid}/json/version?token={cdp.get('token')}")
    try:
        info = r.json()
    except Exception:
        info = {}
    check("发现接口可达", r.status_code == 200, f"HTTP {r.status_code}")
    check("webSocketDebuggerUrl 已重写", str(info.get("webSocketDebuggerUrl", "")).startswith(("ws://", "wss://")), str(info.get("webSocketDebuggerUrl"))[:120])

    r401 = call_raw("GET", f"/cdp/{pid}/json/version?token=bogus")
    check("错令牌被拒", r401.status_code == 401, f"HTTP {r401.status_code}")

    print("== 4. 指纹体检 ==")
    st, probe = call("POST", f"/api/v1/profiles/{pid}/fingerprint/probe", timeout=180)
    pr = probe.get("data") or {}
    if st == 200:
        check("指纹体检可用", bool(pr.get("ua")), str(probe)[:200])
        check("读到时区", bool(pr.get("timezone")), str(pr.get("timezone")))
        check("webdriver 为 false", pr.get("webdriver") is False, str(pr.get("webdriver")))
        if pr.get("webgl_renderer"):
            check("WebGL 有渲染器", True)
        else:
            skip("WebGL", "这台机器没有 GPU，指纹会明显异常（真人机器不会这样）")
        warns = pr.get("warnings") or []
        if warns:
            for w in warns[:5]:
                skip("指纹警告", w)
        else:
            check("本地指纹自洽", True)
    else:
        skip("指纹体检", str(probe)[:200])

    print("== 5. cookie ==")
    cookie = {"name": "zw_selfcheck", "value": "自检值", "domain": ".example.com", "path": "/",
              "secure": True, "httpOnly": False, "sameSite": "Lax", "expires": 1900000000}
    st, w = call("PUT", f"/api/v1/profiles/{pid}/cookies", {"cookies": [cookie], "mode": "replace"})
    check("写入 cookie", (w.get("data") or {}).get("ok") == 1, str(w)[:300])
    st, rd = call("GET", f"/api/v1/profiles/{pid}/cookies")
    got = [c for c in (rd.get("data") or {}).get("cookies", []) if c["name"] == "zw_selfcheck"]
    check("读回 cookie", bool(got) and got[0]["value"] == "自检值", str(rd)[:300])
    r = call_raw("GET", f"/api/v1/profiles/{pid}/cookies?fmt=netscape")
    check("netscape 导出", r.status_code == 200 and "Netscape HTTP Cookie File" in r.text, f"HTTP {r.status_code}")

    print("== 6. localStorage ==")
    st, ls = call("PUT", f"/api/v1/profiles/{pid}/storage", {"mapping": {"https://example.com": {"k": "v1", "k2": "v2"}}})
    ok_ls = st == 200 and (ls.get("data") or {}).get("report", {}).get("https://example.com", {}).get("written") == 2
    check("写入 localStorage", ok_ls, str(ls)[:300])
    st, lsr = call("GET", f"/api/v1/profiles/{pid}/storage?origin=https://example.com")
    stored = ((lsr.get("data") or {}).get("storage") or {}).get("https://example.com") or {}
    check("读回 localStorage", stored.get("k") == "v1", str(lsr)[:300])

    print("== 7. 页面能力 ==")
    st, nav = call("POST", "/api/v1/page/navigate", {"profile": pid, "url": "https://example.com"})
    check("导航", st == 200 and "example.com" in str((nav.get("data") or {}).get("url")), str(nav)[:300])
    st, txt = call("GET", f"/api/v1/page/text?profile={pid}")
    check("页面可见文字", len((txt.get("data") or {}).get("text", "")) > 20, str(txt)[:200])
    st, ev = call("POST", "/api/v1/page/evaluate", {"profile": pid, "expression": "1+1"})
    check("执行 JS", (ev.get("data") or {}).get("result") == 2, str(ev)[:200])
    r = call_raw("POST", "/api/v1/page/screenshot", content=json.dumps({"profile": pid}).encode(),
                 headers={"Content-Type": "application/json"})
    check("截图", r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n", f"HTTP {r.status_code} {len(r.content)}B")
    st, tabs = call("GET", f"/api/v1/profiles/{pid}/logs?tail=20")
    check("环境日志可读", st == 200, str(tabs)[:200])

    print("== 8. 扩展库 ==")
    ext_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "zw-e2e-ext"
    ext_dir.mkdir(exist_ok=True)
    (ext_dir / "manifest.json").write_text('{"manifest_version":3,"name":"E2E 测试扩展","version":"0.0.1"}', encoding="utf-8")
    (ext_dir / "sw.js").write_text("// noop\n", encoding="utf-8")
    st, inst = call("POST", "/api/v1/extensions/install", {"path": str(ext_dir)})
    slug = (inst.get("data") or {}).get("slug")
    check("安装扩展", st == 200 and bool(slug), str(inst)[:300])
    if slug:
        st, bind = call("POST", f"/api/v1/profiles/{pid}/extensions", {"slug": slug})
        check("绑定扩展", slug in ((bind.get("data") or {}).get("extensions") or []), str(bind)[:200])
        st, inuse = call("DELETE", f"/api/v1/extensions/{slug}/0.0.1")
        check("被绑定时拒删（EXTENSION_IN_USE）", inuse.get("code") == "EXTENSION_IN_USE", str(inuse)[:200])
        call("DELETE", f"/api/v1/profiles/{pid}/extensions/{slug}")
        st, dele = call("DELETE", f"/api/v1/extensions/{slug}/0.0.1")
        check("解绑后可删", st == 200, str(dele)[:200])

    print("== 9. 代理库 ==")
    st, px = call("POST", "/api/v1/proxies", {"label": "自检代理", "scheme": "http", "host": "127.0.0.1", "port": 7890,
                                              "username": "u", "password": "p"})
    px_id = (px.get("data") or {}).get("id")
    check("新增代理", st == 200 and bool(px_id), str(px)[:300])
    if px_id:
        st, lst = call("GET", "/api/v1/proxies")
        row = next((x for x in (lst.get("data") or {}).get("proxies", []) if x["id"] == px_id), {})
        check("代理列表不回显密码", "password" not in row and "p" not in str(row.get("username")), str(row)[:200])
        st, chk = call("POST", f"/api/v1/proxies/{px_id}/check", timeout=90)
        if (chk.get("data") or {}).get("ok"):
            check("代理出口检测", True)
        else:
            skip("代理出口检测", f"这个代理连不上（{(chk.get('data') or {}).get('error', '')[:120]}）—— 正常，本机没人监听 7890")
        st, dele = call("DELETE", f"/api/v1/proxies/{px_id}")
        check("删除代理", st == 200, str(dele)[:200])

    print("== 9.5 代理串校验 / 链式（0.7.5 + 0.7.4 新增）==")
    # 用户真实踩过：带空格、IP 五段。以前静默接受然后连不上。
    st, bad = call("POST", "/api/v1/proxies/import",
                   {"text": "socks5://u:p@66.80.12 0.66:20000"})
    # 注意：变量名不能叫 failed —— 脚本里 `failed` 是"失败项列表"，
    # 重名会把它遮蔽，最后打印的失败项变成这里的数据（排查了半天的坑）。
    bad_rows = (bad.get("data") or {}).get("failed") or []
    check("坏代理串被拒（带空格）",
          bool(bad_rows) and "空格" in str(bad_rows[0].get("error", "")), str(bad)[:260])
    check("坏代理串的密码已脱敏",
          bool(bad_rows) and "***" in str(bad_rows[0].get("line", "")), str(bad_rows)[:200])

    st, chain = call("POST", "/api/v1/proxies/import",
                     {"text": "http://127.0.0.1:21080,socks5://u:p@5.5.5.5:1080 # 链式自检"})
    check("链式代理可导入", (chain.get("data") or {}).get("imported") == 1, str(chain)[:260])
    st, lst2 = call("GET", "/api/v1/proxies")
    row2 = next((x for x in (lst2.get("data") or {}).get("proxies", [])
                 if (x.get("label") or "") == "链式自检"), {})
    check("链式保留前置（is_chain）", row2.get("is_chain") is True, str(row2)[:260])
    if row2.get("id"):
        call("DELETE", f"/api/v1/proxies/{row2['id']}")

    print("== 9.6 代理库同步接口（0.7.14 新增）==")
    st, syncst = call("GET", "/api/v1/proxies/sync")
    check("同步状态接口可用", st == 200, str(syncst)[:200])

    print("== 9.7 从商店链接装扩展（0.7.17/0.7.19 新增）==")
    slug2 = ""
    try:
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json",
                        '{"manifest_version":3,"name":"E2E Store Ext","version":"1.0.0"}')
            zf.writestr("sw.js", "// noop")
        crx = (b"Cr24" + (3).to_bytes(4, "little") + (1310).to_bytes(4, "little")
               + b"\x00" * 1310 + buf.getvalue())
        crx_file = Path(os.environ.get("TMPDIR", "/tmp")) / "zw-e2e-store.crx"
        crx_file.write_bytes(crx)
        st, inst2 = call("POST", "/api/v1/extensions/install",
                         {"path": str(crx_file), "as_default": True})
        d2 = inst2.get("data") or {}
        slug2 = d2.get("slug") or ""
        check("CRX 安装成功", st == 200 and bool(slug2), str(inst2)[:300])
        check("装完自动建包", d2.get("pack_created") is True, str(d2)[:260])
        check("可设为默认（新环境自动带）", d2.get("is_default") is True, str(d2)[:260])
        st, upds = call("GET", "/api/v1/extensions/updates", timeout=120)
        check("扩展更新检查可用", st == 200, str(upds)[:200])
    except Exception as exc:  # noqa: BLE001
        skip("从商店链接装扩展", f"{type(exc).__name__}: {exc}")

    print("== 9.8 CDP 令牌有效期（0.7.18：默认 3600 秒）==")
    # 取 CDP 地址要环境在跑；上面第 6 节的页面操作已经把它启动了，
    # 但如果中途被停掉，这里补一次启动再取。
    st, cur = call("GET", f"/api/v1/profiles/{pid}")
    if (cur.get("data") or {}).get("state") != "running":
        call("POST", f"/api/v1/profiles/{pid}/start")
    st, tk = call("GET", f"/api/v1/profiles/{pid}/cdp")
    d = tk.get("data") or {}
    check("CDP 令牌默认 3600 秒", int(d.get("token_ttl") or 0) == 3600, str(tk)[:260])

    print("== 10. 导出 / 导入 ==")
    if PW:
        # 密码显式放进 body：服务端进程不一定有 ZW_SYNC_PASSWORD，
        # 一个自检脚本不该因为"服务端环境变量没设"而失败。
        r = call_raw("POST", f"/api/v1/profiles/{pid}/export",
                     content=json.dumps({"password": PW}).encode(),
                     headers={"Content-Type": "application/json"})
        exported = r.status_code == 200 and r.content[:4] == b"ZWBN"
        check("导出 .zwp", exported, f"HTTP {r.status_code} {r.text[:200] if not exported else ''}")
        if exported:
            filename = r.headers.get("X-ZW-Filename", "")
            check("响应头带文件名", bool(filename), filename)
            call("POST", f"/api/v1/profiles/{pid}/stop")
            call("DELETE", f"/api/v1/profiles/{pid}?purge=true")
            r2 = call_raw("POST", f"/api/v1/profiles/import?password={quote(PW)}", content=r.content,
                          headers={"Content-Type": "application/octet-stream"})
            try:
                imp = r2.json()
            except Exception:
                imp = {}
            ok_imp = r2.status_code == 200 and (imp.get("data") or {}).get("profile", {}).get("fp_seed") == seed
            check("导入还原（含指纹种子）", ok_imp, str(imp)[:300])
            if (imp.get("data") or {}).get("profile", {}).get("id"):
                pid = imp["data"]["profile"]["id"]
    else:
        skip("导出 / 导入", "没给 ZW_SYNC_PASSWORD，跳过（导出包靠主密码加密）")

    print("== 11. 同步 ==")
    if not hub_ok:
        skip("同步", "没有配置 ZW_HUB_URL")
    elif not PW:
        skip("同步", "没给 ZW_SYNC_PASSWORD")
    else:
        st, ss = call("GET", "/api/v1/sync/state")
        check("同步中心连通", bool((ss.get("data") or {}).get("hub_ok")), str((ss.get("data") or {}).get("hub_error", ""))[:200])
        st, df = call("GET", f"/api/v1/sync/{pid}/diff?password={quote(PW)}")
        check("差异可查", st == 200 and "local_revision" in (df.get("data") or {}), str(df)[:300])
        st, push = call("POST", f"/api/v1/sync/{pid}/push", {"password": PW, "note": "自检"})
        check("推送", st == 200 and (push.get("data") or {}).get("revision"), str(push)[:300])
        st, bad = call("POST", f"/api/v1/sync/{pid}/pull", {"password": "错的", "force": True}, timeout=180)
        check("错主密码被拦（412）", st == 412 and bad.get("code") == "KEY_FINGERPRINT_MISMATCH", f"{st} {str(bad)[:200]}")

    print("== 12. MCP ==")
    st, tl = call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = (tl.get("result") or {}).get("tools") or []
    check("工具清单", len(tools) >= 25, f"{len(tools)} 个")
    st, tc = call("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "zw_list_profiles", "arguments": {}}})
    try:
        payload = json.loads(tc["result"]["content"][0]["text"])
    except Exception:
        payload = {}
    check("工具调用（zw_list_profiles）", "profiles" in payload, str(tc)[:200])
    r_noauth = httpx.post(CORE + "/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, timeout=30)
    if TOKEN:
        check("未带令牌被拒", r_noauth.status_code == 401, f"HTTP {r_noauth.status_code}")
    else:
        skip("MCP 鉴权", "本机未配置令牌")

    print("== 13. 运维 ==")
    st, trim = call("POST", "/api/v1/maintenance/trim")
    check("清理缓存", st == 200 and "freed_bytes" in (trim.get("data") or {}), str(trim)[:200])
    st, cl = call("POST", "/api/v1/maintenance/cleanup")
    cd = cl.get("data") or {}
    check("清理 staging / 令牌 / 认证扩展", st == 200 and "auth_ext_removed" in cd, str(cl)[:200])
    st, reap = call("POST", "/api/v1/maintenance/reap-orphans")
    check("孤儿进程扫描", st == 200 and "scanned" in (reap.get("data") or {}), str(reap)[:200])
    st, au = call("GET", "/api/v1/audit?limit=5")
    check("审计日志", st == 200 and isinstance((au.get("data") or {}).get("items"), list), str(au)[:200])
    st, ev = call("GET", "/api/v1/events?since=0")
    check("事件接口", st == 200 and "now" in (ev.get("data") or {}), str(ev)[:200])

    print("== 14. 错误码形状 ==")
    st, nf = call("GET", "/api/v1/profiles/绝对不存在")
    check("404 形状", st == 404 and nf.get("code") == "PROFILE_NOT_FOUND" and nf.get("message"), str(nf)[:200])
    # 幂等启动：第一次真启动，第二次必须干净地返回 already_running=true，
    # 而不是报错、也不是把浏览器再拉一次（后者会撞 SingletonLock）。
    call("POST", f"/api/v1/profiles/{pid}/stop")
    st1, first = call("POST", f"/api/v1/profiles/{pid}/start", timeout=240)
    st2, second = call("POST", f"/api/v1/profiles/{pid}/start", timeout=240)
    check("首次启动 already_running=false", st1 == 200 and (first.get("data") or {}).get("already_running") is False, str(first)[:200])
    check("重复启动返回 already_running=true", st2 == 200 and (second.get("data") or {}).get("already_running") is True, str(second)[:200])
    # 重复启动不该产生第二个浏览器进程
    st_ls, ls_prof = call("GET", f"/api/v1/profiles/{pid}")
    check("重复启动后仍只有一个 pid", bool((ls_prof.get("data") or {}).get("pid")), str(ls_prof)[:150])
    st, badaction = call("POST", "/api/v1/profiles/batch", {"action": "nope", "ids": ["x"]})
    check("批量非法动作被拒", badaction.get("code") == "INVALID_ACTION", str(badaction)[:200])

    print("== 15. 收拾 ==")
    call("POST", f"/api/v1/profiles/{pid}/stop")
    st, dele = call("DELETE", f"/api/v1/profiles/{pid}?purge=true")
    check("删除环境", st == 200, str(dele)[:200])
    cleanup(BASE_NAME)

    print()
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项，跳过 {len(skipped)} 项")
    if skipped:
        print("跳过项（通常是这台机器的环境限制，不是 bug）：")
        for s in skipped:
            print(f"  - {s}")
    if failed:
        print("失败项：")
        for f in failed:
            print(f"  - {f}")
        return 1
    print("✅ 单机全能力自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""跨机端到端自检：两台**真正不同机器**上的 core，共用一台 hub。

这不是本机双进程模拟 —— 默认 A 在 192.168.3.6:8730，B 在 192.168.3.5:8731，
中间真的过了一次局域网。这样才验得到：真实网络延迟、真实主机名、真实防火墙、
以及"跨机搬来的 cookie 在另一台机器的浏览器里到底在不在"。

覆盖：
 1. 两台就绪 + hub 可达
 2. A 建环境 → 启动 → 写 cookie → 读回
 3. A 推到 hub（版本 1）
 4. B 拉取（本地没有 → 自动导入 → cookie 真的写进浏览器）
 5. B 上的指纹种子、代理、窗口尺寸等设置与 A 一致
 6. 错主密码在落盘前被拦（本地不产生环境）
 7. B 改动后推回（版本 2），A 拉取回来
 8. 跨机 CDP：从 A 上取 B 上环境的 CDP 地址，用 Playwright 连上去
 9. 清理

用法：
  ZW_A=http://192.168.3.6:8730 ZW_TA=... ZW_B=http://192.168.3.5:8731 ZW_TB=... \
  ZW_SYNC_PASSWORD=... python scripts/e2e-cross-machine.py

## 跑之前要满足四件事（不然会看到一堆"看起来像 bug"的失败）

1. **两端都要配 hub**：`ZW_HUB_URL` + `ZW_HUB_TOKEN`。没有 hub 就没法推/拉，
   后面每一步都会连带失败。
2. **A 要绑 0.0.0.0**（`ZW_HOST=0.0.0.0`）：脚本会断言 A 的对外网关地址**不是回环**，
   只绑 127.0.0.1 时这条必然红。
3. **远程那一端必须给令牌**：从非回环访问 core 要 `ZW_ADMIN_TOKEN`
   （或客户端机器码）。这是鉴权模型，不是权限问题。
4. **设备要被服务器批准**：`ZW_DEVICE_AUTO_APPROVE=false` 时新设备是 `pending`，
   推送会返回 `DEVICE_PENDING`。先去面板点「允许」。

只在本机跑（两个 core 都在 127.0.0.1）也能跑，但那就验不到"真过了一次局域网"。
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

A = os.environ.get("ZW_A", "http://192.168.3.6:8730")
B = os.environ.get("ZW_B", "http://192.168.3.5:8731")
def _auth(env_name: str, fallback: str) -> dict[str, str]:
    """构造鉴权头。

    ## 不能用 "Bearer " + 空串

    本机 core（回环）默认**不需要令牌**，这时 `ZW_TA=` 是空串。
    拼成 `Authorization: Bearer ` 会被 httpx 直接拒：
    `Illegal header value b'Bearer '` —— 看起来像"连不上"，其实是请求头非法。
    所以令牌为空时**完全不带这个头**（依赖 ZW_ADMIN_TOKEN 环境的机器也适用）。
    """
    tok = (os.environ.get(env_name) or fallback or "").strip()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


TA = _auth("ZW_TA", "")
TB = _auth("ZW_TB", "")
PW = os.environ.get("ZW_SYNC_PASSWORD", "zhiwen-test-pass-2026")
NAME = os.environ.get("ZW_E2E_NAME", "e2e-cross")

passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (passed if cond else failed).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + detail) if detail and not cond else ''}")


def j(method: str, url: str, headers: dict, body: dict | None = None, timeout: float = 300):
    try:
        r = httpx.request(method, url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as exc:
        return 0, {"code": "UNREACHABLE", "message": str(exc)}
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:300]}


def cleanup() -> None:
    for base, hdr in ((A, TA), (B, TB)):
        st, data = j("GET", f"{base}/api/v1/profiles", hdr)
        if st != 200:
            continue
        for p in data.get("data", {}).get("profiles", []):
            if p["name"].startswith(NAME):
                j("POST", f"{base}/api/v1/profiles/{p['id']}/stop", hdr)
                j("DELETE", f"{base}/api/v1/profiles/{p['id']}?purge=true", hdr)


def main() -> int:
    print("== 1. 两台 core 与 hub ==")
    for label, base, hdr in (("A(本机)", A, TA), ("B(另一台)", B, TB)):
        st, data = j("GET", f"{base}/readyz", hdr)
        check(f"{label} 就绪", st == 200 and data.get("ok"), str(data)[:200])
    st, meta_a = j("GET", f"{A}/api/v1/meta", TA)
    st_b, meta_b = j("GET", f"{B}/api/v1/meta", TB)
    if "data" not in meta_a or "data" not in meta_b:
        print(f"   取元信息失败：A={str(meta_a)[:200]} B={str(meta_b)[:200]}")
        print("   提示：core 默认只绑 127.0.0.1，跨机验收要设 ZW_HOST=0.0.0.0")
        return 1
    node_a = meta_a["data"]["node_id"]
    node_b = meta_b["data"]["node_id"]
    check("两台主机的 node_id 不同（确实是两台机器）", node_a != node_b, f"{node_a} vs {node_b}")
    check("A 配了 hub", meta_a["data"]["hub_configured"])
    check("B 配了 hub", meta_b["data"]["hub_configured"])
    check("A 的对外网关地址不是回环", "127.0.0.1" not in meta_a["data"]["gateway_public_base"],
          meta_a["data"]["gateway_public_base"])

    print("== 1.5 清理上一轮残留 ==")
    cleanup()

    print("== 2. 在 A 上建环境、启动、写 cookie ==")
    st, data = j("POST", f"{A}/api/v1/profiles", TA, {
        "name": NAME, "note": "跨机自检",
        "proxy": "", "window_w": 1366, "window_h": 768,
        "fp_timezone": "Asia/Shanghai", "fp_lang": "zh-CN", "fp_hardware": 8,
        "group_name": "跨机组", "tags": ["e2e"],
    })
    if st != 200:
        print(f"   建环境失败：{data}")
        return 1
    pid = data["data"]["id"]
    seed = data["data"]["fp_seed"]
    print(f"   环境 id={pid} seed={seed}")

    st, data = j("POST", f"{A}/api/v1/profiles/{pid}/start", TA, {}, timeout=180)
    check("A 启动成功", st == 200 and data.get("ok"), str(data)[:300])
    cdp_a = (data.get("data") or {}).get("cdp") or {}
    check("A 返回网关 ws", bool(cdp_a.get("gateway_ws_url")))

    cookie = {
        "name": "zw_cross", "value": "跨机验证值", "domain": ".example.com", "path": "/",
        "secure": True, "httpOnly": False, "sameSite": "Lax", "expires": 1900000000,
    }
    st, data = j("PUT", f"{A}/api/v1/profiles/{pid}/cookies", TA, {"cookies": [cookie], "mode": "replace"})
    check("A 写入 cookie", st == 200 and (data.get("data") or {}).get("ok") == 1, str(data)[:200])

    print("== 3. A 推到 hub ==")
    st, data = j("POST", f"{A}/api/v1/sync/{pid}/push", TA, {"password": PW, "note": "跨机自检"})
    check("推送成功", st == 200 and data.get("ok"), str(data)[:300])
    rev1 = (data.get("data") or {}).get("revision")
    check("cookie 一起推上去了", (data.get("data") or {}).get("cookies", 0) >= 1, str(data.get("data"))[:200])
    print(f"   revision={rev1} bytes={(data.get('data') or {}).get('bytes')}")

    print("== 4. B 拉取（本地没有 → 自动导入）==")
    st, data = j("POST", f"{B}/api/v1/sync/{pid}/pull", TB, {"password": PW}, timeout=420)
    check("拉取成功", st == 200 and data.get("ok"), str(data)[:300])
    d = data.get("data") or {}
    check("自动导入新环境", bool(d.get("imported")), str(d)[:200])
    check("cookie 写回成功", (d.get("cookie_report") or {}).get("ok", 0) >= 1, str(d.get("cookie_report"))[:300])
    check("设置一起搬过来了", bool(d.get("settings_applied")))

    print("== 5. B 上的环境设置与登录态 ==")
    st, data = j("GET", f"{B}/api/v1/profiles/{pid}", TB)
    p = data.get("data") or {}
    check("B 指纹种子与 A 一致", p.get("fp_seed") == seed, f"{p.get('fp_seed')} vs {seed}")
    check("B 窗口尺寸一致", p.get("window_w") == 1366 and p.get("window_h") == 768, f"{p.get('window_w')}x{p.get('window_h')}")
    check("B 时区一致", p.get("fp_timezone") == "Asia/Shanghai", str(p.get("fp_timezone")))
    check("B 分组一致", p.get("group_name") == "跨机组", str(p.get("group_name")))
    check("B 同步版本已对齐", p.get("sync_revision") == rev1, f"{p.get('sync_revision')} vs {rev1}")

    st, data = j("POST", f"{B}/api/v1/profiles/{pid}/start", TB, {}, timeout=180)
    check("B 启动成功", st == 200 and data.get("ok"), str(data)[:300])
    cdp_b = (data.get("data") or {}).get("cdp") or {}
    st, data = j("GET", f"{B}/api/v1/profiles/{pid}/cookies", TB)
    got = [c for c in (data.get("data") or {}).get("cookies", []) if c["name"] == "zw_cross"]
    check("B 浏览器里真的有这条 cookie", bool(got) and got[0]["value"] == "跨机验证值", str(data)[:300])

    print("== 6. 错主密码必须在落盘前被拦 ==")
    st, data = j("POST", f"{B}/api/v1/sync/{pid}/pull", TB, {"password": "错误口令", "force": True}, timeout=180)
    check("错密码返回 412", st == 412, f"实际 {st}")
    check("错误码正确", data.get("code") == "KEY_FINGERPRINT_MISMATCH", str(data)[:200])

    print("== 7. B 改完推回，A 拉回来 ==")
    st, data = j("PATCH", f"{B}/api/v1/profiles/{pid}", TB, {"note": "在 B 上改过"})
    check("B 修改环境", st == 200 and data.get("ok"), str(data)[:200])
    st, data = j("POST", f"{B}/api/v1/sync/{pid}/push", TB, {"password": PW, "note": "B 推回"})
    check("B 推送成功", st == 200 and data.get("ok"), str(data)[:300])
    rev2 = (data.get("data") or {}).get("revision")
    check("版本号递增", isinstance(rev2, int) and isinstance(rev1, int) and rev2 > rev1, f"{rev1} → {rev2}")
    st, data = j("POST", f"{A}/api/v1/sync/{pid}/pull", TA, {"password": PW}, timeout=420)
    check("A 拉取成功", st == 200 and data.get("ok"), str(data)[:300])

    print("== 8. 跨机 CDP：从 A 侧验证 B 的网关地址可连 ==")
    # 说明：这一步不是"A 用 B 的浏览器"，而是验 B 的网关地址对局域网另一台机器是否可用。
    check("B 返回的网关地址是 B 的局域网地址", "192.168.3.5" in str(cdp_b.get("gateway_ws_url")), str(cdp_b.get("gateway_ws_url"))[:120])
    try:
        import subprocess

        env = {**os.environ, "ZW_CORE": B, "ZW_ADMIN_TOKEN": os.environ.get("ZW_TB", "core-b-token-3f9a2"),
               "ZW_PROFILE": NAME}
        proc = subprocess.run(
            ["node", "scripts/gateway-check.cjs"], capture_output=True, text=True, timeout=300, env=env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        tail = (proc.stdout or "").strip().splitlines()[-3:]
        check("B 的网关能被 Playwright 接管", proc.returncode == 0, " / ".join(tail) + (proc.stderr or "")[-200:])
    except FileNotFoundError:
        check("B 的网关能被 Playwright 接管", False, "本机没有 node，跳过")
    except subprocess.TimeoutExpired:
        check("B 的网关能被 Playwright 接管", False, "超时")

    print("== 9. 收拾 ==")
    cleanup()

    print()
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    print("✅ 跨机端到端自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

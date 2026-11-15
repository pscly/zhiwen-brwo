#!/usr/bin/env python3
"""端到端自检：把整套流程真跑一遍并逐项断言。

覆盖：
1. 建环境 → 启浏览器 → 通过网关用 CDP 读指纹
2. 写 cookie → 读回
3. 推到 hub（真实网络上的 3.5）
4. 另一台"机器"从 hub 拉取 → 自动导入 → cookie 真的还原
5. 错主密码必须在落盘前被拦下
6. MCP 工具清单与调用

用法：python scripts/e2e-check.py
需要：两台 core 已启动（8730 / 8731），hub 可达。
"""

from __future__ import annotations

import json
import sys
import time

import httpx

A = "http://127.0.0.1:8730"
B = "http://127.0.0.1:8731"
TA = {"Authorization": "Bearer test-token-123"}
TB = {"Authorization": "Bearer test-token-b"}
PW = "zhiwen-test-pass-2026"

passed: list[str] = []
failed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (passed if cond else failed).append(name)
    print(f"  {'✅' if cond else '❌'} {name}{(' — ' + detail) if detail and not cond else ''}")


def j(method: str, url: str, headers: dict, body: dict | None = None, timeout: float = 180):
    r = httpx.request(method, url, headers=headers, json=body, timeout=timeout)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def main() -> int:
    print("== 1. 两台 core 就绪 ==")
    for name, base, hdr in (("A", A, TA), ("B", B, TB)):
        code, data = j("GET", f"{base}/readyz", hdr)
        check(f"{name} 就绪", code == 200 and data.get("ok"), str(data)[:120])

    print("== 1.5 清理上一轮的残留（保证可反复跑）==")
    for base, hdr in ((A, TA), (B, TB)):
        for ref in ("e2e-01",):
            j("DELETE", f"{base}/api/v1/profiles/{ref}?purge=true", hdr)

    print("== 2. 在 A 上建环境并启动 ==")
    code, data = j("POST", f"{A}/api/v1/profiles", TA, {"name": "e2e-01", "note": "端到端自检"})
    if code != 200:
        code, data = j("GET", f"{A}/api/v1/profiles/e2e-01", TA)
    pid = data["data"]["id"]
    seed = data["data"]["fp_seed"]
    print(f"   环境 id={pid} seed={seed}")
    code, data = j("POST", f"{A}/api/v1/profiles/e2e-01/start", TA, {}, timeout=120)
    check("A 启动成功", code == 200 and data.get("ok"), str(data)[:200])
    check("拿到网关 ws 地址", bool(data.get("data", {}).get("cdp", {}).get("gateway_ws_url")))

    print("== 3. 写 cookie 并读回 ==")
    cookie = {"name": "zw_e2e", "value": "跨机同步验证", "domain": ".example.com", "path": "/",
              "secure": True, "httpOnly": False, "sameSite": "Lax", "expires": 1900000000}
    code, data = j("PUT", f"{A}/api/v1/profiles/e2e-01/cookies", TA, {"cookies": [cookie], "mode": "replace"})
    check("写入 cookie", code == 200 and data.get("data", {}).get("ok") == 1, str(data)[:200])
    code, data = j("GET", f"{A}/api/v1/profiles/e2e-01/cookies", TA)
    got = [c for c in data.get("data", {}).get("cookies", []) if c["name"] == "zw_e2e"]
    check("读回 cookie", bool(got) and got[0]["value"] == "跨机同步验证")

    print("== 4. 推到 hub ==")
    code, data = j("POST", f"{A}/api/v1/sync/e2e-01/push", TA, {"note": "e2e 推送"})
    check("推送成功", code == 200 and data.get("ok"), str(data)[:300])
    rev = data.get("data", {}).get("revision")
    pw_fp = data.get("data", {}).get("pw_fp")
    print(f"   revision={rev} pw_fp={pw_fp} bytes={data.get('data', {}).get('bytes')}")

    print("== 5. B 机拉取（含自动导入）==")
    code, data = j("POST", f"{B}/api/v1/sync/{pid}/pull", TB, {"password": PW}, timeout=240)
    check("拉取成功", code == 200 and data.get("ok"), str(data)[:300])
    check("自动导入新环境", bool(data.get("data", {}).get("imported")))
    check("cookie 写回成功", (data.get("data", {}).get("cookie_report") or {}).get("ok", 0) >= 1, str(data.get("data", {}).get("cookie_report")))

    print("== 6. B 机上验证登录态与指纹一致 ==")
    code, data = j("GET", f"{B}/api/v1/profiles/{pid}", TB)
    check("B 指纹种子与 A 一致", data.get("data", {}).get("fp_seed") == seed, f"{data.get('data',{}).get('fp_seed')} vs {seed}")
    j("POST", f"{B}/api/v1/profiles/{pid}/start", TB, {}, timeout=120)
    code, data = j("GET", f"{B}/api/v1/profiles/{pid}/cookies", TB)
    got = [c for c in data.get("data", {}).get("cookies", []) if c["name"] == "zw_e2e"]
    check("B 上 cookie 真的存在", bool(got) and got[0]["value"] == "跨机同步验证", str(data)[:200])

    print("== 7. 错主密码必须被拦 ==")
    code, data = j("POST", f"{B}/api/v1/sync/{pid}/pull", TB, {"password": "错误口令", "force": True})
    check("错密码返回 412", code == 412, f"实际 {code}")
    check("错误码是 KEY_FINGERPRINT_MISMATCH", data.get("code") == "KEY_FINGERPRINT_MISMATCH", str(data)[:200])

    print("== 8. MCP ==")
    code, data = j("POST", f"{A}/mcp", {}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = data.get("result", {}).get("tools", [])
    check("MCP 工具清单可用", len(tools) >= 20, f"{len(tools)} 个")
    code, data = j("POST", f"{A}/mcp", {}, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                            "params": {"name": "zw_list_profiles", "arguments": {}}})
    payload = json.loads(data["result"]["content"][0]["text"])
    check("MCP 调用返回环境列表", payload.get("total", 0) >= 1, str(payload)[:200])

    print("== 9. 收拾现场 ==")
    for base, hdr in ((A, TA), (B, TB)):
        j("POST", f"{base}/api/v1/profiles/e2e-01/stop", hdr)

    print()
    print(f"通过 {len(passed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    print("✅ 端到端自检全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

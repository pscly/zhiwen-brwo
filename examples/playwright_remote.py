"""用 Playwright 接管局域网里另一台机器上的浏览器。

这是知纹最核心的能力：CDP 出了局域网。
"""

from __future__ import annotations

import json
import os
import urllib.request

from playwright.sync_api import sync_playwright

CORE = os.environ.get("ZW_CORE", "http://192.168.3.6:8730")
TOKEN = os.environ["ZW_ADMIN_TOKEN"]
PROFILE = os.environ.get("ZW_PROFILE", "demo-01")


def fetch_cdp(profile: str) -> dict:
    req = urllib.request.Request(
        f"{CORE}/api/v1/profiles/{profile}/cdp",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req) as resp:
        payload = json.load(resp)
    if not payload.get("ok"):
        raise SystemExit(f"取 CDP 失败：{payload.get('code')} {payload.get('message')}")
    return payload["data"]


def main() -> None:
    info = fetch_cdp(PROFILE)
    print(f"环境 {PROFILE} 的网关地址：{info['gateway_ws_url'][:100]}…")

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(info["gateway_ws_url"])
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        page.goto("https://example.com", wait_until="domcontentloaded", timeout=30000)
        print("标题：", page.title())

        # 顺手看一眼指纹里最容易露馅的两项
        fp = page.evaluate(
            """() => ({
                webgl: !!document.createElement('canvas').getContext('webgl'),
                hc: navigator.hardwareConcurrency,
                tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
                lang: navigator.language,
            })"""
        )
        print("指纹概况：", fp)
        if not fp["webgl"]:
            print("⚠️ WebGL 为 null —— 这台机器没有显卡，不适合承担真人身份")

        page.screenshot(path="remote-shot.png")
        print("截图已保存 remote-shot.png")
        browser.close()   # 只断开自动化连接，不关浏览器


if __name__ == "__main__":
    main()

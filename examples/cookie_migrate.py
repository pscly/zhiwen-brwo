"""把一个环境的 cookie 导出成文件，再导入到另一个环境。

在没有 hub 的情况下，这就是最小的"跨机搬家"：
左边机器导文件 → 拷过去 → 右边机器导入。
"""

from __future__ import annotations

import json
import os

import httpx

CORE = os.environ.get("ZW_CORE", "http://127.0.0.1:8730")
TOKEN = os.environ["ZW_ADMIN_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}


def dump(profile: str, out: str) -> None:
    r = httpx.get(f"{CORE}/api/v1/profiles/{profile}/cookies", headers=H, timeout=120)
    r.raise_for_status()
    data = r.json()["data"]
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(data["cookies"], fh, ensure_ascii=False, indent=2)
    print(f"已导出 {data['count']} 条 cookie → {out}")


def load(profile: str, src: str) -> None:
    with open(src, encoding="utf-8") as fh:
        cookies = json.load(fh)
    r = httpx.put(
        f"{CORE}/api/v1/profiles/{profile}/cookies",
        headers=H,
        json={"cookies": cookies, "mode": "replace"},
        timeout=180,
    )
    r.raise_for_status()
    print("导入结果：", r.json()["data"])


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print("用法：python cookie_migrate.py dump <环境> <文件>")
        print("      python cookie_migrate.py load <环境> <文件>")
        raise SystemExit(1)
    action, profile, path = sys.argv[1:4]
    (dump if action == "dump" else load)(profile, path)

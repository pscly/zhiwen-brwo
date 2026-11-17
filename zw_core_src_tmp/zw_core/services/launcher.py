"""启动页策略：先恢复"上次关掉时的网址"，再额外打开检测页。

## 为什么不解析 Chromium 的会话文件

用户要的语义是"打开上次关闭时的那些网址"。Chromium 自己的会话确实存在
`Default/Sessions/Session_*`、`Default/Current Tabs`、`Default/Last Tabs`，
但它们是**二进制私有格式**（SNSS 流，带命令 ID、版本号、压缩片段）。
解析它等于逆向 Chromium 的内部存储，内核一升级就得重写一遍；更麻烦的是
"解析错了"没有任何提示——你会得到一个空的或半截的标签页列表。

所以这里换一条**语义等价、可测、不赌内部格式**的路：

- **关闭时抓**：`manager.stop()` 在关浏览器之前，用 CDP 的
  `Target.getTargets` 把当前所有 page 的 URL 取下来，存进 `profiles.start_urls`。
  这就是"上次关闭时的网址"这份事实的唯一来源，而且是运行时真值。
- **启动时用**：`profiles.start_urls` 本来就会被 `engines.build_cmdline`
  当成命令行末尾的 URL 列表交给 Chromium。所以恢复不需要任何额外机制。

代价（如实记下来）：只有"经我们 `stop()` 关掉"的环境才记得住标签页。
被任务管理器打死、断电、或用户在 `chrome://quit` 里退出的，记忆会停留在上一次
正常停止那一刻。这是刻意的取舍——宁可少记一次，也不要猜一个可能错误的列表
去覆盖用户真正想要的。

## 顺序为什么是"恢复的在前、检测页在后"

用户原话是「默认先打开上次被关闭时候的网址，然后再加一个检测网址」。
Chromium 按命令行顺序开标签，所以顺序一模一样落地；去重保留**第一次出现**的位置，
这样即使用户上次就把检测页开着，重启后它们仍然在最后，不会跑到自己的标签前面。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..config import get_settings

logger = logging.getLogger("zw.launcher")

# 从活动标签里抓 URL 时最多回传这么多（防止有人开 500 个标签把命令行撑爆）
MAX_REMEMBERED = 40
# 抓取时最多看这么多 target（getTargets 本身会返回所有 target，含 iframe/worker）
MAX_TARGETS = 200

# 只记"内容页"。chrome://、devtools://、chrome-extension://、about:blank 这些
# 不是用户想恢复的东西，带上只会让新窗口一片空白或报错。
WEB_SCHEMES = ("http://", "https://")


# ── 列表整形 ──────────────────────────────────────────────
def _as_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(u).strip() for u in raw if str(u).strip()]


def _web_only(urls: list[str]) -> list[str]:
    return [u for u in urls if u.lower().startswith(WEB_SCHEMES)]


def _dedupe(urls: list[str]) -> list[str]:
    """去重，保留第一次出现的位置。

    键用 `rstrip("/")` 归一：`https://a.com` 与 `https://a.com/` 在 Chromium 眼里
    是同一个地址，当成两个会在启动时开两个相同标签。
    """
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        key = u.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(u)
    return out


# ── 三个对外函数 ──────────────────────────────────────────
def remembered_urls(profile: dict[str, Any]) -> list[str]:
    """上次关闭时记下来的网址（只保留 http/https）。"""
    if not get_settings().restore_tabs:
        return []
    return _dedupe(_web_only(_as_list(profile.get("start_urls"))))[:MAX_REMEMBERED]


def detect_urls(profile: dict[str, Any] | None = None) -> list[str]:
    """检测页列表。

    优先级：环境上显式的 `detect_urls`（为将来的"单环境定制"留的口子，库里还没有
    这一列，所以现在传进来也只会来自调用方）> 配置 `ZW_DETECT_URLS` > 内置默认。
    """
    override = (profile or {}).get("detect_urls")
    if isinstance(override, (list, tuple)):
        return _dedupe(_as_list(override))
    return _dedupe(_as_list(get_settings().detect_urls))


def compose_start_urls(profile: dict[str, Any]) -> list[str]:
    """最终交给 `--` 后面的 URL 列表：恢复的在前，检测页在后，整体去重。

    两个开关各管一半：`ZW_RESTORE_TABS` 管"要不要恢复"，`ZW_OPEN_DETECT_ON_START`
    管"要不要开检测页"。只剩一半时也照常工作（比如只想要检测页的新环境）。
    全空时返回 `[]`，由 `engines.build_cmdline` 兜底成 about:blank。
    """
    s = get_settings()
    urls: list[str] = []
    if s.restore_tabs:
        urls.extend(remembered_urls(profile))
    if s.open_detect_on_start:
        urls.extend(detect_urls(profile))
    return _dedupe(urls)


# ── 关闭时抓标签页 ────────────────────────────────────────
def _local_ws(port: int) -> str:
    """本机直连的浏览器级 ws 地址。拿不到就返回空串（不是错误，环境可能已退）。"""
    import httpx

    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=3)
        return str(r.json().get("webSocketDebuggerUrl") or "")
    except Exception:
        return ""


async def _capture_pages(ws_url: str) -> list[str]:
    """用 CDP 读所有 page 类型的 target 的 url。

    只发 `Target.getTargets` —— 它是**浏览器级**命令，不需要挂页面会话
    （cookies.py 顶部那条约束说的就是这件事：Network/Page/Runtime 才必须挂会话）。
    这里故意不做 attach：attach 会改动浏览器状态，而我们只想"看一眼"。
    """
    from . import cookies as ck

    async with ck.CDP(ws_url, timeout=8) as cdp:
        targets = await cdp.targets()
    out: list[str] = []
    for t in targets[:MAX_TARGETS]:
        if t.get("type") != "page":
            continue
        url = str(t.get("url") or "")
        if url:
            out.append(url)
    return out


def capture_tab_urls(ws_url: str) -> list[str]:
    """同步入口：抓当前打开的页面 URL（已过滤成 http/https、去重、截断）。"""
    from . import cookies as ck

    if not ws_url:
        return []
    raw = ck.run(_capture_pages(ws_url))
    return _dedupe(_web_only(raw))[:MAX_REMEMBERED]


def remember_open_tabs(profile: dict[str, Any]) -> dict[str, Any]:
    """把当前打开的标签页记进 `profiles.start_urls`。由 `manager.stop()` 调用。

    三个"没记"的分支都返回原因而不是抛异常：停止流程不该因为"记不住标签页"失败。
    """
    pid = str(profile.get("id") or "")
    if not pid:
        return {"saved": 0, "reason": "没有环境标识"}
    if not profile.get("cdp_enabled", True):
        return {"saved": 0, "reason": "该环境关闭了调试端口，读不到标签页"}
    port = int(profile.get("cdp_port") or 0)
    if not port:
        return {"saved": 0, "reason": "没有调试端口（环境可能没真正跑起来）"}

    ws = _local_ws(port)
    if not ws:
        return {"saved": 0, "reason": "调试端口不可达，按「读不到」处理（不覆盖旧记录）"}
    try:
        urls = capture_tab_urls(ws)
    except Exception as exc:  # noqa: BLE001 —— 见 docstring
        logger.warning("抓标签页失败（保留上一次的记录）：%s", exc)
        return {"saved": 0, "reason": f"CDP 读取失败：{exc}"}

    if not urls:
        # 只开着 about:blank / devtools 时不覆盖：那等于把旧记录清空，
        # 用户下次启动就一个标签都没有了，比"记着旧的"更糟。
        return {"saved": 0, "reason": "当前没有可恢复的网页标签"}

    from . import manager  # 延迟导入：manager.stop() 反向调用本函数

    manager._update(pid, start_urls=urls)
    logger.info("记住标签页 name=%s 数量=%s", profile.get("name"), len(urls))
    return {"saved": len(urls), "urls": urls}

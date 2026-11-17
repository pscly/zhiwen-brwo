"""本机代理库 ↔ 服务器代理库的双向同步。

## 用户要求（原话）

> 代理可以在服务器里面进行添加管控，也可以在用户机上添加管控，都是一样的…
> 用户机上添加过的代理，都会自动同步到服务器里面，也可以在服务器里面删除，或者隐藏

所以是**一份共享库**，不是单向下发：

- **上行**：本机导入/新增/改过代理后，把这批推给服务器（`push`）
- **下行**：启动时/定时拉服务器上的（`pull`），把本机没有的补进来
- 服务器**隐藏**的不会出现在 `pull` 结果里 → 本机保留但不再主动展示
- 服务器**删除**的：**本机不会跟着删**（见下方"为什么不做级联删除"）

## 为什么不做"整库覆盖"

两边都能改。整库覆盖会把对方刚加的东西抹掉。所以：

- 上行按 (scheme+host+port+username+hop) 去重，只 upsert，不删
- 下行只"补缺 + 更新"，**从不删**

## 为什么不做级联删除（曾经文档写错、实现里没有）

早先这里写着"服务器删了本机也会删"，但 `pull()` 从来没实现过删除 —— 文档比代码
承诺得多。想清楚之后决定**保持不删**，并把文档改成实话：

- 本机环境是按 `proxy_id` 引用代理的。级联删除会把"正在被某个环境使用的代理"
  从库里抹掉，那个环境下次启动就变成"代理 #N（库里已没有这一条）"。
- 服务器侧的"删除"语义其实是"从我这份共享库里移除"，不代表"每台机器都必须立刻
  丢掉它"。**隐藏**才是"我不想再看到它"的正确表达。
- 真要清掉本机某条，用户在客户端 GUI 里删就是了 —— 有明确意图，也有明确后果。

## 密码

- 上行时会带上本机密码（服务器要加密存下来，其他机器才能用）
- 下行时服务器会把密码一起给（这条是本机要用它的前提）
- 任何一次同步失败都只记日志，**绝不阻断本地操作**
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .. import config
from ..db import get_db
from . import proxylib

logger = logging.getLogger("zw.proxysync")

# 记一次同步结果，给界面/自检看
_LAST: dict[str, Any] = {"at": 0.0, "up": None, "down": None, "error": ""}


def last_result() -> dict[str, Any]:
    return dict(_LAST)


def _hub() -> tuple[str, str, str]:
    """(hub_url, device_id, token)。没配置就返回空。"""
    s = config.get_settings()
    return (str(getattr(s, "hub_url", "") or "").rstrip("/"),
            str(getattr(s, "device_id", "") or ""),
            str(getattr(s, "hub_token", "") or ""))


def _ensure_registered(hub: str, dev: str, token: str) -> None:
    """同步前先确保这台机器在服务器上注册过。

    首次运行（或服务器换过库）时设备还没注册，`/v1/devices/proxies` 会 403，
    用户看到的就是"同步失败"，但其实只是没注册。这里补一次，
    失败也不拦（注册接口本身可能被 CIDR 限制）。
    """
    try:
        from . import shellagent

        shellagent.ensure_registered(hub, dev, token)
    except Exception as exc:  # noqa: BLE001
        logger.debug("同步前注册设备失败（继续尝试同步）：%s", exc)


def _err_of(r: httpx.Response) -> str:
    """把非 200 的响应变成人话（不能只说 [None] None）。"""
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = None
    if isinstance(body, dict):
        e = body.get("error")
        if isinstance(e, dict) and e.get("code"):
            return f"HTTP {r.status_code} [{e.get('code')}] {e.get('message')}"
        if body.get("detail"):
            return f"HTTP {r.status_code} {body.get('detail')}"
    return f"HTTP {r.status_code} {str(r.text or '')[:160]}"


def _headers(dev: str, token: str) -> dict[str, str]:
    h = {"X-ZW-Device": dev}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _local_rows() -> list[dict[str, Any]]:
    """本机库（**带明文密码**，只用于上行；不落日志）。"""
    proxylib._ensure_hop_column()
    with get_db().conn() as c:
        rows = c.execute("SELECT * FROM proxies ORDER BY id").fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        spec = proxylib._spec_of_row(r)
        out.append({
            "scheme": spec.scheme,
            "host": spec.host,
            "port": int(spec.port),
            "username": spec.username,
            "password": spec.password,
            "hop": proxylib._hop_to_text(spec) or "",
            "label": r["label"] or "",
        })
    return out


def push(items: list[dict[str, Any]] | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
    """把本机代理推给服务器。`items` 为空 = 推整库。"""
    hub, dev, token = _hub()
    if not hub or not dev:
        return {"ok": False, "skipped": "没有配置服务器或设备号"}
    payload = {"items": items if items is not None else _local_rows()}
    if not payload["items"]:
        return {"ok": True, "skipped": "本机没有代理可同步", "created": 0, "updated": 0}
    _ensure_registered(hub, dev, token)
    try:
        r = httpx.post(f"{hub}/v1/devices/proxies", json=payload,
                       headers=_headers(dev, token), timeout=timeout)
        data = (r.json() or {}).get("data") or {}
        if r.status_code != 200:
            raise RuntimeError(_err_of(r))
        _LAST.update(at=__import__("time").time(), up=data, error="")
        logger.info("代理库上行 %s 条（新增 %s / 更新 %s）",
                    data.get("total"), data.get("created"), data.get("updated"))
        return {"ok": True, **data}
    except Exception as exc:  # noqa: BLE001  同步失败绝不阻断本地操作
        _LAST.update(at=__import__("time").time(), error=f"{type(exc).__name__}: {exc}")
        logger.warning("代理库上行失败（不影响本机使用）：%s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def pull(*, timeout: float = 10.0) -> dict[str, Any]:
    """从服务器拉取，补进本机库。

    返回 `{added, updated, removed, hidden_missing}`。
    """
    hub, dev, token = _hub()
    if not hub or not dev:
        return {"ok": False, "skipped": "没有配置服务器或设备号"}
    _ensure_registered(hub, dev, token)
    try:
        r = httpx.get(f"{hub}/v1/devices/proxies", headers=_headers(dev, token),
                      params={"with_secrets": "true"}, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(_err_of(r))
        items = ((r.json() or {}).get("data") or {}).get("proxies") or []
    except Exception as exc:  # noqa: BLE001
        _LAST.update(at=__import__("time").time(), error=f"{type(exc).__name__}: {exc}")
        logger.warning("代理库下行失败（不影响本机使用）：%s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    added = updated = 0
    proxylib._ensure_hop_column()
    with get_db().conn() as c:
        local = {proxylib._row_key(r): r for r in c.execute("SELECT * FROM proxies")}
    for it in items:
        try:
            line = _as_line(it)
            if not line:
                continue
            spec, note = proxylib.parse_line(line)
            if spec is None:
                continue
            key = proxylib._spec_key(spec)
            if key in local:
                # 已存在：只在标签不同时更新，不覆盖本机改过的密码
                if note and note != (local[key]["label"] or ""):
                    with get_db().conn() as c:
                        c.execute("UPDATE proxies SET label=? WHERE id=?", (note, int(local[key]["id"])))
                    updated += 1
                continue
            row, created = proxylib._upsert(spec, note=str(it.get("label") or ""))
            if created:
                added += 1
        except Exception as exc:  # noqa: BLE001  单条坏不影响其它
            logger.debug("下行单条失败：%s", exc)

    res = {"ok": True, "added": added, "updated": updated,
           "server_count": len(items)}
    _LAST.update(at=__import__("time").time(), down=res, error="")
    logger.info("代理库下行：新增 %s 条，更新 %s 条（服务器共 %s 条）",
                added, updated, len(items))
    return res


def _as_line(it: dict[str, Any]) -> str:
    """服务器条目 → 本机 parse_line 能吃的代理串（含链式前置）。"""
    scheme = str(it.get("scheme") or "http").strip().lower()
    host = str(it.get("host") or "").strip()
    port = int(it.get("port") or 0)
    if not host or not (0 < port < 65536):
        return ""
    user = str(it.get("username") or "")
    pw = str(it.get("password") or "")
    auth = ""
    if user:
        from urllib.parse import quote

        auth = f"{quote(user, safe='')}:{quote(pw, safe='')}@"
    base = f"{scheme}://{auth}{host}:{port}"
    hop = str(it.get("hop") or "").strip()
    return f"{hop},{base}" if hop else base


def sync(*, timeout: float = 10.0) -> dict[str, Any]:
    """一次完整往返：先上行（把本机新增的交上去），再下行（补服务器新增的）。"""
    up = push(timeout=timeout)
    down = pull(timeout=timeout)
    return {"ok": bool(up.get("ok") or down.get("ok")), "up": up, "down": down}

"""环境生命周期管理器：这是 core 的心脏。

一次 `start` 做的事情（顺序不能乱）：
1. 查租约（本机 + hub），有别人持有就 409
2. 检查并发上限、磁盘余量
3. 从端口池取端口
4. 拼命令行、拉起进程
5. 等 CDP 就绪（读 `/json/version`）
6. 写回 pid/port/state，登记到网关
7. 返回网关地址（含短期令牌）

一次 `stop`：CDP 优雅关 → SIGTERM → SIGKILL → 释放端口 → 撤销网关路由 → 可选 trim。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..db import get_db
from . import engines
from .leases import LeaseBusy, LocalLease

logger = logging.getLogger("zw.lifecycle")

# 进程内的端口池（重启后从数据库里的 cdp_port 重建）
_POOL: engines.PortPool | None = None


def pool() -> engines.PortPool:
    global _POOL
    if _POOL is None:
        s = get_settings()
        _POOL = engines.PortPool(s.port_pool_start, s.port_pool_end)
        # 用数据库里仍在 running 的环境占位，避免重启后撞端口
        for p in list_profiles(state="running"):
            if p.get("cdp_port"):
                _POOL.reserve(int(p["cdp_port"]), p["id"])
    return _POOL


class ProfileError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ── 行 ↔ 字典 ─────────────────────────────────────────────
_JSON_FIELDS = ("tags", "start_urls", "extensions")


def row_to_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    for f in _JSON_FIELDS:
        raw = d.get(f) or "[]"
        try:
            d[f] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            d[f] = []
    for f in ("cdp_enabled", "headless", "ephemeral", "trim_on_stop", "sync_enabled"):
        if f in d and d[f] is not None:
            d[f] = bool(d[f])
    return d


def profile_dir(profile_id: str) -> Path:
    return get_settings().profiles_dir / profile_id


def udd_path(profile_id: str) -> Path:
    return profile_dir(profile_id) / "udd"


# ── 查询 ──────────────────────────────────────────────────
def list_profiles(state: str | None = None, group: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM profiles WHERE 1=1"
    args: list[Any] = []
    if state:
        sql += " AND state=?"
        args.append(state)
    if group:
        sql += " AND group_name=?"
        args.append(group)
    if q:
        sql += " AND (name LIKE ? OR note LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY created_at DESC"
    with get_db().conn() as c:
        return [row_to_dict(r) for r in c.execute(sql, args).fetchall()]


def get_profile(ref: str) -> dict[str, Any]:
    with get_db().conn() as c:
        row = c.execute("SELECT * FROM profiles WHERE id=? OR name=?", (ref, ref)).fetchone()
    if row is None:
        raise ProfileError("PROFILE_NOT_FOUND", f"找不到环境：{ref}", status=404)
    return row_to_dict(row)


def _update(profile_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    if "tags" in fields and not isinstance(fields["tags"], str):
        fields["tags"] = json.dumps(fields["tags"], ensure_ascii=False)
    if "start_urls" in fields and not isinstance(fields["start_urls"], str):
        fields["start_urls"] = json.dumps(fields["start_urls"], ensure_ascii=False)
    if "extensions" in fields and not isinstance(fields["extensions"], str):
        fields["extensions"] = json.dumps(fields["extensions"], ensure_ascii=False)
    for key in ("cdp_enabled", "headless", "ephemeral", "trim_on_stop", "sync_enabled"):
        if key in fields and isinstance(fields[key], bool):
            fields[key] = int(fields[key])
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_db().conn() as c:
        c.execute(f"UPDATE profiles SET {cols} WHERE id=?", (*fields.values(), profile_id))


# ── 创建 / 修改 ───────────────────────────────────────────
def allocate_seed() -> int:
    """分配一个全局唯一的指纹种子。同一个种子 = 同一个指纹 = 账号可关联，所以必须唯一。"""
    with get_db().conn() as c:
        for _ in range(50):
            seed = secrets.randbelow(2**32 - 2) + 1
            if not c.execute("SELECT 1 FROM profiles WHERE fp_seed=?", (seed,)).fetchone():
                return seed
    raise ProfileError("SEED_ALLOC_FAILED", "连续 50 次都没找到空闲的指纹种子，环境数量可能太多了")


def create_profile(payload: dict[str, Any], profile_id: str | None = None) -> dict[str, Any]:
    """创建环境。`profile_id` 只在"从同步中心导入"时由外部指定（必须与远端一致）。"""
    s = get_settings()
    name = (payload.get("name") or "").strip()
    if not name:
        raise ProfileError("INVALID_NAME", "环境名不能为空")
    with get_db().conn() as c:
        if c.execute("SELECT 1 FROM profiles WHERE name=?", (name,)).fetchone():
            if profile_id:
                # 导入时同名冲突：自动加后缀，避免打断导入
                name = f"{name}-{profile_id[:6]}"
            else:
                raise ProfileError("NAME_TAKEN", f"环境名已存在：{name}", status=409)

    pid = profile_id or uuid.uuid4().hex[:20]
    now = time.time()
    seed = int(payload.get("fp_seed") or 0) or allocate_seed()

    engine_path = ""
    engine_version = ""
    try:
        chrome = engines.find_chrome(s)
        engine_path = str(chrome)
        engine_version = engines.engine_version(chrome)
    except engines.EngineError:
        pass  # 环境可以先建，启动时再要求引擎

    with get_db().conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO profiles (
                id,name,group_name,tags,note,engine_path,engine_version,
                fp_seed,fp_platform,fp_brand,fp_brand_version,fp_hardware,fp_timezone,fp_lang,fp_ua,
                proxy,start_urls,window_w,window_h,extensions,state,cdp_enabled,node_id,
                restart_policy,headless,ephemeral,trim_on_stop,sync_enabled,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid, name, payload.get("group_name", ""), json.dumps(payload.get("tags") or [], ensure_ascii=False),
                payload.get("note", ""), engine_path, engine_version,
                seed, payload.get("fp_platform", "host"), payload.get("fp_brand", ""),
                payload.get("fp_brand_version", ""), payload.get("fp_hardware"),
                payload.get("fp_timezone", ""), payload.get("fp_lang", ""), payload.get("fp_ua", ""),
                payload.get("proxy", ""), json.dumps(payload.get("start_urls") or [], ensure_ascii=False),
                int(payload.get("window_w") or 1280), int(payload.get("window_h") or 800),
                json.dumps(payload.get("extensions") or [], ensure_ascii=False),
                "stopped", 1 if payload.get("cdp_enabled", True) else 0, s.node_id,
                payload.get("restart_policy", "on-failure"),
                1 if payload.get("headless") else 0,
                1 if payload.get("ephemeral") else 0,
                1 if payload.get("trim_on_stop") else 0,
                1 if payload.get("sync_enabled", True) else 0,
                now, now,
            ),
        )
    udd_path(pid).mkdir(parents=True, exist_ok=True)
    get_db().audit("profile.create", pid, detail={"name": name, "seed": seed})
    logger.info("创建环境 name=%s id=%s seed=%s", name, pid, seed)
    return get_profile(pid)


def update_profile(ref: str, payload: dict[str, Any]) -> dict[str, Any]:
    p = get_profile(ref)
    allowed = {
        "name", "group_name", "tags", "note", "fp_platform", "fp_brand", "fp_brand_version",
        "fp_hardware", "fp_timezone", "fp_lang", "fp_ua", "proxy", "start_urls",
        "window_w", "window_h", "extensions", "restart_policy", "headless", "ephemeral",
        "trim_on_stop", "sync_enabled", "cdp_enabled",
    }
    bad = set(payload) - allowed - {"fp_seed"}
    if "fp_seed" in payload:
        raise ProfileError(
            "SEED_IMMUTABLE",
            "指纹种子不可直接修改。改动它等于换一台设备，会让已有登录态全部失效。"
            "如果确定要换，请调用 POST /api/v1/profiles/{id}/fingerprint/reset。",
        )
    if bad:
        raise ProfileError("UNKNOWN_FIELDS", f"不认识的字段：{'、'.join(sorted(bad))}")
    if p["state"] in ("starting", "stopping"):
        raise ProfileError("STATE_CONFLICT", f"环境正在 {p['state']}，等它稳定后再改", status=409)
    _update(p["id"], **payload)
    get_db().audit("profile.update", p["id"], detail={"fields": sorted(payload)})
    return get_profile(p["id"])


def reset_fingerprint(ref: str, confirm: str) -> dict[str, Any]:
    p = get_profile(ref)
    if confirm != p["name"]:
        raise ProfileError("CONFIRM_REQUIRED", f"这是危险操作：会换掉指纹并清空该环境的 cookie。请把 confirm 写成环境名「{p['name']}」")
    if p["state"] == "running":
        raise ProfileError("STATE_CONFLICT", "请先停止环境再重置指纹", status=409)
    new_seed = allocate_seed()
    _update(p["id"], fp_seed=new_seed, sync_state="dirty")
    udd = udd_path(p["id"])
    if udd.exists():
        shutil.rmtree(udd, ignore_errors=True)
    udd.mkdir(parents=True, exist_ok=True)
    get_db().audit("profile.fingerprint_reset", p["id"], detail={"old": p["fp_seed"], "new": new_seed})
    logger.warning("重置指纹 name=%s 旧种子=%s 新种子=%s", p["name"], p["fp_seed"], new_seed)
    return get_profile(p["id"])


def delete_profile(ref: str, purge: bool = False) -> dict[str, Any]:
    p = get_profile(ref)
    if p["state"] in ("running", "starting"):
        stop(p["id"], force=True)
    with get_db().conn() as c:
        c.execute("DELETE FROM profiles WHERE id=?", (p["id"],))
    if purge:
        shutil.rmtree(profile_dir(p["id"]), ignore_errors=True)
    get_db().audit("profile.delete", p["id"], detail={"name": p["name"], "purge": purge})
    logger.warning("删除环境 name=%s purge=%s", p["name"], purge)
    return {"deleted": True, "id": p["id"], "purged": purge}


# ── 启停 ──────────────────────────────────────────────────
def start(ref: str, urls: list[str] | None = None, wait: bool = True) -> dict[str, Any]:
    s = get_settings()
    p = get_profile(ref)
    if p["state"] in ("running", "starting"):
        return {"profile": p, "already_running": True, "cdp": cdp_info(p)}

    running = len(list_profiles(state="running"))
    if running >= s.max_running:
        raise ProfileError("MAX_RUNNING", f"已达到并发上限 {s.max_running}（ZW_MAX_RUNNING），先停掉一些环境", status=409)

    lease = LocalLease(profile_dir(p["id"]))
    try:
        lease.acquire("run")
    except LeaseBusy as exc:
        raise ProfileError(
            "PROFILE_BUSY",
            f"{p['name']} 正被 {exc.owner} 占用（{exc.kind}，剩余 {exc.remain} 秒）。"
            "如果确认那是已经死掉的进程，可以用 zw profile unlock 强制释放。",
            status=409,
        ) from exc

    try:
        chrome = engines.find_chrome(s)
    except engines.EngineError as exc:
        lease.release()
        raise ProfileError(exc.code, exc.message, status=422) from exc

    udd = udd_path(p["id"])
    udd.mkdir(parents=True, exist_ok=True)
    port = pool().allocate(p["id"])

    row = dict(p)
    if urls:
        row["start_urls"] = urls
    cmdline = engines.build_cmdline(chrome, udd, port, row, extra_args=s.chrome_extra_args)

    _update(p["id"], state="starting", cdp_port=port, last_error="")
    logger.info("启动环境 name=%s port=%s", p["name"], port)
    try:
        proc = engines.spawn(cmdline, profile_dir(p["id"]) / "browser.log")
    except OSError as exc:
        pool().release(port)
        _update(p["id"], state="failed", last_error=str(exc))
        lease.release()
        raise ProfileError("SPAWN_FAILED", f"拉起浏览器失败：{exc}", status=500) from exc

    _update(p["id"], pid=proc.pid)
    try:
        ws_url, http_url = engines.wait_ready(port, timeout=s.launch_timeout, proc=proc)
    except engines.EngineError as exc:
        engines.terminate(proc.pid, grace=2)
        pool().release(port)
        _update(p["id"], state="failed", pid=None, last_error=exc.message)
        lease.release()
        get_db().audit("profile.start", p["id"], result="fail", error=exc.message)
        raise ProfileError(exc.code, exc.message, status=500) from exc

    from .. import gateway

    gateway.ROUTER.register(p["id"], port, p["name"])
    _update(p["id"], state="running", last_start_at=time.time(), last_error="")
    get_db().audit("profile.start", p["id"], detail={"port": port, "pid": proc.pid})
    logger.info("环境已就绪 name=%s pid=%s ws=%s", p["name"], proc.pid, ws_url)

    fresh = get_profile(p["id"])
    return {
        "profile": fresh,
        "already_running": False,
        "cdp": cdp_info(fresh),
        "local": {"pid": proc.pid, "port": port, "ws_url": ws_url, "http_url": http_url},
        "cmdline": proc.args if hasattr(proc, "args") else cmdline,
    }


def stop(ref: str, force: bool = False) -> dict[str, Any]:
    p = get_profile(ref)
    if p["state"] not in ("running", "starting", "crashed"):
        return {"profile": p, "already_stopped": True}

    from .. import gateway

    port = p.get("cdp_port") or 0
    pid = p.get("pid") or 0
    _update(p["id"], state="stopping")

    method = "none"
    if port and not force:
        if engines.close_via_cdp(port):
            method = "cdp-browser.close"
            for _ in range(20):
                if not engines.probe(port):
                    break
                time.sleep(0.25)
    if pid:
        m2 = engines.terminate(pid, grace=get_settings().stop_grace)
        method = f"{method}+{m2}" if method != "none" else m2
    if port:
        pool().release(port)
    gateway.ROUTER.unregister(p["id"])

    freed = None
    if p.get("trim_on_stop"):
        freed = engines.trim(udd_path(p["id"]))

    _update(p["id"], state="stopped", pid=None, last_stop_at=time.time())
    # 明确要释放，不能因为 owner 不匹配而失败
    LocalLease(profile_dir(p["id"])).force_release()
    get_db().audit("profile.stop", p["id"], detail={"method": method, "freed": freed})
    logger.info("停止环境 name=%s 方式=%s", p["name"], method)
    return {"profile": get_profile(p["id"]), "method": method, "trim": freed}


def restart(ref: str, urls: list[str] | None = None) -> dict[str, Any]:
    p = get_profile(ref)
    stop(p["id"])
    # 坑：不等端口真正释放就重启，会撞 SingletonLock
    for _ in range(40):
        if not p.get("cdp_port") or engines._port_free(int(p["cdp_port"])):
            break
        time.sleep(0.25)
    return start(p["id"], urls=urls)


# ── CDP 令牌与地址 ────────────────────────────────────────
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# 每个环境最多同时保留多少个有效令牌。
# 令牌是"一次连接凭证"，正常情况下客户端拿到就用；封顶是为了防止
# 某个 UI 反复点"取 CDP"把表撑大。
MAX_LIVE_TOKENS_PER_PROFILE = 20


def issue_cdp_token(profile_id: str, ttl: int | None = None, issued_to: str = "") -> str:
    s = get_settings()
    ttl = min(max(ttl or s.cdp_token_ttl, 60), 7200)
    raw = "zwc_" + secrets.token_urlsafe(32)
    now = time.time()
    with get_db().conn() as c:
        # 先清掉这个环境的过期令牌
        c.execute("DELETE FROM cdp_tokens WHERE profile_id=? AND expires_at < ?", (profile_id, now))
        # 再按数量封顶：这次插入之后总数不能超过 MAX，所以先裁到 MAX-1
        keep = max(0, MAX_LIVE_TOKENS_PER_PROFILE - 1)
        live = c.execute(
            "SELECT token_hash FROM cdp_tokens WHERE profile_id=? ORDER BY issued_at DESC", (profile_id,)
        ).fetchall()
        for row in live[keep:]:
            c.execute("DELETE FROM cdp_tokens WHERE token_hash=?", (row["token_hash"],))
        c.execute(
            "INSERT INTO cdp_tokens(token_hash,profile_id,issued_to,issued_at,expires_at) VALUES(?,?,?,?,?)",
            (_token_hash(raw), profile_id, issued_to, now, now + ttl),
        )
    return raw


def purge_expired_tokens() -> int:
    """清理全库过期令牌。由健康巡检周期调用，返回删除行数。"""
    with get_db().conn() as c:
        cur = c.execute("DELETE FROM cdp_tokens WHERE expires_at < ?", (time.time(),))
        return cur.rowcount or 0


def token_stats() -> dict[str, Any]:
    now = time.time()
    with get_db().conn() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM cdp_tokens").fetchone()["n"]
        live = c.execute("SELECT COUNT(*) AS n FROM cdp_tokens WHERE expires_at > ?", (now,)).fetchone()["n"]
    return {"total": total, "live": live, "expired": total - live}


def verify_cdp_token(profile_id: str, token: str) -> bool:
    if not token:
        return False
    with get_db().conn() as c:
        row = c.execute(
            "SELECT 1 FROM cdp_tokens WHERE token_hash=? AND profile_id=? AND expires_at>?",
            (_token_hash(token), profile_id, time.time()),
        ).fetchone()
    return row is not None


def cdp_info(p: dict[str, Any], ttl: int | None = None) -> dict[str, Any] | None:
    """返回给客户端的连接信息。

    关键点：Playwright 的 `connectOverCDP` 只接受两种形状 ——
    `http://host:port`（它会自己去取 `/json/version`）或完整的
    `ws://host:port/devtools/browser/<GUID>`。
    因为网关的路径里带令牌查询串，直接拼 http 形状会被 Playwright 拼坏，
    所以这里**主动解析一次**发现接口，把重写后的完整 ws 地址返回给调用方。
    """
    s = get_settings()
    if p.get("state") != "running" or not p.get("cdp_port"):
        return None
    ttl_eff = min(max(ttl or s.cdp_token_ttl, 60), 7200)
    token = issue_cdp_token(p["id"], ttl=ttl_eff)
    base = s.gateway_public_base
    ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
    raw_json = f"{base}/cdp/{p['id']}/json/version?token={token}"
    full_ws = ""
    resolve_error = ""
    try:
        import httpx

        resp = httpx.get(raw_json, timeout=6)
        resp.raise_for_status()
        full_ws = resp.json().get("webSocketDebuggerUrl", "")
    except Exception as exc:
        resolve_error = str(exc)

    info: dict[str, Any] = {
        "token": token,
        "token_ttl": ttl_eff,
        "local_port": p["cdp_port"],
        "gateway_http_endpoint": f"{base}/cdp/{p['id']}",
        "raw_cdp_json": raw_json,
        "gateway_ws_url": full_ws or f"{ws_base}/cdp/{p['id']}?token={token}",
    }
    if full_ws:
        info["playwright"] = f"await chromium.connectOverCDP('{full_ws}')"
        info["puppeteer"] = f"await puppeteer.connect({{ browserWSEndpoint: '{full_ws}' }})"
        info["hint"] = "直接用 playwright / puppeteer 字段里的地址即可；令牌过期后重新取一次。"
    else:
        info["playwright"] = f"// 暂时取不到完整地址（{resolve_error}），先 GET raw_cdp_json 手动取 webSocketDebuggerUrl"
        info["hint"] = f"发现接口暂时不可用：{resolve_error}"
    return info


def resolve_ws_url(profile_id: str, token: str, timeout: float = 8.0) -> str:
    """在网关侧把「环境」解析成真正的 ws 地址（已带 token）。"""
    import httpx

    s = get_settings()
    url = f"{s.gateway_public_base}/cdp/{profile_id}/json/version?token={token}"
    resp = httpx.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()["webSocketDebuggerUrl"]


# ── 健康巡检 ──────────────────────────────────────────────
def rebuild_gateway_routes() -> dict[str, Any]:
    """core 重启后重建网关路由表（浏览器进程是独立于 core 的，可能还活着）。

    返回 {恢复: [...], 判定崩溃: [...]}。这是"控制面重启不影响正在跑的浏览器"的落地。
    """
    from .. import gateway

    restored: list[str] = []
    crashed: list[str] = []
    reaped: list[str] = []

    # 先清理"持有者进程已经死了"的本地租约，否则它会挡住下一次启动 90 秒
    for p in list_profiles():
        if p["state"] == "running":
            continue
        try:
            if LocalLease(profile_dir(p["id"])).reap_if_stale():
                reaped.append(p["name"])
        except Exception:
            pass

    for p in list_profiles(state="running"):
        port = p.get("cdp_port") or 0
        if port and engines.probe(port):
            pool().reserve(port, p["id"])
            gateway.ROUTER.register(p["id"], port, p["name"])
            restored.append(p["name"])
        else:
            _update(p["id"], state="crashed", last_error="core 重启时发现调试端口已不可达")
            crashed.append(p["name"])
    if restored or crashed or reaped:
        logger.info("网关路由重建：恢复 %s，判定崩溃 %s，清理死租约 %s", restored, crashed, reaped)
    return {"restored": restored, "crashed": crashed, "reaped_leases": reaped}


def health_tick() -> list[dict[str, Any]]:
    """由后台任务周期调用：发现崩溃的环境并按策略处理。"""
    events: list[dict[str, Any]] = []
    for p in list_profiles(state="running"):
        port = p.get("cdp_port") or 0
        if not port or engines.probe(port):
            continue
        # 端口不通了 —— 判定为崩溃
        _update(p["id"], state="crashed", last_error="健康检查发现调试端口不可达")
        from .. import gateway

        gateway.ROUTER.unregister(p["id"])
        pool().release(port)
        events.append({"type": "profile.crashed", "id": p["id"], "name": p["name"]})
        logger.warning("环境疑似崩溃 name=%s port=%s", p["name"], port)
        if p.get("restart_policy") in ("on-failure", "always"):
            try:
                stop(p["id"], force=True)
                start(p["id"])
                events.append({"type": "profile.restarted", "id": p["id"], "name": p["name"]})
            except Exception as exc:  # 重启失败不要影响巡检
                logger.error("自动重启失败 name=%s: %s", p["name"], exc)
    return events


def stats() -> dict[str, Any]:
    rows = list_profiles()
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    s = get_settings()
    total = 0
    for r in rows:
        total += engines.dir_size(udd_path(r["id"]))
    return {
        "profiles": len(rows),
        "by_state": by_state,
        "running": by_state.get("running", 0),
        "max_running": s.max_running,
        "port_pool": {"start": s.port_pool_start, "end": s.port_pool_end, "used": len(pool().used())},
        "disk_bytes": total,
        "disk_human": _human(total),
        "root": str(s.root),
        "engine": _engine_summary(),
        "node_id": s.node_id,
        "cdp_tokens": token_stats(),
    }


def _engine_summary() -> dict[str, Any]:
    try:
        chrome = engines.find_chrome(get_settings())
        return {"path": str(chrome), "version": engines.engine_version(chrome), "ok": True}
    except engines.EngineError as exc:
        return {"path": "", "version": "", "ok": False, "error": exc.message}


def _human(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.1f}{u}"
        size /= 1024
    return f"{n}B"

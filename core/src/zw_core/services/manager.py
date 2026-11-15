"""环境生命周期管理器：这是 core 的心脏。

一次 `start` 做的事情（顺序不能乱）：
1. 查租约（本机 + hub），有别人持有就 409
2. 检查并发上限
3. 从端口池取端口；**确保 UDD 里没有残留的 SingletonLock / DevToolsActivePort**
4. 拼命令行（指纹 / 代理 / 扩展 / 窗口）与子进程环境（时区、语言）
5. 拉起进程
6. 等 CDP 就绪，并用 `DevToolsActivePort` 证明端口归属（防串号）
7. 写回 pid/port/state，登记到网关
8. 返回网关地址（含短期令牌）

一次 `stop`：CDP 优雅关 → SIGTERM → SIGKILL → 释放端口 → 撤销网关路由 → 可选 trim。

## 0.2.0 修掉的真实缺陷

- `restart` 以前先 `stop` 再 `start`，中间靠 `_port_free` 猜端口是否释放；
  现在 `stop` 会等到进程真的消失，且 `start` 会清掉残留锁文件，不再撞 SingletonLock。
- 崩溃自动重启以前没有次数上限，一个坏内核能把 CPU 打满；现在有退避与上限。
- `stats()` 以前每次请求都全盘遍历所有 UDD 算体积，几百个环境时会卡住控制台；
  现在体积结果缓存 60 秒。
- 扩展字段以前只认绝对路径；现在认库里的 slug，并在启动前解析、校验缺失。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..db import get_db
from . import engines, proxy as proxysvc
from .leases import LeaseBusy, LocalLease

logger = logging.getLogger("zw.lifecycle")

# 进程内的端口池（重启后从数据库里的 cdp_port 重建）
_POOL: engines.PortPool | None = None
_POOL_LOCK = threading.Lock()

# UDD 里的锁文件：进程异常退出后会留下，下次启动会撞 SingletonLock 直接退出
_STALE_LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie", "DevToolsActivePort", "lockfile")

# 体积缓存：{profile_id: (ts, bytes)}
_SIZE_CACHE: dict[str, tuple[float, int]] = {}
_SIZE_TTL = 60.0


def pool() -> engines.PortPool:
    global _POOL
    with _POOL_LOCK:
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
_BOOL_FIELDS = ("cdp_enabled", "headless", "ephemeral", "trim_on_stop", "sync_enabled")


def row_to_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    for f in _JSON_FIELDS:
        raw = d.get(f) or "[]"
        try:
            d[f] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            d[f] = []
    for f in _BOOL_FIELDS:
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
        sql += " AND (name LIKE ? OR note LIKE ? OR tags LIKE ?)"
        args += [f"%{q}%", f"%{q}%", f"%{q}%"]
    sql += " ORDER BY created_at DESC"
    with get_db().conn() as c:
        return [row_to_dict(r) for r in c.execute(sql, args).fetchall()]


def get_profile(ref: str) -> dict[str, Any]:
    if not ref:
        raise ProfileError("PROFILE_NOT_FOUND", "没有指定环境（id 或名字）", status=404)
    with get_db().conn() as c:
        row = c.execute("SELECT * FROM profiles WHERE id=? OR name=?", (ref, ref)).fetchone()
    if row is None:
        raise ProfileError("PROFILE_NOT_FOUND", f"找不到环境：{ref}", status=404)
    return row_to_dict(row)


def groups() -> list[dict[str, Any]]:
    """按分组聚合。

    坑：`GROUP BY name` 里的 `name` 会被 SQLite 解析成 **profiles.name 列**
    （每个环境都不同），而不是上面那个 `AS name` 别名 —— 结果每个分组计数都是 1。
    所以这里显式 group by 表达式本身，不靠别名。
    """
    with get_db().conn() as c:
        rows = c.execute(
            "SELECT COALESCE(NULLIF(group_name,''),'未分组') AS gname, COUNT(*) AS n, "
            "SUM(CASE WHEN state='running' THEN 1 ELSE 0 END) AS running "
            "FROM profiles GROUP BY COALESCE(NULLIF(group_name,''),'未分组') ORDER BY n DESC"
        ).fetchall()
    return [{"name": r["gname"], "count": r["n"], "running": r["running"] or 0} for r in rows]


def _update(profile_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    for key in _JSON_FIELDS:
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    for key in _BOOL_FIELDS:
        if key in fields and isinstance(fields[key], bool):
            fields[key] = int(fields[key])
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_db().conn() as c:
        c.execute(f"UPDATE profiles SET {cols} WHERE id=?", (*fields.values(), profile_id))


# ── 扩展解析 ──────────────────────────────────────────────
def resolve_extensions(profile: dict[str, Any]) -> tuple[list[str], list[str]]:
    """把环境里的扩展引用解析成绝对路径。返回 (路径列表, 警告列表)。

    兼容两种写法：
    - 库里 slug：`"ublock-origin"` → 查 extensions 表拿最新版本的 path
    - 老数据里的绝对路径：存在就用，不存在给警告
    """
    refs = profile.get("extensions") or []
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except json.JSONDecodeError:
            refs = []
    paths: list[str] = []
    warnings: list[str] = []
    with get_db().conn() as c:
        for ref in refs:
            if isinstance(ref, dict):
                ref = ref.get("slug") or ref.get("path") or ""
            ref = str(ref).strip()
            if not ref:
                continue
            row = c.execute(
                "SELECT path FROM extensions WHERE slug=? ORDER BY added_at DESC LIMIT 1", (ref,)
            ).fetchone()
            if row:
                paths.append(row["path"])
                continue
            if Path(ref).exists():
                paths.append(ref)
                continue
            warnings.append(f"扩展 {ref} 在库里找不到，已跳过")
    return paths, warnings


def proxy_spec_for(profile: dict[str, Any]) -> proxysvc.ProxySpec | None:
    """环境生效的代理：优先 `proxy` 字段，其次 `proxy_id` 指向的库条目。"""
    raw = (profile.get("proxy") or "").strip()
    if not raw and profile.get("proxy_id"):
        with get_db().conn() as c:
            row = c.execute("SELECT * FROM proxies WHERE id=?", (int(profile["proxy_id"]),)).fetchone()
        if row:
            auth = f"{row['username']}:{row['password']}@" if row["username"] else ""
            raw = f"{row['scheme']}://{auth}{row['host']}:{row['port']}"
    return proxysvc.parse_proxy(raw)


# ── 创建 / 修改 ───────────────────────────────────────────
def allocate_seed() -> int:
    """分配一个全局唯一的指纹种子。同一个种子 = 同一个指纹 = 账号可关联，所以必须唯一。"""
    with get_db().conn() as c:
        for _ in range(50):
            seed = secrets.randbelow(2**32 - 2) + 1
            if not c.execute("SELECT 1 FROM profiles WHERE fp_seed=?", (seed,)).fetchone():
                return seed
    raise ProfileError("SEED_ALLOC_FAILED", "连续 50 次都没找到空闲的指纹种子，环境数量可能太多了")


_WRITABLE = {
    "name", "group_name", "tags", "note", "fp_platform", "fp_platform_version", "fp_brand",
    "fp_brand_version", "fp_hardware", "fp_timezone", "fp_lang", "fp_accept_lang",
    "fp_disable_spoofing", "fp_ua", "proxy", "proxy_id", "start_urls", "window_w", "window_h",
    "extensions", "restart_policy", "headless", "ephemeral", "trim_on_stop", "sync_enabled",
    "cdp_enabled",
}

_RESTART_POLICIES = ("never", "on-failure", "always")

# 跨机/跨机搬家的环境设置字段：不含 name/note/state/pid 这些"本地身份"与运行时字段。
# 定义放这里，因为 manager 与 sync 都要用；重复定义两遍迟早会漂移。
PORTABLE_FIELDS = (
    "group_name", "tags", "fp_platform", "fp_platform_version", "fp_brand", "fp_brand_version",
    "fp_hardware", "fp_timezone", "fp_lang", "fp_accept_lang", "fp_disable_spoofing", "fp_ua",
    "proxy", "proxy_id", "start_urls", "window_w", "window_h", "extensions",
    "restart_policy", "headless", "trim_on_stop",
)


def portable_fields(profile: dict[str, Any]) -> dict[str, Any]:
    """抽出可以从一台机器搬到另一台机器的设置。"""
    return {f: profile[f] for f in PORTABLE_FIELDS if f in profile}


def _validate(payload: dict[str, Any]) -> None:
    """把明显错误的输入挡在入库前——它们不该等到启动时才炸。"""
    if "window_w" in payload or "window_h" in payload:
        w = int(payload.get("window_w") or 1280)
        h = int(payload.get("window_h") or 800)
        if not (320 <= w <= 8192 and 240 <= h <= 8192):
            raise ProfileError("INVALID_WINDOW", "窗口尺寸要在 320×240 到 8192×8192 之间")
    if payload.get("fp_platform") and payload["fp_platform"] not in ("host", "windows", "linux", "macos"):
        raise ProfileError("INVALID_PLATFORM", "fp_platform 只能是 host / windows / linux / macos")
    if payload.get("restart_policy") and payload["restart_policy"] not in _RESTART_POLICIES:
        raise ProfileError("INVALID_RESTART_POLICY", f"restart_policy 只能是 {'/'.join(_RESTART_POLICIES)}")
    if payload.get("proxy"):
        proxysvc.parse_proxy(str(payload["proxy"]))  # 非法就抛 PROXY_*
    if payload.get("fp_timezone"):
        tz = str(payload["fp_timezone"])
        if not _tz_valid(tz):
            raise ProfileError("INVALID_TIMEZONE", f"不认识的时区：{tz}（例如 Asia/Shanghai）")
    urls = payload.get("start_urls")
    if isinstance(urls, list):
        for u in urls:
            if not str(u).startswith(("http://", "https://", "about:", "file://", "chrome://")):
                raise ProfileError("INVALID_URL", f"起始网址只接受 http/https/about/file/chrome，收到 {u}")


def _tz_valid(tz: str) -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
        return True
    except Exception:
        return False


def create_profile(payload: dict[str, Any], profile_id: str | None = None) -> dict[str, Any]:
    """创建环境。`profile_id` 只在"从同步中心导入"时由外部指定（必须与远端一致）。"""
    s = get_settings()
    name = (payload.get("name") or "").strip()
    if not name:
        raise ProfileError("INVALID_NAME", "环境名不能为空")
    if len(name) > 64:
        raise ProfileError("INVALID_NAME", "环境名不要超过 64 个字符")
    _validate(payload)
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
                fp_seed,fp_platform,fp_platform_version,fp_brand,fp_brand_version,fp_hardware,
                fp_timezone,fp_lang,fp_accept_lang,fp_disable_spoofing,fp_ua,
                proxy,proxy_id,start_urls,window_w,window_h,extensions,state,cdp_enabled,node_id,
                restart_policy,headless,ephemeral,trim_on_stop,sync_enabled,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid, name, payload.get("group_name", ""), json.dumps(payload.get("tags") or [], ensure_ascii=False),
                payload.get("note", ""), engine_path, engine_version,
                seed, payload.get("fp_platform", "host"), payload.get("fp_platform_version", ""),
                payload.get("fp_brand", ""), payload.get("fp_brand_version", ""), payload.get("fp_hardware"),
                payload.get("fp_timezone", ""), payload.get("fp_lang", ""), payload.get("fp_accept_lang", ""),
                payload.get("fp_disable_spoofing", ""), payload.get("fp_ua", ""),
                _clean_proxy_for_store(payload),
                int(payload["proxy_id"]) if payload.get("proxy_id") else None,
                json.dumps(payload.get("start_urls") or [], ensure_ascii=False),
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


def _clean_proxy_for_store(payload: dict[str, Any]) -> str:
    """入库前把凭据从 `proxy` 字段里剥掉，只留上游地址。

    凭据单独存在 proxies 表（带 password 列）或由 proxy_id 引用；
    这样 `GET /profiles` 的响应里永远不会出现密码（0.1.0 会把整串带密码的
    代理地址原样返回给前端，是个真实的凭据泄漏）。
    """
    raw = (payload.get("proxy") or "").strip()
    if not raw:
        return ""
    spec = proxysvc.parse_proxy(raw)
    return spec.upstream if spec else ""


def update_profile(ref: str, payload: dict[str, Any]) -> dict[str, Any]:
    p = get_profile(ref)
    if "fp_seed" in payload:
        raise ProfileError(
            "SEED_IMMUTABLE",
            "指纹种子不可直接修改。改动它等于换一台设备，会让已有登录态全部失效。"
            "如果确定要换，请调用 POST /api/v1/profiles/{id}/fingerprint/reset。",
        )
    bad = set(payload) - _WRITABLE
    if bad:
        raise ProfileError("UNKNOWN_FIELDS", f"不认识的字段：{'、'.join(sorted(bad))}")
    if p["state"] in ("starting", "stopping"):
        raise ProfileError("STATE_CONFLICT", f"环境正在 {p['state']}，等它稳定后再改", status=409)
    _validate(payload)

    clean = dict(payload)
    if "name" in clean:
        new_name = str(clean["name"]).strip()
        if not new_name:
            raise ProfileError("INVALID_NAME", "环境名不能为空")
        with get_db().conn() as c:
            row = c.execute("SELECT id FROM profiles WHERE name=? AND id<>?", (new_name, p["id"])).fetchone()
        if row:
            raise ProfileError("NAME_TAKEN", f"环境名已存在：{new_name}", status=409)
        clean["name"] = new_name

    if "proxy" in clean:
        # 换成"凭据已剥离"的上游地址；同时清掉 proxy_id，避免两个来源打架
        clean["proxy"] = _clean_proxy_for_store(clean)
        if "proxy_id" not in clean:
            clean["proxy_id"] = None
    if "proxy_id" in clean and clean["proxy_id"]:
        with get_db().conn() as c:
            row = c.execute("SELECT 1 FROM proxies WHERE id=?", (int(clean["proxy_id"]),)).fetchone()
        if not row:
            raise ProfileError("PROXY_NOT_FOUND", f"代理库里没有 id={clean['proxy_id']}", status=404)

    _update(p["id"], **clean)
    get_db().audit("profile.update", p["id"], detail={"fields": sorted(clean)})

    fresh = get_profile(p["id"])
    # 正在跑的环境改了关键启动参数，提示需要重启才生效（不擅自替用户重启）
    hot = {"proxy", "proxy_id", "extensions", "headless", "fp_platform", "fp_platform_version",
           "fp_brand", "fp_brand_version", "fp_hardware", "fp_timezone", "fp_lang",
           "fp_accept_lang", "fp_disable_spoofing", "fp_ua", "window_w", "window_h", "cdp_enabled"}
    needs_restart = sorted(set(clean) & hot) if fresh["state"] == "running" else []
    fresh["needs_restart"] = needs_restart
    if needs_restart:
        fresh["restart_hint"] = "这些字段要重启环境才生效：" + "、".join(needs_restart)
    return fresh


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
        c.execute("DELETE FROM cdp_tokens WHERE profile_id=?", (p["id"],))
    if purge:
        shutil.rmtree(profile_dir(p["id"]), ignore_errors=True)
    _SIZE_CACHE.pop(p["id"], None)
    get_db().audit("profile.delete", p["id"], detail={"name": p["name"], "purge": purge})
    logger.warning("删除环境 name=%s purge=%s", p["name"], purge)
    return {"deleted": True, "id": p["id"], "purged": purge}


# ── 启停 ──────────────────────────────────────────────────
def _clear_stale_locks(udd: Path) -> list[str]:
    """删掉上次异常退出留下的锁文件。返回删掉的名单（进日志用）。"""
    removed: list[str] = []
    for name in _STALE_LOCK_FILES:
        f = udd / name
        if f.exists() or f.is_symlink():
            try:
                f.unlink()
                removed.append(name)
            except OSError:
                pass
    return removed


def _prepare_launch(profile: dict[str, Any]) -> dict[str, Any]:
    """把数据库行加工成可以直接喂给 build_cmdline 的字典（解析扩展、注入代理扩展）。"""
    row = dict(profile)
    paths, warnings = resolve_extensions(row)
    if warnings:
        row["_extension_warnings"] = warnings
        logger.warning("环境 %s 的扩展有问题：%s", row.get("name"), warnings)
    spec = proxy_spec_for(row)
    if spec:
        row["proxy"] = spec.upstream
        if spec.needs_auth:
            # Chromium 的 --proxy-server 不支持认证，必须靠扩展应答挑战
            ext = proxysvc.ensure_auth_extension(spec)
            paths = [str(ext)] + paths
            row["_proxy_auth"] = True
    row["_resolved_extensions"] = paths
    return row


def start(ref: str, urls: list[str] | None = None, wait: bool = True) -> dict[str, Any]:
    s = get_settings()
    p = get_profile(ref)
    if p["state"] in ("running", "starting"):
        return {"profile": p, "already_running": True, "cdp": cdp_info(p)}

    running = len(list_profiles(state="running"))
    if running >= s.max_running:
        raise ProfileError("MAX_RUNNING", f"已达到并发上限 {s.max_running}（ZW_MAX_RUNNING），先停掉一些环境", status=409)

    # 人主动启动 = "我处理过了，再试一次"，把崩溃退避计数清零。
    # 不清的话，崩溃 3 次后的环境即使你修好了问题，也永远不会再被自动重启。
    # 注意：健康巡检内部的自动重启走的是 restart_count+1 的那条路径，不会被这里重置。
    if int(p.get("restart_count") or 0):
        _update(p["id"], restart_count=0)
        p = get_profile(p["id"])

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
    locks = _clear_stale_locks(udd)
    if locks:
        logger.info("清理 %s 的残留锁文件：%s", p["name"], locks)

    port = pool().allocate(p["id"])
    try:
        row = _prepare_launch(p)
    except proxysvc.ProxyError as exc:
        pool().release(port)
        lease.release()
        raise ProfileError(exc.code, exc.message, status=422) from exc

    if urls:
        row["start_urls"] = urls
    cmdline = engines.build_cmdline(chrome, udd, port, row, extra_args=s.chrome_extra_args)

    _update(p["id"], state="starting", cdp_port=port, last_error="")
    logger.info("启动环境 name=%s port=%s", p["name"], port)
    try:
        proc = engines.spawn(cmdline, profile_dir(p["id"]) / "browser.log", env=engines.child_env(p))
    except OSError as exc:
        pool().release(port)
        _update(p["id"], state="failed", last_error=str(exc))
        lease.release()
        raise ProfileError("SPAWN_FAILED", f"拉起浏览器失败：{exc}", status=500) from exc

    _update(p["id"], pid=proc.pid)
    try:
        ws_url, http_url = engines.wait_ready(port, timeout=s.launch_timeout, proc=proc, udd=udd)
    except engines.EngineError as exc:
        engines.terminate(proc.pid, grace=2)
        pool().release(port)
        _update(p["id"], state="failed", pid=None, cdp_port=None, last_error=exc.message)
        lease.release()
        get_db().audit("profile.start", p["id"], result="fail", error=exc.message)
        raise ProfileError(exc.code, exc.message, status=500) from exc

    from .. import gateway

    gateway.ROUTER.register(p["id"], port, p["name"])
    _update(p["id"], state="running", last_start_at=time.time(), last_active_at=time.time(), last_error="")
    get_db().audit("profile.start", p["id"], detail={"port": port, "pid": proc.pid})
    logger.info("环境已就绪 name=%s pid=%s ws=%s", p["name"], proc.pid, ws_url)

    fresh = get_profile(p["id"])
    out: dict[str, Any] = {
        "profile": fresh,
        "already_running": False,
        "cdp": cdp_info(fresh),
        "local": {"pid": proc.pid, "port": port, "ws_url": ws_url, "http_url": http_url},
        "cmdline": proc.args if hasattr(proc, "args") else cmdline,
    }
    if row.get("_extension_warnings"):
        out["warnings"] = row["_extension_warnings"]
    if row.get("_proxy_auth"):
        out.setdefault("warnings", []).append("该环境的代理带用户名密码，已通过内置认证扩展生效（不是命令行参数）")
    return out


def stop(ref: str, force: bool = False) -> dict[str, Any]:
    p = get_profile(ref)
    if p["state"] not in ("running", "starting", "crashed", "failed", "stopping"):
        return {"profile": p, "already_stopped": True}

    from .. import gateway

    port = p.get("cdp_port") or 0
    pid = p.get("pid") or 0
    _update(p["id"], state="stopping")

    method = "none"
    if port and not force:
        if engines.close_via_cdp(port):
            method = "cdp-browser.close"
            for _ in range(40):
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

    # cdp_port 必须清掉：它是"这一次运行"的临时事实，留着会让控制台/CLI 显示一个
    # 属于别人的端口（端口很快会被下一个环境拿走），排错时极易被误导。
    _update(p["id"], state="stopped", pid=None, cdp_port=None, last_stop_at=time.time(), restart_count=0, last_error="")
    # 明确要释放，不能因为 owner 不匹配而失败
    LocalLease(profile_dir(p["id"])).force_release()
    if p.get("ephemeral"):
        # 临时环境：stop 即销毁数据（这是"用完就扔"环境的定义）
        shutil.rmtree(udd_path(p["id"]), ignore_errors=True)
        udd_path(p["id"]).mkdir(parents=True, exist_ok=True)
        logger.info("临时环境 %s 已清空数据目录", p["name"])
    get_db().audit("profile.stop", p["id"], detail={"method": method, "freed": freed})
    logger.info("停止环境 name=%s 方式=%s", p["name"], method)
    return {"profile": get_profile(p["id"]), "method": method, "trim": freed}


def restart(ref: str, urls: list[str] | None = None) -> dict[str, Any]:
    p = get_profile(ref)
    old_port = p.get("cdp_port") or 0
    old_pid = p.get("pid") or 0
    stop(p["id"])
    # 等到"进程真的没了 + 端口真的空了"，否则新进程会撞 SingletonLock 或占到脏端口
    deadline = time.time() + 20
    while time.time() < deadline:
        if old_pid and engines.pid_alive(int(old_pid)):
            time.sleep(0.2)
            continue
        if old_port and not engines._port_free(int(old_port)):
            time.sleep(0.2)
            continue
        break
    return start(p["id"], urls=urls)


def start_many(refs: list[str], concurrency: int = 3) -> dict[str, Any]:
    """批量启动。真并发（线程池），但每个环境仍受租约与并发上限约束。"""
    import concurrent.futures

    def one(rid: str) -> dict[str, Any]:
        try:
            start(rid)
            return {"ref": rid, "ok": True}
        except Exception as exc:  # 单个失败不影响其它
            return {"ref": rid, "ok": False, "error": getattr(exc, "message", str(exc))}

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(concurrency, 8))) as poolx:
        for r in poolx.map(one, refs):
            results.append(r)
    return {"total": len(results), "ok": sum(1 for r in results if r["ok"]), "results": results}


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


def local_ws(profile: dict[str, Any]) -> str | None:
    """本机直连的 ws 地址（cookie/页面操作用，走网关反而绕远）。"""
    if profile.get("state") != "running" or not profile.get("cdp_port"):
        return None
    if not profile.get("cdp_enabled", True):
        return None
    try:
        import httpx

        r = httpx.get(f"http://127.0.0.1:{profile['cdp_port']}/json/version", timeout=4)
        return r.json().get("webSocketDebuggerUrl")
    except Exception:
        return None


def touch(profile_id: str) -> None:
    """记一次"有人在用这个环境"，空闲回收据此判断。"""
    _update(profile_id, last_active_at=time.time())


# ── 指纹体检 ──────────────────────────────────────────────
def probe_fingerprint(ref: str, start_if_needed: bool = True) -> dict[str, Any]:
    """读真机指纹，并给出"看起来自洽吗"的警告。

    这些警告只是启发式：真机指纹是否合格要靠 browserscan / creepjs 实测，
    本地能判断的只有"内部是否自相矛盾"（UA 说 Windows 但 platform 是 Linux 之类）。
    """
    from . import cookies as ck

    p = get_profile(ref)
    if p["state"] != "running":
        if not start_if_needed:
            raise ProfileError("STATE_CONFLICT", f"{p['name']} 没有在运行", status=409)
        start(p["id"])
        p = get_profile(p["id"])
    ws = local_ws(p)
    if ws is None:
        raise ProfileError("CDP_UNAVAILABLE", f"{p['name']} 没有可用的调试端口（cdp_enabled=false？）", status=409)
    data = ck.probe_fingerprint_sync(ws)
    data["profile_id"] = p["id"]
    data["name"] = p["name"]
    data["state"] = p["state"]
    data["warnings"] = _fingerprint_warnings(p, data)
    return data


def _fingerprint_warnings(p: dict[str, Any], d: dict[str, Any]) -> list[str]:
    out: list[str] = []
    ua = str(d.get("ua") or "")
    platform = str(d.get("platform") or "")
    ua_data = d.get("ua_data") or {}
    tz = str(d.get("timezone") or "")

    if d.get("webdriver") is True:
        out.append("navigator.webdriver = true，任何风控都会一眼认出这是自动化")
    if not d.get("webgl_renderer"):
        out.append("WebGL 渲染器为空：这台机器没有 GPU，指纹明显异常（真人机器不会这样）")
    elif "SwiftShader" in str(d.get("webgl_renderer")):
        out.append("WebGL 走的是 SwiftShader 软件渲染，属于典型无显卡/无头特征")

    headless_hint = d.get("outer") or {}
    if headless_hint.get("w") == 0 and headless_hint.get("h") == 0:
        out.append("outerWidth/outerHeight 为 0，是 headless 的典型特征")

    want = (p.get("fp_platform") or "host").lower()
    if want in ("windows", "linux", "macos"):
        expect = {"windows": ("Win", "Windows"), "linux": ("Linux", "Linux"), "macos": ("Mac", "macOS")}[want]
        if expect[0].lower() not in platform.lower():
            out.append(f"环境声明平台是 {want}，但 navigator.platform 是 {platform!r}（说明内核没生效）")
        if ua_data.get("platform") and expect[1].lower() not in str(ua_data.get("platform")).lower():
            out.append(f"UA-CH 里的平台是 {ua_data.get('platform')!r}，与环境声明的 {want} 不一致")
        if expect[0].lower() not in ua.lower():
            out.append(f"UA 里没有 {expect[0]}，与声明的平台不一致")

    if p.get("fp_timezone") and tz and p["fp_timezone"] != tz:
        out.append(f"环境设定时区 {p['fp_timezone']}，但浏览器报告 {tz}（内核未应用该参数）")
    if not tz:
        out.append("读不到时区（Intl 被破坏？）")

    if p.get("fp_lang") and d.get("lang") and not str(d["lang"]).lower().startswith(str(p["fp_lang"]).lower()[:2]):
        out.append(f"环境设定语言 {p['fp_lang']}，但 navigator.language 是 {d['lang']}")

    if d.get("hardware_concurrency") and p.get("fp_hardware") and int(p["fp_hardware"]) != int(d["hardware_concurrency"]):
        out.append(f"环境设定 CPU 核数 {p['fp_hardware']}，实际 {d['hardware_concurrency']}")
    return out


# ── 导出 / 导入（单机离线搬家，不走 hub）──────────────────
def export_profile(ref: str, password: str | None = None, cross_machine: bool = True) -> tuple[bytes, str]:
    """把一个环境打成加密单文件，返回 (字节, 建议文件名)。"""
    from . import bundle
    from . import sync as syncsvc

    s = get_settings()
    pw = syncsvc._password(password)  # 复用同步的主密码策略（含 PASSWORD_REQUIRED）
    p = get_profile(ref)
    if p["state"] in ("starting", "stopping"):
        raise ProfileError("EXPORT_DISABLED", f"{p['name']} 正在 {p['state']}，等它稳定再导出", status=409)
    if p.get("ephemeral"):
        raise ProfileError("EPHEMERAL_PROTECTED", "临时环境不允许导出（它的定位就是用完即弃）", status=409)

    was_running = p["state"] == "running"
    cookies: list[dict[str, Any]] = []
    storage: dict[str, Any] = {}
    ws = local_ws(p)
    if ws:
        try:
            from . import cookies as ck

            cookies = ck.dump_cookies_sync(ws)
            origins = sorted({
                f"{c.get('sourceScheme', 'https')}://{str(c.get('domain', '')).lstrip('.')}"
                for c in cookies if c.get("domain")
            })
            origins = [o for o in origins[:30] if o.startswith("http")]
            if origins:
                storage = ck.dump_storage_sync(ws, origins)
        except Exception as exc:
            logger.warning("导出时读取 cookie 失败（将只导出目录）：%s", exc)

    blob, man = bundle.pack(
        udd_path(p["id"]),
        profile_id=p["id"],
        name=p["name"],
        revision=max(1, int(p.get("sync_revision") or 0)),
        password=pw,
        fingerprint={
            "seed": p["fp_seed"], "platform": p["fp_platform"],
            "platform_version": p.get("fp_platform_version", ""),
            "brand": p["fp_brand"], "hardware": p["fp_hardware"],
            "tz": p["fp_timezone"], "lang": p["fp_lang"],
        },
        cookies=cookies,
        storage=storage,
        engine_version=p.get("engine_version", ""),
        source_node=s.node_id,
        extensions=p.get("extensions") or [],
        cross_machine=cross_machine,
        profile_meta=portable_fields(p),
    )
    if len(blob) > s.sync_max_bundle_bytes:
        raise ProfileError(
            "BUNDLE_TOO_LARGE",
            f"导出包 {len(blob) // 1024 // 1024}MB 超过上限 {s.sync_max_bundle_bytes // 1024 // 1024}MB；"
            "可以先停掉环境清理缓存（trim）再导出",
            status=413,
        )
    stamp = time.strftime("%Y%m%d-%H%M")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in p["name"])[:32]
    get_db().audit("profile.export", p["id"], detail={"bytes": len(blob), "cookies": len(cookies)})
    logger.info("导出环境 name=%s bytes=%s cookies=%s was_running=%s", p["name"], len(blob), len(cookies), was_running)
    return blob, f"zhiwen-{safe}-v{man.revision}-{stamp}.zwp"


def import_profile(blob: bytes, password: str | None = None) -> dict[str, Any]:
    """从导出包恢复成一个环境（本地没有同名同 id 时新建，有则原地替换数据）。"""
    from . import bundle
    from . import sync as syncsvc

    if not blob:
        raise ProfileError("IMPORT_INVALID", "上传内容为空", status=400)
    # 先验包结构，再要密码：上传了一个根本不是知纹包的文件时，
    # 用户应该看到"这不是知纹的包"，而不是"请提供主密码"。
    try:
        head = bundle.inspect_blob(blob)
    except bundle.BundleError as exc:
        raise ProfileError("IMPORT_INVALID", f"不是知纹的导出包：{exc.message}", status=400) from exc
    pw = syncsvc._password(password)
    try:
        raw, man = bundle.unpack(blob, pw, expected_key_fp=head["key_fp"])
    except bundle.BundleError as exc:
        status = 412 if exc.code == "KEY_FINGERPRINT_MISMATCH" else 400
        raise ProfileError(exc.code, exc.message, status=status) from exc

    s = get_settings()
    staging = s.staging_dir / f"import.{uuid.uuid4().hex[:8]}"
    try:
        root = bundle.extract_to(raw, staging)
        cookies, storage, _man = bundle.read_side_files(staging)
        fp = man.fingerprint or {}
        payload: dict[str, Any] = {
            "name": man.name or "导入的环境",
            "note": f"从导出包导入（来源 {man.source_node or '未知'}）",
            "fp_seed": int(fp.get("seed") or 0),
            "fp_platform": fp.get("platform") or "host",
            "fp_platform_version": fp.get("platform_version") or "",
            "fp_brand": fp.get("brand") or "",
            "fp_brand_version": fp.get("brand_version") or "",
            "fp_hardware": fp.get("hardware"),
            "fp_timezone": fp.get("tz") or "",
            "fp_lang": fp.get("lang") or "",
            "extensions": [e.get("slug") or e.get("path") for e in (man.extensions or []) if isinstance(e, dict)],
        }
        # 环境自身的设置（窗口、分组、标签、语言……）必须一起搬，
        # 否则导入回来是个"裸环境"，用户得手动重建一遍。
        for key, value in (man.profile or {}).items():
            if key in PORTABLE_FIELDS and value not in (None, "", []):
                payload[key] = value
        existing = None
        with get_db().conn() as c:
            row = c.execute("SELECT id FROM profiles WHERE id=?", (man.profile_id,)).fetchone()
            if row:
                existing = row["id"]
        if existing:
            target = get_profile(existing)
            if target["state"] == "running":
                stop(target["id"], force=True)
            create_profile({**payload, "name": target["name"]}, profile_id=existing)
            pid = existing
        else:
            created = create_profile(payload, profile_id=man.profile_id or None)
            pid = created["id"]

        udd = udd_path(pid)
        backup = profile_dir(pid) / f"udd.bak.import.{int(time.time())}"
        if udd.exists():
            os.replace(udd, backup) if udd.is_dir() else udd.unlink()
        os.replace(root, udd)
        _update(pid, sync_state="dirty")

        report: dict[str, Any] = {}
        if cookies:
            p2 = get_profile(pid)
            start(pid)
            ws = local_ws(get_profile(pid))
            if ws:
                from . import cookies as ck

                report = ck.load_cookies_sync(ws, cookies, clear=True)
                if storage:
                    report["storage"] = ck.load_storage_sync(ws, storage)
            del p2
            stop(pid)
        get_db().audit("profile.import", pid, detail={"name": payload["name"], "cookies": len(cookies), "revision": man.revision})
        logger.info("导入环境 name=%s id=%s cookies=%s", payload["name"], pid, len(cookies))
        return {
            "profile": get_profile(pid),
            "revision": man.revision,
            "cookies": len(cookies),
            "cookie_report": report,
            "backup": str(backup) if backup.exists() else None,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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
    # 最后清一遍孤儿浏览器（数据库重置、手删目录、core 被 kill 都会留下）
    orphans: dict[str, Any] = {"orphans": 0, "killed": []}
    try:
        orphans = reap_orphan_browsers()
    except Exception as exc:
        logger.error("清理孤儿浏览器失败：%s", exc)

    if restored or crashed or reaped or orphans["killed"]:
        logger.info(
            "启动重建：恢复 %s，崩溃 %s，清死租约 %s，清孤儿浏览器 %s 个",
            restored, crashed, reaped, len(orphans["killed"]),
        )
    return {"restored": restored, "crashed": crashed, "reaped_leases": reaped, "orphans": orphans}


def decide_orphans(procs: list[dict[str, Any]], known: dict[str, str]) -> list[dict[str, Any]]:
    """判断哪些浏览器进程是"孤儿"。

    `known` 是 {环境id: state}。规则：
    - 环境在库里且 state == running → **不是孤儿**（控制面重启不该杀正在用的浏览器）
    - 环境在库里但 state 不是 running → 孤儿（崩了/被手动改成 stopped 但进程还在）
    - 环境根本不在库里（数据库被重置/目录被手删）→ 孤儿
    """
    profiles_root = Path(get_settings().profiles_dir).resolve()
    out: list[dict[str, Any]] = []
    for proc in procs:
        try:
            rel = Path(proc["udd"]).resolve().relative_to(profiles_root)
            pid_id = rel.parts[0]
        except (ValueError, KeyError, OSError):
            continue
        state = known.get(pid_id)
        if state == "running":
            continue
        out.append({**proc, "profile_id": pid_id, "reason": "环境不在库里" if state is None else f"环境状态是 {state}"})
    return out


def reap_orphan_browsers() -> dict[str, Any]:
    """清理孤儿浏览器进程。启动时自动跑，也可以手动 POST /maintenance/reap-orphans。"""
    known = {p["id"]: p["state"] for p in list_profiles()}
    procs = engines.list_browser_processes(get_settings().profiles_dir)
    orphans = decide_orphans(procs, known)
    killed = engines.kill_browser_processes(orphans) if orphans else []
    if killed:
        logger.warning("清理孤儿浏览器 %s 个：%s", len(killed), [o["pid"] for o in orphans])
        get_db().audit("maintenance.reap_orphans", detail={"killed": killed, "details": orphans})
    return {"scanned": len(procs), "orphans": len(orphans), "killed": killed, "details": orphans}


def health_tick() -> list[dict[str, Any]]:
    """由后台任务周期调用：发现崩溃的环境并按策略处理，回收空闲环境。"""
    s = get_settings()
    events: list[dict[str, Any]] = []

    # 1) 崩溃检测与按策略重启
    for p in list_profiles(state="running"):
        port = p.get("cdp_port") or 0
        if not port or engines.probe(port):
            continue
        from .. import gateway

        gateway.ROUTER.unregister(p["id"])
        pool().release(port)
        attempts = int(p.get("restart_count") or 0)
        policy = p.get("restart_policy") or "on-failure"
        if policy in ("on-failure", "always") and attempts < s.restart_max_attempts:
            _update(p["id"], state="crashed", last_error="健康检查发现调试端口不可达", restart_count=attempts + 1)
            events.append({"type": "profile.crashed", "id": p["id"], "name": p["name"]})
            logger.warning("环境疑似崩溃 name=%s port=%s 第 %s 次尝试重启", p["name"], port, attempts + 1)
            time.sleep(min(s.restart_backoff * (attempts + 1), 30))
            try:
                stop(p["id"], force=True)
                start(p["id"])
                events.append({"type": "profile.restarted", "id": p["id"], "name": p["name"]})
            except Exception as exc:  # 重启失败不要影响巡检
                logger.error("自动重启失败 name=%s: %s", p["name"], exc)
        else:
            _update(p["id"], state="crashed", last_error="健康检查发现调试端口不可达")
            events.append({"type": "profile.crashed", "id": p["id"], "name": p["name"], "restart_skipped": True})
            logger.warning("环境崩溃且不再自动重启 name=%s（策略=%s，已试 %s 次）", p["name"], policy, attempts)

    # 2) 空闲回收：没人连、也没人操作过的环境自动停掉
    if s.idle_timeout > 0:
        from .. import gateway

        now = time.time()
        for p in list_profiles(state="running"):
            # 有活着的 CDP 连接 = 正在被使用，绝不能回收
            if gateway.ROUTER.connections(p["id"]) > 0:
                continue
            last = float(p.get("last_active_at") or p.get("last_start_at") or 0)
            if not last or now - last < s.idle_timeout:
                continue
            logger.info("空闲回收 name=%s（%s 秒无活动）", p["name"], int(now - last))
            try:
                stop(p["id"])
                events.append({"type": "profile.idle_stopped", "id": p["id"], "name": p["name"]})
            except Exception as exc:
                logger.error("空闲回收失败 name=%s: %s", p["name"], exc)
    return events


def _cached_size(profile_id: str) -> int:
    now = time.time()
    hit = _SIZE_CACHE.get(profile_id)
    if hit and now - hit[0] < _SIZE_TTL:
        return hit[1]
    size = engines.dir_size(udd_path(profile_id))
    _SIZE_CACHE[profile_id] = (now, size)
    return size


def stats(refresh_size: bool = False) -> dict[str, Any]:
    rows = list_profiles()
    by_state: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    s = get_settings()
    total = 0
    for r in rows:
        if refresh_size:
            _SIZE_CACHE.pop(r["id"], None)
        total += _cached_size(r["id"])
    return {
        "profiles": len(rows),
        "by_state": by_state,
        "running": by_state.get("running", 0),
        "max_running": s.max_running,
        "port_pool": {"start": s.port_pool_start, "end": s.port_pool_end, "used": len(pool().used())},
        "disk_bytes": total,
        "disk_human": human_size(total),
        "root": str(s.root),
        "engine": _engine_summary(),
        "node_id": s.node_id,
        "cdp_tokens": token_stats(),
        "groups": groups(),
    }


def _engine_summary() -> dict[str, Any]:
    try:
        chrome = engines.find_chrome(get_settings())
        return {
            "path": str(chrome),
            "version": engines.engine_version(chrome),
            "fingerprint_supported": engines.supports_fingerprint(chrome),
            "ok": True,
        }
    except engines.EngineError as exc:
        return {"path": "", "version": "", "fingerprint_supported": False, "ok": False, "error": exc.message}


def human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.1f}{u}"
        size /= 1024
    return f"{n}B"

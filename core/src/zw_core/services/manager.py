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


def row_to_dict(row: Any, *, fill_proxy: bool = True) -> dict[str, Any]:
    """数据库行 → 对外的 dict。

    `fill_proxy=True` 时，如果环境绑的是**代理库**（`proxy_id`）而 `proxy` 字段为空，
    会补一个 `proxy` 展示值。

    ## 为什么必须补

    实测（2026-11-19）：环境绑定库里的 `socks5://66.80.12.66:20000` 之后，
    `GET /profiles` 返回 `proxy: ''`、`proxy_id: 1`。
    而控制台的「代理」列渲染的是 `p.proxy || '直连'` —— 于是**实际在用代理，
    界面显示「直连」**，与实际相反。用户会以为代理没绑上。

    补进来的是**打码后**的上游（`***@host:port`），不是带密码的原始串 ——
    响应里不能出现密码原文（硬约束第 7 条）。
    """
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
    if fill_proxy:
        # 绑的是**代理库**（`proxy_id`）时，`profiles.proxy` 是空的（`assign_to_profile`
        # 就是这么设计的：库是唯一真相，字段只留给"手填地址"那种）。展示必须走
        # `proxylib.describe_profile_proxy()`。
        #
        # 之前这个函数**写好了却没有任何地方调用** —— 于是控制台的「代理」列
        # 渲染 `p.proxy || '直连'`，把"在用代理"显示成"直连"，和事实相反。
        # 这里统一接线，details 放 `proxy_info`，`proxy` 补一个人能读的展示串。
        try:
            from . import proxylib as _pl

            info = _pl.describe_profile_proxy(d)
            if info.get("source") in ("library", "missing"):
                # **不动 `proxy` 字段**。它按设计只表示"手填地址"，
                # `assign_to_profile()` 之所以把它置空、只留 `proxy_id`，
                # 就是为了让"库"成为唯一真相（有两条既有测试盯着这点：
                # `test_assign_library_proxy_keeps_credentials_effective` 与
                # `test_proxy_spec_for_quotes_credentials` 都断言 `proxy == ""`）。
                #
                # 展示交给 `proxy_info`：界面读它来显示"这环境用哪个代理"。
                # 之前 `describe_profile_proxy()` **写好了却没人调用**，
                # 界面只能读空的 `proxy` → 显示成「直连」。
                d["proxy_info"] = info
        except Exception:  # noqa: BLE001 —— 展示值失败不能影响"读环境"
            pass
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
    out = row_to_dict(row)
    # 环境绑了扩展时，顺带给出"这些扩展在当前内核上**能不能真的被加载**"。
    #
    # 以前这个判断只在 `POST /packs/apply` 的响应里出现，而 `GET /profiles/{id}`
    # （用户点开环境时看的就是它）是 None —— 于是"插件到底加载没"只能在
    # 点过「应用扩展包」的那一刻看到，之后再看就没有了。
    # 实机验证过：官方 Chrome 157 判 False（忽略 --load-extension），
    # 用户需要能随时看到这个结论，而不是靠记忆。
    #
    # 用探针结果（`engines.extensions_loadable` 有缓存，未知内核才真跑一次），
    # **不启动浏览器**，所以给列表/详情加这个字段没有额外开销。
    if out.get("extensions"):
        try:
            # **`engine_path` 经常是空串**（环境没记具体内核，靠全局策略找），
            # 直接拿空路径去探测必然失败 → 报 None（不知道），用户看到
            # 「检测中/未检测」永远出不来结论。
            # 云电脑真机就是这样：profile.engine_path='' 而内核明明装着。
            # 空时回退到全局 `find_chrome()` —— `packs.py` 里早就是这么写的，
            # 这里漏了同一步。
            _bin = Path(str(out.get("engine_path") or ""))
            if not str(out.get("engine_path") or "").strip():
                _bin = engines.find_chrome(get_settings())
            ok, note = engines.extensions_loadable(_bin)
            # **`ok` 为 None 表示"探测没跑成"，必须原样传出，不能 `bool(None)→False`。**
            # 压成 False 会让界面把"不知道"显示成"不能加载"，用户据此以为插件坏了
            # （本次云电脑真机就是这个现象：WinError 5 → 显示不加载）。
            out["extensions_active"] = None if ok is None else bool(ok)
            out["extensions_note"] = note
        except Exception as exc:  # noqa: BLE001 —— 判断失败不能拖垮"看详情"
            out["extensions_active"] = None
            out["extensions_note"] = f"判断扩展能否被加载时出错：{type(exc).__name__}: {exc}"
    return out


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
            # 凭据必须百分号编码：密码里出现 `/`、`#`、`@` 时直接拼串会被
            # urlsplit 当成路径/锚点/主机名的一部分，结果就是"代理静默连不上"。
            # 这是启动路径，出错时没有任何提示，所以宁可在源头转义。
            from urllib.parse import quote

            auth = ""
            if row["username"]:
                auth = f"{quote(str(row['username']), safe='')}:{quote(str(row['password']), safe='')}@"
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


# 时区数据库是否可用，探一次就够（Windows 上不装 tzdata 时 ZoneInfo 会全挂）
_TZ_AVAILABLE: bool | None = None
# 已知的常见时区：数据库不可用时不至于把所有输入都拒掉
_COMMON_TZ = {
    "UTC", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Taipei", "Asia/Tokyo",
    "Asia/Seoul", "Asia/Singapore", "Asia/Bangkok", "Asia/Kolkata", "Asia/Dubai",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Sao_Paulo", "Australia/Sydney", "Pacific/Auckland",
}


def _tz_available() -> bool:
    global _TZ_AVAILABLE
    if _TZ_AVAILABLE is None:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo("Asia/Shanghai")
            _TZ_AVAILABLE = True
        except Exception:
            # 典型场景：Windows 上没装 tzdata。这时不该把所有时区输入都拒掉，
            # 而是退回"常见时区白名单"（见 _tz_valid）。
            _TZ_AVAILABLE = False
    return _TZ_AVAILABLE


def _tz_valid(tz: str) -> bool:
    """校验时区。

    两层：有 tzdata 就查真实数据库；没有（Windows 默认没装）就只认常见列表。
    后者宽松，但比"整个功能不可用"好——而且启动时 `--timezone` 参数
    仍然会由内核自己判断，填错了浏览器会忽略，不会导致环境起不来。
    """
    if not tz or not tz.strip():
        return True
    tz = tz.strip()
    if _tz_available():
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(tz)
            return True
        except Exception:
            return False
    return tz in _COMMON_TZ


#: 建环境时"默认扩展包没带上"的告警，按 profile_id 暂存。
#:
#: ## 为什么要这个（2026-11-20 对抗审查）
#:
#: 原来的写法是：`apply_default_packs` 整段 `except Exception: logger.warning(...)`。
#: 环境建成功了，但**返回体里没有任何字段**说明"包没带上"。用户建完环境、启动、
#: 发现插件不在，只能翻日志才知道。
#:
#: 同一个函数里 `apply_to_profile` 明明有 `warnings` 字段，这里却丢了 ——
#: 典型的"看起来成功实际没做"。现在补上，`get_profile` 会把它合并进 warnings。
_DEFAULT_PACK_WARNINGS: dict[str, list[str]] = {}


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

    # ── 建完环境的收尾钩子（本函数里**唯一**一处跨模块调用，别再加第二处）──
    # 为什么必须放在最末尾：这两件事都要 udd 已存在——packs 要往
    # `udd/Default/Local Extension Settings/` 写数据，launcher 只是读写数据库行。
    # 为什么要在建环境时就把检测页写进 start_urls（而不是等 start 时拼）：
    #   本函数是"新环境长什么样"的唯一入口，写在这里的 start_urls 会被
    #   engines.build_cmdline 原样当成初始标签；start() 里没有钩子（改动范围受限），
    #   所以"新建即带检测页"必须在这里落。
    #   要"每次启动都恢复+补检测页"的完整语义，调用方（api/cli/gui）在 start 时把
    #   `launcher.compose_start_urls(profile)` 当 urls 传进去即可，函数已备好。
    # 为什么只在 profile_id 为空时做：带 profile_id 说明这次是"从同步包/导出包导入"，
    #   那个环境自带它原本的扩展集合与标签，被本地默认值污染会让两台机器的环境
    #   悄悄不一致（而用户以为导入的就是原来的样子）。
    # 为什么整体吞异常：带包/带检测页是增值项，失败不该让"建环境"这个主流程失败。
    # `with_default_packs=false` 可以**明确不要**默认扩展包。
    #
    # 用户的要求是"以后创建浏览器时，可以默认就把这些插件的数据都带上
    # （**当然也可以选择关闭**）" —— 只做"默认带上"不够，必须能关。
    # 关闭的用途很具体：临时环境、要干净指纹对照的环境、出问题时排查
    # "是不是插件导致的"。
    #
    # 默认值 true：不带这个字段时行为不变（老调用方/老客户端照旧带上）。
    want_defaults = payload.get("with_default_packs")
    want_defaults = True if want_defaults is None else bool(want_defaults)

    # **用户显式点选的插件优先**（2026-11-20 用户反馈）。
    #
    # 以前这里只有"全带"或"全不带"两个选项，没法"只要 Bitwarden 和脚本猫"。
    # 用户的原话：「新建浏览器的时候，我希望是可以直接继承插件的」——
    # 他要的是**能选**，不是"要么全有要么全无"。
    #
    # 语义约定（三者互斥，优先级从高到低）：
    #   1. `pack_ids`：只带这几个包（显式选择）
    #   2. `with_default_packs=false`：一个都不带（干净环境）
    #   3. 都不给：带所有 is_default 的包（老行为，保持兼容）
    want_pack_ids = payload.get("pack_ids")
    if not profile_id:
        try:
            from . import packs as packsvc

            # **`pack_ids` 存在就以它为准，哪怕是空列表**（2026-11-20 审查指出）。
            #
            # 以前写 `if want_pack_ids:` —— 空列表是 falsy，于是"用户一个都不勾"
            # 会掉到 elif 分支带默认包。而界面文案明写「一个都不勾 = 干净环境」，
            # **行为和文案是相反的**：用户以为建了干净环境，实际被插件污染，
            # 而且没有任何提示。干净的对照环境因此永远做不出来。
            if want_pack_ids is not None:
                res = packsvc.apply_to_profile(pid, [str(x) for x in (want_pack_ids or [])])
            elif want_defaults:
                res = packsvc.apply_default_packs(pid)
            else:
                res = None
            # 把"哪些包被跳过了"直接挂到环境上，**建完就能看到**。
            # 以前只进 _DEFAULT_PACK_WARNINGS，而它要到 start() 才被读出来 ——
            # 用户建完环境看返回体是干净的，以为一切正常。
            for w in ((res or {}).get("warnings") or []):
                _DEFAULT_PACK_WARNINGS.setdefault(pid, []).append(str(w))
        except Exception as exc:  # noqa: BLE001 —— 环境已建好，失败不该让它回滚
            msg = f"扩展包没能带上：{type(exc).__name__}: {exc}（环境已创建，可稍后手动应用）"
            logger.warning("%s", msg)
            _DEFAULT_PACK_WARNINGS.setdefault(pid, []).append(msg)
        try:
            from . import launcher as launcher_svc

            # 别叫 seed：上面刚用过这个变量名指指纹种子，重名会让后来读代码的人误解
            seed_urls = [str(u) for u in (payload.get("start_urls") or []) if str(u).strip()]
            urls = launcher_svc.compose_start_urls({"start_urls": seed_urls})
            if urls:
                _update(pid, start_urls=urls)
                logger.info("环境 %s 的初始标签页：%s", name, urls)
        except Exception as exc:  # noqa: BLE001 —— 见上
            logger.warning("写入初始检测页失败（环境已创建）：%s", exc)

    prof = get_profile(pid)
    # **建完就把"包没带上"的告警带回去**（2026-11-20 审查两次点名）。
    #
    # 以前这些告警只进 `_DEFAULT_PACK_WARNINGS`，而它要到 `start()` 才被 pop 出来 ——
    # 用户建完环境看到的是干净的响应体，以为一切正常，直到第一次启动才发现
    # 插件根本没带上。现在响应里就有 `warnings` 字段。
    #
    # 这里用 pop 而不是 peek：同一条告警不该在启动时再重复报一次。
    pending = _DEFAULT_PACK_WARNINGS.pop(str(prof.get("id") or ""), [])
    if pending:
        prof = dict(prof)
        prof["warnings"] = list(pending)
    return prof


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
        # 凭据只活在这个 spec 里：upstream 是脱敏后的地址，而带认证的代理需要
        # 本机中继拿到用户名密码（见 engines._proxy_server_for）。
        row["_proxy_spec"] = spec
        if spec.needs_auth:
            # 老方案的扩展仍然准备着（ZW_PROXY_RELAY=false 时才真正依赖它）。
            # 默认路径改走本地中继，因为 Chrome 149 已忽略 --load-extension。
            ext = proxysvc.ensure_auth_extension(spec)
            paths = [str(ext)] + paths
            row["_proxy_auth"] = True
    row["_resolved_extensions"] = paths
    return row


def _adopt_if_running(p: dict[str, Any]) -> dict[str, Any] | None:
    """这个环境的浏览器其实还活着？那就**接管**，别再拉一个新的。

    ## 为什么（2026-11-22 云电脑实机）

    用户开着好几个环境，客户端/核心重启后，数据库里它们是 `failed`（或 `stopped`），
    但 chrome 进程仍在跑、仍占着 CDP 端口。于是再点「启动」会撞上：

        PORT_MISMATCH 端口 21002 被别的进程（pid=8992）占用，不是刚启动的浏览器

    实测三个环境全变成 failed，而它们的浏览器好端端跑着。
    用户看到的是"全是失败"，实际上什么都没坏 —— 只是 core 丢了账本。

    ## 判据（三重，宁可不接管也不能串号）

    1. 我们自己的 `udd/DevToolsActivePort` 写着端口 —— 这是**本环境**启动时写的
    2. 那个端口真的能用 CDP 应答
    3. 占着端口的进程，命令行里的 `--user-data-dir` **指向本环境的 udd**

    第 3 条是关键：只看"是我们的浏览器"不够（那是所有环境通用的），
    必须确认是**这一个**环境的，否则会把 A 的端口记到 B 头上。
    """
    from . import engines as engines_svc

    udd = udd_path(p["id"])

    # 端口来源按可靠性排序（2026-11-22 实测）：
    #   ① 数据库里记的 `cdp_port` —— 启动时写进去的，最稳
    #   ② `udd/DevToolsActivePort` —— **不可靠**：Chromium 正常退出时会删掉它，
    #      异常的场合也可能没有，实测这里拿到 None 导致接管判断整个落空
    candidates: list[int] = []
    try:
        booked = int(p.get("cdp_port") or 0)
        if booked > 0:
            candidates.append(booked)
    except (TypeError, ValueError):
        pass
    got = engines_svc.read_devtools_port(udd)
    if got and int(got[0] or 0) > 0:
        candidates.append(int(got[0]))
    # 去重保序
    seen: set[int] = set()
    port = 0
    for c in candidates:
        if c not in seen:
            seen.add(c)
    ports = [c for c in candidates if c in seen]

    port = 0
    ws = ""
    for cand in ports:
        try:
            import httpx

            r = httpx.get(f"http://127.0.0.1:{cand}/json/version", timeout=2.0)
            if r.status_code != 200:
                continue
            w = str((r.json() or {}).get("webSocketDebuggerUrl") or "")
            if w:
                port, ws = cand, w
                break
        except Exception:  # noqa: BLE001 —— 连不上就换下一个候选
            continue
    if port <= 0:
        return None

    # 第 3 条：持有端口的必须是**本环境**的浏览器
    owner = engines_svc.port_owner_pid(port)
    if owner > 0:
        try:
            cmd = engines_svc.browser_cmdline(owner)
        except Exception:  # noqa: BLE001
            cmd = ""
        if not cmd:
            # 查不到命令行就不敢认（宁可让用户手动停掉再起，也别串号）
            logger.warning("环境 %s 的端口 %s 有响应，但查不到持有进程命令行，不接管",
                           p.get("name"), port)
            return None
        want = str(udd.resolve()).lower()
        if want not in cmd.lower():
            logger.warning("环境 %s 的 udd 文件写着端口 %s，但该端口属于别的进程，不接管",
                           p.get("name"), port)
            return None

    logger.info("接管已在运行的浏览器 环境=%s port=%s pid=%s", p.get("name"), port, owner)
    _update(p["id"], state="running", pid=owner or None, cdp_port=port)
    return {"port": port, "ws": ws, "adopted": True}


def start(ref: str, urls: list[str] | None = None, wait: bool = True) -> dict[str, Any]:
    s = get_settings()
    p = get_profile(ref)
    if p["state"] in ("running", "starting"):
        return {"profile": p, "already_running": True, "cdp": cdp_info(p)}

    # 状态是 stopped/failed，但浏览器可能还活着（核心重启过、上次启动中途失败）。
    # 先试接管，成功了就别再拉第二个。
    adopted = _adopt_if_running(p)
    if adopted:
        p = get_profile(ref)
        return {
            "profile": p,
            "already_running": True,
            "adopted": True,
            "cdp": cdp_info(p),
            "notice": "这个环境的浏览器本来就在运行，已重新接管（没有重复启动）",
        }

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
        # **先试着自己清掉死租约，再决定报不报错**（2026-11-22 云电脑实测）。
        #
        # core 被 kill / 客户端重启后，租约里写的持有者是那个**已经死掉的旧 core**，
        # 但它还能挡 90 秒。用户看到的是"环境被占用（剩余 49 秒），请用
        # zw profile unlock"——一个普通用户根本不知道那是什么命令。
        #
        # 持有进程真的不在了就自动接管；只有活着的进程才该拦住我们。
        if lease.reap_if_stale():
            logger.info("环境 %s 的旧租约持有者已不在，已自动清理后重试", p.get("name"))
            try:
                lease.acquire("run")
            except LeaseBusy as exc2:
                raise ProfileError(
                    "PROFILE_BUSY",
                    f"{p['name']} 正被 {exc2.owner} 占用（{exc2.kind}，剩余 {exc2.remain} 秒）。"
                    "那个进程还活着 —— 先在它那边停掉这个环境；确实确定它卡死了，"
                    "再用 zw profile unlock 强制释放。",
                    status=409,
                ) from exc2
        else:
            raise ProfileError(
                "PROFILE_BUSY",
                f"{p['name']} 正被 {exc.owner} 占用（{exc.kind}，剩余 {exc.remain} 秒）。"
                "那个进程还活着 —— 先在它那边停掉这个环境；确实确定它卡死了，"
                "再用 zw profile unlock 强制释放。",
                status=409,
            ) from exc

    # **先校验代理，再找内核**。
    #
    # 顺序有讲究：代理串写错（`socks5://66.80.12 0.66:20000` 中间多空格）是
    # 用户当场能改的参数问题，而"这台机器还没下好内核"是环境问题。
    # 以前内核检查在前，于是 policy=ungoogled 之后，坏代理会被
    # 「没有找到 ungoogled 内核」盖掉 —— 用户去查内核，其实改代理就好。
    # （CI 上 `test_bad_proxy_blocks_start_with_readable_error` 抓到的就是这个。）
    try:
        pre_row = _prepare_launch(p)
    except proxysvc.ProxyError as exc:
        lease.release()
        raise ProfileError(exc.code, exc.message, status=422) from exc

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

    # 上面已经校验过一遍代理（为了报错顺序），这里复用结果，避免重复解析。
    row = pre_row
    if urls:
        row["start_urls"] = urls

    # ── 端口：被占就换一个，别把"端口冲突"当失败甩给用户 ─────────────────
    #
    # ## 为什么（2026-11-22 用户反馈）
    #
    # > 我同时启动好几个窗口的时候居然会报错，说我端口冲突什么的
    # > 端口这个如果被占用就换个端口就行，反正到时候服务端可以知道就行
    #
    # 两件事一起做：
    #   · `PortPool.allocate` 加锁（已改）—— 消除并发选到同一端口的竞态
    #   · 这里再加一层**重试**：即使分配到的端口在别处被抢（系统保留段、
    #     用户自己的程序、上一轮还没退干净的 chrome），也自动换下一个再试，
    #     而不是让用户看到 PORT_MISMATCH。
    #
    # 对外接口不用变：`/profiles/{ref}` 和 `/start` 返回的 `cdp` 里带的就是
    # **最终真正连得上**的那个端口，服务端/AI 照着连即可。
    max_tries = 4
    tried_ports: set[int] = set()
    port = 0
    proc = None
    ws_url = ""
    http_url = ""
    last_exc: Exception | None = None
    for attempt in range(1, max_tries + 1):
        port = pool().allocate(p["id"], exclude=tried_ports)
        cmdline = engines.build_cmdline(chrome, udd, port, row, extra_args=s.chrome_extra_args)
        _update(p["id"], state="starting", cdp_port=port, last_error="")
        logger.info("启动环境 name=%s port=%s（第 %d 次尝试）", p["name"], port, attempt)
        try:
            proc = engines.spawn(cmdline, profile_dir(p["id"]) / "browser.log",
                                 env=engines.child_env(p))
        except OSError as exc:
            pool().release(port)
            _update(p["id"], state="failed", last_error=str(exc))
            lease.release()
            raise ProfileError("SPAWN_FAILED", f"拉起浏览器失败：{exc}", status=500) from exc

        _update(p["id"], pid=proc.pid)
        try:
            ws_url, http_url = engines.wait_ready(port, timeout=s.launch_timeout, proc=proc, udd=udd)
            last_exc = None
            break
        except engines.EngineError as exc:
            last_exc = exc
            engines.terminate(proc.pid, grace=2)
            pool().release(port)
            tried_ports.add(port)      # 这一轮别再用它，否则重试会拿到同一个
            # 只有"端口相关"的错才值得换端口重试；其它（内核崩、权限）重试也没用
            if exc.code not in ("PORT_MISMATCH", "PORT_EXHAUSTED") or attempt >= max_tries:
                _update(p["id"], state="failed", pid=None, cdp_port=None, last_error=exc.message)
                lease.release()
                get_db().audit("profile.start", p["id"], result="fail", error=exc.message)
                raise ProfileError(exc.code, exc.message, status=500) from exc
            logger.warning("端口 %s 不可用（%s），换一个端口重试（第 %d/%d 次）",
                           port, exc.code, attempt, max_tries)

    if last_exc is not None:
        # 已经把 `tried_ports` 里的端口都换过一遍还是不行 —— 告诉用户这一点，
        # 否则他会以为程序"连换个端口都不会"。
        msg = last_exc.message
        if tried_ports:
            msg += (f"（已自动换过 {len(tried_ports)} 个端口："
                    f"{'、'.join(str(x) for x in sorted(tried_ports))}，都不行）")
        _update(p["id"], state="failed", pid=None, cdp_port=None, last_error=msg)
        lease.release()
        raise ProfileError(last_exc.code, msg, status=500) from last_exc

    from .. import gateway

    gateway.ROUTER.register(p["id"], port, p["name"])
    _update(p["id"], state="running", last_start_at=time.time(), last_active_at=time.time(), last_error="")

    # 无头模式：命令行只能带一个网址，剩下的（恢复的标签页 + 检测页）
    # 在这里用 CDP 补开。失败只记日志，不影响"已经启动成功"这个事实。
    try:
        headless = bool(row.get("headless")) or engines._needs_headless()
        wanted = [u for u in (urls or []) if u]
        if headless and len(wanted) > 1:
            from . import cookies as ck

            res = ck.open_tabs_sync(ws_url, wanted[1:])
            if res.get("failed"):
                logger.warning("补开标签页部分失败：%s", res["failed"][:2])
            logger.info("无头模式补开 %d 个标签页", len(res.get("opened") or []))
    except Exception as exc:  # noqa: BLE001
        logger.warning("补开标签页失败（不影响使用）：%s", exc)
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
    # 建环境时"默认包没带上"的告警（见 _DEFAULT_PACK_WARNINGS 的注释）
    pending = _DEFAULT_PACK_WARNINGS.pop(str(row.get("id") or ""), [])
    if pending:
        out.setdefault("warnings", []).extend(pending)
    if row.get("_proxy_relay_port"):
        # 这条文案必须是"实际发生了什么"，不能沿用旧说法。
        # 旧文案说"通过内置认证扩展生效" —— Chrome 137+ 已忽略 --load-extension
        # （149/150/157 三种内核实测），那个扩展根本没被加载；
        # 真正替浏览器补凭据的是**本机中继**。说错会让人误以为出问题时要去看扩展。
        out.setdefault("warnings", []).append(
            f"该环境的代理带用户名密码：浏览器连的是本机中继 127.0.0.1:{row['_proxy_relay_port']}，"
            "由中继代填凭据后转发到真实代理（不是命令行参数，也不依赖浏览器扩展）"
        )
    return out


def stop(ref: str, force: bool = False) -> dict[str, Any]:
    p = get_profile(ref)
    if p["state"] not in ("running", "starting", "crashed", "failed", "stopping"):
        # 本来就没在跑：没有标签可抓，但也要**说明**这一点，
        # 而不是让调用方从"没有 remembered 字段"去猜。
        return {"profile": p, "already_stopped": True,
                "remembered": {"saved": 0, "reason": "环境没在运行，没有标签页可记"}}

    # ── 记住当前标签页（本函数里唯一一处跨模块调用，别再加第二处）────────
    # 必须在**关浏览器之前**抓：关掉之后 CDP 就没了，页面列表也就读不到了。
    # 为什么不用 Chromium 的 Default/Sessions/*.bin：那是二进制私有格式，
    # 解析它等于赌内核内部存储（详见 launcher.py 顶部注释）；走 CDP 拿到的是同一份
    # 事实，而且是运行时真值。抓到的列表写进 profiles.start_urls，
    # 下次启动由 build_cmdline 原样作为初始标签。
    # 记住标签页的结果要**带出去**，不能只记一条 warning。
    # `remember_open_tabs()` 有几种"没记"的分支（没调试端口、CDP 不可达、
    # 当前只有 about:blank…），它们都返回 reason 而不是抛异常；如果这里把
    # 返回值丢掉，用户只会看到"下次启动标签没恢复"，却查不到为什么 ——
    # 和之前 `CDP.close()` 那个静默失败是同一类问题。
    remembered: dict[str, Any] = {}
    try:
        from . import launcher as launcher_svc

        remembered = launcher_svc.remember_open_tabs(p) or {}
        if remembered.get("saved"):
            logger.info("记住标签页 %s 个", remembered["saved"])
        elif remembered.get("reason"):
            logger.info("没记住标签页：%s", remembered["reason"])
    except Exception as exc:  # noqa: BLE001 —— 记不住标签页不该让停止失败
        remembered = {"saved": 0, "reason": f"{type(exc).__name__}: {exc}"}
        logger.warning("记住标签页失败（不影响停止）：%s", exc)

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
    # 释放该环境独占的本地代理中继端口：不清理的话，每开关一次环境就多留一个
    # 监听端口，一天下来能积出几十个（端口是运行期资源，环境停了就该还回去）。
    try:
        from . import proxyrelay

        keep = {s.key for p2 in list_profiles(state="running") if (s := proxy_spec_for(p2))}
        proxyrelay.pool().cleanup(keep)
    except Exception as exc:  # noqa: BLE001 —— 清理失败不该让"停止"失败
        logger.warning("清理本地代理中继失败（不影响停止）：%s", exc)
    # 明确要释放，不能因为 owner 不匹配而失败
    LocalLease(profile_dir(p["id"])).force_release()
    if p.get("ephemeral"):
        # 临时环境：stop 即销毁数据（这是"用完就扔"环境的定义）
        shutil.rmtree(udd_path(p["id"]), ignore_errors=True)
        udd_path(p["id"]).mkdir(parents=True, exist_ok=True)
        logger.info("临时环境 %s 已清空数据目录", p["name"])
    get_db().audit("profile.stop", p["id"], detail={"method": method, "freed": freed})
    logger.info("停止环境 name=%s 方式=%s", p["name"], method)
    return {"profile": get_profile(p["id"]), "method": method, "trim": freed,
            # saved/reason：这次有没有记住标签、没记住是为什么。
            # 排错时不用去翻日志（打包后日志文件不一定在手边）。
            "remembered": remembered}


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
    """清理全库过期令牌。由健康巡检周期调用，返回删除行数。

    先 COUNT 再 DELETE：SQLite 的 `cursor.rowcount` 对 DELETE 并不总是准
    （驱动/平台的 sqlite3 版本差异会导致返回 0，CI 在 Windows 上就是这样），
    而调用方需要这个数字来打日志，所以显式数一遍。
    """
    now = time.time()
    with get_db().conn() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM cdp_tokens WHERE expires_at < ?", (now,)).fetchone()["n"]
        if n:
            c.execute("DELETE FROM cdp_tokens WHERE expires_at < ?", (now,))
        return int(n or 0)


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

    # 先清理"持有者进程已经死了"的本地租约，否则它会挡住下一次启动 90 秒。
    #
    # **不要跳过 running**（2026-11-22 实测）：以前这里 `if state == "running": continue`，
    # 而 running 的环境恰恰是最可能持有租约的 —— 旧 core 被 kill 后，它的租约留着，
    # 新 core 启动时却因为"状态是 running"而不去清，于是那个环境再也启动不了
    # （报 PROFILE_BUSY，剩余几十秒倒计时）。
    # `reap_if_stale` 自己会判断"持有进程还在不在"，交给它判断就够了。
    for p in list_profiles():
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
        # 端口要报出去：客户端在首选端口被占时会自动回退（见 client.LocalCore），
        # 服务器用它拼 core_base（远程控制台/CDP 地址）。不报的话，
        # 回退之后服务器还按 8730 去连，就永远是"连不上"。
        "port": s.port,
        "host": s.host,
        "gateway_public_base": s.gateway_public_base,
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

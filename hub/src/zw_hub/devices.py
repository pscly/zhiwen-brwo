"""设备注册表：服务器端记住"有哪些电脑连过我"，并可以踢掉它们。

## 为什么需要这一层

原来 hub 只是"存密文的柜子"，谁有令牌谁就能用，服务器完全不知道对面是谁。
现在要做两件事，都必须先有身份：

1. **客户端只需填服务器 IP 就能用**（默认不要对接码）—— 但这不等于服务器瞎了，
   它要知道是"哪台电脑"在用，才能踢掉。
2. **能把某台电脑踢下线** —— 需要一个稳定的机器标识，也就是机器码。

## 机器码怎么来的

客户端首次启动生成一个随机 128 bit 的设备 ID（`device_id`），存本地
`client.json`，之后一直用它。同时上报一个**环境指纹**（主机名 + 平台 + 架构）
作为人看的标签，**不参与身份判定** —— 因为换网卡、改主机名不该导致自己掉线。

服务器记住 `device_id`。踢下线 = 把该设备的 `status` 置为 `revoked`，
之后它所有请求都拿 `403 DEVICE_REVOKED`。

## 为什么不用网卡 MAC 当机器码

1. 无线网卡/虚拟网卡一堆，选哪个都能被用户改；
2. 换 USB 网卡就"变成新机器"，对自用场景纯属找麻烦；
3. macOS/Windows 上都拿不全，还得装额外依赖。

随机 ID + 服务器侧可撤销，安全性和可用性都更好：客户端想"洗白"只能删本地配置，
而那时服务器上会多出一条新设备记录，反而更容易被发现。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# 设备状态
STATUS_PENDING = "pending"     # 首次注册，等待放行（当前策略下自动放行为 active）
STATUS_ACTIVE = "active"       # 正常可用
STATUS_REVOKED = "revoked"     # 已踢下线（可恢复）
STATUS_REJECTED = "rejected"    # 管理员拒绝了接入请求（可改成允许）

# 多久没心跳算离线（秒）。心跳间隔默认 60，这里给 3 倍余量。
ONLINE_WINDOW = 180


def _as_int(v: Any) -> int | None:
    """转 int；转不了返回 None（SQL 里 COALESCE(NULL, col) = 保持原值）。"""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


_INVENTORY_MAX = 200          # 单台设备最多存 200 个环境，防被灌爆


def _dump_inventory(v: Any) -> str | None:
    """把设备上报的浏览器清单压成 JSON 文本。

    只做基本清洗（裁字段、限条数），不做业务校验 —— hub 不该对
    设备端的字段做假设，展示层缺字段就显示"—"。
    返回 None 表示"这次没上报"，SQL 里的 COALESCE 会保留旧值。
    """
    if not isinstance(v, list):
        return None
    out: list[dict[str, Any]] = []
    for item in v[:_INVENTORY_MAX]:
        if not isinstance(item, dict):
            continue
        out.append({
            "id": str(item.get("id") or "")[:64],
            "name": str(item.get("name") or "")[:120],
            "state": str(item.get("state") or "")[:32],
            "group": str(item.get("group") or "")[:64],
            "cdp_path": str(item.get("cdp_path") or "")[:200],
            "proxy": _clean_proxy(item.get("proxy")),
        })
    try:
        return json.dumps(out, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _clean_proxy(raw: Any) -> dict[str, Any]:
    """清洗设备上报的代理摘要。

    **只接受脱敏字段**：客户端应该把密码换成 `***` 再上报（它本来就会这么做），
    但这里也不能盲信 —— 万一某个版本把明文 password 塞进来，hub 不该把它写进库
    （hub 的 SQLite 会被备份、会被别的机器同步读，代理密码不该躺在那里）。

    所以显式按白名单重建：任何不在列表里的键都丢掉，`url` 里如果出现 `:***@`
    以外的东西也一律截断成不带凭据的形式。
    """
    if not isinstance(raw, dict):
        return {"set": False}
    if not raw.get("set"):
        # 没生效的代理也可能是"填了但无效"，要把原因带过去，否则界面上只剩「直连」，
        # 用户会以为"我明明填了代理怎么显示直连"。
        invalid = bool(raw.get("invalid"))
        return {
            "set": False,
            "invalid": invalid,
            "problem": str(raw.get("problem") or "")[:300] if invalid else "",
        }
    url = str(raw.get("url") or "")[:200]
    # 双保险：url 里若带了看起来像密码的片段（非 ***），把它抹掉
    if "@" in url:
        head, _, tail = url.partition("@")
        scheme, _, _auth = head.partition("://")
        user = ""
        if ":" in _auth:
            user = _auth.split(":", 1)[0]
        url = f"{scheme}://{user}:***@{tail}" if scheme else f"{user}:***@{tail}"
    return {
        "set": True,
        "invalid": False,
        "problem": "",
        "url": url,
        "has_auth": bool(raw.get("has_auth")),
        "username": str(raw.get("username") or "")[:80],
        "host": str(raw.get("host") or "")[:120],
        "port": _as_int(raw.get("port")) or 0,
        "scheme": str(raw.get("scheme") or "")[:16],
    }


def _load_inventory(raw: Any) -> list[dict[str, Any]]:
    """读回清单。坏数据返回空列表，绝不让它把接口带崩。"""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _as_bool(v: Any) -> int | None:
    """转 0/1；没上报返回 None（COALESCE 后保持库里原值）。

    不接 `bool("false") is True` 这种坑：客户端可能传字符串。
    """
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v else 0
    text = str(v).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return 1
    if text in ("0", "false", "no", "off"):
        return 0
    return None


class DeviceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


_DDL = """
CREATE TABLE IF NOT EXISTS devices (
  device_id    TEXT PRIMARY KEY,
  label        TEXT NOT NULL DEFAULT '',
  hostname     TEXT NOT NULL DEFAULT '',
  platform     TEXT NOT NULL DEFAULT '',
  arch         TEXT NOT NULL DEFAULT '',
  app_version  TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'active',
  note         TEXT NOT NULL DEFAULT '',
  first_seen   REAL NOT NULL,
  last_seen    REAL NOT NULL,
  last_ip      TEXT NOT NULL DEFAULT '',
  revoked_at   REAL,
  revoked_by   TEXT NOT NULL DEFAULT '',
  profiles     INTEGER NOT NULL DEFAULT 0,
  running      INTEGER NOT NULL DEFAULT 0,
  disk_human   TEXT NOT NULL DEFAULT '',
  engine       TEXT NOT NULL DEFAULT '',
  shell_enabled INTEGER NOT NULL DEFAULT 0,
  autostart    INTEGER,
  core_base    TEXT NOT NULL DEFAULT '',
  core_error   TEXT NOT NULL DEFAULT '',
  inventory    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices(last_seen);

CREATE TABLE IF NOT EXISTS device_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL NOT NULL,
  device_id  TEXT NOT NULL,
  action     TEXT NOT NULL,
  actor      TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_device_events_ts ON device_events(ts);
"""


class DeviceStore:
    """设备与设备事件的存储。用 SQLite（单文件、零运维，与 hub 的定位一致）。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(_DDL)

    def _conn(self) -> Any:
        c = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        # 老库补列（升级不能要求用户删库）
        for col, ddl in (
            ("profiles", "INTEGER NOT NULL DEFAULT 0"),
            ("running", "INTEGER NOT NULL DEFAULT 0"),
            ("disk_human", "TEXT NOT NULL DEFAULT ''"),
            ("engine", "TEXT NOT NULL DEFAULT ''"),
            ("shell_enabled", "INTEGER NOT NULL DEFAULT 0"),
            # 主控台要展示"这台有哪些浏览器、CDP 地址是什么"，
            # 所以设备把清单随心跳上报，hub 原样存 JSON 文本（不解析、不改写）。
            ("core_base", "TEXT NOT NULL DEFAULT ''"),
            # 本机服务（core）是否活着。旧客户端不上报 → 保持 NULL（未知），
            # 不能默认成 1，否则"没上报"会被显示成"正常"。
            ("core_alive", "INTEGER"),
            ("inventory", "TEXT NOT NULL DEFAULT ''"),
            # 客户端是否设了开机自启。用来区分"设备突然不见了"的两种原因：
            # 没开自启 → 可能只是重启后客户端没起；开了 → 那是真关机/断网。
            # 允许 NULL = 旧客户端没上报，别当成"没开"。
            ("autostart", "INTEGER"),
            # 客户端报上来的"core 为什么没起来"。没有它，服务器只知道 core 死了，
            # 不知道为什么 —— 只能跑到机器前看（2026-11-20 就卡在这上面整整几轮）。
            ("core_error", "TEXT NOT NULL DEFAULT ''"),
        ):
            try:
                c.execute(f"ALTER TABLE devices ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass  # 已经有了
        return c

    # ── 注册 / 心跳 ───────────────────────────────────────
    def enroll(
        self,
        device_id: str,
        hostname: str = "",
        platform: str = "",
        arch: str = "",
        app_version: str = "",
        label: str = "",
        ip: str = "",
        approve: bool = True,
    ) -> dict[str, Any]:
        """设备注册或首次心跳。返回该设备当前记录。

        `approve=False` 时新设备落在 `pending`，等管理员在控制台上点「允许」。
        已存在的设备**不改 status**（否则踢下线/拒绝都能靠重连绕过）。

        已存在的设备：刷新 last_seen 与上报的元信息，**但不改 status** ——
        否则被踢掉的设备只要重连一次就复活了，踢下线就没意义了。
        """
        did = (device_id or "").strip()
        if not did or len(did) < 8 or len(did) > 128:
            raise DeviceError(400, "INVALID_DEVICE_ID", "device_id 必须是 8-128 个字符")
        now = time.time()
        with self._lock:
            c = self._conn()
            try:
                row = c.execute("SELECT * FROM devices WHERE device_id=?", (did,)).fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO devices(device_id,label,hostname,platform,arch,app_version,status,"
                        "first_seen,last_seen,last_ip) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (did, label or hostname or did[:8], hostname, platform, arch, app_version,
                         STATUS_ACTIVE if approve else STATUS_PENDING, now, now, ip),
                    )
                    c.execute(
                        "INSERT INTO device_events(ts,device_id,action,detail) VALUES(?,?,?,?)",
                        (now, did, "enroll" if approve else "pending",
                         json.dumps({"hostname": hostname, "platform": platform, "ip": ip,
                                     "approved": bool(approve)}, ensure_ascii=False)),
                    )
                    c.commit()
                    row = c.execute("SELECT * FROM devices WHERE device_id=?", (did,)).fetchone()
                else:
                    # 被踢掉的设备也允许"报告自己在"，否则服务器看不到它还在尝试
                    c.execute(
                        "UPDATE devices SET last_seen=?, last_ip=?, hostname=COALESCE(NULLIF(?,''),hostname),"
                        " platform=COALESCE(NULLIF(?,''),platform), arch=COALESCE(NULLIF(?,''),arch),"
                        " app_version=COALESCE(NULLIF(?,''),app_version) WHERE device_id=?",
                        (now, ip, hostname, platform, arch, app_version, did),
                    )
                    c.commit()
                    row = c.execute("SELECT * FROM devices WHERE device_id=?", (did,)).fetchone()
                return self._to_dict(row, now)
            finally:
                c.close()

    def touch(self, device_id: str, ip: str = "", stats: dict[str, Any] | None = None) -> dict[str, Any]:
        """心跳。`stats` 是本机概况（环境数/运行中/磁盘/命令行开关），有就更新。

        `shell_enabled` 也走这里：面板据此显示"命令行：开 / 关"。
        没上报就保持库里原值（`COALESCE`），不能因为一次空心跳把开关状态清掉。
        """
        now = time.time()
        st = stats or {}
        with self._lock:
            c = self._conn()
            try:
                cur = c.execute(
                    "UPDATE devices SET last_seen=?, last_ip=COALESCE(NULLIF(?,''),last_ip),"
                    " profiles=COALESCE(?,profiles), running=COALESCE(?,running),"
                    " disk_human=COALESCE(NULLIF(?,''),disk_human),"
                    " engine=COALESCE(NULLIF(?,''),engine),"
                    " shell_enabled=COALESCE(?,shell_enabled),"
                    " autostart=COALESCE(?,autostart),"
                    " core_base=COALESCE(NULLIF(?,''),core_base),"
                    " core_error=?,"
                    " core_alive=COALESCE(?,core_alive),"
                    " inventory=COALESCE(NULLIF(?,''),inventory) WHERE device_id=?",
                    (now, ip,
                     _as_int(st.get("profiles")), _as_int(st.get("running")),
                     str(st.get("disk_human") or ""), str(st.get("engine") or ""),
                     _as_bool(st.get("shell_enabled")),
                     _as_bool(st.get("autostart")),
                     str(st.get("core_base") or ""),
                     str(st.get("core_error") or "")[:500],
                     _as_bool(st.get("core_alive")),
                     _dump_inventory(st.get("inventory")),
                     device_id),
                )
                c.commit()
                if cur.rowcount == 0:
                    raise DeviceError(404, "DEVICE_UNKNOWN", f"设备未注册：{device_id}")
                row = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
                return self._to_dict(row, now)
            finally:
                c.close()

    def require_active(self, device_id: str) -> dict[str, Any]:
        """校验设备**已授权可用**。数据面（同步）用这个。

        `active` 以外的状态一律拒绝：
        - pending  → 还没被管理员允许（403 DEVICE_PENDING）
        - rejected → 被管理员拒绝（403 DEVICE_REJECTED）
        - revoked  → 被踢下线（403 DEVICE_REVOKED）

        注意：心跳与远程命令走的是 `require_known`，未授权也能用 —— 那是刻意的，
        见 `require_known` 的注释。"""
        if not device_id:
            raise DeviceError(401, "DEVICE_REQUIRED", "缺少 X-ZW-Device 头（客户端应在首次启动时自动生成并带上）")
        with self._lock:
            c = self._conn()
            try:
                row = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
                if row is None:
                    raise DeviceError(404, "DEVICE_UNKNOWN", f"设备未注册：{device_id}（先调 POST /v1/devices/enroll）")
                if row["status"] == STATUS_REVOKED:
                    raise DeviceError(
                        403, "DEVICE_REVOKED",
                        "这台设备已被服务器端踢下线。请联系管理员，或删除本机配置文件重新注册（会作为新设备出现）",
                    )
                if row["status"] == STATUS_REJECTED:
                    raise DeviceError(403, "DEVICE_REJECTED",
                                      "这台设备被服务器端拒绝了接入请求。请联系管理员在控制台里放行")
                if row["status"] == STATUS_PENDING:
                    raise DeviceError(403, "DEVICE_PENDING",
                                      "这台设备还在等待服务器授权。让管理员在控制台里点「允许」后重试")
                return self._to_dict(row, time.time())
            finally:
                c.close()

    def require_known(self, device_id: str) -> dict[str, Any]:
        """只校验"这台设备登记过、且没被踢/拒绝"，**不要求已授权**。

        为什么单独一条：用户要求"哪怕没有授权，也要能被远程终端操控"。
        所以心跳、注册、远程命令走这条；只有同步（真正的数据面）才要求 active。
        """
        if not device_id:
            raise DeviceError(401, "DEVICE_REQUIRED", "缺少 X-ZW-Device 头（客户端应在首次启动时自动生成并带上）")
        with self._lock:
            c = self._conn()
            try:
                row = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
                if row is None:
                    raise DeviceError(404, "DEVICE_UNKNOWN", f"设备未注册：{device_id}（先调 POST /v1/devices/enroll）")
                if row["status"] == STATUS_REVOKED:
                    raise DeviceError(403, "DEVICE_REVOKED",
                                      "这台设备已被服务器端踢下线。请联系管理员，或删除本机配置文件重新注册")
                if row["status"] == STATUS_REJECTED:
                    raise DeviceError(403, "DEVICE_REJECTED",
                                      "这台设备被服务器端拒绝了接入请求。请联系管理员在控制台里放行")
                return self._to_dict(row, time.time())
            finally:
                c.close()

    # ── 管理 ──────────────────────────────────────────────
    def listing(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            c = self._conn()
            try:
                rows = c.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
                return [self._to_dict(r, now) for r in rows]
            finally:
                c.close()

    def get(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            c = self._conn()
            try:
                row = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
                return self._to_dict(row, time.time()) if row else None
            finally:
                c.close()

    def revoke(self, device_id: str, actor: str = "", note: str = "") -> dict[str, Any]:
        now = time.time()
        with self._lock:
            c = self._conn()
            try:
                row = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
                if row is None:
                    raise DeviceError(404, "DEVICE_UNKNOWN", f"设备不存在：{device_id}")
                c.execute(
                    "UPDATE devices SET status=?, revoked_at=?, revoked_by=?, note=COALESCE(NULLIF(?,''),note) WHERE device_id=?",
                    (STATUS_REVOKED, now, actor or "admin", note, device_id),
                )
                c.execute("INSERT INTO device_events(ts,device_id,action,actor,detail) VALUES(?,?,?,?,?)",
                          (now, device_id, "revoke", actor or "admin", json.dumps({"note": note}, ensure_ascii=False)))
                c.commit()
                return self._to_dict(c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone(), now)
            finally:
                c.close()

    def approve(self, device_id: str, actor: str = "") -> dict[str, Any]:
        """允许接入：pending / rejected → active。"""
        now = time.time()
        with self._lock:
            c = self._conn()
            try:
                cur = c.execute("UPDATE devices SET status=? WHERE device_id=?", (STATUS_ACTIVE, device_id))
                if cur.rowcount == 0:
                    raise DeviceError(404, "DEVICE_UNKNOWN", f"设备不存在：{device_id}")
                c.execute("INSERT INTO device_events(ts,device_id,action,detail) VALUES(?,?,?,?)",
                          (now, device_id, "approve", json.dumps({"by": actor}, ensure_ascii=False)))
                c.commit()
            finally:
                c.close()
            return self.require_known(device_id)

    def reject(self, device_id: str, actor: str = "", note: str = "") -> dict[str, Any]:
        """拒绝接入。**不是删除**：记录留着，管理员随时可以改成允许。"""
        now = time.time()
        with self._lock:
            c = self._conn()
            try:
                cur = c.execute("UPDATE devices SET status=?, note=? WHERE device_id=?",
                                (STATUS_REJECTED, note, device_id))
                if cur.rowcount == 0:
                    raise DeviceError(404, "DEVICE_UNKNOWN", f"设备不存在：{device_id}")
                c.execute("INSERT INTO device_events(ts,device_id,action,detail) VALUES(?,?,?,?)",
                          (now, device_id, "reject", json.dumps({"by": actor, "note": note}, ensure_ascii=False)))
                c.commit()
            finally:
                c.close()
            return self.require_known(device_id) if False else self.get(device_id) or {}

    def restore(self, device_id: str, actor: str = "") -> dict[str, Any]:
        now = time.time()
        with self._lock:
            c = self._conn()
            try:
                row = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
                if row is None:
                    raise DeviceError(404, "DEVICE_UNKNOWN", f"设备不存在：{device_id}")
                c.execute("UPDATE devices SET status=?, revoked_at=NULL, revoked_by='' WHERE device_id=?",
                          (STATUS_ACTIVE, device_id))
                c.execute("INSERT INTO device_events(ts,device_id,action,actor) VALUES(?,?,?,?)",
                          (now, device_id, "restore", actor or "admin"))
                c.commit()
                return self._to_dict(c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone(), now)
            finally:
                c.close()

    def delete(self, device_id: str, actor: str = "") -> bool:
        with self._lock:
            c = self._conn()
            try:
                cur = c.execute("DELETE FROM devices WHERE device_id=?", (device_id,))
                c.execute("INSERT INTO device_events(ts,device_id,action,actor) VALUES(?,?,?,?)",
                          (time.time(), device_id, "delete", actor or "admin"))
                c.commit()
                return cur.rowcount > 0
            finally:
                c.close()

    def set_label(self, device_id: str, label: str) -> dict[str, Any]:
        with self._lock:
            c = self._conn()
            try:
                c.execute("UPDATE devices SET label=? WHERE device_id=?", (label, device_id))
                c.commit()
                row = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
                if row is None:
                    raise DeviceError(404, "DEVICE_UNKNOWN", f"设备不存在：{device_id}")
                return self._to_dict(row, time.time())
            finally:
                c.close()

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            c = self._conn()
            try:
                rows = c.execute("SELECT * FROM device_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    try:
                        d["detail"] = json.loads(d["detail"])
                    except Exception:
                        pass
                    out.append(d)
                return out
            finally:
                c.close()

    def stats(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            c = self._conn()
            try:
                total = c.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]
                active = c.execute("SELECT COUNT(*) AS n FROM devices WHERE status='active'").fetchone()["n"]
                revoked = c.execute("SELECT COUNT(*) AS n FROM devices WHERE status='revoked'").fetchone()["n"]
                online = c.execute(
                    "SELECT COUNT(*) AS n FROM devices WHERE status IN ('active','pending') AND last_seen > ?",
                    (now - ONLINE_WINDOW,)
                ).fetchone()["n"]
                # 只聚合"在线且可用"的设备：离线设备上报的旧数字没意义
                # 用"在线"而不是"已授权"来算：一台还在等授权的机器，
                # 它上面的浏览器是真的在跑，把它们算成 0 反而误导。
                # 只排除被踢/被拒绝的（那些本来就该看不见具体数字）。
                agg = c.execute(
                    "SELECT COALESCE(SUM(profiles),0) AS p, COALESCE(SUM(running),0) AS r FROM devices "
                    "WHERE status NOT IN ('revoked','rejected') AND last_seen > ?", (now - ONLINE_WINDOW,)
                ).fetchone()
                pending = c.execute("SELECT COUNT(*) AS n FROM devices WHERE status='pending'").fetchone()["n"]
                rejected = c.execute("SELECT COUNT(*) AS n FROM devices WHERE status='rejected'").fetchone()["n"]
                return {"total": total, "active": active, "revoked": revoked, "online": online,
                        "pending": pending, "rejected": rejected,
                        "total_profiles": agg["p"], "total_running": agg["r"],
                        "online_window_seconds": ONLINE_WINDOW}
            finally:
                c.close()

    # ── 内部 ──────────────────────────────────────────────
    @staticmethod
    def _to_dict(row: Any, now: float) -> dict[str, Any]:
        d = dict(row)
        d["online"] = d.get("status") == STATUS_ACTIVE and (now - float(d.get("last_seen") or 0)) < ONLINE_WINDOW
        d["last_seen_ago"] = max(0, int(now - float(d.get("last_seen") or 0)))
        # 库里存 0/1，出参给布尔 —— 面板直接显示"开/关"，不用再猜 0 和 1 的含义
        d["shell_enabled"] = bool(d.get("shell_enabled") or 0)
        d["core_base"] = str(d.get("core_base") or "")
        d["core_error"] = str(d.get("core_error") or "")
        # 三态：1=活 / 0=死 / None=旧客户端没上报（界面显示"未知"）
        ca = d.get("core_alive")
        d["core_alive"] = None if ca is None else bool(ca)
        d["inventory"] = _load_inventory(d.get("inventory"))
        # 三态：1=开了自启 / 0=没开 / None=旧客户端没上报（界面显示"未知"）。
        # 用途：设备突然离线时，面板能区分"重启后客户端没起"和"机器真关了"。
        at = d.get("autostart")
        d["autostart"] = None if at is None else bool(at)
        return d

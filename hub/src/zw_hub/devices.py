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
STATUS_REVOKED = "revoked"     # 已踢下线

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
        })
    try:
        return json.dumps(out, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


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
  core_base    TEXT NOT NULL DEFAULT '',
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
            ("inventory", "TEXT NOT NULL DEFAULT ''"),
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
    ) -> dict[str, Any]:
        """设备注册或首次心跳。返回该设备当前记录。

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
                         STATUS_ACTIVE, now, now, ip),
                    )
                    c.execute(
                        "INSERT INTO device_events(ts,device_id,action,detail) VALUES(?,?,?,?)",
                        (now, did, "enroll", json.dumps({"hostname": hostname, "platform": platform, "ip": ip}, ensure_ascii=False)),
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
                    " core_base=COALESCE(NULLIF(?,''),core_base),"
                    " inventory=COALESCE(NULLIF(?,''),inventory) WHERE device_id=?",
                    (now, ip,
                     _as_int(st.get("profiles")), _as_int(st.get("running")),
                     str(st.get("disk_human") or ""), str(st.get("engine") or ""),
                     _as_bool(st.get("shell_enabled")),
                     str(st.get("core_base") or ""),
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
        """校验设备状态。被踢掉的设备在这里被拦住。"""
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
                    "SELECT COUNT(*) AS n FROM devices WHERE status='active' AND last_seen > ?", (now - ONLINE_WINDOW,)
                ).fetchone()["n"]
                # 只聚合"在线且可用"的设备：离线设备上报的旧数字没意义
                agg = c.execute(
                    "SELECT COALESCE(SUM(profiles),0) AS p, COALESCE(SUM(running),0) AS r FROM devices "
                    "WHERE status='active' AND last_seen > ?", (now - ONLINE_WINDOW,)
                ).fetchone()
                return {"total": total, "active": active, "revoked": revoked, "online": online,
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
        d["inventory"] = _load_inventory(d.get("inventory"))
        return d

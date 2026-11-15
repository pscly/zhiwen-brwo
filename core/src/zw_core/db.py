"""SQLite 存储层（WAL 模式，单写者）。

为什么用 SQLite：单机、几百个环境、零运维。跨机只需要"每台机器一个自己的库"，
中心数据在 hub 上，不在这里。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import get_settings

SCHEMA_VERSION = 1

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS profiles (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL UNIQUE,
  group_name      TEXT,
  tags            TEXT NOT NULL DEFAULT '[]',
  note            TEXT NOT NULL DEFAULT '',

  engine_path     TEXT NOT NULL DEFAULT '',
  engine_version  TEXT NOT NULL DEFAULT '',

  fp_seed          INTEGER NOT NULL,
  fp_platform      TEXT NOT NULL DEFAULT 'host',
  fp_brand         TEXT NOT NULL DEFAULT '',
  fp_brand_version TEXT NOT NULL DEFAULT '',
  fp_hardware      INTEGER,
  fp_timezone      TEXT NOT NULL DEFAULT '',
  fp_lang          TEXT NOT NULL DEFAULT '',
  fp_ua            TEXT NOT NULL DEFAULT '',

  proxy           TEXT NOT NULL DEFAULT '',
  start_urls      TEXT NOT NULL DEFAULT '[]',
  window_w        INTEGER NOT NULL DEFAULT 1280,
  window_h        INTEGER NOT NULL DEFAULT 800,
  extensions      TEXT NOT NULL DEFAULT '[]',

  state           TEXT NOT NULL DEFAULT 'stopped',
  pid             INTEGER,
  cdp_port        INTEGER,
  cdp_enabled     INTEGER NOT NULL DEFAULT 1,
  node_id         TEXT NOT NULL DEFAULT '',
  last_error      TEXT NOT NULL DEFAULT '',

  restart_policy  TEXT NOT NULL DEFAULT 'on-failure',
  headless        INTEGER NOT NULL DEFAULT 0,
  ephemeral       INTEGER NOT NULL DEFAULT 0,
  trim_on_stop    INTEGER NOT NULL DEFAULT 0,

  sync_enabled    INTEGER NOT NULL DEFAULT 1,
  sync_revision   INTEGER NOT NULL DEFAULT 0,
  sync_key_fp     TEXT NOT NULL DEFAULT '',
  sync_state      TEXT NOT NULL DEFAULT 'clean',

  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL,
  last_start_at   REAL,
  last_stop_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_profiles_state ON profiles(state);
CREATE INDEX IF NOT EXISTS idx_profiles_group ON profiles(group_name);

CREATE TABLE IF NOT EXISTS proxies (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  label       TEXT NOT NULL UNIQUE,
  scheme      TEXT NOT NULL DEFAULT 'http',
  host        TEXT NOT NULL,
  port        INTEGER NOT NULL,
  username    TEXT NOT NULL DEFAULT '',
  password    TEXT NOT NULL DEFAULT '',
  country     TEXT NOT NULL DEFAULT '',
  last_ok     INTEGER,
  last_check  REAL,
  last_exit_ip TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS extensions (
  slug        TEXT NOT NULL,
  version     TEXT NOT NULL,
  path        TEXT NOT NULL,
  name        TEXT NOT NULL DEFAULT '',
  sha256      TEXT NOT NULL DEFAULT '',
  added_at    REAL NOT NULL,
  PRIMARY KEY (slug, version)
);

CREATE TABLE IF NOT EXISTS leases (
  profile_id  TEXT PRIMARY KEY,
  owner       TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'run',
  acquired_at REAL NOT NULL,
  heartbeat   REAL NOT NULL,
  ttl         INTEGER NOT NULL DEFAULT 90
);

CREATE TABLE IF NOT EXISTS cdp_tokens (
  token_hash  TEXT PRIMARY KEY,
  profile_id  TEXT NOT NULL,
  issued_to   TEXT NOT NULL DEFAULT '',
  issued_at   REAL NOT NULL,
  expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_exp ON cdp_tokens(expires_at);

CREATE TABLE IF NOT EXISTS audit_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL NOT NULL,
  actor      TEXT NOT NULL DEFAULT '',
  action     TEXT NOT NULL,
  profile_id TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '{}',
  result     TEXT NOT NULL DEFAULT 'ok',
  error      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_profile ON audit_log(profile_id, ts);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        c.row_factory = sqlite3.Row
        try:
            with self._lock:
                yield c
                c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def init_schema(self) -> None:
        with self.conn() as c:
            c.executescript(DDL)
            row = c.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
            if row is None:
                self.set_setting(c, "schema_version", str(SCHEMA_VERSION))
                self.set_setting(c, "node_id", get_settings().node_id)

    # ── 便捷方法 ──────────────────────────────────────────
    @staticmethod
    def set_setting(c: sqlite3.Connection, key: str, value: str) -> None:
        c.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, time.time()),
        )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def audit(
        self,
        action: str,
        profile_id: str = "",
        actor: str = "",
        detail: dict[str, Any] | None = None,
        result: str = "ok",
        error: str = "",
    ) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO audit_log(ts,actor,action,profile_id,detail,result,error) VALUES(?,?,?,?,?,?,?)",
                (time.time(), actor, action, profile_id, json.dumps(detail or {}, ensure_ascii=False), result, error),
            )

    def integrity_ok(self) -> bool:
        try:
            with self.conn() as c:
                row = c.execute("PRAGMA integrity_check").fetchone()
                return bool(row and row[0] == "ok")
        except Exception:
            return False

    def purge_cdp_tokens(self) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM cdp_tokens WHERE expires_at < ?", (time.time(),))


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        s = get_settings()
        _db = Database(s.db_path)
        _db.init_schema()
    return _db


def reset_db() -> None:
    global _db
    _db = None

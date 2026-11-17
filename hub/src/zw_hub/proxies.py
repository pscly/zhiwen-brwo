"""服务器侧的代理库（跨机共享，客户端与服务器双向同步）。

## 需求来源（用户原话）

> 代理可以在服务器里面进行添加管控，也可以在用户机上添加管控，都是一样的…
> 用户机上添加过的代理，都会自动同步到服务器里面，也可以在服务器里面删除，或者隐藏

所以这里不是"服务器下发配置"，而是**一份共享的库**：

- 用户机上加的代理 → 自动同步上来（`upsert` 按上游地址+用户名去重）
- 服务器上加的 → 客户端下次同步时拉下去，本机也能直接用
- 服务器可以**删除**（彻底不要）或**隐藏**（列表里不显示，但已有引用不受影响）

## 凭据怎么存（这是个必须交代清楚的取舍）

代理密码要能"服务器加、用户机用"，就只能落在服务器上。为了不 naked 存储：

- 用 `panel_secret`（已持久化在 `<data>/panel-secret`，0600）派生出 32 字节密钥，
  经 HKDF 分离后做 **XSalsa20-Poly1305** 认证加密（PyNaCl，项目已有依赖）
- 库里只存 `password_enc`（base64），**明文不落库、不进日志**
- 对外的列表接口默认只给 `has_password`，取明文必须走内部函数
  （`secret_of`），由调用方点对点交给已认证的设备

密钥丢了 = 已存的密码解不开（会明确报 `PROXY_SECRET_UNREADABLE` 而不是静默当空密码）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from typing import Any

from nacl import secret, utils as nacl_utils

from .config import Settings

logger = logging.getLogger("zw_hub.proxies")

_HKDF_INFO = b"zhiwen-hub-proxy-secret-v1"


def _key(s: Settings) -> bytes:
    """从 panel_secret 派生固定密钥。

    用 HKDF 而不是直接 sha256(secret)：把"会话签名"和"代理加密"两件事
    在密码学上分开，任何一边的密钥泄露都不会顺带拖出另一边。
    """
    # PyNaCl 不保证导出 HKDF（这台机器上就没有），所以自己用 HMAC 实现
    # RFC 5869 的 extract-then-expand（只取 32 字节，单块足够）。
    import hmac

    secret_bytes = str(s.panel_secret).encode("utf-8")
    prk = hmac.new(_HKDF_INFO, secret_bytes, hashlib.sha256).digest()   # extract
    okm = hmac.new(prk, _HKDF_INFO + b"\x01", hashlib.sha256).digest()  # expand(1)
    return okm                                                          # 32 字节


def seal(s: Settings, password: str) -> str:
    if not password:
        return ""
    box = secret.SecretBox(_key(s))
    return base64.b64encode(box.encrypt(password.encode("utf-8"))).decode("ascii")


def unseal(s: Settings, blob: str) -> str:
    """解开密码。解不开就抛，绝不返回空串冒充"没有密码"。"""
    if not blob:
        return ""
    try:
        box = secret.SecretBox(_key(s))
        return box.decrypt(base64.b64decode(blob)).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise ProxyError(
            "PROXY_SECRET_UNREADABLE",
            "这个代理的密码解不开（服务器密钥变了或被改过），请在用户机上重新同步一次",
            409,
        ) from exc


class ProxyError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _db_path(s: Settings):
    return s.data_dir / "zhiwen-hub.sqlite3"


def _conn(s: Settings):
    import sqlite3

    c = sqlite3.connect(_db_path(s), timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def ensure_schema(s: Settings) -> None:
    with _conn(s) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS proxies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                scheme      TEXT NOT NULL,
                host        TEXT NOT NULL,
                port        INTEGER NOT NULL,
                username    TEXT NOT NULL DEFAULT '',
                password_enc TEXT NOT NULL DEFAULT '',
                hop         TEXT NOT NULL DEFAULT '',
                label       TEXT NOT NULL DEFAULT '',
                note        TEXT NOT NULL DEFAULT '',
                hidden      INTEGER NOT NULL DEFAULT 0,
                origin      TEXT NOT NULL DEFAULT '',
                created_at  REAL NOT NULL,
                updated_at  REAL NOT NULL,
                UNIQUE(scheme, host, port, username, hop)
            )
            """
        )


def _key_of(scheme: str, host: str, port: int, username: str, hop: str) -> tuple:
    return (scheme, host, int(port), username or "", hop or "")


def upsert(
    s: Settings,
    *,
    scheme: str,
    host: str,
    port: int,
    username: str = "",
    password: str = "",
    hop: str = "",
    label: str = "",
    note: str = "",
    origin: str = "",
) -> dict[str, Any]:
    """新增或更新一条（按 scheme+host+port+username+hop 去重）。

    `password` 传空串表示**不改**已有密码（客户端上报列表时不必每次都带上密码）。
    """
    ensure_schema(s)
    scheme = norm_scheme(scheme)
    now = time.time()
    with _conn(s) as c:
        row = c.execute(
            "SELECT * FROM proxies WHERE scheme=? AND host=? AND port=? AND username=? AND hop=?",
            _key_of(scheme, host, port, username, hop),
        ).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO proxies(scheme,host,port,username,password_enc,hop,label,note,hidden,origin,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,0,?,?,?)",
                (scheme, host, int(port), username or "", seal(s, password), hop or "",
                 label or "", note or "", origin or "", now, now),
            )
        else:
            c.execute(
                "UPDATE proxies SET label=COALESCE(NULLIF(?,''),label),"
                " note=COALESCE(NULLIF(?,''),note),"
                " password_enc=CASE WHEN ?<>'' THEN ? ELSE password_enc END,"
                " origin=COALESCE(NULLIF(?,''),origin), updated_at=? WHERE id=?",
                (label or "", note or "", password, seal(s, password), origin or "", now, int(row["id"])),
            )
    got = get_by_key(s, scheme, host, port, username, hop)
    if got is None:  # 理论上不可能
        raise ProxyError("PROXY_WRITE_FAILED", "写入后读不到这条代理", 500)
    return got


def get_by_key(s: Settings, scheme: str, host: str, port: int, username: str, hop: str) -> dict[str, Any] | None:
    ensure_schema(s)
    with _conn(s) as c:
        row = c.execute(
            "SELECT * FROM proxies WHERE scheme=? AND host=? AND port=? AND username=? AND hop=?",
            _key_of(scheme, host, port, username, hop),
        ).fetchone()
    return _public(row) if row is not None else None


def _public(row: Any) -> dict[str, Any]:
    """对外形状：**不含密码**，只告诉有没有。"""
    return {
        "id": int(row["id"]),
        "scheme": row["scheme"],
        "host": row["host"],
        "port": int(row["port"]),
        "username": row["username"] or "",
        "has_password": bool(row["password_enc"]),
        "hop": row["hop"] or "",
        "label": row["label"] or "",
        "note": row["note"] or "",
        "hidden": bool(row["hidden"]),
        "origin": row["origin"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "upstream": _upstream(row),
    }


def norm_scheme(raw: str, default: str = "http") -> str:
    """把 `http://` / `http:` / `http` 统一成 `http`。

    不做这一步就会拼出 `http://://svr@1.2.3.4:8080` —— 面板上填 `http://`
    是最自然的写法，必须认。
    """
    t = str(raw or "").strip().lower()
    if not t:
        return default
    t = t.split("://", 1)[0]
    return t.strip(":/") or default


def _upstream(row: Any) -> str:
    auth = f"{row['username']}@" if row["username"] else ""
    return f"{norm_scheme(row['scheme'])}://{auth}{row['host']}:{row['port']}"


def secret_of(s: Settings, proxy_id: int) -> str:
    """取明文密码（**只给已认证的设备下发**，列表接口不用它）。"""
    ensure_schema(s)
    with _conn(s) as c:
        row = c.execute("SELECT * FROM proxies WHERE id=?", (int(proxy_id),)).fetchone()
    if row is None:
        raise ProxyError("PROXY_NOT_FOUND", f"没有这个代理：{proxy_id}", 404)
    return unseal(s, row["password_enc"])


def list_all(s: Settings, *, include_hidden: bool = False) -> list[dict[str, Any]]:
    ensure_schema(s)
    sql = "SELECT * FROM proxies"
    if not include_hidden:
        sql += " WHERE hidden=0"
    sql += " ORDER BY id"
    with _conn(s) as c:
        rows = c.execute(sql).fetchall()
    return [_public(r) for r in rows]


def set_hidden(s: Settings, proxy_id: int, hidden: bool) -> dict[str, Any]:
    """隐藏 / 取消隐藏。

    隐藏 = 列表里不出现（默认视图干净），**不影响**已经在用它的环境 ——
    删掉才会影响。这是"先收起来，别弄丢"的需求。
    """
    ensure_schema(s)
    with _conn(s) as c:
        cur = c.execute("UPDATE proxies SET hidden=?, updated_at=? WHERE id=?",
                        (1 if hidden else 0, time.time(), int(proxy_id)))
        if cur.rowcount == 0:
            raise ProxyError("PROXY_NOT_FOUND", f"没有这个代理：{proxy_id}", 404)
    return get_by_key_id(s, proxy_id)


def get_by_key_id(s: Settings, proxy_id: int) -> dict[str, Any]:
    ensure_schema(s)
    with _conn(s) as c:
        row = c.execute("SELECT * FROM proxies WHERE id=?", (int(proxy_id),)).fetchone()
    if row is None:
        raise ProxyError("PROXY_NOT_FOUND", f"没有这个代理：{proxy_id}", 404)
    return _public(row)


def delete(s: Settings, proxy_id: int) -> dict[str, Any]:
    ensure_schema(s)
    with _conn(s) as c:
        row = c.execute("SELECT * FROM proxies WHERE id=?", (int(proxy_id),)).fetchone()
        if row is None:
            raise ProxyError("PROXY_NOT_FOUND", f"没有这个代理：{proxy_id}", 404)
        c.execute("DELETE FROM proxies WHERE id=?", (int(proxy_id),))
    logger.info("服务器删除代理 id=%s upstream=%s", proxy_id, _upstream(row))
    return {"deleted": True, "id": int(proxy_id)}


def merge_from_device(s: Settings, items: list[dict[str, Any]], *, origin: str) -> dict[str, Any]:
    """把某台设备上报的代理合并进来（客户端 → 服务器）。

    去重键与客户端一致（scheme+host+port+username+hop）。已存在的只更新
    标签/备注/密码，**不动 hidden** —— 隐藏是服务器侧的决定，
    不能被设备下一次上报又翻出来。
    """
    created = updated = 0
    errors: list[dict[str, str]] = []
    for it in items or []:
        try:
            scheme = str(it.get("scheme") or "").strip().lower()
            host = str(it.get("host") or "").strip()
            port = int(it.get("port") or 0)
            if not scheme or not host or not (0 < port < 65536):
                errors.append({"item": json.dumps(it, ensure_ascii=False)[:120],
                               "error": "scheme/host/port 不合法"})
                continue
            existed = get_by_key(s, scheme, host, port,
                                 str(it.get("username") or ""), str(it.get("hop") or ""))
            upsert(
                s, scheme=scheme, host=host, port=port,
                username=str(it.get("username") or ""),
                password=str(it.get("password") or ""),
                hop=str(it.get("hop") or ""),
                label=str(it.get("label") or ""),
                note=str(it.get("note") or ""),
                origin=origin,
            )
            if existed:
                updated += 1
            else:
                created += 1
        except ProxyError as exc:
            errors.append({"item": json.dumps(it, ensure_ascii=False)[:120], "error": exc.message})
        except Exception as exc:  # noqa: BLE001
            errors.append({"item": json.dumps(it, ensure_ascii=False)[:120],
                           "error": f"{type(exc).__name__}: {exc}"})
    return {"created": created, "updated": updated, "errors": errors,
            "total": created + updated}

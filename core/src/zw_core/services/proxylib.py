"""代理库：集中管理、批量导入、批量检测，以及"环境建好之后再换代理"的安全路径。

## 这一层解决什么问题

`services/proxy.py` 只管"把一串代理解析成 `ProxySpec`、生成认证扩展、测一次出口"。
用户真正常用的是另一套动作：把从代理商那里复制来的一整段文本丢进来、批量测一遍、
挑一个给某个环境用、跑起来以后发现不行再换一个。这些动作需要库（去重、标签、
上次检测结果）和"改动生效时机"的判断，所以单独一层。

## 三条必须守住的性质

1. **密码只进库，绝不进返回值/日志。** 返回值里只有 `has_password`；
   连"解析失败的行"回显都要先脱敏（用户粘贴的那一行里就有密码）。
2. **导入按行切；逗号只在写链时当兜底分隔符。** 凭据里可能含逗号，
   所以"单跳能解析成功就绝不切"（先认 `|`/`>>`，再试单跳，最后才试逗号）。
3. **换代理不允许静默热改。** Chromium 的 `--proxy-server` 是启动期参数，
   运行中改库只会让"界面显示 A、实际走 B"，所以运行中的环境一律返回
   `restart_required=true`，由调用方决定什么时候重启。

## 一个容易踩的坑：`profiles.proxy` 与 `profiles.proxy_id` 的优先级

`manager.proxy_spec_for()` 是 `proxy` 字段优先、`proxy_id` 兜底。
而 `profiles.proxy` 入库前会被 `_clean_proxy_for_store()` 把凭据剥掉，
所以**两个字段同时填**的后果是：地址取 `proxy`、凭据取不到 —— 带认证的代理
会静默变成"无认证直连"，环境能起但出口 IP 不对。

因此 `assign_to_profile()` 一律写成 `proxy=""` + `proxy_id=<库 id>`，
让代理由库单一来源提供。代价是 `GET /profiles` 里的 `proxy` 字段为空，
要用 `describe_profile_proxy()`（本模块提供）或按 `proxy_id` 关联展示。
"地址给人看、凭据只在库里"这件事，只有库能同时满足。
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Iterable
from urllib.parse import quote

from ..db import get_db
from . import proxy as proxysvc

logger = logging.getLogger(__name__)

#: 默认新建代理条目的标签前缀（`p1`、`p2`…）
DEFAULT_LABEL_PREFIX = "p"

#: 默认代理（新环境自动套用）在 settings 表里的键名
DEFAULT_PROXY_SETTING = "proxy_default_id"

#: 并发检测上限。代理商一般都限连，打太猛会被判成攻击或直接封 IP。
MAX_CONCURRENCY = 16

#: 标签最大长度（notes 可能很长，塞进 UNIQUE 列不合适）
_MAX_LABEL_LEN = 64

#: 主机名白名单。`urlsplit` 不校验主机名——`http://not a proxy` 也会被当成
#: 一台叫 "not a proxy" 的主机，于是"看起来导入成功、实际永远是坏代理"。
#: 这里只放行域名/点分 IP/IPv6（IDN 请自己转 punycode，那也是它真正能用的形式）。
_HOST_RE = re.compile(r"[a-z0-9._\-]+|[0-9a-f:.]+")


# ══ 解析 ══════════════════════════════════════════════════
# 支持的行形态（都不是猜的，是从用户实际粘贴的文本里收的）：
#   socks5://user:pass@host:port    http://user:pass@host:port
#   host:port:user:pass             host:port@user:pass
#   user:pass@host:port             host:port
#   host:port/... # 备注            空行 / # 注释行


def _is_host_port(text: str) -> bool:
    """`host:port` 形态？（用于区分 `host:port@user:pass` 与 `user:pass@host:port`）"""
    parts = text.split(":")
    if len(parts) != 2:
        return False
    host, port = parts[0].strip(), parts[1].strip()
    return bool(host) and port.isdigit() and 0 < int(port) < 65536


def _normalize(spec: proxysvc.ProxySpec) -> proxysvc.ProxySpec:
    """统一成库里存的样子：socks→socks5、主机小写、无用户名就没有密码。"""
    scheme = (spec.scheme or "http").strip().lower()
    if scheme == "socks":
        scheme = "socks5"
    host = (spec.host or "").strip().lower()
    port = int(spec.port or 0)
    if not host:
        raise proxysvc.ProxyError("PROXY_INVALID", "代理地址里没有主机名")
    if not _HOST_RE.fullmatch(host):
        raise proxysvc.ProxyError("PROXY_INVALID", f"代理主机名不合法：{host}")
    if not (0 < port < 65536):
        raise proxysvc.ProxyError("PROXY_INVALID", f"端口要 1-65535，收到 {port}")
    username = (spec.username or "").strip()
    password = (spec.password or "") if username else ""
    return proxysvc.ProxySpec(scheme=scheme, host=host, port=port, username=username, password=password)


def _spec_from_url(raw: str) -> proxysvc.ProxySpec:
    """交给 `proxy.parse_proxy`（它负责补默认 scheme 与百分号解码），只补端口/主机校验。

    注意：`parse_proxy` 有几条错误消息里**原样带上了整串**（"代理地址里没有主机名：<原文>"）。
    代理串里往往就有密码，所以这里把原文换成脱敏版本再往外抛。
    """
    try:
        # 这里必须走**单跳**解析：`_spec_from_url` 被 `_parse_core_single` 与 `_build` 用，
        # 链的切分已经在 `_parse_core` 那一层做完了，再走一遍链式解析会把 `hop` 丢掉。
        spec = proxysvc.parse_single_proxy(raw)
    except proxysvc.ProxyError as exc:
        message = exc.message.replace(raw, _redact(raw)) if raw else exc.message
        raise proxysvc.ProxyError(exc.code, message) from exc
    except ValueError as exc:  # urlsplit 解析端口时抛的是 ValueError，不是 ProxyError
        raise proxysvc.ProxyError("PROXY_INVALID", f"代理串的端口不是数字：{_redact(raw)}") from exc
    if spec is None:
        raise proxysvc.ProxyError("PROXY_INVALID", "空代理串")
    return _normalize(spec)


def _build(scheme: str, host: str, port: Any, user: str, password: str) -> proxysvc.ProxySpec:
    """用拆出来的四段拼回 URL，再走 `parse_proxy` 校验/解码。

    凭据必须先 quote：拆解来的 user/password 里可能含 `:`、`@`、`%`，
    不编码就拼 URL 会被 urlsplit 当成结构字符（`pa@ss` 会变成别的主机）。
    """
    auth = ""
    if user:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    return _spec_from_url(f"{scheme}://{auth}{host}:{port}")


def _parse_core(text: str) -> proxysvc.ProxySpec:
    """把一句话解析成 ProxySpec（不含备注）。失败抛 `proxy.ProxyError`。

    支持两跳链：`前置 | 落地`（也认 `>>` 与 `,`）。
    链式判定与单跳解析的先后顺序由 `proxy.parse_with_chain` 统一管 ——
    两边必须用同一套策略，否则"库里能导入、启动时解析不了"这种不一致会很难查。
    """
    raw = (text or "").strip()
    if not raw:
        raise proxysvc.ProxyError("PROXY_INVALID", "空代理串")
    return proxysvc.parse_with_chain(raw, _parse_core_single)


def _parse_core_single(text: str) -> proxysvc.ProxySpec:
    """单跳解析（同时也认代理商那几种 `host:port:user:pass` 简写）。

    判定顺序是按"哪个位置最不可能看错"排的：

    1. 有 `://` → 标准 URL，交给 `parse_proxy`；
    2. 第一个 `@` 左边就是 `host:port` → `host:port@user:pass`；
       最后一个 `@` 右边是 `host:port` → `user:pass@host:port`；
    3. 两个方向都不成立 → 可能是 `host:port:user:pass` 而凭据里正好含 `@`，
       落到四段切分；
    4. 都不成立 → `host:port`（无认证）。

    **已知歧义**：`u:1234@1.2.3.4:8080` 这种"密码是纯数字"的写法，
    规则 2 会把它读成 `host=u, port=1234, user=1.2.3.4`。想按
    `user:pass@host:port` 理解就带上协议：`http://u:1234@1.2.3.4:8080`。
    """
    raw = (text or "").strip()
    if not raw:
        raise proxysvc.ProxyError("PROXY_INVALID", "空代理串")
    if "://" in raw:
        return _spec_from_url(raw)
    if "@" in raw:
        first_head, _, first_tail = raw.partition("@")
        if _is_host_port(first_head):
            host, port = first_head.split(":")
            user, _, pwd = first_tail.partition(":")
            return _build("http", host, port, user, pwd)
        _last_head, _, last_tail = raw.rpartition("@")
        if _is_host_port(last_tail):
            return _spec_from_url(raw)  # user:pass@host:port
        # 落到下面：可能是 host:port:user:pass，凭据里含 @
    parts = raw.split(":")
    # host:port:user:pass —— 代理商最常见的一种。密码里可能有 `:`，只切前四段
    if len(parts) >= 4 and parts[1].strip().isdigit() and _HOST_RE.fullmatch(parts[0].strip().lower() or ""):
        return _build("http", parts[0], parts[1], parts[2], ":".join(parts[3:]))
    return _spec_from_url(raw)  # host:port（无认证）或 urlsplit 能直接认的其它形态


def _extract_note(text: str) -> tuple[str, str]:
    """切出行尾的 `# 备注`。

    判定一个 `#` 是不是备注标记，两个条件缺一不可：

    1. **要么前面有空白，要么这一行是 URL 形态（含 `://`）。** URL 里 `#` 本来就是
       fragment，写不写空格都是备注；但 `host:port:user:pass` 形态里 `#` 没有任何
       结构含义，紧贴着写的 `#` 更可能是密码的一部分（`...:u:p#ss`）—— 把它当备注
       就等于悄悄把密码截短，是"看起来导入成功、实际永远认证失败"那类问题。
    2. **切完左边仍然是一个合法代理。** 否则 `#` 属于凭据。

    从第一个满足条件的 `#` 开始切，所以 `host:port:u:p#ss # 备注` 也能正确处理。
    """
    for idx, ch in enumerate(text):
        if ch != "#" or idx == 0:
            continue
        spaced = text[idx - 1].isspace()
        if not spaced and "://" not in text[:idx]:
            continue
        head, tail = text[:idx].rstrip(), text[idx + 1:].strip()
        if not head or not tail:
            continue
        try:
            _parse_core(head)
        except proxysvc.ProxyError:
            continue
        return head, tail
    return text, ""


def parse_line(line: str) -> tuple[proxysvc.ProxySpec | None, str]:
    """解析一行导入文本。返回 (spec, 备注)；空行/注释行返回 (None, "")。

    解析失败抛 `proxy.ProxyError`（调用方负责收集，不要整批失败）。
    """
    text = (line or "").strip()
    if not text or text.startswith("#"):
        return None, ""
    body, note = _extract_note(text)
    return _parse_core(body), note


def _redact(text: str) -> str:
    """给"失败行"回显做脱敏。

    失败行会被原样放进响应里给用户看是哪一行错了，但那一行里往往就带着密码。
    宁可少给点信息，也不能把凭据回显出去。
    """
    s = (text or "").strip()
    # 链式行逐段脱敏：整行一起处理会把"前置"那一段连带吃掉，
    # 用户就看不到到底哪一段写错了（导入失败的提示要能定位）。
    segs = proxysvc.split_chain(s)
    if len(segs) > 1:
        return " | ".join(_redact(seg) for seg in segs)
    if "@" in s:
        head, _, tail = s.rpartition("@")
        if "://" in s:
            scheme = s.split("://", 1)[0]
            return f"{scheme}://***@{tail}"
        if _is_host_port(head):
            return f"{head}@***"  # host:port@user:pass 方向相反
        return f"***@{tail}"
    parts = s.split(":")
    if len(parts) >= 3 and parts[1].strip().isdigit():
        return f"{parts[0]}:{parts[1]}:***"
    return s


# ══ 前置（链式代理的左半边）怎么落库 ═══════════════════════
def _ensure_hop_column() -> None:
    """给 `proxies` 表补一列 `hop`，存"前置"那一跳（空串 = 单跳）。

    ## 为什么是懒迁移，而不是改建表语句

    这次改动的边界是代理相关的服务文件；动 `db.py` 的建表语句会牵到 schema 版本号
    与所有存量库，收益远小于风险。SQLite 的 `ADD COLUMN` 本身是幂等的（列已存在就
    报错），这里把异常吃掉即可：老库自动升级，新库同样。

    ## 为什么每次都先查一遍 PRAGMA，而不是留一个模块级"已迁移"标志

    标志看着更省，但它**不成立**：`db.reset_db()` 会换成一个全新的库
    （测试每个用例一个库、运维重建库、换 ZW_ROOT 都会发生），而模块级标志还留着
    True，于是新库永远等不到这条 DDL —— 症状是"第一次能导入，之后报
    no column named hop"。`PRAGMA table_info` 只读一次 schema，代价可忽略，
    换来的是"永远和当前这个库对齐"。

    ## 为什么必须落库

    前置**带着自己的凭据**，不能让用户"再填一次"。而且导入时就得能按
    "前置 + 落地"去重 —— 同一个落地配不同前置是两条完全不同的链路（出口 IP 不同），
    当成一条会直接串号。
    """
    with get_db().conn() as c:
        cols = {row[1] for row in c.execute("PRAGMA table_info(proxies)")}
        if not cols:  # 表还没建出来，等真正建表的路径来加
            return
        if "hop" in cols:
            return
        try:
            c.execute("ALTER TABLE proxies ADD COLUMN hop TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001 —— 并发下另一个人刚好加好了，不影响后续读写
            pass


def _hop_to_text(spec: proxysvc.ProxySpec) -> str:
    """把"前置"这一跳序列化成一行 URL（**含凭据**，与 password 列同等对待）。

    凭据必须 quote：密码里的 `@`、`:`、`/` 直接拼进去会被 urlsplit 读成别的结构
    （`pa@ss` 会变成"另一台主机上的另一个用户"）。
    """
    hop = spec.hop
    if hop is None:
        return ""
    auth = ""
    if hop.username:
        auth = f"{quote(hop.username, safe='')}:{quote(hop.password, safe='')}@"
    return f"{hop.scheme}://{auth}{hop.host}:{hop.port}"


def _hop_of_row(row: Any) -> proxysvc.ProxySpec | None:
    """从库行里读回"前置"。没有 hop 列 / 列为空 / 解析不了 → None（= 单跳）。"""
    try:
        text = row["hop"]
    except (IndexError, KeyError, TypeError):
        return None
    text = (text or "").strip()
    if not text:
        return None
    try:
        return _normalize(proxysvc.parse_single_proxy(text))
    except proxysvc.ProxyError:
        return None


def _hop_key(spec: proxysvc.ProxySpec | None) -> tuple[str, str, int, str, str]:
    """去重键里的"前置"部分。无前置用全空元组（与历史数据天然兼容）。"""
    if spec is None:
        return ("", "", 0, "", "")
    return (spec.scheme, spec.host, spec.port, spec.username, spec.password)


def _spec_of_row(row: Any) -> proxysvc.ProxySpec:
    """库里的行 → ProxySpec（含 `hop`）。

    凭据必须重新 quote：库里存的是**原文**（`pa/ss@x` 这种很常见），
    而 `urlsplit` 会把 `/` 当路径分隔、把 `#` 当 fragment、把 `@` 当 userinfo
    分隔符 —— 直接拼进 URL 会得到"另一个代理"，而且看起来还挺正常。
    """
    user = row["username"] or ""
    if user:
        auth = f"{quote(user, safe='')}:{quote(row['password'] or '', safe='')}@"
    else:
        auth = ""
    base = _normalize(proxysvc.parse_single_proxy(f"{row['scheme']}://{auth}{row['host']}:{row['port']}"))
    hop = _hop_of_row(row)
    return base if hop is None else replace(base, hop=hop)


def _row_to_public(row: Any) -> dict[str, Any]:
    """库里一行 → 可以安全返回的字典（没有 password）。"""
    spec = _spec_of_row(row)
    return {
        "id": int(row["id"]),
        "label": row["label"],
        "scheme": spec.scheme,
        "host": spec.host,
        "port": spec.port,
        "username": spec.username,
        "has_password": bool(spec.password),
        "upstream": spec.upstream,
        # 链式：`is_chain` 给界面决定要不要画"前置 → 落地"，`chain` 是不含凭据的展示串
        "is_chain": spec.hop is not None,
        "chain": spec.chain_text,
        "country": row["country"] or "",
        "last_ok": row["last_ok"],
        "last_check": row["last_check"],
        "last_exit_ip": row["last_exit_ip"] or "",
    }


def _load(proxy_id: int | None) -> Any:
    if proxy_id is None:
        return None
    try:
        pid = int(proxy_id)
    except (TypeError, ValueError):
        return None
    _ensure_hop_column()  # SELECT * 想拿到 hop 列，先保证它存在
    with get_db().conn() as c:
        return c.execute("SELECT * FROM proxies WHERE id=?", (pid,)).fetchone()


def _spec_key(spec: proxysvc.ProxySpec) -> tuple:
    """去重键：同 scheme+host+port+user+password **且同前置** 才算同一个代理。

    前置必须进键：同一个落地配两条不同前置是两条**出口不同**的链路，
    合并成一条就等于把 A 环境的流量从 B 的前置发出去（串号，且没有任何报错）。
    """
    return (_hop_key(spec.hop), spec.scheme, spec.host, spec.port, spec.username, spec.password)


def _row_key(row: Any) -> tuple:
    """库行 → 去重键。与 `_spec_key` 必须同构，否则"库里有、又被判成新的"会无限重复导入。"""
    return (
        _hop_key(_hop_of_row(row)),
        row["scheme"],
        row["host"],
        int(row["port"]),
        row["username"],
        row["password"],
    )


# ══ 标签 ══════════════════════════════════════════════════
def _unique_label(base: str, used: set[str]) -> str:
    """在 `used` 里找一个没被占用的标签，自动加 `-2`/`-3` 后缀。"""
    base = (base or "").strip()[:_MAX_LABEL_LEN]
    if not base:
        base = DEFAULT_LABEL_PREFIX
    if base not in used:
        used.add(base)
        return base
    n = 2
    while True:
        cand = f"{base}-{n}"[:_MAX_LABEL_LEN]
        if cand not in used:
            used.add(cand)
            return cand
        n += 1


def _numbered_label(prefix: str, used: set[str]) -> str:
    """`p1`、`p2`… 这种递增标签（跳过已被占用的号）。"""
    prefix = (prefix or DEFAULT_LABEL_PREFIX).strip()[:_MAX_LABEL_LEN]
    n = 1
    while True:
        cand = f"{prefix}{n}"
        if cand not in used:
            used.add(cand)
            return cand
        n += 1


# ══ 导入 ══════════════════════════════════════════════════
def import_batch(text: str, label_prefix: str = "") -> dict:
    """批量导入代理。**按行切**，一行一条。

    返回 `{"ok": True, "data": {...}}`：
      - `imported`: 新入库条数
      - `skipped`: 与库里已有条目重复（或本批内部重复）而跳过的行数
      - `ignored`: 空行与 `#` 注释行（不算失败，也不算跳过）
      - `failed`: `[{"line": 脱敏后的行, "error": 原因}]`，失败行不影响其它行
      - `ids` / `labels`: 新入库的 id 与标签，顺序与输入一致

    单行失败只进 `failed`，绝不整批回滚 —— 用户粘 50 条有 3 条格式不对，
    剩下 47 条必须能用。

    去重按 `_spec_key`（**含前置**）：同一行重复粘贴只入库一次，但
    "同一个落地 + 不同前置"必须各占一条（出口 IP 不同的两条链路）。
    """
    # 粘贴来的文本常常带 BOM 或 CRLF：`splitlines()` 处理换行，
    # BOM 得自己剥，否则第一行会变成 "\ufeff1.2.3.4:1080" 而解析失败。
    lines = (text or "").lstrip("\ufeff").splitlines()
    failed: list[dict[str, str]] = []
    ids: list[int] = []
    labels: list[str] = []
    skipped = 0
    ignored = 0

    _ensure_hop_column()
    with get_db().conn() as c:
        existing = {_row_key(r) for r in c.execute("SELECT * FROM proxies")}
        used = {r["label"] for r in c.execute("SELECT label FROM proxies")}

        for line in lines:
            if not line.strip():
                ignored += 1
                continue
            if line.strip().startswith("#"):
                ignored += 1
                continue
            try:
                spec, note = parse_line(line)
            except proxysvc.ProxyError as exc:
                failed.append({"line": _redact(line), "error": exc.message})
                continue
            if spec is None:
                ignored += 1
                continue
            key = _spec_key(spec)
            if key in existing:
                skipped += 1
                continue
            label = _unique_label(note, used) if note else _numbered_label(label_prefix, used)
            cur = c.execute(
                "INSERT INTO proxies(label,scheme,host,port,username,password,hop,country,last_ok,last_check,last_exit_ip) "
                "VALUES(?,?,?,?,?,?,?,'',NULL,NULL,'')",
                (label, spec.scheme, spec.host, spec.port, spec.username, spec.password,
                 _hop_to_text(spec)),
            )
            existing.add(key)
            ids.append(int(cur.lastrowid or 0))
            labels.append(label)
            # 只记上游地址与标签；密码永不进日志。逐条走 DEBUG，
            # INFO 只留一条汇总（用户一次可能粘几百行，别把日志刷爆）。
            logger.debug("代理导入 label=%s upstream=%s", label, spec.chain_text)

    if ids or failed:
        logger.info(
            "代理批量导入：新增 %s 条，重复跳过 %s 条，失败 %s 条（共 %s 行）",
            len(ids), skipped, len(failed), len(lines),
        )
        get_db().audit("proxy.import", detail={"imported": len(ids), "skipped": skipped, "failed": len(failed)})
    return {
        "ok": True,
        "data": {
            "total": len(lines),
            "imported": len(ids),
            "skipped": skipped,
            "ignored": ignored,
            "failed": failed,
            "ids": ids,
            "labels": labels,
        },
    }


# ══ 列出 ══════════════════════════════════════════════════
def _bound_profiles(proxy_id: int | None = None) -> list[dict[str, Any]]:
    """哪些环境绑着这个代理（不传 id 就返回全部绑定关系）。

    直接走 SQL 而不是 `manager.list_profiles()`：这里只关心 id/name/state，
    而 manager 那一层会把每行都解析成完整字典（还要算 cdp、租约），
    在这个"只是想知道有没有人用"的场景里太贵，也更容易被并发改动牵连。
    """
    sql = "SELECT id,name,state,proxy_id FROM profiles WHERE proxy_id IS NOT NULL"
    args: tuple[Any, ...] = ()
    if proxy_id is not None:
        sql += " AND proxy_id=?"
        args = (int(proxy_id),)
    with get_db().conn() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY name", args)]


def list_all() -> list[dict[str, Any]]:
    """列出代理库（脱敏），附带"被哪些环境在用"。"""
    _ensure_hop_column()
    with get_db().conn() as c:
        rows = c.execute("SELECT * FROM proxies ORDER BY id").fetchall()

    used: dict[int, list[str]] = {}
    try:
        for p in _bound_profiles():
            used.setdefault(int(p["proxy_id"]), []).append(p["name"])
    except Exception:  # 环境表还没建好时，列表仍然要能返回
        logger.debug("代理库列出时读取环境占用失败", exc_info=True)

    out = []
    for row in rows:
        item = _row_to_public(row)
        item["used_by"] = used.get(item["id"], [])
        item["in_use"] = bool(item["used_by"])
        out.append(item)
    return out


def describe_profile_proxy(profile: dict[str, Any]) -> dict[str, Any]:
    """给"这个环境实际用哪个代理"的一站式回答（给接口/界面展示用）。

    `assign_to_profile()` 会把 `profiles.proxy` 置空、只留 `proxy_id`，
    所以列表页的 `proxy` 字段会看起来是空的 —— 展示时走这个函数，
    不要直接读 `profiles.proxy`。
    """
    proxy_id = profile.get("proxy_id")
    if proxy_id:
        row = _load(int(proxy_id))
        if row is not None:
            spec = _spec_of_row(row)
            return {
                "source": "library",
                "proxy_id": int(row["id"]),
                "label": row["label"],
                "scheme": spec.scheme,
                "host": spec.host,
                "port": spec.port,
                "username": spec.username,
                "has_password": bool(spec.password),
                "upstream": spec.upstream,
                "is_chain": spec.hop is not None,
                "chain": spec.chain_text,
                "last_ok": row["last_ok"],
                "last_check": row["last_check"],
                "last_exit_ip": row["last_exit_ip"] or "",
                "missing": False,
            }
        # 库条目被删掉了（或这台机器上还没有这个库）：报出来，别装作没代理
        return {
            "source": "missing",
            "proxy_id": int(proxy_id),
            "label": "",
            "scheme": "",
            "host": "",
            "port": 0,
            "username": "",
            "has_password": False,
            "upstream": "",
            "is_chain": False,
            "chain": "",
            "last_ok": None,
            "last_check": None,
            "last_exit_ip": "",
            "missing": True,
        }
    raw = (profile.get("proxy") or "").strip()
    if not raw:
        return {
            "source": "none", "proxy_id": None, "label": "", "scheme": "", "host": "",
            "port": 0, "username": "", "has_password": False, "upstream": "",
            "is_chain": False, "chain": "",
            "last_ok": None, "last_check": None, "last_exit_ip": "", "missing": False,
        }
    spec = proxysvc.parse_proxy(raw)
    return {
        "source": "inline",
        "proxy_id": None,
        "label": "",
        "scheme": spec.scheme if spec else "",
        "host": spec.host if spec else "",
        "port": spec.port if spec else 0,
        "username": spec.username if spec else "",
        "has_password": bool(spec.password) if spec else False,
        "upstream": spec.upstream if spec else "",
        "is_chain": bool(spec and spec.hop is not None),
        "chain": spec.chain_text if spec else "",
        "last_ok": None,
        "last_check": None,
        "last_exit_ip": "",
        "missing": False,
    }


# ══ 检测 ══════════════════════════════════════════════════
def _probe(spec: proxysvc.ProxySpec, *, timeout: float, retries: int = 0) -> dict[str, Any]:
    """调 `check_egress`，失败重试 `retries` 次（只对失败重试，成功立即返回）。"""
    last: dict[str, Any] = {"ok": False, "exit_ip": "", "latency_ms": 0, "country": "", "error": ""}
    for _ in range(max(0, retries) + 1):
        try:
            last = proxysvc.check_egress(spec, timeout=timeout)
        except Exception as exc:  # check_egress 自己已兜异常，这里防它将来改动
            last = {
                "ok": False, "exit_ip": "", "latency_ms": 0, "country": "",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if last.get("ok"):
            return last
    return last


def _save_result(proxy_id: int, result: dict[str, Any]) -> float:
    """把检测结果写回库。返回写下的时间戳。"""
    now = time.time()
    with get_db().conn() as c:
        c.execute(
            "UPDATE proxies SET last_ok=?, last_check=?, last_exit_ip=?, "
            "country=COALESCE(NULLIF(?,''), country) WHERE id=?",
            (1 if result.get("ok") else 0, now, str(result.get("exit_ip") or ""),
             str(result.get("country") or ""), int(proxy_id)),
        )
    return now


def _result_payload(
    spec: proxysvc.ProxySpec,
    result: dict[str, Any],
    *,
    proxy_id: int | None,
    label: str = "",
    checked_at: float | None = None,
) -> dict[str, Any]:
    ok = bool(result.get("ok"))
    return {
        "ok": ok,
        "code": "" if ok else "EGRESS_FAILED",
        "proxy_id": proxy_id,
        "label": label,
        "scheme": spec.scheme,
        "host": spec.host,
        "port": spec.port,
        "username": spec.username,
        "has_password": bool(spec.password),  # 只报有没有，不报是什么
        "upstream": spec.upstream,
        "exit_ip": str(result.get("exit_ip") or ""),
        "latency_ms": int(result.get("latency_ms") or 0),
        "country": str(result.get("country") or ""),
        "error": str(result.get("error") or ""),
        "checked_at": checked_at or time.time(),
        "saved": checked_at is not None,
    }


def test_one(proxy_id: int | None, *, raw: str = "", timeout: float = 15.0) -> dict[str, Any]:
    """检测一个代理。

    - 给 `proxy_id`：从库里取，检测结果写回 `last_ok/last_check/last_exit_ip/country`；
    - 给 `raw`：只测不存（用户"先测一下再决定要不要加"的场景）；
    - 两个都不给：返回 `PROXY_INVALID`。

    服务层不抛异常，一律用 `ok` + `code` 表达结果（`PROXY_NOT_FOUND` /
    `PROXY_INVALID` / `PROXY_SCHEME_UNSUPPORTED` / `EGRESS_FAILED`），
    由接口层决定映射成哪个 HTTP 状态。
    """
    row = None
    if proxy_id is not None:
        row = _load(proxy_id)
        if row is None:
            return {
                "ok": False, "code": "PROXY_NOT_FOUND",
                "error": f"代理库里没有 id={proxy_id}", "proxy_id": int(proxy_id),
                "label": "", "upstream": "", "has_password": False,
                "exit_ip": "", "latency_ms": 0, "country": "", "checked_at": time.time(),
                "saved": False,
            }
        spec = _spec_of_row(row)
        label = row["label"]
    elif (raw or "").strip():
        try:
            spec, _note = parse_line(raw)
        except proxysvc.ProxyError as exc:
            return {
                "ok": False, "code": exc.code, "error": exc.message, "proxy_id": None,
                "label": "", "upstream": _redact(raw), "has_password": False,
                "exit_ip": "", "latency_ms": 0, "country": "", "checked_at": time.time(),
                "saved": False,
            }
        if spec is None:
            return {
                "ok": False, "code": "PROXY_INVALID", "error": "没有可检测的代理",
                "proxy_id": None, "label": "", "upstream": "", "has_password": False,
                "exit_ip": "", "latency_ms": 0, "country": "", "checked_at": time.time(),
                "saved": False,
            }
        label = ""
    else:
        return {
            "ok": False, "code": "PROXY_INVALID", "error": "没有可检测的代理",
            "proxy_id": None, "label": "", "upstream": "", "has_password": False,
            "exit_ip": "", "latency_ms": 0, "country": "", "checked_at": time.time(),
            "saved": False,
        }

    result = _probe(spec, timeout=timeout, retries=0)
    saved_at = _save_result(int(row["id"]), result) if row is not None else None
    out = _result_payload(spec, result, proxy_id=int(row["id"]) if row is not None else None,
                          label=label, checked_at=saved_at)
    logger.info(
        "代理检测 label=%s upstream=%s ok=%s exit_ip=%s",
        label or "-", spec.upstream, out["ok"], out["exit_ip"] or "-",
    )
    return out


def _probe_and_save(proxy_id: int, timeout: float, retries: int) -> dict[str, Any]:
    row = _load(proxy_id)
    if row is None:
        return {
            "ok": False, "code": "PROXY_NOT_FOUND", "error": f"代理库里没有 id={proxy_id}",
            "proxy_id": int(proxy_id), "label": "", "upstream": "", "has_password": False,
            "exit_ip": "", "latency_ms": 0, "country": "", "checked_at": time.time(),
            "saved": False,
        }
    spec = _spec_of_row(row)
    result = _probe(spec, timeout=timeout, retries=retries)
    saved_at = _save_result(int(row["id"]), result)
    return _result_payload(spec, result, proxy_id=int(row["id"]), label=row["label"], checked_at=saved_at)


def test_many(
    ids: Iterable[int],
    concurrency: int = 4,
    *,
    timeout: float = 15.0,
    retries: int = 1,
) -> dict[str, Any]:
    """并发检测一批代理，逐条把结果写回库。

    `concurrency` 默认只有 4：这些代理往往是买的共享 IP，一口气开 50 个连接
    会被当成扫描，轻则限速重则封号。失败默认重试 1 次（第一次失败常是抖动）。
    """
    uniq: list[int] = []
    seen: set[int] = set()
    for raw_id in ids or []:
        try:
            pid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if pid not in seen:
            seen.add(pid)
            uniq.append(pid)

    conc = max(1, min(int(concurrency or 4), MAX_CONCURRENCY))
    results: list[dict[str, Any]] = []
    if uniq:
        with ThreadPoolExecutor(max_workers=conc, thread_name_prefix="zw-proxy") as pool:
            futures = [pool.submit(_probe_and_save, pid, timeout, retries) for pid in uniq]
            for fut in futures:
                results.append(fut.result())

    ok_count = sum(1 for r in results if r.get("ok"))
    logger.info("代理批量检测 共 %s 条，成功 %s 条（并发 %s）", len(results), ok_count, conc)
    return {
        "total": len(results),
        "ok_count": ok_count,
        "fail_count": len(results) - ok_count,
        "concurrency": conc,
        "results": results,
    }


# ══ 删除 ══════════════════════════════════════════════════
def delete(proxy_id: int) -> dict[str, Any]:
    """删除一个代理条目。还被环境引用时拒绝（不静默断开别人的出口）。"""
    row = _load(proxy_id)
    if row is None:
        return {"ok": False, "code": "PROXY_NOT_FOUND", "error": f"代理库里没有 id={proxy_id}", "deleted": False}
    bound = [p["name"] for p in _bound_profiles(int(row["id"]))]
    if bound:
        return {
            "ok": False,
            "code": "PROXY_IN_USE",
            "error": f"代理还被这些环境使用：{'、'.join(bound)}",
            "deleted": False,
            "used_by": bound,
            "hint": "先在这些环境里清掉代理（assign_to_profile 传空），再删除",
        }
    with get_db().conn() as c:
        c.execute("DELETE FROM proxies WHERE id=?", (int(row["id"]),))
    # 默认代理指向它的话一并清掉，否则新环境会套用一个已经不存在的代理
    if default_for_new_profile() == int(row["id"]):
        set_default(None)
    get_db().audit("proxy.delete", detail={"id": int(row["id"]), "label": row["label"]})
    return {"ok": True, "deleted": True, "id": int(row["id"]), "label": row["label"]}


# ══ 绑定到环境 ════════════════════════════════════════════
def _upsert(spec: proxysvc.ProxySpec, *, note: str = "", label_prefix: str = "") -> tuple[Any, bool]:
    """把 spec 写进库（已存在就复用）。返回 (行, 是否新建)。"""
    key = _spec_key(spec)
    _ensure_hop_column()
    with get_db().conn() as c:
        for r in c.execute("SELECT * FROM proxies ORDER BY id"):
            if _row_key(r) == key:
                return r, False
        used = {r["label"] for r in c.execute("SELECT label FROM proxies")}
        label = _unique_label(note, used) if note else _numbered_label(label_prefix, used)
        cur = c.execute(
            "INSERT INTO proxies(label,scheme,host,port,username,password,hop,country,last_ok,last_check,last_exit_ip) "
            "VALUES(?,?,?,?,?,?,?,'',NULL,NULL,'')",
            (label, spec.scheme, spec.host, spec.port, spec.username, spec.password,
             _hop_to_text(spec)),
        )
        row = c.execute("SELECT * FROM proxies WHERE id=?", (int(cur.lastrowid),)).fetchone()
    return row, True


def _reset_check_state(proxy_id: int) -> None:
    """换了地址/凭据之后，旧的"上次检测通过"必须作废，否则界面会亮着绿灯骗人。"""
    with get_db().conn() as c:
        c.execute(
            "UPDATE proxies SET last_ok=NULL, last_check=NULL, last_exit_ip='' WHERE id=?",
            (int(proxy_id),),
        )


def assign_to_profile(ref: str, *, proxy_id: int | None, raw: str = "") -> dict[str, Any]:
    """给某个环境设置 / 更换 / 清除代理。

    语义（三种情况，边界清楚）：

    | 入参 | 行为 |
    |---|---|
    | `proxy_id=N` | 引用库条目 N（凭据仍在库里，`profiles.proxy` 置空） |
    | `proxy_id=None, raw="..."` | 先把 raw upsert 进库（已存在则复用），再按上面那条绑定 |
    | `proxy_id=None, raw=""` | **清除代理**：`proxy=""`、`proxy_id=NULL` |

    为什么 `profiles.proxy` 要置空：见模块顶部「一个容易踩的坑」。
    带认证时会顺带生成/复用认证扩展目录，保证**下次启动**能应答代理认证挑战。

    已经在跑的环境一律 `restart_required=true` —— Chromium 的代理是启动期参数，
    这里不做热改，也不替用户重启（重启会丢当前页面状态，得由人决定）。
    """
    from . import manager as mgr

    try:
        profile = mgr.get_profile(ref)
    except mgr.ProfileError as exc:
        return {"ok": False, "code": exc.code, "error": exc.message, "profile": ref}

    state = profile.get("state") or "stopped"
    if state in ("starting", "stopping"):
        return {
            "ok": False,
            "code": "STATE_CONFLICT",
            "error": f"环境正在 {state}，等它稳定后再改代理",
            "profile": profile["id"],
            "name": profile["name"],
            "state": state,
        }

    raw_text = (raw or "").strip()
    cleared = proxy_id is None and not raw_text
    row = None
    spec: proxysvc.ProxySpec | None = None
    created = False

    if cleared:
        mgr._update(profile["id"], proxy="", proxy_id=None)
        logger.info("环境 %s 已清除代理", profile["name"])
        get_db().audit("profile.proxy_assign", profile["id"], detail={"cleared": True})
    else:
        if proxy_id is not None:
            row = _load(proxy_id)
            if row is None:
                return {
                    "ok": False, "code": "PROXY_NOT_FOUND",
                    "error": f"代理库里没有 id={proxy_id}",
                    "profile": profile["id"], "name": profile["name"],
                }
            spec = _spec_of_row(row)
        else:
            try:
                spec, note = parse_line(raw_text)
            except proxysvc.ProxyError as exc:
                return {
                    "ok": False, "code": exc.code, "error": exc.message,
                    "profile": profile["id"], "name": profile["name"],
                }
            if spec is None:
                return {
                    "ok": False, "code": "PROXY_INVALID", "error": "没有可用的代理串",
                    "profile": profile["id"], "name": profile["name"],
                }
            row, created = _upsert(spec, note=note, label_prefix=DEFAULT_LABEL_PREFIX)
            if created:
                logger.info("环境 %s 的临时代理已入库 label=%s upstream=%s",
                            profile["name"], row["label"], spec.upstream)

        # 库是唯一真相：profiles.proxy 置空，只留 proxy_id（见模块文档）
        mgr._update(profile["id"], proxy="", proxy_id=int(row["id"]))
        if spec.needs_auth:
            # 认证扩展按"上游+凭据"哈希落目录，重复调用就是复用；这里保证
            # 用户改完代理不用再等下一次启动才有人去生成它。
            proxysvc.ensure_auth_extension(spec)
        get_db().audit("profile.proxy_assign", profile["id"],
                       detail={"proxy_id": int(row["id"]), "label": row["label"], "created": created})
        logger.info("环境 %s 代理改为 %s（label=%s）", profile["name"], spec.upstream, row["label"])

    running = state in ("running", "starting")
    out: dict[str, Any] = {
        "ok": True,
        "profile": profile["id"],
        "name": profile["name"],
        "state": state,
        "cleared": cleared,
        "created": created,
        "proxy_id": int(row["id"]) if row is not None else None,
        "label": row["label"] if row is not None else "",
        # profiles.proxy 一律置空，实际地址看 upstream（见模块文档）
        "proxy": "",
        "upstream": spec.upstream if spec is not None else "",
        "scheme": spec.scheme if spec is not None else "",
        "host": spec.host if spec is not None else "",
        "port": spec.port if spec is not None else 0,
        "username": spec.username if spec is not None else "",
        "has_password": bool(spec.password) if spec is not None else False,
        "restart_required": running,
        "restart_hint": (
            "该环境正在运行，代理是启动期参数：请重启环境后才会走新代理"
            if running else ""
        ),
    }
    return out


# ══ 修改库里的条目 ════════════════════════════════════════
def update_proxy(
    proxy_id: int,
    *,
    raw: str = "",
    label: str | None = None,
    scheme: str | None = None,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """改一条已入库的代理（用户的"这个代理还能改"）。

    两种用法：

    - 给 `raw`：整条替换（重新解析，凭据也跟着换）；
    - 给字段：局部改（`password=None` 表示不动，`password=""` 表示清掉认证）。

    **改完必须作废上一次检测结果**：地址或凭据变了，"上次出口 IP 是 1.2.3.4"
    就不再成立，留着绿灯会让人以为新配置已经验过。
    若这条代理正被运行中的环境使用，返回 `restart_required=true` 与受影响的环境
    名单 —— 库改了但进程里还是旧代理，这个差必须让调用方看见。
    """
    row = _load(proxy_id)
    if row is None:
        return {"ok": False, "code": "PROXY_NOT_FOUND", "error": f"代理库里没有 id={proxy_id}"}

    current = _spec_of_row(row)
    changed: list[str] = []

    if (raw or "").strip():
        try:
            spec, raw_note = parse_line(raw)
        except proxysvc.ProxyError as exc:
            return {"ok": False, "code": exc.code, "error": exc.message, "id": int(row["id"])}
        if spec is None:
            return {"ok": False, "code": "PROXY_INVALID", "error": "没有可用的代理串", "id": int(row["id"])}
        changed.extend(["scheme", "host", "port", "username", "password"])
        # 只有前置**真的变了**才把 hop 记进 changed：单跳改单跳时 changed 的内容
        # 必须和以前一模一样（接口与界面都按这个集合判断"要不要重启/作废检测"）。
        if _hop_key(current.hop) != _hop_key(spec.hop):
            changed.append("hop")
        if raw_note and not note:
            note = raw_note
    else:
        next_scheme = (scheme or current.scheme)
        next_host = (host if host is not None else current.host)
        next_port = port if port is not None else current.port
        next_user = username if username is not None else current.username
        # password=None 不动；给空串 = 清掉认证（连同用户名，因为没有用户名的密码没有意义）
        next_pass = current.password if password is None else password
        try:
            spec = _normalize(
                proxysvc.ProxySpec(
                    scheme=str(next_scheme), host=str(next_host), port=int(next_port),
                    username=str(next_user or ""), password=str(next_pass or ""),
                )
            )
        except proxysvc.ProxyError as exc:
            return {"ok": False, "code": exc.code, "error": exc.message, "id": int(row["id"])}
        for field, before, after in (
            ("scheme", current.scheme, spec.scheme),
            ("host", current.host, spec.host),
            ("port", current.port, spec.port),
            ("username", current.username, spec.username),
            ("password", current.password, spec.password),
        ):
            if before != after:
                changed.append(field)

    next_label = row["label"]
    if label is not None and label.strip() and label.strip() != row["label"]:
        candidate = label.strip()[:_MAX_LABEL_LEN]
        with get_db().conn() as c:
            taken = c.execute("SELECT 1 FROM proxies WHERE label=? AND id<>?", (candidate, int(row["id"]))).fetchone()
        if taken:
            return {"ok": False, "code": "LABEL_TAKEN", "error": f"代理标签已存在：{candidate}", "id": int(row["id"])}
        next_label = candidate
        changed.append("label")

    if not changed:
        return {
            "ok": True,
            "id": int(row["id"]),
            "label": next_label,
            "changed": [],
            "upstream": current.upstream,
            "has_password": bool(current.password),
            "restart_required": False,
            "affected_profiles": [],
            "restart_hint": "",
        }

    spec_changed = any(
        f in changed for f in ("scheme", "host", "port", "username", "password", "hop")
    )
    # 只改字段（不给 raw）时 hop 原样保留：`_normalize` 造出来的 spec 没有 hop，
    # 直接写回去会把"链式"悄悄降级成"直连落地" —— 那种错完全不报，只是永远连不上。
    next_hop_text = _hop_to_text(spec) if ("hop" in changed or (raw or "").strip()) else (
        (row["hop"] or "") if "hop" in row.keys() else ""
    )
    with get_db().conn() as c:
        c.execute(
            "UPDATE proxies SET label=?, scheme=?, host=?, port=?, username=?, password=?, hop=?, "
            "country=COALESCE(NULLIF(?,''), country) WHERE id=?",
            (next_label, spec.scheme, spec.host, spec.port, spec.username, spec.password,
             next_hop_text, note, int(row["id"])),
        )
    if spec_changed:
        _reset_check_state(int(row["id"]))
        if spec.needs_auth:
            proxysvc.ensure_auth_extension(spec)  # 新凭据也要有扩展，下次启动才管用

    affected = _bound_profiles(int(row["id"]))
    running = [p for p in affected if (p.get("state") or "") in ("running", "starting")]
    get_db().audit("proxy.update", detail={"id": int(row["id"]), "fields": sorted(changed)})
    logger.info("代理条目已改 id=%s label=%s upstream=%s 字段=%s",
                int(row["id"]), next_label, spec.chain_text, ",".join(sorted(changed)))
    return {
        "ok": True,
        "id": int(row["id"]),
        "label": next_label,
        "changed": sorted(changed),
        "upstream": spec.upstream,
        "scheme": spec.scheme,
        "host": spec.host,
        "port": spec.port,
        "username": spec.username,
        "has_password": bool(spec.password),
        "restart_required": bool(running),
        "affected_profiles": [
            {"id": p["id"], "name": p["name"], "state": p["state"]} for p in affected
        ],
        "restart_hint": (
            "这些环境正在运行，仍走旧代理：" + "、".join(p["name"] for p in running)
            if running else ""
        ),
    }


# ══ 默认代理 ══════════════════════════════════════════════
def default_for_new_profile() -> int | None:
    """新建环境时自动套用的代理 id（没设置、或指向的条目已不存在时返回 None）。"""
    raw = get_db().get_setting(DEFAULT_PROXY_SETTING, "")
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if _load(pid) is not None else None


def set_default(proxy_id: int | None) -> dict[str, Any]:
    """设置/清除"新环境默认代理"。`proxy_id=None` = 清除。"""
    if proxy_id is None:
        with get_db().conn() as c:
            c.execute("DELETE FROM settings WHERE key=?", (DEFAULT_PROXY_SETTING,))
        get_db().audit("proxy.set_default", detail={"proxy_id": None})
        return {"ok": True, "proxy_id": None}
    row = _load(proxy_id)
    if row is None:
        return {"ok": False, "code": "PROXY_NOT_FOUND", "error": f"代理库里没有 id={proxy_id}"}
    with get_db().conn() as c:
        get_db().set_setting(c, DEFAULT_PROXY_SETTING, str(int(row["id"])))
    get_db().audit("proxy.set_default", detail={"proxy_id": int(row["id"]), "label": row["label"]})
    return {"ok": True, "proxy_id": int(row["id"]), "label": row["label"]}


def _mask_spec_url(spec: proxysvc.ProxySpec) -> str:
    """单跳的展示串：密码换成 `***`（用户名保留，用来区分"是谁的代理"）。"""
    auth = f"{spec.username}:***@" if spec.username else ""
    return f"{spec.scheme}://{auth}{spec.host}:{spec.port}"


def _chain_display(spec: proxysvc.ProxySpec) -> str:
    """`前置 → 落地`（两边密码都脱敏）。单跳时就是落地本身。"""
    if spec.hop is None:
        return _mask_spec_url(spec)
    return f"{_mask_spec_url(spec.hop)} → {_mask_spec_url(spec)}"


def display_summary(spec: "proxysvc.ProxySpec | None", *, label: str = "") -> dict[str, Any]:
    """给主控台展示用的代理摘要。

    ## 为什么不直接给原始串

    主控台跑在**服务器**上（hub），而这个摘要会随心跳一起发过去。
    把代理密码明文发到别处、还存进 hub 的 SQLite，是没必要的暴露 ——
    要改代理本来就得在客户端上改（hub 改不了本机启动参数）。

    所以这里只给：
      - `url`：把密码换成 `***` 的完整地址（足以确认"是不是这个代理"；链式时是**落地**）
      - `chain`：`前置 → 落地`（**两边**密码都是 `***`），单跳时等于 `url`
      - `is_chain`：界面要不要把 `chain` 画成两段
      - `has_auth` / `username` / `host` / `port` / `scheme`
      - `local`：是否走了本机中继（Chromium 的 --proxy-server 实际指向哪）

    链式时 `username`/`host`/`port` 讲的都是**落地**那一跳：那才是"出口是谁"。
    前置的地址只在 `chain` 里出现 —— 单独给一份前置字段会诱导界面把它当出口显示。
    """
    if spec is None:
        return {"set": False, "url": "", "chain": "", "is_chain": False, "has_auth": False,
                "username": "", "host": "", "port": 0, "scheme": "", "label": label, "local": ""}
    return {
        "set": True,
        "url": _mask_spec_url(spec),
        "chain": _chain_display(spec),
        "is_chain": spec.hop is not None,
        "has_auth": bool(spec.username) or bool(spec.hop and spec.hop.username),
        "username": spec.username,
        "host": spec.host,
        "port": spec.port,
        "scheme": spec.scheme,
        "label": label,
        "local": "",
    }


def attach_hop(proxy_id: int, spec: proxysvc.ProxySpec) -> proxysvc.ProxySpec:
    """把库里存的"前置"补回一个 spec，返回新的 spec（**给启动路径用**）。

    ## 为什么需要在启动时补一次

    `manager.proxy_spec_for()` 是从 `proxies` 表的
    scheme/host/port/username/password **五列**重建代理的，那五列里没有前置。
    于是"库里存的是链式、启动时拿到的却是单跳" —— 本机中继会去直连落地，
    永远连不上（而落地本来就必须经前置才通）。

    `services/engines.py` 的 `_proxy_spec_of()` 在拿到 spec 后调用本函数补一次，
    这样"库里是什么，启动就是什么"这条一致性就闭合了，而且不必改 manager。
    """
    if spec is None or spec.hop is not None:
        return spec
    row = _load(proxy_id)
    if row is None:
        return spec
    hop = _hop_of_row(row)
    return spec if hop is None else replace(spec, hop=hop)

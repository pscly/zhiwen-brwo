"""代理支持：上游地址解析、凭据注入、出口检测。

## 为什么带密码的代理要单独做一个扩展

Chromium 的 `--proxy-server` **不支持**用户名密码。命令行里写成
`http://user:pass@host:port` 时，Chromium 会把整串当主机名，结果就是
连不上，而且报错信息完全看不出是这个原因（这一条在 0.1.0 里是真的踩了）。

标准解法是用扩展调 `chrome.proxy.settings` 并挂 `onAuthRequired` 自动填凭据。
所以这里做两件事：

1. `resolve_extension_dirs()`：把 `--proxy-server` 需要的上游地址算出来；
2. `ensure_auth_extension()`：按凭据生成/复用一个 MV3 扩展目录，
   里面写死了 host:port 与凭据，环境启动时和用户扩展一起 `--load-extension` 加载。

## 为什么扩展目录按凭据哈希命名

同一个代理可能被很多环境共用，目录内容完全一样，没必要每个环境复制一份；
而不同代理必须用不同目录（内容不同），所以用 `sha256(上游+凭据)` 的前 16 位做目录名。

## 链式代理（前置 → 落地）

有些落地代理**只能经某条前置**访问到源码/机房（机场的 SOCKS5 落地、只放行白名单
IP 的住宅代理都是这样）：直连它永远是超时，而从前置过去就通。

这类需求在代理串里用 `|` 写成两段，左边是前置、右边是落地：

```
http://127.0.0.1:21080 | socks5://user:pass@66.80.12.66:20000
```

`ProxySpec.hop` 存的就是"前置"那一跳（`None` = 单跳，行为与以前完全一致）。
`upstream` / `key` / `chain_text` 三个属性的语义在链式下要注意：

- `upstream` 仍然是**落地**地址（日志与展示说"这个环境走哪个出口"）
- `key` **必须包含前置**，否则"同一个落地 + 不同前置"会是同一条链的两份配置，
  而它们出口 IP 完全不同 —— 中继端口一旦复用就是串号（A 环境的流量从 B 的前置出去）
- `chain_text` 是给人和日志看的 `前置|落地`，**不含凭据**

只支持两跳。三跳以上不做：每多一跳就多一层握手与一层超时叠加，收益远小于排查成本，
所以宁可明确报 `PROXY_CHAIN_TOO_LONG`，也不要默默支持出一个"偶尔能通"的怪东西。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from ..config import get_settings

#: 链分隔符。顺序有讲究：`|` 最明确，`>>` 次之，`,` 只是兜底
#: （凭据里真的可能有逗号，所以逗号必须放在"单跳解析失败之后"才用，见 `parse_with_chain`）。
CHAIN_SEPARATORS = ("|", ">>", ",")


@dataclass(frozen=True)
class ProxySpec:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""
    #: 前置代理。`None` = 单跳（直连这个代理），行为与 0.7.x 完全一致。
    hop: "ProxySpec | None" = None

    @property
    def upstream(self) -> str:
        """给 `--proxy-server` 用的形式（不带凭据）。链式下这是**落地**地址。"""
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def needs_auth(self) -> bool:
        """这一跳自己需不需要认证（不含前置）。

        不看 `hop`：中继里"要不要补认证头"是逐跳判断的，
        把前置的凭据算成落地需要认证会往落地多发一条凭据。
        """
        return bool(self.username)

    @property
    def key(self) -> str:
        """中继端口复用键。**必须含前置**：同落地不同前置是两条不同的链路。"""
        raw = f"{self.scheme}|{self.host}|{self.port}|{self.username}|{self.password}"
        if self.hop is not None:
            raw = f"hop[{self.hop.key}]|{raw}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def chain_text(self) -> str:
        """给人看的 `前置|落地`（**不含凭据**，可以进日志/响应）。单跳时就是 upstream。"""
        if self.hop is None:
            return self.upstream
        return f"{self.hop.upstream}|{self.upstream}"

    def safe_dict(self) -> dict[str, Any]:
        """脱敏版本，可以安全地进日志/响应。"""
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "has_password": bool(self.password),
            "upstream": self.upstream,
            "is_chain": self.hop is not None,
            "chain": self.chain_text,
        }


def split_chain(text: str) -> list[str]:
    """按链分隔符把一行切段（**只切，不校验**）。

    优先级 `|` → `>>` → `,`，命中第一个就切。返回的段已经 strip 过。
    长度 >2 的合法性由调用方判断（要报 `PROXY_CHAIN_TOO_LONG`）。
    """
    raw = (text or "").strip()
    for sep in CHAIN_SEPARATORS:
        if sep in raw:
            return [p.strip() for p in raw.split(sep)]
    return [raw]


def mask_url(raw: str) -> str:
    """把代理串里的密码换成 `***`（错误消息 / 日志 / 展示用）。

    故意**不**走解析：这是给"解析失败"那一步用的兜底脱敏，
    解析得失败才走到这里 —— 再调解析就成了循环。
    """
    def one(seg: str) -> str:
        seg = (seg or "").strip()
        if not seg or "@" not in seg:
            return seg
        head, _, tail = seg.rpartition("@")
        if "://" in head:
            return f"{head.split('://', 1)[0]}://***@{tail}"
        return f"***@{tail}"

    return "|".join(one(p) for p in split_chain(raw))


class ProxyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_host(host: str, raw: str) -> None:
    """校验主机名形状。

    ## 为什么必须严格

    用户贴过 `socks5://user:pass@66.80.12 0.66:20000` —— 中间**有个空格**，
    而且 IP 有**五段**。urlsplit 会老老实实把它当成一个主机名收下，
    于是这个代理被存进库、被显示在界面上，然后**连不上，且没有任何提示**。
    排查时只能靠猜。

    所以这里把明显的错误当场拒掉，并说清错在哪：
      - 含空白（粘贴时混进来的空格/制表符）
      - 看着像 IPv4（只有数字和点）但不是正好 4 段、或某段 > 255
      - 其它非法字符
    """
    h = (host or "").strip()
    if not h:
        raise ProxyError("PROXY_INVALID_HOST", "代理主机名是空的")
    if any(ch.isspace() for ch in h):
        raise ProxyError(
            "PROXY_INVALID_HOST",
            f"代理主机名里有空格：{mask_url(raw)}。"
            "多半是粘贴时多打了空格，或者 IP 本身写错了（例如把 66.80.12.66 写成了 66.80.12 0.66）",
        )
    # 形似 IPv4（只由数字和点组成）就严格按 IPv4 校验
    if all(ch.isdigit() or ch == "." for ch in h):
        parts_ = h.split(".")
        if len(parts_) != 4:
            raise ProxyError(
                "PROXY_INVALID_HOST",
                f"IP 应该是 4 段（a.b.c.d），现在有 {len(parts_)} 段：{h}",
            )
        for seg in parts_:
            if not seg or not seg.isdigit() or not (0 <= int(seg) <= 255):
                raise ProxyError(
                    "PROXY_INVALID_HOST",
                    f"IP 段不合法：「{seg}」（每段应在 0-255）：{h}",
                )
        return
    # IPv6 字面量：含 `:` 就按 IPv6 校验（urlsplit 会把 `[::1]` 的方括号去掉，
    # hostname 拿到的是 `::1`）。不认这一支的话，本机/环回的 v6 代理会被误拒。
    if ":" in h:
        import ipaddress as _ip

        try:
            _ip.IPv6Address(h)
        except ValueError as exc:
            raise ProxyError(
                "PROXY_INVALID_HOST", f"IPv6 地址不合法：{h}（例如 ::1 或 2001:db8::1）"
            ) from exc
        return
    # 域名：只允许常见字符，别把整串说明文字当地址收下
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")
    if not set(h) <= allowed:
        raise ProxyError(
            "PROXY_INVALID_HOST",
            f"代理主机名里有非法字符：{h}（只允许字母、数字、点、连字符）",
        )


def parse_proxy(raw: str) -> ProxySpec | None:
    """解析用户填的代理串。空串返回 None。

    接受单跳：`http://h:p`、`socks5://u:p@h:p`、`h:p`、`u:p@h:p`
    也接受两跳链：`前置 | 落地`（分隔符 `|`、`>>`、`,` 都行）。

    为什么要在这里就认链式：`--proxy-server` 只能填一个代理，链式必须由本机中继
    串起来（见 services/proxyrelay.py），而中继拿到的是 `ProxySpec` ——
    所以"两段"这件事必须在解析这一层就被表达出来，否则后面所有调用方都看不见它。
    """
    text = (raw or "").strip()
    if not text:
        return None
    return parse_with_chain(text, parse_single_proxy)


def parse_single_proxy(raw: str) -> ProxySpec:
    """单跳解析（**不做**链切分）。链式判定见 `parse_with_chain`。"""
    text = (raw or "").strip()
    if not text:
        raise ProxyError("PROXY_INVALID", "空代理串")
    if "://" not in text:
        text = f"http://{text}"
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        # urlsplit 对"方括号里的东西不是合法 IPv6"（如 `http://[::zz]:8080`）
        # 直接抛 ValueError。不接住它就会变成一个 500，用户只看到"导入失败"。
        raise ProxyError(
            "PROXY_INVALID_HOST",
            f"代理地址不合法：{mask_url(raw)}（方括号里必须是合法 IPv6，例如 [::1]）",
        ) from exc
    scheme = (parts.scheme or "http").lower()
    if scheme == "socks5h":
        scheme = "socks5"  # SOCKS5 中继本来就把目标域名交给上游解析。
    if scheme not in ("http", "https", "socks5", "socks4", "socks"):
        raise ProxyError("PROXY_SCHEME_UNSUPPORTED", f"不支持的代理协议：{scheme}（可用 http / https / socks5）")
    if not parts.hostname:
        raise ProxyError("PROXY_INVALID", f"代理地址里没有主机名：{mask_url(raw)}")
    _validate_host(parts.hostname, raw)
    try:
        port = parts.port
    except ValueError as exc:
        # urlsplit 在端口不是数字时抛 ValueError，不是 ProxyError；
        # 不接住它就会变成一个莫名其妙的 500（用户看到的只是"导入失败"）。
        raise ProxyError("PROXY_INVALID", f"代理串的端口不是数字：{mask_url(raw)}") from exc
    if not port:
        port = 1080 if scheme.startswith("socks") else 8080
    if not (0 < int(port) < 65536):
        raise ProxyError("PROXY_INVALID_PORT", f"端口超出范围：{port}（应在 1-65535）")
    # urlsplit 不会解码百分号编码（"pa%40ss" 原样留着），但用户填的凭据
    # 常常是编码过的（@ 在 URL 里必须写成 %40），所以要显式还原。
    from urllib.parse import unquote

    return ProxySpec(
        scheme=scheme,
        host=parts.hostname,
        port=int(port),
        username=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
    )


def parse_with_chain(text: str, single: Callable[[str], ProxySpec]) -> ProxySpec:
    """"先认显式链 → 再试单跳 → 最后兜底逗号链"的解析策略。

    为什么是这个顺序（不是一上来就按分隔符切）：凭据是代理商随机生成的，
    **密码里出现逗号完全可能**。`http://u:p,ss@h:8080` 按逗号切会变成
    "http://u:p" + "ss@h:8080" 两段，两段都能解析成功 —— 于是悄悄变成一条
    不存在的链，出口 IP 完全不对且没有任何报错。而 `|` 与 `>>`
    不会出现在任何密码策略里，所以它们可以无条件当分隔符。
    """
    parts = split_chain(text)
    if len(parts) > 2:
        raise ProxyError(
            "PROXY_CHAIN_TOO_LONG",
            f"只支持两跳（前置 | 落地），这行有 {len(parts)} 段：{mask_url(text)}",
        )
    explicit = "|" in text or ">>" in text
    if len(parts) == 2 and explicit:
        return _chain_of(parts, single)
    try:
        return single(text)
    except (ProxyError, ValueError):
        if len(parts) != 2:
            raise
        # 单跳解析不了，而串里确实有两个可解析的段 → 那就是逗号写的链
        return _chain_of(parts, single)


def _chain_of(parts: list[str], single: Callable[[str], ProxySpec]) -> ProxySpec:
    """把 `[前置, 落地]` 两段合成一个带 `hop` 的 spec。"""
    hop_text, exit_text = parts[0], parts[1]
    if not hop_text or not exit_text:
        raise ProxyError(
            "PROXY_INVALID",
            f"链式代理两段都要填（前置 | 落地），现在只有一段：{mask_url('|'.join(parts))}",
        )
    hop = single(hop_text)
    exit_spec = single(exit_text)
    if exit_spec.hop is not None:
        raise ProxyError("PROXY_CHAIN_TOO_LONG", "只支持两跳（前置 | 落地）")
    return replace(exit_spec, hop=hop)


# ── 认证扩展 ──────────────────────────────────────────────
_MANIFEST = {
    "manifest_version": 3,
    "name": "知纹代理认证",
    "version": "1.0.0",
    "description": "为知纹管理的浏览器环境自动填写代理用户名密码。由知纹生成，请勿手动修改。",
    "permissions": ["proxy", "storage", "webRequest", "webRequestAuthProvider"],
    "host_permissions": ["<all_urls>"],
    "background": {"service_worker": "sw.js"},
    "minimum_chrome_version": "108",
}

_SW_JS = """// 由知纹生成：把代理配置与凭据下发到 chrome.proxy，并自动应答认证挑战。
const CONFIG = %CONFIG%;

function apply() {
  const cfg = {
    mode: "fixed_servers",
    rules: {
      singleProxy: { scheme: CONFIG.scheme, host: CONFIG.host, port: CONFIG.port },
      bypassList: ["localhost", "127.0.0.1", "<local>"],
    },
  };
  chrome.proxy.settings.set({ value: cfg, scope: "regular" }, () => {
    if (chrome.runtime.lastError) {
      console.error("[知纹] 设置代理失败", chrome.runtime.lastError.message);
    }
  });
}

chrome.runtime.onInstalled.addListener(apply);
chrome.runtime.onStartup.addListener(apply);
apply();

if (CONFIG.username) {
  chrome.webRequest.onAuthRequired.addListener(
    (details, callback) => {
      if (!details.isProxy) { callback({}); return; }
      callback({ authCredentials: { username: CONFIG.username, password: CONFIG.password } });
    },
    { urls: ["<all_urls>"] },
    ["asyncBlocking"]
  );
}
"""


def ensure_auth_extension(spec: ProxySpec) -> Path:
    """生成（或复用）代理认证扩展目录，返回绝对路径。"""
    s = get_settings()
    target = Path(s.runtime_dir) / "proxy-auth" / spec.key
    manifest_path = target / "manifest.json"
    if manifest_path.exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    config = {
        "scheme": spec.scheme if spec.scheme != "socks" else "socks5",
        "host": spec.host,
        "port": spec.port,
        "username": spec.username,
        "password": spec.password,
    }
    (target / "manifest.json").write_text(json.dumps(_MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "sw.js").write_text(_SW_JS.replace("%CONFIG%", json.dumps(config, ensure_ascii=False)), encoding="utf-8")
    return target


def cleanup_auth_extensions(keep: set[str] | None = None) -> int:
    """清掉没人用的认证扩展目录（里面含明文凭据，不能无限堆积）。"""
    s = get_settings()
    root = Path(s.runtime_dir) / "proxy-auth"
    if not root.exists():
        return 0
    keep = keep or set()
    removed = 0
    for d in root.iterdir():
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


# ── 出口检测 ──────────────────────────────────────────────
# 多个源轮询：任一成功即可，避免某个站被墙/挂了就判定代理坏
_ECHO_ENDPOINTS = [
    "https://api.ipify.org?format=json",
    "https://ifconfig.me/all.json",
    "https://ipinfo.io/json",
]


def _echo_endpoints() -> list[str]:
    """出口检测要访问的 IP 回显地址（可配置）。

    ## 为什么要可配置

    1. **国内网络**：这三个都在境外，很可能慢或被拦。用户自己的机器上
       完全可以换成自建的一个返回 `{"ip": "..."}` 的地址。
    2. **可测性**：硬编码外网地址意味着这条"代理出口检测"路径**没法在本机验证**
       —— 之前 `scripts/e2e-check.py` 里它一直是跳过项，等于没跑过。

    配置：`ZW_EGRESS_ENDPOINTS`（逗号分隔）。留空就用上面那三个默认值。
    """
    from ..config import get_settings

    try:
        custom = list(get_settings().egress_endpoints or [])
    except Exception:  # noqa: BLE001 —— 配置读不出来不该让检测直接崩
        custom = []
    return custom or list(_ECHO_ENDPOINTS)


def check_egress(spec: ProxySpec, timeout: float = 15.0) -> dict[str, Any]:
    """通过这个代理访问一次出口 IP 接口，返回 {ok, exit_ip, latency_ms, country, error}。

    链式代理走另一条路：**本机中继**。理由很直接 —— 落地代理直连不通（这正是它
    需要前置的原因），httpx 也不会替我们串两跳。而中继里已经有完整的
    "穿过前置再对落地握手"的实现，复用它比在这里再写一遍第二套链式逻辑可靠得多。
    """
    if spec.hop is not None:
        return _check_egress_via_relay(spec, timeout)

    proxy_url = spec.scheme
    if spec.scheme == "socks":
        proxy_url = "socks5"
    auth = f"{spec.username}:{spec.password}@" if spec.username else ""
    if spec.scheme.startswith("socks"):
        # httpx 的 socks 支持需要 httpx[socks]；没装时给出清晰提示而不是 500
        try:
            import socksio  # noqa: F401
        except ImportError:
            return {
                "ok": False,
                "exit_ip": "",
                "latency_ms": 0,
                "country": "",
                "error": "本机缺少 socks 支持（pip install 'httpx[socks]'），无法检测 socks 代理",
            }
    return _probe_egress(f"{proxy_url}://{auth}{spec.host}:{spec.port}", timeout)


def _check_egress_via_relay(spec: ProxySpec, timeout: float) -> dict[str, Any]:
    """临时起一个中继，把两跳串起来再去取出口 IP，探完立刻关。

    为什么不放进 `RelayPool`：检测是低频动作（用户点一下"测试"），
    没必要为此常驻一个监听端口；池里的端口要留给真正在跑的环境。
    """
    try:
        from . import proxyrelay  # 延迟导入：proxyrelay 反过来依赖本模块的 ProxySpec
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exit_ip": "", "latency_ms": 0, "country": "",
                "error": f"链式代理需要本机中继，但中继模块加载失败：{exc}"}
    server = proxyrelay.RelayServer(spec)
    try:
        port = server.start()
    except OSError as exc:
        return {"ok": False, "exit_ip": "", "latency_ms": 0, "country": "",
                "error": f"链式检测无法启动本机中继（{type(exc).__name__}: {exc}）"}
    try:
        out = _probe_egress(f"http://127.0.0.1:{port}", timeout)
        if not out["ok"]:
            # 中继自己知道卡在哪一跳（前置还是落地），这个原因比 httpx 的
            # "连接被重置"有用得多。中继已经把细节存在 stats().last_error 里。
            detail = (server.stats().get("last_error") or "").strip()
            if detail:
                out["error"] = f"{out['error']}（中继：{detail}）" if out["error"] else detail
        return out
    finally:
        server.stop()


def _probe_egress(proxy_url: str, timeout: float) -> dict[str, Any]:
    """真正去轮询出口接口。`proxy_url` 是标准 `scheme://[auth@]host:port` 形态。"""
    last_error = ""
    for endpoint in _echo_endpoints():
        started = time.perf_counter()
        try:
            with httpx.Client(proxy=proxy_url, timeout=timeout, follow_redirects=True) as client:
                resp = client.get(endpoint, headers={"User-Agent": "curl/8.5.0"})
            latency = int((time.perf_counter() - started) * 1000)
            if resp.status_code >= 400:
                last_error = f"出口接口返回 HTTP {resp.status_code}"
                continue
            data: Any
            try:
                data = resp.json()
            except Exception:
                data = {}
            ip = ""
            country = ""
            if isinstance(data, dict):
                ip = str(data.get("ip") or data.get("origin") or "").split(",")[0].strip()
                country = str(data.get("country") or data.get("country_code") or "")
            if not ip:
                ip = resp.text.strip()[:64]
            if not ip:
                last_error = "出口接口没有返回 IP"
                continue
            return {"ok": True, "exit_ip": ip, "latency_ms": latency, "country": country, "error": ""}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
    return {"ok": False, "exit_ip": "", "latency_ms": 0, "country": "", "error": last_error or "所有出口检测源都失败了"}


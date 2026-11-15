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
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import get_settings


@dataclass(frozen=True)
class ProxySpec:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def upstream(self) -> str:
        """给 `--proxy-server` 用的形式（不带凭据）。"""
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def needs_auth(self) -> bool:
        return bool(self.username)

    @property
    def key(self) -> str:
        raw = f"{self.scheme}|{self.host}|{self.port}|{self.username}|{self.password}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def safe_dict(self) -> dict[str, Any]:
        """脱敏版本，可以安全地进日志/响应。"""
        return {
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "has_password": bool(self.password),
            "upstream": self.upstream,
        }


class ProxyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_proxy(raw: str) -> ProxySpec | None:
    """解析用户填的代理串。空串返回 None。

    接受：`http://h:p`、`socks5://u:p@h:p`、`h:p`、`u:p@h:p`
    """
    text = (raw or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"http://{text}"
    parts = urlsplit(text)
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https", "socks5", "socks4", "socks"):
        raise ProxyError("PROXY_SCHEME_UNSUPPORTED", f"不支持的代理协议：{scheme}（可用 http / https / socks5）")
    if not parts.hostname:
        raise ProxyError("PROXY_INVALID", f"代理地址里没有主机名：{raw}")
    port = parts.port
    if not port:
        port = 1080 if scheme.startswith("socks") else 8080
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


def check_egress(spec: ProxySpec, timeout: float = 15.0) -> dict[str, Any]:
    """通过这个代理访问一次出口 IP 接口，返回 {ok, exit_ip, latency_ms, country, error}。"""
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
    url = f"{proxy_url}://{auth}{spec.host}:{spec.port}"

    last_error = ""
    for endpoint in _ECHO_ENDPOINTS:
        started = time.perf_counter()
        try:
            with httpx.Client(proxy=url, timeout=timeout, follow_redirects=True) as client:
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

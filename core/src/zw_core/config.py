"""核心配置。全部来自环境变量 / .env 文件，便于 Docker 与裸装两种部署。

Windows 上无需设置任何变量即可跑起来（有默认值），这是刻意的：
用户明天拿到手，双击/一条命令就应该能起。
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _default_root() -> Path:
    """默认数据目录。

    - Windows：`%LOCALAPPDATA%\\zhiwen`
    - Linux/macOS：`~/.local/share/zhiwen`
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "zhiwen"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "zhiwen"


@dataclass
class Settings:
    # ── 基础 ────────────────────────────────────────────
    root: Path = field(default_factory=lambda: Path(_env("ZW_ROOT") or _default_root()))
    node_id: str = field(default_factory=lambda: _env("ZW_NODE_ID") or socket.gethostname())
    host: str = field(default_factory=lambda: _env("ZW_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("ZW_PORT", 8730))

    # 管理令牌。留空 = 只允许本机回环且不校验（开发模式）
    admin_token: str = field(default_factory=lambda: _env("ZW_ADMIN_TOKEN", ""))
    # 内部令牌：gateway ↔ core
    internal_token: str = field(default_factory=lambda: _env("ZW_INTERNAL_TOKEN", ""))

    # ── 引擎 ────────────────────────────────────────────
    engine_dir: Path = field(default_factory=lambda: Path(_env("ZW_ENGINE_DIR", "")) if _env("ZW_ENGINE_DIR") else Path(""))
    # 显式指定 chrome 可执行文件（优先级最高）
    chrome_bin: str = field(default_factory=lambda: _env("ZW_CHROME_BIN", ""))
    # 浏览器额外启动参数（空格分隔），例如 "--enable-unsafe-swiftshader"
    chrome_extra_args: list[str] = field(
        default_factory=lambda: _env("ZW_CHROME_EXTRA_ARGS").split() if _env("ZW_CHROME_EXTRA_ARGS") else []
    )
    # 是否允许以 root 运行（仅 Linux 容器内需要）
    allow_root: bool = field(default_factory=lambda: _env_bool("ZW_ALLOW_ROOT", False))

    # ── 端口池 ──────────────────────────────────────────
    port_pool_start: int = field(default_factory=lambda: _env_int("ZW_PORT_POOL_START", 21001))
    port_pool_end: int = field(default_factory=lambda: _env_int("ZW_PORT_POOL_END", 21100))
    max_running: int = field(default_factory=lambda: _env_int("ZW_MAX_RUNNING", 20))

    # ── 生命周期 ────────────────────────────────────────
    launch_timeout: int = field(default_factory=lambda: _env_int("ZW_LAUNCH_TIMEOUT", 45))
    stop_grace: int = field(default_factory=lambda: _env_int("ZW_STOP_GRACE", 6))
    health_interval: int = field(default_factory=lambda: _env_int("ZW_HEALTH_INTERVAL", 15))
    idle_timeout: int = field(default_factory=lambda: _env_int("ZW_IDLE_TIMEOUT", 0))  # 0 = 不自动回收

    # ── 网关 ────────────────────────────────────────────
    gateway_enabled: bool = field(default_factory=lambda: _env_bool("ZW_GATEWAY_ENABLED", True))
    gw_public_base: str = field(default_factory=lambda: _env("ZW_GATEWAY_PUBLIC_BASE", ""))
    cdp_token_ttl: int = field(default_factory=lambda: _env_int("ZW_CDP_TOKEN_TTL", 900))
    cdp_max_conn_per_profile: int = field(default_factory=lambda: _env_int("ZW_CDP_MAX_CONN", 8))

    # ── 网络访问控制 ────────────────────────────────────
    allowed_cidrs: list[str] = field(
        default_factory=lambda: [c.strip() for c in _env("ZW_ALLOWED_CIDRS", "127.0.0.1/32").split(",") if c.strip()]
    )
    # 绑 0.0.0.0 时是否强制要求 token
    force_token_on_public: bool = field(default_factory=lambda: _env_bool("ZW_FORCE_TOKEN_ON_PUBLIC", True))

    # ── 同步 ────────────────────────────────────────────
    hub_url: str = field(default_factory=lambda: _env("ZW_HUB_URL", ""))
    hub_token: str = field(default_factory=lambda: _env("ZW_HUB_TOKEN", ""))
    # 同步主密码：不落盘，只从环境变量或交互式输入
    sync_password: str = field(default_factory=lambda: _env("ZW_SYNC_PASSWORD", ""))
    sync_enabled: bool = field(default_factory=lambda: _env_bool("ZW_SYNC_ENABLED", True))

    # ── 日志 ────────────────────────────────────────────
    log_level: str = field(default_factory=lambda: _env("ZW_LOG_LEVEL", "INFO").upper())
    log_json: bool = field(default_factory=lambda: _env_bool("ZW_LOG_JSON", False))
    log_keep_days: int = field(default_factory=lambda: _env_int("ZW_LOG_KEEP_DAYS", 14))

    # ── 派生路径（resolve 后填充）────────────────────────
    db_path: Path = field(default=Path("."))
    profiles_dir: Path = field(default=Path("."))
    ext_dir: Path = field(default=Path("."))
    log_dir: Path = field(default=Path("."))
    staging_dir: Path = field(default=Path("."))

    def resolve(self) -> "Settings":
        self.root = Path(self.root).expanduser().resolve()
        self.db_path = self.root / "zhiwen.db"
        self.profiles_dir = self.root / "profiles"
        self.ext_dir = self.root / "extensions"
        self.log_dir = self.root / "logs"
        self.staging_dir = self.root / "staging"
        if not self.engine_dir:
            self.engine_dir = self.root / "engines"
        self.engine_dir = Path(self.engine_dir).expanduser().resolve()
        for d in (self.root, self.profiles_dir, self.ext_dir, self.log_dir, self.staging_dir, self.engine_dir):
            Path(d).mkdir(parents=True, exist_ok=True)

        if self.port_pool_end <= self.port_pool_start:
            raise ValueError("ZW_PORT_POOL_END 必须大于 ZW_PORT_POOL_START")

        # 安全兜底：对外监听时必须要有 admin token
        if self.host not in ("127.0.0.1", "localhost", "::1") and self.force_token_on_public and not self.admin_token:
            raise ValueError(
                "ZW_HOST 不是回环地址时必须设置 ZW_ADMIN_TOKEN，否则任何人都能控制你的浏览器。"
                "如果确实不需要（例如只在内网且已用防火墙隔离），显式设置 ZW_FORCE_TOKEN_ON_PUBLIC=false。"
            )
        if self.host not in ("127.0.0.1", "localhost", "::1") and not self.internal_token:
            self.internal_token = self.admin_token  # 单机部署常见情形，网关与核心共用
        if not self.internal_token:
            self.internal_token = "dev-internal-token"
        return self

    @property
    def gateway_public_base(self) -> str:
        """对外可访问的网关地址（含协议与端口）。"""
        if self.gw_public_base:
            return self.gw_public_base.rstrip("/")
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        s = Settings()
        _settings = s.resolve()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None

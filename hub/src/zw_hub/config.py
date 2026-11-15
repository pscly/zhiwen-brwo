"""zw-hub 配置。全部来自环境变量，便于 Docker 部署。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """运行期设置。所有字段都有安全默认值，缺省即可启动。"""

    # 数据根目录（容器内挂卷）
    data_dir: Path = field(default_factory=lambda: Path(_env("ZW_DATA_DIR", "/data")))

    # 访问令牌：客户端必须带 Authorization: Bearer <token>
    # 留空则只允许本机回环访问（方便开发）
    token: str = field(default_factory=lambda: _env("ZW_HUB_TOKEN", ""))

    # 监听
    host: str = field(default_factory=lambda: _env("ZW_HUB_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("ZW_HUB_PORT", 8700))

    # 单个快照块的最大字节数（默认 256 MiB，够 8MB 包留足余量）
    max_blob_bytes: int = field(default_factory=lambda: _env_int("ZW_MAX_BLOB_BYTES", 256 * 1024 * 1024))

    # 每个 profile 保留的历史版本数（0 = 不限制）
    keep_revisions: int = field(default_factory=lambda: _env_int("ZW_KEEP_REVISIONS", 10))

    # 租约默认时长（秒）
    lease_ttl: int = field(default_factory=lambda: _env_int("ZW_LEASE_TTL", 90))

    # 是否要求请求签名（HMAC-SHA256）。开放给局域网时建议开
    require_signature: bool = field(default_factory=lambda: _env_bool("ZW_REQUIRE_SIGNATURE", False))
    signature_secret: str = field(default_factory=lambda: _env("ZW_SIGNATURE_SECRET", ""))

    # 允许的来源 CIDR，逗号分隔；留空表示不限制（Docker 内网场景）
    allowed_cidrs: list[str] = field(
        default_factory=lambda: [c.strip() for c in _env("ZW_ALLOWED_CIDRS", "").split(",") if c.strip()]
    )

    # 日志
    log_level: str = field(default_factory=lambda: _env("ZW_LOG_LEVEL", "INFO").upper())
    log_dir: Path = field(default_factory=lambda: Path(_env("ZW_LOG_DIR", "")))

    def resolve_paths(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "blobs").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "latest").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "leases").mkdir(parents=True, exist_ok=True)
        if not self.log_dir:
            self.log_dir = self.data_dir / "logs"
        self.log_dir = Path(self.log_dir).expanduser().resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        s = Settings()
        s.resolve_paths()
        _settings = s
    return _settings


def reset_settings() -> None:
    """仅供测试使用。"""
    global _settings
    _settings = None

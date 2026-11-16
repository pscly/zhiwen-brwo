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

    # 访问令牌（可选加固）。
    #
    # 默认**留空**：个人自用的形态是"客户端只填服务器 IP 就能用"，
    # 再要求一串对接码就违背了这个目标。留空时准入靠设备注册表
    # （每台电脑都要注册成设备，服务器端可以按机器码踢掉）。
    #
    # 需要更严时设 ZW_HUB_TOKEN：此时客户端要填对接码，且所有 API 都要求它。
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

    # ── 管理面板 ────────────────────────────────────────
    # 面板登录。默认值按"个人自用"给了固定的账号密码，但可用环境变量覆盖；
    # 部署脚本会把 ZW_PANEL_PASSWORD 随机化后写进 .env，避免真的用仓库里的默认值。
    panel_user: str = field(default_factory=lambda: _env("ZW_PANEL_USER", "admin"))
    panel_password: str = field(default_factory=lambda: _env("ZW_PANEL_PASSWORD", "1234"))
    # 面板会话密钥（签名 cookie 用）。留空则每次启动随机生成（重启后需重新登录）。
    panel_secret: str = field(default_factory=lambda: _env("ZW_PANEL_SECRET", ""))
    # 面板会话有效期（秒）
    panel_session_ttl: int = field(default_factory=lambda: _env_int("ZW_PANEL_SESSION_TTL", 7 * 24 * 3600))
    # 新设备是否自动放行。个人自用 = True（只填 IP 就能用）；
    # 想审核就设 false，新设备会停在 pending，需要在面板里点放行。
    auto_approve: bool = field(default_factory=lambda: _env_bool("ZW_DEVICE_AUTO_APPROVE", True))

    # ── 远程命令行（hub 侧）──────────────────────────────
    # 总开关。关掉之后管理员侧的命令行接口一律 403 SHELL_DISABLED ——
    # 这是"整台服务器不许下发命令"，和单台设备的本地开关是两回事（纵深防御）。
    shell_enabled: bool = field(default_factory=lambda: _env_bool("ZW_SHELL_ENABLED", True))
    # stdout / stderr 各自的上限（字节）。输出按 UTF-8 安全边界截断。
    shell_max_output: int = field(default_factory=lambda: _env_int("ZW_SHELL_MAX_OUTPUT", 65536))
    # 每台设备排队中的命令上限，到顶返回 429 SHELL_QUEUE_FULL
    shell_queue_limit: int = field(default_factory=lambda: _env_int("ZW_SHELL_QUEUE_LIMIT", 200))

    # 日志
    log_level: str = field(default_factory=lambda: _env("ZW_LOG_LEVEL", "INFO").upper())
    log_dir: Path = field(default_factory=lambda: Path(_env("ZW_LOG_DIR", "")))

    def resolve_paths(self) -> None:
        if not self.panel_secret:
            # 会话签名密钥：随机生成，重启后旧 cookie 失效（可接受，登录一次很快）
            import secrets

            self.panel_secret = secrets.token_urlsafe(32)
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

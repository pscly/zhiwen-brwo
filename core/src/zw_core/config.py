"""核心配置。全部来自环境变量 / .env 文件，便于 Docker 与裸装两种部署。

Windows 上无需设置任何变量即可跑起来（有默认值），这是刻意的：
用户拿到手，双击/一条命令就应该能起。

## 一条约定

**配置只在这里读，别的地方一律走 `get_settings()`。** 因为 settings 是单例，
测试里靠 `reset_settings()` 换掉整套环境变量；谁绕过它直接读 os.environ，
测试之间就会串味（这个坑在 0.1.0 里踩过）。
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ── .env 文件支持 ────────────────────────────────────────────
#
# 模块顶部的注释一直写着"全部来自环境变量 / .env 文件"，但**代码里从来没有
# 加载过 .env**：`_env()` 只读 `os.environ`。对 Docker 部署没影响（compose 里
# 直接给 environment），但对**单文件客户端**影响很大 —— 用户在 Windows 上想配
# `ZW_DOWNLOAD_PROXY`，只能去"系统属性 → 环境变量"里加，改完还要重开程序，
# 绝大多数人根本不会做。
#
# 这里补上真正的 .env 读取：启动时从两处按顺序找，**先找到的赢**，
# 且**不覆盖已有的真实环境变量**（环境变量优先级永远更高，Docker/CI 行为不变）。
_PREFERRED_ENV_FILES: tuple[str, ...] = ("zhiwen.env", ".env")
_dotenv_loaded = False


def _dotenv_candidates() -> list[Path]:
    """找 .env 的位置：先配置目录（用户容易找到），再数据目录/工作目录。"""
    out: list[Path] = []
    override = os.environ.get("ZW_ENV_FILE", "").strip()
    if override:
        out.append(Path(override).expanduser())
    home_env = os.environ.get("ZW_CLIENT_HOME", "").strip()
    if home_env:
        base = Path(home_env).expanduser()
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "zhiwen-client"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "zhiwen-client"
    for name in _PREFERRED_ENV_FILES:
        out.append(base / name)
    cwd = Path.cwd()
    for name in _PREFERRED_ENV_FILES:
        out.append(cwd / name)
    return out


def load_dotenv_once() -> Path | None:
    """读第一个存在的 .env，返回它用了哪个文件（没读就返回 None）。

    规则与常见实践一致：`KEY=VALUE`，`#` 开头是注释，允许 `export KEY=VALUE`，
    值两端的引号会被去掉。**已存在的环境变量不会被覆盖** —— 这样命令行/CI
    里显式设的值永远优先，行为可预期。
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return None
    _dotenv_loaded = True
    for path in _dotenv_candidates():
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            key, _, val = line.partition("=")
            key = key.strip()
            if not key:
                continue
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            os.environ.setdefault(key, val)
        return path
    return None


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


# ── 启动时默认打开的"检测页"────────────────────────────────
# 挑选标准：无需登录、不需要人机验证、打开就能看到出口 IP / 指纹 / IP 干净度。
# 2026-11-17 实测（curl 拿到 200 之后，再用 google-chrome 149 --headless=new
# 真实渲染一次，确认页面里真的有数据，而不是只剩一个 JS 空壳）：
#
#   https://browserleaks.com/ip                    200，渲染出 "IP Address 118.114.95.247"，
#                                                  同时给出 WebRTC 泄漏结果 —— 出口 IP 与泄漏一起看
#   https://ip123.in/                              200，渲染出「欺诈评分 0/100 低风险」「代理 IP / VPN
#                                                  特征：未检测到」「黑名单状态：未检测到」「IP 来源：原生 IP」
#                                                  「IP 属性：家庭宽带」—— 中文的 IP 干净度页，正好对上需求
#   https://abrahamjuliot.github.io/creepjs/       200，渲染出 "FP ID: 11e647…" 与 fuzzy hash、
#                                                  WebRTC 候选、时区/语言 —— 综合指纹
#
# 被剔掉的候选（都试过，各自有硬伤）：
#   https://ping0.cc/                 curl 200 但 DOM 是空的：Cloudflare Turnstile 人机验证墙，
#                                     机器启动时打开只会看到验证码，不算"能用"
#   https://scamalytics.com/ip        curl/渲染都 200，但页面是**查询表单**，不会自动显示
#                                     "你自己这个 IP"的欺诈分；要填 IP 才有结果
#   https://www.ip.cn/                渲染出 127.0.0.1（页面走了本地探测），不能反映出口 IP
#   https://whoer.net/ 、https://ipcheck.ing/   渲染出来只有"请稍候…"，是纯前端挑战页
#   https://www.ipqualityscore.com/  需要 API Key
#
# 备选（也验证过能用，嫌默认三页太多时可以换上来）：
#   https://ipinfo.io/json           纯 JSON，最轻：{"ip":"…","city":"…","org":"AS4134 …"}
#   https://browserleaks.com/canvas  单独看 canvas 指纹
DEFAULT_DETECT_URLS = (
    "https://browserleaks.com/ip",
    "https://ip123.in/",
    "https://abrahamjuliot.github.io/creepjs/",
)


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
    # 默认监听 0.0.0.0：客户端控制台要能被同网段/远程访问。
    # **安全边界不靠"只绑回环"**，而是靠 require_auth：
    #   - 来自回环 → 直接放行（本机打开控制台不需要任何密码）
    #   - 来自其它地址 → 必须提供机器码（device_id）或 ZW_ADMIN_TOKEN
    # 浏览器本身仍然只听 127.0.0.1（见 engines.build_cmdline），
    # 所以对外暴露的只有控制台 API，不是 CDP。
    host: str = field(default_factory=lambda: _env("ZW_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("ZW_PORT", 8730))

    # 管理令牌。留空 = 只允许本机回环且不校验（开发模式）
    admin_token: str = field(default_factory=lambda: _env("ZW_ADMIN_TOKEN", ""))
    # （这里曾经有个 `internal_token`：注释说"gateway ↔ core 的内部令牌"，
    #   但**没有任何代码读它** —— 网关鉴权只用 `admin_token` / `device_id`
    #   （见 api.py 的 require_auth）。已删除，避免让人以为存在内部鉴权。）

    # ── 引擎 ────────────────────────────────────────────
    engine_dir: Path = field(default_factory=lambda: Path(_env("ZW_ENGINE_DIR", "")) if _env("ZW_ENGINE_DIR") else Path(""))
    # 显式指定 chrome 可执行文件（优先级最高）
    chrome_bin: str = field(default_factory=lambda: _env("ZW_CHROME_BIN", ""))
    # 浏览器额外启动参数（空格分隔），例如 "--enable-unsafe-swiftshader"
    chrome_extra_args: list[str] = field(
        default_factory=lambda: _env("ZW_CHROME_EXTRA_ARGS").split() if _env("ZW_CHROME_EXTRA_ARGS") else []
    )
    # 是否允许以 root 运行。
    #
    # **默认 true**：容器 / CI / 很多服务器上就是 root，Chromium 在 root 下必须加
    # `--no-sandbox` 才能启动，这是历史行为。把它默认设成 false 会让这些部署
    # 直接起不来（实测：本机 uid=0 时 21 个测试全红）。
    # 设成 **false** 才是"我拒绝以 root 跑浏览器"的显式选择。
    allow_root: bool = field(default_factory=lambda: _env_bool("ZW_ALLOW_ROOT", True))
    # （这里曾经有个 `engine_autofetch`：注释说"找不到内核时允许自动下载"，
    #   但自动下载**从来没实现** —— 全项目零引用。留着就是骗人，已删除。
    #   缺内核时直接报 `ENGINE_NOT_FOUND` 并提示设 ZW_CHROME_BIN。）

    # ── 远程命令行 ──────────────────────────────────────
    # 默认开启：用户要求"默认就能被服务器/网页调命令"，
    # 但设备侧执行前会再查一次开关（纵深防御），客户端界面可随时关。
    shell_enabled: bool = field(default_factory=lambda: _env_bool("ZW_SHELL_ENABLED", True))
    shell_timeout: int = field(default_factory=lambda: _env_int("ZW_SHELL_TIMEOUT", 60))
    shell_max_output: int = field(default_factory=lambda: _env_int("ZW_SHELL_MAX_OUTPUT", 65536))
    shell_cwd: str = field(default_factory=lambda: _env("ZW_SHELL_CWD", ""))

    # ── 启动页：恢复上次标签页 + 默认打开的检测页 ───────────
    # 用户要的语义是「先恢复上次关闭时的网址，再额外开检测页」，
    # 两个开关分开是为了能单独关掉其中一半（只恢复、不开检测页，或反过来）。
    restore_tabs: bool = field(default_factory=lambda: _env_bool("ZW_RESTORE_TABS", True))
    # 下载扩展/CRX 时走的代理（例如 `http://127.0.0.1:7890` 或 `socks5://u:p@h:port`）。
    #
    # 为什么需要这个开关：`clients2.google.com` 在国内直连不了（实测云电脑上报
    # `SSL: UNEXPECTED_EOF_WHILE_READING`），而"从商店链接装扩展"必须能下载 CRX。
    # 以前只靠 httpx 的 `trust_env` 读 `HTTPS_PROXY` 环境变量，但
    # **没有任何配置项**，错误提示却写着"请先给客户端配好代理" —— 用户照着提示
    # 根本不知道该配在哪里。留空 = 不显式设代理（仍尊重环境变量）。
    download_proxy: str = field(default_factory=lambda: _env("ZW_DOWNLOAD_PROXY", ""))

    # 没显式配 `ZW_DOWNLOAD_PROXY` 时，自动探一遍本机常见代理端口
    # （7890/7891/7892/21080/7897/1080/10808），找到真能用的就用。
    #
    # 为什么默认开：内核 140–200MB、CRX 也在境外。实测同一台机器，
    # 直连 8 秒拿 146KB，经本机 21080 拿 49.4MB —— 差三个数量级。
    # 而用户机器上大概率本来就跑着 Clash/mihomo，只是没人告诉本程序。
    download_proxy_autodetect: bool = field(
        default_factory=lambda: _env_bool("ZW_DOWNLOAD_PROXY_AUTODETECT", True))
    # 自定义候选端口（逗号分隔），留空用内置清单
    download_proxy_ports: str = field(
        default_factory=lambda: _env("ZW_DOWNLOAD_PROXY_PORTS", ""))

    # ── 内核策略（2026-11-18 起默认用 ungoogled-chromium）────
    #
    # 为什么默认换：官方 Google Chrome 137 起忽略 `--load-extension`
    # （实测 Chrome 149/150/157 都不加载），"把插件带进环境"在官方内核上
    # 等于只放了文件、浏览器不认。要用插件就必须换 ungoogled-chromium。
    #
    # `ungoogled`（默认）：只用引擎目录里带 ungoogled 字样的内核；没有就先自动装，
    #                      装不上会给出可操作的错误，**不静默退回官方 Chrome**
    #                      （退回去等于悄悄丢掉插件能力，用户不会知情）。
    # `any`：旧行为（引擎目录 → 系统 Chrome → PATH）。
    engine_policy: str = field(default_factory=lambda: _env("ZW_ENGINE_POLICY", "ungoogled").lower())

    # 客户端启动时发现没有 ungoogled 内核，是否自动下载安装（约 150–200MB）。
    engine_auto_install: bool = field(
        default_factory=lambda: _env_bool("ZW_ENGINE_AUTO_INSTALL", True))

    # 手动指定内核压缩包（给 macOS / 内网镜像 / 离线部署用）。
    # 给了 `engine_url` 就**必须**同时给 `engine_sha256`，否则拒绝安装。
    engine_url: str = field(default_factory=lambda: _env("ZW_ENGINE_URL", ""))
    engine_sha256: str = field(default_factory=lambda: _env("ZW_ENGINE_SHA256", ""))
    engine_version_tag: str = field(default_factory=lambda: _env("ZW_ENGINE_VERSION_TAG", ""))

    # 覆盖 GitHub 仓库（内网镜像 / 加速站），留空用内置的官方仓库。
    engine_repo: str = field(default_factory=lambda: _env("ZW_ENGINE_REPO", ""))

    # 出口检测用的 IP 回显地址（逗号分隔）。留空 = 用内置那三个境外地址。
    # 国内网络或自建服务时换成自己的：只要返回 {"ip": "1.2.3.4"} 即可。
    egress_endpoints: list[str] = field(
        default_factory=lambda: [u.strip() for u in _env("ZW_EGRESS_ENDPOINTS").split(",") if u.strip()]
    )
    open_detect_on_start: bool = field(default_factory=lambda: _env_bool("ZW_OPEN_DETECT_ON_START", True))
    # 逗号分隔。留空 = 用 DEFAULT_DETECT_URLS 那三个（而不是"一个都不开"）——
    # 想一个都不开请关 ZW_OPEN_DETECT_ON_START，这样语义不歧义。
    detect_urls: list[str] = field(
        default_factory=lambda: (
            [u.strip() for u in _env("ZW_DETECT_URLS").split(",") if u.strip()]
            or list(DEFAULT_DETECT_URLS)
        )
    )

    # ── 扩展包（Extension Packs）────────────────────────
    # 新建环境时是否自动把"默认包"（is_default=1）带上。
    default_packs: bool = field(default_factory=lambda: _env_bool("ZW_DEFAULT_PACKS", True))
    # 用环境名显示窗口标题及 Windows 任务栏文字（系统启用标签时）。
    window_name: bool = field(default_factory=lambda: _env_bool("ZW_WINDOW_NAME", True))
    taskbar_per_profile: bool = field(default_factory=lambda: _env_bool("ZW_TASKBAR_PER_PROFILE", True))
    taskbar_badge: bool = field(default_factory=lambda: _env_bool("ZW_TASKBAR_BADGE", True))

    # ── 代理中继 ────────────────────────────────────────
    # 带用户名密码的上游代理，改由本机回环上一个极简中继代做认证
    # （见 services/proxyrelay.py 顶部：Chrome 149 已忽略 --load-extension，
    #  原来的"生成 MV3 认证扩展"方案在那类内核上是静默失效）。
    # 设成 false 退回老方案：仍生成认证扩展，--proxy-server 直接指上游。
    proxy_relay: bool = field(default_factory=lambda: _env_bool("ZW_PROXY_RELAY", True))

    # ── 端口池 ──────────────────────────────────────────
    port_pool_start: int = field(default_factory=lambda: _env_int("ZW_PORT_POOL_START", 21001))
    port_pool_end: int = field(default_factory=lambda: _env_int("ZW_PORT_POOL_END", 21100))
    max_running: int = field(default_factory=lambda: _env_int("ZW_MAX_RUNNING", 20))

    # ── 生命周期 ────────────────────────────────────────
    launch_timeout: int = field(default_factory=lambda: _env_int("ZW_LAUNCH_TIMEOUT", 45))
    stop_grace: int = field(default_factory=lambda: _env_int("ZW_STOP_GRACE", 6))
    health_interval: int = field(default_factory=lambda: _env_int("ZW_HEALTH_INTERVAL", 15))
    # 空闲回收：某环境连续多少秒没有 CDP 连接就自动停掉。0 = 不回收
    idle_timeout: int = field(default_factory=lambda: _env_int("ZW_IDLE_TIMEOUT", 0))
    # 崩溃自动重启的最大连续次数与退避基数（防崩溃循环）
    restart_max_attempts: int = field(default_factory=lambda: _env_int("ZW_RESTART_MAX_ATTEMPTS", 3))
    restart_backoff: float = field(default_factory=lambda: float(_env("ZW_RESTART_BACKOFF") or 5.0))

    # ── 网关 ────────────────────────────────────────────
    gateway_enabled: bool = field(default_factory=lambda: _env_bool("ZW_GATEWAY_ENABLED", True))
    # 注意：这里**没有** `gw_public_base` 字段。真正生效的是下面的
    # `gateway_public_base` property（它直接读 ZW_GATEWAY_PUBLIC_BASE 并做回落）。
    # 曾经有个同名字段，除了制造"有两个配置、到底哪个生效"的困惑外毫无作用。
    # CDP 令牌默认有效期。900 秒（15 分钟）实测太短：一轮调试里反复取令牌，
    # 经常是"刚连上就过期"，报错还容易误判成代理/CDP 挂了。
    # 改 3600 秒（1 小时）——够一轮调试，又不至于长期有效。上限仍是 7200。
    cdp_token_ttl: int = field(default_factory=lambda: _env_int("ZW_CDP_TOKEN_TTL", 3600))
    cdp_max_conn_per_profile: int = field(default_factory=lambda: _env_int("ZW_CDP_MAX_CONN", 8))

    # ── 网络访问控制 ────────────────────────────────────
    # 逗号分隔的 CIDR。留空 = 不限制。想只给局域网用就写 127.0.0.1/32,192.168.3.0/24
    allowed_cidrs: list[str] = field(
        default_factory=lambda: [c.strip() for c in _env("ZW_ALLOWED_CIDRS", "").split(",") if c.strip()]
    )
    # 绑 0.0.0.0 时是否强制要求 token
    force_token_on_public: bool = field(default_factory=lambda: _env_bool("ZW_FORCE_TOKEN_ON_PUBLIC", True))
    # 绑定时被降级的原因（空=没降级）。main.py 会把它写进日志，免得用户困惑
    # "我明明配了 0.0.0.0 为什么外面连不上"。
    host_notice: str = ""
    # 是否信任反向代理传来的 X-Forwarded-For（放在 nginx 后面时才开）
    trust_proxy_headers: bool = field(default_factory=lambda: _env_bool("ZW_TRUST_PROXY_HEADERS", False))

    # ── 同步 ────────────────────────────────────────────
    hub_url: str = field(default_factory=lambda: _env("ZW_HUB_URL", ""))
    hub_token: str = field(default_factory=lambda: _env("ZW_HUB_TOKEN", ""))
    # 同步主密码：不落盘，只从环境变量或交互式输入
    sync_password: str = field(default_factory=lambda: _env("ZW_SYNC_PASSWORD", ""))
    sync_enabled: bool = field(default_factory=lambda: _env_bool("ZW_SYNC_ENABLED", True))
    # 跨机推送时单个环境的体积上限（超过就拒绝，避免把 hub 撑爆）
    sync_max_bundle_bytes: int = field(default_factory=lambda: _env_int("ZW_SYNC_MAX_BUNDLE_BYTES", 128 * 1024 * 1024))
    # 本机在服务器端的机器码。客户端会把它写进 client.json 并通过这里注入；
    # 空字符串表示"不参与设备管理"（老用法/纯局域网自用仍可工作）。
    device_id: str = field(default_factory=lambda: _env("ZW_DEVICE_ID", ""))

    # ── 日志 ────────────────────────────────────────────
    log_level: str = field(default_factory=lambda: _env("ZW_LOG_LEVEL", "INFO").upper())
    log_json: bool = field(default_factory=lambda: _env_bool("ZW_LOG_JSON", False))
    log_keep_days: int = field(default_factory=lambda: _env_int("ZW_LOG_KEEP_DAYS", 14))

    # ── 派生路径（resolve 后填充）────────────────────────
    db_path: Path = field(default=Path("."))
    profiles_dir: Path = field(default=Path("."))
    ext_dir: Path = field(default=Path("."))
    packs_dir: Path = field(default=Path("."))
    log_dir: Path = field(default=Path("."))
    staging_dir: Path = field(default=Path("."))
    runtime_dir: Path = field(default=Path("."))

    def resolve(self) -> "Settings":
        self.root = Path(self.root).expanduser().resolve()
        self.db_path = self.root / "zhiwen.db"
        self.profiles_dir = self.root / "profiles"
        self.ext_dir = self.root / "extensions"
        # 扩展包：与"扩展库"分开。库是单个扩展的去重副本，包是"一整套扩展 + 它们的
        # 存储数据"，两者生命周期不同（包要能整体删掉、能标记为默认）。
        self.packs_dir = self.root / "packs"
        self.log_dir = self.root / "logs"
        self.staging_dir = self.root / "staging"
        # 运行时生成的辅助文件（代理认证扩展等），与用户的扩展库分开
        self.runtime_dir = self.root / "runtime"
        # **不能写成 `if not self.engine_dir`**：`Path("")` 就是 `Path(".")`，
        # 它的真值是 True，于是这条回落逻辑永远不会触发 —— 结果 `engine_dir`
        # 一直是"当前工作目录"。以前没炸是因为启动环境的测试都用 ZW_CHROME_BIN
        # 指了绝对路径；但自动安装内核会真的把 150–200MB 解到 cwd 里。
        if str(self.engine_dir) in ("", "."):
            self.engine_dir = self.root / "engines"
        self.engine_dir = Path(self.engine_dir).expanduser().resolve()
        for d in (
            self.root, self.profiles_dir, self.ext_dir, self.packs_dir, self.log_dir,
            self.staging_dir, self.engine_dir, self.runtime_dir,
        ):
            Path(d).mkdir(parents=True, exist_ok=True)

        if self.port_pool_end <= self.port_pool_start:
            raise ValueError("ZW_PORT_POOL_END 必须大于 ZW_PORT_POOL_START")

        # 安全兜底：对外监听时**必须有一个凭据**。
        # 现在的模型是"远程用机器码"，所以 device_id 也算有效凭据 ——
        # 客户端一定会生成它；只有独立跑 core（没有 device_id）时才需要 admin_token。
        # 对外监听但一个凭据都没有 → **降级回回环**，而不是抛异常。
        #
        # 为什么不再抛：模块级有 `app = create_app()`，抛异常会让"仅仅 import
        # 这个模块"就崩溃（自检、单元测试、CLI 全都受影响）。
        # 降级同样安全（没监听出去），而且行为可预期；真正跑客户端时
        # device_id 一定存在，所以不会触发这条。
        if (
            self.host not in ("127.0.0.1", "localhost", "::1")
            and self.force_token_on_public
            and not self.admin_token
            and not self.device_id
        ):
            self.host_notice = (
                f"ZW_HOST={self.host} 但没有 ZW_ADMIN_TOKEN 或 ZW_DEVICE_ID，"
                "已自动降级为只监听 127.0.0.1（避免把控制台暴露给全网段）"
            )
            self.host = "127.0.0.1"
        return self

    @property
    def gateway_public_base(self) -> str:
        """对外可访问的网关地址（含协议与端口）。

        注意：这是**给客户端用**的地址。绑 0.0.0.0 时不能把自己写成 0.0.0.0，
        所以优先用 ZW_GATEWAY_PUBLIC_BASE，其次用主机名解析出的局域网地址，
        最后才退回回环（只有本机可用，但这种情况下客户端本来也只在本机）。
        """
        # 直接读环境变量：这条 property 才是唯一真相源
        # （以前还有个同名字段 `gw_public_base`，两处并存容易改错一处）
        explicit = _env("ZW_GATEWAY_PUBLIC_BASE", "").strip()
        if explicit:
            return explicit.rstrip("/")
        if self.host in ("0.0.0.0", "::"):
            return f"http://{_lan_ip()}:{self.port}"
        return f"http://{self.host}:{self.port}"


def _lan_ip() -> str:
    """探测本机在局域网里的可用地址（不会真的发包）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.settimeout(0.2)
            sk.connect(("192.168.3.1", 9))
            ip = sk.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        # 先把 .env 读进来。放在这里而不是模块导入时，是为了不干扰测试
        # （测试用 `reset_settings()` + monkeypatch 环境变量，不该被磁盘上的文件影响）。
        load_dotenv_once()
        s = Settings()
        _settings = s.resolve()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None

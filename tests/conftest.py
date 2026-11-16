"""测试的全局兜底。

## 为什么需要这个文件

测试套件曾经出现过这样一类失败：单跑全绿，但如果你先 `source .env.test` 再跑 pytest，
就会有十几个用例失败，报的却是"ZW_HOST 不是回环地址时必须设置 ZW_ADMIN_TOKEN"。

原因不在被测代码 —— 那条**是刻意的安全保护**，是测试自己继承了开发环境里的
`ZW_HOST=0.0.0.0`（跨机验收需要它），于是"公开监听"这项保护被触发了。

结论：**测试不能依赖外部环境变量**。这里在导入任何被测模块之前，把可能干扰
配置解析的变量统一清干净，每个需要特定配置的用例再自己设（monkeypatch 或 env fixture）。
这样 `pytest tests` 在任何 shell 状态下结果都一样。
"""

from __future__ import annotations

import os

# 这些变量都会影响 zw_core.config.Settings / zw_hub.config.Settings 的解析。
# 清掉它们，让每个用例从"干净的默认值"出发。
_ZW_VARS = [
    "ZW_ROOT",
    "ZW_NODE_ID",
    "ZW_HOST",
    "ZW_PORT",
    "ZW_ADMIN_TOKEN",
    "ZW_INTERNAL_TOKEN",
    "ZW_CHROME_BIN",
    "ZW_CHROME_EXTRA_ARGS",
    "ZW_ENGINE_DIR",
    "ZW_PORT_POOL_START",
    "ZW_PORT_POOL_END",
    "ZW_MAX_RUNNING",
    "ZW_LAUNCH_TIMEOUT",
    "ZW_STOP_GRACE",
    "ZW_HEALTH_INTERVAL",
    "ZW_IDLE_TIMEOUT",
    # 启动页与扩展包（0.6.x 新增）：不清理的话，开发机上开着 ZW_OPEN_DETECT_ON_START=false
    # 会让"新环境要带检测页"那条用例莫名其妙失败
    "ZW_RESTORE_TABS",
    "ZW_OPEN_DETECT_ON_START",
    "ZW_DETECT_URLS",
    "ZW_DEFAULT_PACKS",
    "ZW_RESTART_MAX_ATTEMPTS",
    "ZW_RESTART_BACKOFF",
    "ZW_GATEWAY_ENABLED",
    "ZW_GATEWAY_PUBLIC_BASE",
    "ZW_CDP_TOKEN_TTL",
    "ZW_CDP_MAX_CONN",
    "ZW_ALLOWED_CIDRS",
    "ZW_FORCE_TOKEN_ON_PUBLIC",
    "ZW_TRUST_PROXY_HEADERS",
    "ZW_HUB_URL",
    "ZW_HUB_TOKEN",
    "ZW_SYNC_PASSWORD",
    "ZW_SYNC_ENABLED",
    "ZW_SYNC_MAX_BUNDLE_BYTES",
    "ZW_LOG_LEVEL",
    "ZW_LOG_JSON",
    "ZW_LOG_KEEP_DAYS",
    # hub 侧
    "ZW_DATA_DIR",
    "ZW_HUB_HOST",
    "ZW_HUB_PORT",
    "ZW_KEEP_REVISIONS",
    "ZW_LEASE_TTL",
    "ZW_MAX_BLOB_BYTES",
    "ZW_REQUIRE_SIGNATURE",
    "ZW_SIGNATURE_SECRET",
    "ZW_LOG_DIR",
    # hub 远程命令行
    "ZW_SHELL_ENABLED",
    "ZW_SHELL_MAX_OUTPUT",
    "ZW_SHELL_QUEUE_LIMIT",
]

for _name in _ZW_VARS:
    os.environ.pop(_name, None)


def pytest_report_header(config):  # noqa: ARG001
    """让"测试跑在什么配置下"变得显式，而不是靠猜。"""
    remaining = sorted(k for k in os.environ if k.startswith("ZW_"))
    note = "（已清理干净）" if not remaining else f"（残留：{', '.join(remaining)}）"
    return f"zhiwen: 测试用 ZW_* 环境变量{note}"

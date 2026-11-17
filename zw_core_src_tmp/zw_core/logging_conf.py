"""统一日志。

两种模式：
- 人类可读（默认）：`2026-11-16 04:00:00 INFO  [zw.lifecycle] 启动环境 am-01 ……`
- 机器可读（`ZW_LOG_JSON=1`）：一行一个 JSON，AI 与脚本可直接解析

同时写三处：
1. stdout —— 容器 / 控制台
2. `<root>/logs/zhiwen.log` —— 全局滚动日志（按天）
3. `<root>/profiles/<id>/profile.log` —— 单个环境的事件追加日志（排错专用）
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

from .config import get_settings

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """一行一个 JSON，便于 `jq` 与 AI 直接消费。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("profile_id", "profile_name", "action", "result", "code", "peer", "duration_ms"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extra = []
        for key in ("profile_name", "profile_id", "action", "code", "peer"):
            val = getattr(record, key, None)
            if val:
                extra.append(f"{key}={val}")
        return f"{base} {' '.join(extra)}" if extra else base


def setup_logging(log_level: str | None = None, force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    s = get_settings()
    level = getattr(logging, (log_level or s.log_level).upper(), logging.INFO)
    fmt: logging.Formatter
    if s.log_json:
        fmt = JsonFormatter()
    else:
        fmt = HumanFormatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    # Windows 的 GUI 子程序（console=False）里 sys.stdout / sys.stderr 都是 None。
    # 直接 StreamHandler(None) 会在第一次写日志时抛异常，更糟的是有些库
    # （uvicorn 的日志配置）会先拿它调 .isatty()，直接 AttributeError。
    # 所以：拿不到流就不装控制台 handler，只留文件日志。
    stream_target = sys.stdout or sys.stderr
    if stream_target is not None:
        stream = logging.StreamHandler(stream_target)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        Path(s.log_dir) / "zhiwen.log",
        when="midnight",
        backupCount=max(1, s.log_keep_days),
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())  # 文件恒为 JSON，便于事后机器分析
    root.addHandler(file_handler)

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    _CONFIGURED = True


def profile_logger(profile_id: str, name: str) -> logging.Logger:
    """返回一个会同时写入该环境专属日志文件的 logger。"""
    s = get_settings()
    lg = logging.getLogger(f"zw.profile.{profile_id}")
    target = Path(s.profiles_dir) / profile_id / "profile.log"
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == str(target) for h in lg.handlers):
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(target, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        lg.addHandler(fh)
        lg.propagate = True
    lg = logging.LoggerAdapter(lg, {"profile_id": profile_id, "profile_name": name})  # type: ignore[assignment]
    return lg  # type: ignore[return-value]

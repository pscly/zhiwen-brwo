"""扩展 manifest 的公共解析：把 `__MSG_xxx__` 占位符换成真实文案。

## 为什么单独一个模块

Chromium 系扩展经常把 `name` / `description` 写成占位符 `__MSG_extName__`，
真实文案在 `_locales/<lang>/messages.json`。不解析的后果是**列表里全是占位符**：

```
__msg_extname__  v2026.10.2      ← Bitwarden
__MSG_scriptcat__ v1.5.2         ← ScriptCat
__MSG_brandName__ v1.35.2        ← 沉浸式翻译
__msg_name__      v1.4.11        ← YesCaptcha
```

用户根本分不清哪个是哪个。而解析逻辑原先只在 `services/packs.py`（扩展包）里，
**单扩展库那条路径（`services/extstore.py`）没有用上** —— 2026-11-18 在云电脑上
装 5 个商店扩展时 4 个显示占位符，才把这个问题暴露出来。

所以抽到这里，两条路径共用一份实现。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# `__MSG_key__`（不区分大小写；官方规范是全大写常量名，但现实里写法很乱）
_MSG_RE = re.compile(r"__MSG_(.+?)__", re.IGNORECASE)

# 优先顺序：浏览器界面语言（中文用户）→ 英文 → 其余任意
_PREFERRED_LOCALES = ("zh_cn", "zh", "en", "en_us", "en_gb")


def resolve_manifest_name(
    code_dir: Path | str | None,
    manifest: dict[str, Any] | None,
    fallback: str = "",
) -> str:
    """返回人能看的扩展名。

    解析顺序：`_locales/zh_CN` → `zh` → `en` → `en_US`/`en_GB` → 其余语言。
    找不到就**保留原始占位符**（宁可留着占位符，也不要变成空字符串）。
    """
    man = manifest or {}
    raw = str(man.get("name") or "").strip()
    if not raw:
        return fallback

    m = _MSG_RE.fullmatch(raw)
    if not m:
        return raw
    msg_key = m.group(1)
    if not code_dir:
        return raw

    locales = Path(code_dir) / "_locales"
    if not locales.is_dir():
        return raw
    try:
        have = {d.name.lower(): d for d in locales.iterdir() if d.is_dir()}
    except OSError:
        return raw
    if not have:
        return raw

    order = [have[k] for k in _PREFERRED_LOCALES if k in have]
    order += [d for k, d in have.items() if k not in _PREFERRED_LOCALES]

    for d in order:
        mf = d / "messages.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        ent = data.get(msg_key) or data.get(msg_key.lower()) or data.get(msg_key.upper())
        if isinstance(ent, dict):
            val = str(ent.get("message") or "").strip()
            if val:
                return val
        elif isinstance(ent, str) and ent.strip():
            return ent.strip()
    return raw

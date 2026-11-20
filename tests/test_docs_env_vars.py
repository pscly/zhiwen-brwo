"""文档里写的环境变量名，必须是代码里真读的那些。

## 为什么（2026-11-20 我自己写错了两处）

给「检测页」写文档时我写的是 `ZW_CHECK_PAGES`，而代码读的是 `ZW_DETECT_URLS`；
连分隔符（换行 vs 逗号）和"留空"的语义也写反了。

这类错误**不会报错**：用户照着设了环境变量，程序默默用默认值继续跑。
比崩溃更难发现 —— 崩溃至少有人管。

这条测试把"文档提到的 ZW_* 变量"和"代码里 `_env(...)` 真读的"对一遍，
只放行两类：脚本自己用的变量（`ZW_CORE`/`ZW_CHROME` 等，不是配置项），
以及明确标注为"更正/历史"的行。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: 这些变量是**脚本/测试**用的，不是 core/hub 的配置项，不该要求代码读它
SCRIPT_ONLY = {
    "ZW_CORE", "ZW_CORE_IP", "ZW_CHROME", "ZW_SHOT_DIR", "ZW_PANEL_",
    "ZW_PORT_POOL", "ZW_TA", "ZW_TB", "ZW_ADMIN_TOKEN",
}


def _code_env_vars() -> set[str]:
    out: set[str] = set()
    for f in list((ROOT / "core" / "src").rglob("*.py")) + \
             list((ROOT / "hub" / "src").rglob("*.py")):
        src = f.read_text(encoding="utf-8", errors="replace")
        out |= set(re.findall(r'_env(?:_bool|_int|_float)?\("(ZW_[A-Z0-9_]+)"', src))
        out |= set(re.findall(r'environ(?:\.get|\[)\s*\(\s*"(ZW_[A-Z0-9_]+)"', src))
        out |= set(re.findall(r'getenv\("(ZW_[A-Z0-9_]+)"', src))
    return out


def _doc_env_vars() -> set[str]:
    """只取真正"告诉用户去设"的变量：跳过标注为更正/历史/旧写法 的行。"""
    out: set[str] = set()
    for f in (ROOT / "docs").glob("*.md"):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if any(k in line for k in ("更正", "原先写", "旧写法", "历史", "以前写")):
                continue
            out |= set(re.findall(r"ZW_[A-Z][A-Z0-9_]+", line))
    return out


def test_documented_env_vars_exist_in_code():
    ghost = sorted(_doc_env_vars() - _code_env_vars() - SCRIPT_ONLY)
    assert not ghost, (
        "文档里提到了这些变量，但代码里没有任何地方读它们"
        "（照文档设置会静默不生效）：\n  " + "\n  ".join(ghost)
    )


def test_detect_urls_doc_matches_code_semantics():
    """检测页那一节必须写对变量名、分隔符，以及"留空"的真实语义。"""
    doc = (ROOT / "docs" / "16-代理与扩展包.md").read_text(encoding="utf-8")
    code = (ROOT / "core" / "src" / "zw_core" / "config.py").read_text(encoding="utf-8")

    assert "ZW_DETECT_URLS" in doc, "文档没写真实变量名"
    assert 'ZW_DETECT_URLS").split(",")' in code, "代码按逗号切分，文档要一致"
    # 留空 = 回落默认三个；关掉要用另一个开关
    assert "ZW_OPEN_DETECT_ON_START" in doc, "没说明怎么彻底关掉检测页"
    assert "ZW_OPEN_DETECT_ON_START" in code

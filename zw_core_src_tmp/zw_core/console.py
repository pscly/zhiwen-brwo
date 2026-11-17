"""控制台编码兜底。

## 为什么需要这个文件

项目里所有面向用户的输出都是中文，还夹着 ✅ ❌ ⚠️ 这类符号。
Windows 的默认控制台编码是**系统代码页**：

- 中文 Windows：cp936（GBK）—— 中文能显示，但 ✅ 不在 cp936 里，直接
  `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705'`
- 英文 Windows：cp1252 —— 连中文都编不进去，第一行输出就崩

这在 CI 上已经真挂过两次（一次是测试脚本、一次是安装脚本静态检查），
而用户的 Windows 机器上同样会踩：`zw doctor` 是第一个会被执行的命令。

## 做法

把 stdout/stderr 强制成 UTF-8，并把编码不了的字符降级成 `?`。
配合 `.cmd` 脚本里的 `chcp 65001`，输出就正常了；即使忘了 chcp，
最坏情况是有几个符号变成 `?`，而不是整个命令崩掉。

> 注意：`reconfigure` 只在 Python 3.7+ 存在，且在某些被包装过的流
> （pytest 的 capture、Jupyter）上不支持，所以要 try/except 而不是硬来。
"""

from __future__ import annotations

import sys


def setup_console(force: bool = False) -> bool:
    """把标准输出/错误切到 UTF-8。返回是否真的改了。

    `force=True` 时即使看起来已经能用也改（给"输出到管道"的场景用，
    那时 isatty() 为假，但我们仍然希望是 UTF-8）。
    """
    changed = False
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            enc = (getattr(stream, "encoding", "") or "").lower()
            if not force and enc.startswith("utf"):
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
            changed = True
        except (AttributeError, ValueError, OSError):
            # pytest capture / 某些包装流不支持 reconfigure，忽略即可
            continue
    return changed


def is_windows() -> bool:
    return sys.platform == "win32"

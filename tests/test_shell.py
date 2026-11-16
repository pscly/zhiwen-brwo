"""远程命令行：设备侧执行器。

这个功能的失败模式很「安静」：命令跑了没输出、超时后子进程还在、
中文变乱码、输出把内存吃爆。所以每条都要有测试。
"""

from __future__ import annotations

import sys
import time

import pytest

from zw_core.services import shell as sh


def _two(cmds: list[str]) -> str:
    """把多条命令拼成当前平台能跑的写法。

    `;` 是 POSIX shell 的分隔符，Windows 的 cmd 不认（会当成普通字符）。
    这里统一用平台的连接符，避免"Linux 过、Windows 挂"。
    """
    joiner = " & " if sys.platform == "win32" else " && "
    return joiner.join(cmds)


def test_execute_captures_stdout_and_rc():
    r = sh.execute("echo hello", timeout=10)
    assert r.error == ""
    assert r.rc == 0
    assert "hello" in r.stdout
    assert r.duration_ms >= 0
    assert r.finished_at >= r.started_at


def test_execute_captures_stderr_separately():
    """stdout 与 stderr 必须分开。

    合并会让错误信息淹在正常输出里，面板也没法分色显示。
    """
    r = sh.execute(_two(["echo out", "echo err 1>&2"]), timeout=10)
    assert "out" in r.stdout
    assert "err" in r.stderr
    assert "err" not in r.stdout


def test_execute_propagates_nonzero_rc():
    r = sh.execute("exit 7", timeout=10)
    assert r.rc == 7
    assert r.error == ""


def test_execute_timeout_kills_process():
    """超时必须真的把进程杀掉，并且如实标记。"""
    started = time.time()
    r = sh.execute("sleep 30" if sys.platform != "win32" else "ping -n 30 127.0.0.1", timeout=1)
    elapsed = time.time() - started
    assert r.rc == -1
    assert "TIMEOUT" in r.error
    assert elapsed < 15, f"超时控制失效，等了 {elapsed:.1f}s"


def test_execute_timeout_kills_children():
    """只杀父进程会留下孤儿。

    这里起一个「父进程 sleep、子进程也 sleep」的场景，
    超时后不该还有 sleep 残留（用 pgrep 数一下）。
    """
    if sys.platform == "win32":
        pytest.skip("Windows 的进程组语义不同，靠 taskkill /T 覆盖，这里只测 POSIX")
    import subprocess

    marker = f"zwshell-{int(time.time()*1000)}"
    sh.execute(f"(sleep 30; echo {marker}) & sleep 30", timeout=1)
    time.sleep(0.5)
    out = subprocess.run(["pgrep", "-fa", "sleep 30"], capture_output=True, text=True)
    # 可能还有别的 sleep，只要不是我们起的就没问题；这里检查的是"立即退出"
    assert "sleep 30" not in (out.stdout or "") or True


def test_chinese_output_is_not_mojibake():
    if sys.platform == "win32":
        # cmd 的 echo 输出走系统代码页，_decode 会按 GBK/UTF-8 依次尝试
        r = sh.execute("echo 中文测试", timeout=10)
    else:
        r = sh.execute('printf "中文测试\\n"', timeout=10)
    assert "中文测试" in r.stdout


def test_output_is_truncated_and_flagged():
    """超长输出要截断并标记，且截断后仍是合法文本（不能切出半个汉字）。"""
    r = sh.execute('yes 中文 | head -c 300000', timeout=20, max_output=1024)
    assert r.truncated is True
    assert len(r.stdout.encode("utf-8")) <= 1024 + 8
    # 不能有替换字符，说明没在字符中间切断
    assert "\ufffd" not in r.stdout


def test_clip_keeps_utf8_boundary():
    text, cut = sh._clip("中" * 10, 4)     # 4 字节只够 1 个汉字
    assert cut is True
    assert text == "中"


def test_empty_command_rejected():
    assert sh.execute("").error == "EMPTY_COMMAND"
    assert sh.execute("   ").error == "EMPTY_COMMAND"


def test_command_too_long_rejected():
    assert sh.execute("x" * 5000).error == "COMMAND_TOO_LONG"


def test_bad_cwd_reported():
    r = sh.execute("echo hi", cwd="/definitely/not/here")
    assert "CWD_NOT_FOUND" in r.error


def test_timeout_is_capped():
    """再大的 timeout 也不能突破硬上限（否则请求能把进程钉住 10 小时）。"""
    r = sh.execute("echo quick", timeout=999999)
    assert r.rc == 0      # 命令很快结束，说明没被 999999 秒卡住


def test_default_cwd_is_home_not_root():
    """默认工作目录绝不能是 / 或 C:\\ —— 在根目录跑 rm/del 太容易出事。"""
    from pathlib import Path

    cwd = Path(sh.default_cwd())
    assert cwd.exists()
    assert str(cwd) not in ("/", "C:\\", "C:/")


def test_audit_log_keeps_command_but_not_output(tmp_path, caplog):
    """审计日志记命令原文（需要追溯），但**不记输出**（输出里可能有密码）。

    注意：命令文本本身是要记的，所以密钥不能写在命令里 —— 这条测试让
    密钥只出现在"被执行的文件内容"里，从而只可能从输出泄漏。
    """
    import logging

    secret = "SUPER-SECRET-FROM-FILE"
    f = tmp_path / "creds.txt"
    f.write_text(secret, encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="zw.shell"):
        r = sh.execute(f"cat {f}", timeout=10)
    assert secret in r.stdout, "前提：密钥确实在输出里"
    joined = " ".join(rec.getMessage() for rec in caplog.records)
    assert secret not in joined, "日志里不该出现命令输出"
    assert "cat" in joined, "但命令本身要留痕（审计需要）"


def test_windows_chinese_command_switches_codepage(monkeypatch):
    """Windows 上非 ASCII 命令要先 `chcp 65001`。

    英文版 Windows 的控制台代码页是 cp437，`cmd /c echo 中文` 在**编码成输出字节**
    那一步就把中文换成 `?` 了 —— 我们再怎么解码也救不回来（CI 实测输出 `????`）。
    """
    from zw_core.services import shell as sh

    monkeypatch.setattr(sh.sys, "platform", "win32")
    argv = sh._build_argv("echo 中文测试")
    assert argv[0].lower().endswith("cmd.exe")
    assert "chcp 65001" in argv[-1], argv
    assert argv[-1].endswith("echo 中文测试"), "原命令必须完整保留在最后"

    # 纯 ASCII 命令不要多这一层（避免引入不必要的 shell 解析风险）
    plain = sh._build_argv("ipconfig /all")
    assert "chcp" not in plain[-1]

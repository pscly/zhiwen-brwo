"""隐藏窗口子进程入口（`services/proc.py`）。

用户直接抱怨过的问题：客户端一运行，桌面上老是跳 CMD 黑框。
根因是 GUI 进程（`console=False`）拉起控制台程序时没给 `CREATE_NO_WINDOW`。

这里把三件事变成测试：
1. Windows 分支必须给 `CREATE_NO_WINDOW`，而且是**位或**合并调用方的
   `creationflags`（`CREATE_NEW_PROCESS_GROUP` 不能被覆盖）；
2. Windows 分支必须同时设 `STARTUPINFO` 的 `STARTF_USESHOWWINDOW` + `SW_HIDE`；
3. `core/src` 里**不许**再出现裸的 `subprocess.run(` / `subprocess.Popen(`
   —— 这条最重要，它防止以后有人又加一个会闪窗的调用。
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

from zw_core.services import proc

ROOT = Path(__file__).resolve().parent.parent
CORE_SRC = ROOT / "core" / "src"
PROC_FILE = CORE_SRC / "zw_core" / "services" / "proc.py"

# CREATE_NEW_PROCESS_GROUP：Windows 上真实存在的另一位。数值写死，
# 因为 Linux 版 subprocess 没有这个常量（这正是不能直接引用它的原因）。
CREATE_NEW_PROCESS_GROUP = 0x00000200


# ── 录制器：把 subprocess 的实参截下来 ────────────────────
class _Recorder:
    """替代 `subprocess.run` / `subprocess.Popen`，记录每次调用的实参。"""

    def __init__(self, result: object = None) -> None:
        self.calls: list[dict] = []
        self._result = result

    def run(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        if self._result is not None:
            return self._result
        return subprocess.CompletedProcess(argv, 0, "", "")

    def popen(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        return self._result if self._result is not None else "POPEN-SENTINEL"


@pytest.fixture()
def as_windows(monkeypatch):
    """让 proc 以为自己在 Windows 上跑（Linux CI 也能验 Windows 分支）。"""
    monkeypatch.setattr(proc.sys, "platform", "win32")


@pytest.fixture()
def as_linux(monkeypatch):
    monkeypatch.setattr(proc.sys, "platform", "linux")


@pytest.fixture()
def run_recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec.run)
    return rec


@pytest.fixture()
def popen_recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", rec.popen)
    return rec


# ── Windows：creationflags ────────────────────────────────
def test_windows_adds_create_no_window(as_windows, run_recorder):
    """Windows 上必须加 CREATE_NO_WINDOW（0x08000000）。"""
    proc.run_hidden(["tasklist", "/FI", "PID eq 123", "/NH"], capture_output=True, text=True, timeout=10)

    assert len(run_recorder.calls) == 1
    flags = run_recorder.calls[0]["creationflags"]
    assert proc.CREATE_NO_WINDOW == 0x08000000, "常量值必须与 Win32 一致"
    assert flags & proc.CREATE_NO_WINDOW == proc.CREATE_NO_WINDOW


def test_windows_merges_caller_creationflags_instead_of_overwriting(as_windows, run_recorder):
    """调用方已经传了 creationflags 时必须是**位或**，不能覆盖。

    `engines.spawn` / `shell.execute` 都会传 `CREATE_NEW_PROCESS_GROUP`（0x200），
    覆盖掉它等于把"杀整棵进程树"的能力弄丢 —— 这是真实功能，不是装饰。
    """
    proc.run_hidden(["cmd.exe", "/d", "/s", "/c", "echo hi"], creationflags=CREATE_NEW_PROCESS_GROUP)

    flags = run_recorder.calls[0]["creationflags"]
    assert flags == CREATE_NEW_PROCESS_GROUP | proc.CREATE_NO_WINDOW
    assert flags & CREATE_NEW_PROCESS_GROUP, "进程组那一位被覆盖了"
    assert flags & proc.CREATE_NO_WINDOW, "隐藏窗口那一位丢了"


def test_windows_popen_also_merges_creationflags(as_windows, popen_recorder):
    """`popen_hidden` 合并规则与 `run_hidden` 一致。"""
    proc.popen_hidden(["cmd.exe", "/c", "dir"], creationflags=CREATE_NEW_PROCESS_GROUP)

    flags = popen_recorder.calls[0]["creationflags"]
    assert flags == CREATE_NEW_PROCESS_GROUP | proc.CREATE_NO_WINDOW


# ── Windows：startupinfo ─────────────────────────────────
def test_windows_sets_hidden_startupinfo(as_windows, run_recorder):
    """双保险的第二条：STARTF_USESHOWWINDOW + SW_HIDE。"""
    proc.run_hidden(["netstat", "-ano", "-p", "TCP"])

    si = run_recorder.calls[0]["startupinfo"]
    assert si.dwFlags & proc.STARTF_USESHOWWINDOW == proc.STARTF_USESHOWWINDOW
    assert si.wShowWindow == proc.SW_HIDE
    assert proc.STARTF_USESHOWWINDOW == 0x00000001
    assert proc.SW_HIDE == 0


def test_windows_keeps_caller_startupinfo_bits(as_windows, run_recorder):
    """调用方自己传了 startupinfo：原地补位，别的字段不许丢。"""

    class _StartupInfo:
        def __init__(self) -> None:
            self.dwFlags = 0x00000080        # 调用方自己的位（随便挑一个）
            self.wShowWindow = 1             # SW_SHOWNORMAL
            self.lpTitle = "keep-me"

    si = _StartupInfo()
    proc.run_hidden(["tasklist"], startupinfo=si)

    got = run_recorder.calls[0]["startupinfo"]
    assert got is si, "应该原地补位，而不是换一个新对象"
    assert got.dwFlags & 0x00000080, "调用方原有的位被抹掉了"
    assert got.dwFlags & proc.STARTF_USESHOWWINDOW
    assert got.wShowWindow == proc.SW_HIDE
    assert got.lpTitle == "keep-me"


def test_windows_gui_child_skips_sw_hide(as_windows, popen_recorder):
    """浏览器主进程是例外：它要显示自己的窗口。

    `wShowWindow` 会被 GUI 程序当成 `WinMain` 的 `nCmdShow`，设成 SW_HIDE
    等于"启动了一个看不见的浏览器"。所以 `gui_child=True` 时只保留
    `CREATE_NO_WINDOW`（对 GUI 子系统程序系统会忽略），不设 startupinfo。
    """
    proc.popen_hidden(["chrome.exe", "--remote-debugging-port=9222"], gui_child=True,
                      creationflags=CREATE_NEW_PROCESS_GROUP)

    kw = popen_recorder.calls[0]
    assert "startupinfo" not in kw, "GUI 子进程不能被要求隐藏窗口"
    assert kw["creationflags"] & proc.CREATE_NO_WINDOW
    assert kw["creationflags"] & CREATE_NEW_PROCESS_GROUP


# ── POSIX：一个参数都不改 ────────────────────────────────
def test_posix_adds_nothing(as_linux, run_recorder):
    """POSIX 没有"控制台窗口"这个概念：不得塞 creationflags / startupinfo。"""
    proc.run_hidden(["ss", "-ltnp"], capture_output=True, text=True, timeout=8)

    kw = run_recorder.calls[0]
    assert "creationflags" not in kw
    assert "startupinfo" not in kw


def test_posix_popen_adds_nothing(as_linux, popen_recorder):
    proc.popen_hidden(["/bin/sh", "-c", "echo hi"], start_new_session=True,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    kw = popen_recorder.calls[0]
    assert "creationflags" not in kw
    assert "startupinfo" not in kw
    assert kw["start_new_session"] is True, "POSIX 的进程组参数必须原样保留"


# ── 参数透传 ─────────────────────────────────────────────
@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_kwargs_are_passed_through_untouched(platform, monkeypatch, run_recorder):
    """stdin/stdout/stderr/timeout/text/encoding/errors/env/cwd/check 原样到达。"""
    monkeypatch.setattr(proc.sys, "platform", platform)
    env = {"ZW_DEVICE_ID": "zwd_x", "PATH": "/usr/bin"}
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "timeout": 7,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
        "cwd": "/tmp",
        "check": False,
    }

    proc.run_hidden(["tasklist"], **kwargs)

    got = run_recorder.calls[0]
    assert got["argv"] == ["tasklist"]
    for key, value in kwargs.items():
        assert got[key] is value or got[key] == value, f"{key} 没透传：{got[key]!r} != {value!r}"


def test_popen_kwargs_are_passed_through(monkeypatch, popen_recorder):
    monkeypatch.setattr(proc.sys, "platform", "win32")

    proc.popen_hidden(["netstat"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                      env={"A": "1"}, cwd="/tmp", text=True)

    got = popen_recorder.calls[0]
    assert got["stdout"] is subprocess.PIPE
    assert got["stdin"] is subprocess.DEVNULL
    assert got["env"] == {"A": "1"}
    assert got["cwd"] == "/tmp"
    assert got["text"] is True


def test_popen_hidden_returns_subprocess_result(as_linux, popen_recorder):
    """返回值必须是 `subprocess.Popen` 的返回对象本身，不能包一层。"""
    assert proc.popen_hidden(["true"]) == "POPEN-SENTINEL"


# ── POSIX 真跑：功能没坏 ─────────────────────────────────
@pytest.mark.skipif(sys.platform == "win32", reason="这里验的是 POSIX 分支的实跑")
def test_posix_real_run_still_works(tmp_path):
    r = proc.run_hidden(["sh", "-c", "printf hi"], capture_output=True, text=True,
                        cwd=str(tmp_path), timeout=10)

    assert r.returncode == 0
    assert r.stdout == "hi"


@pytest.mark.skipif(sys.platform == "win32", reason="这里验的是 POSIX 分支的实跑")
def test_posix_check_true_still_raises():
    """`check=True` 的语义不能被这层包丢：非零退出照样抛。"""
    with pytest.raises(subprocess.CalledProcessError):
        proc.run_hidden(["sh", "-c", "exit 3"], check=True, capture_output=True, text=True)


@pytest.mark.skipif(sys.platform == "win32", reason="这里验的是 POSIX 分支的实跑")
def test_posix_real_popen_pipes_work(tmp_path):
    """管道读写不受影响（远程命令行就靠这个）。"""
    p = proc.popen_hidden(["sh", "-c", "printf out; printf err 1>&2"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=str(tmp_path))
    out, err = p.communicate(timeout=10)

    assert p.returncode == 0
    assert out == b"out"
    assert err == b"err"


# ── 审计：不许再出现裸调 ─────────────────────────────────
_SPAWN_ATTRS = {"run", "Popen", "call", "check_call", "check_output"}
_OS_SPAWN_ATTRS = {"system", "popen"}

# 豁免标记：必须写在违规那一行或紧邻的上一行，且冒号后面要写清原因。
# 只给"确实不能隐藏窗口"的极少数场景用（例如将来要开一个用户看得见的控制台），
# 写"临时""TODO"这种糊弄话没用 —— 原因不能为空，但意义靠 review 把关。
_EXEMPT_RE = re.compile(r"#\s*proc-exempt\s*:\s*(\S.*)")


def _exempt_reason(lines: list[str], lineno: int) -> str:
    """违规行（1-based）自己或它上一行有没有写明豁免原因。"""
    for idx in (lineno - 1, lineno - 2):
        if 0 <= idx < len(lines):
            match = _EXEMPT_RE.search(lines[idx])
            if match and match.group(1).strip():
                return match.group(1).strip()
    return ""


def _ast_violations(path: Path, root: Path = CORE_SRC) -> list[str]:
    """用 AST 找"裸调起进程"的地方（不受注释/文档字符串干扰）。"""
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src, filename=str(path))

    sub_modules = {"subprocess"}
    os_modules = {"os"}
    sub_direct: dict[str, str] = {}
    os_direct: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    sub_modules.add(alias.asname or alias.name)
                elif alias.name == "os":
                    os_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module == "subprocess" and alias.name in _SPAWN_ATTRS:
                    sub_direct[local] = f"subprocess.{alias.name}"
                elif node.module == "os" and alias.name in _OS_SPAWN_ATTRS:
                    os_direct[local] = f"os.{alias.name}"

    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        label = ""
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in sub_modules and func.attr in _SPAWN_ATTRS:
                label = f"subprocess.{func.attr}"
            elif func.value.id in os_modules and func.attr in _OS_SPAWN_ATTRS:
                label = f"os.{func.attr}"
        elif isinstance(func, ast.Name):
            label = sub_direct.get(func.id) or os_direct.get(func.id) or ""
        if label and not _exempt_reason(lines, node.lineno):
            bad.append(f"{path.relative_to(root)}:{node.lineno}: {label}(...)")
    return bad


_BARE_RE = re.compile(
    r"\b(?:subprocess\.(?:run|Popen|call|check_call|check_output)|os\.(?:system|popen))\s*\("
)


def _code_only(src: str) -> str:
    """掏空注释与字符串字面量（保留换行以对齐行号），只留会执行的代码。

    直接用 grep 会被文档字符串里的 `subprocess.run(...)` 举例误伤；
    这里用 tokenize 把注释/字符串挖掉，等价于"只看真正的代码"。
    """
    out: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            out.append("\n" * tok.string.count("\n"))
        else:
            out.append(tok.string)
    return "".join(out)


def test_no_bare_spawn_calls_outside_proc():
    """审计（AST）：core/src 里除 proc.py 外不许裸调 subprocess.run / Popen。

    hub/ 跑在 Linux 容器里，那段代码不在这里扫 —— 它没有"闪黑框"这回事。
    """
    offenders: list[str] = []
    for path in sorted(CORE_SRC.rglob("*.py")):
        if path == PROC_FILE:
            continue                      # 唯一的合法出口，它内部当然要调 subprocess
        offenders.extend(_ast_violations(path))

    assert not offenders, (
        "这些地方会绕过 services/proc.py 直接起子进程，Windows 上会闪控制台黑框：\n  "
        + "\n  ".join(offenders)
        + "\n改用 services.proc.run_hidden / popen_hidden（GUI 子进程加 gui_child=True）。"
    )


def test_no_bare_spawn_calls_by_text_grep():
    """审计（文本 grep）：与 AST 那条互为交叉验证，行号更直观。"""
    offenders: list[str] = []
    for path in sorted(CORE_SRC.rglob("*.py")):
        if path == PROC_FILE:
            continue
        raw = path.read_text(encoding="utf-8")
        lines = raw.splitlines()
        code = _code_only(raw)
        for lineno, line in enumerate(code.splitlines(), start=1):
            if _BARE_RE.search(line) and not _exempt_reason(lines, lineno):
                offenders.append(f"{path.relative_to(CORE_SRC)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "core/src 里出现裸的起进程调用（Windows 上会闪控制台黑框）：\n  "
        + "\n  ".join(offenders)
    )


def test_audit_catches_a_planted_violation(tmp_path):
    """审计本身要有效：拿一份故意违规的源码，必须被抓到。

    没有这条，审计可能因为正则写错/文件没扫到而永远"通过"。
    """
    bad_src = (
        "import subprocess as sp\n"
        "from os import system as run_shell\n"
        "\n"
        "def f():\n"
        "    sp.run(['tasklist'])\n"
        "    subprocess_Popen = sp.Popen(['cmd'])\n"
        "    run_shell('echo hi')\n"
    )
    # 换一个假的根目录，让 relative_to 能算出来
    fake_root = tmp_path / "src"
    fake = fake_root / "zw_core" / "bad.py"
    fake.parent.mkdir(parents=True)
    fake.write_text(bad_src, encoding="utf-8")

    hits = _ast_violations(fake, root=fake_root)

    labels = " ".join(hits)
    assert "subprocess.run" in labels, hits
    assert "subprocess.Popen" in labels, hits
    assert "os.system" in labels, hits


def test_exempt_marker_must_carry_a_reason(tmp_path):
    """豁免只能靠"写清原因"的注释拿到；空原因的标记不算。

    任务允许"注释里注明原因的极少数例外"，所以留了这个口子。
    但它必须真的被逼着写原因 —— 否则以后有人加一行 `# proc-exempt:`
    就能绕过整个审计。
    """
    root = tmp_path / "src"
    root.mkdir()

    with_reason = root / "with_reason.py"
    with_reason.write_text(
        "import subprocess\n"
        "\n"
        "def f():\n"
        "    # proc-exempt: 这个控制台窗口是给用户自己看着敲的，必须可见\n"
        "    subprocess.run(['cmd'])\n"
        "    subprocess.run(['tasklist'])  # proc-exempt: 同上，另一处\n",
        encoding="utf-8",
    )
    assert _ast_violations(with_reason, root=root) == []

    empty_reason = root / "empty_reason.py"
    empty_reason.write_text(
        "import subprocess\n"
        "\n"
        "def g():\n"
        "    # proc-exempt:\n"
        "    subprocess.run(['cmd'])\n",
        encoding="utf-8",
    )
    hits = _ast_violations(empty_reason, root=root)
    assert len(hits) == 1, f"空原因的豁免必须仍然算违规：{hits}"
    assert "subprocess.run" in hits[0]

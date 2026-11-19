"""桌面客户端启动/连接不能用同步阻塞卡住 Tk 主线程。

用户反馈：「每次打开都会无响应几秒钟」。

实测定位（2026-11-19）：`connect()` 原来整个跑在 Tk 主线程上，其中三步都是秒级阻塞：

    1. probe_server(base)   网络往返；服务器不通时会等到超时
    2. enroll(...)          又一次网络往返
    3. core.start(...)      起 uvicorn 并**轮询等就绪**（本机量到 1215ms，
                            Windows 过杀软扫描更久）

而启动时还会 `root.after(200, connect)` 自动连一次 → 每次打开都卡住界面。
（本地对照实验：同样的 1200ms 任务，主线程直接跑 → 界面停顿 1250ms；
 放进后台线程 → 停顿 50ms。）

这条测试用 AST 把结构钉住：上面三个调用必须出现在 `connect()` 内部的
**嵌套函数**里（也就是后台工作函数），不能出现在 `connect()` 自己的语句里。
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
BLOCKING = ("probe_server", "enroll", "start")


def _gui_tree() -> ast.Module:
    src = (ROOT / "core" / "src" / "zw_core" / "gui.py").read_text(encoding="utf-8")
    return ast.parse(src)


def _find_func(node: ast.AST, name: str) -> ast.FunctionDef | None:
    for child in ast.walk(node):
        if isinstance(child, ast.FunctionDef) and child.name == name:
            return child
    return None


def _direct_calls(fn: ast.FunctionDef) -> set[str]:
    """只收集 `fn` **自己**语句里的调用名，跳过嵌套函数定义。"""
    names: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue          # 嵌套函数里的调用不算"直接阻塞主线程"
            if isinstance(child, ast.Call):
                f = child.func
                if isinstance(f, ast.Name):
                    names.add(f.id)
                elif isinstance(f, ast.Attribute):
                    names.add(f.attr)
            visit(child)

    visit(fn)
    return names


def test_connect_does_not_block_tk_main_thread():
    tree = _gui_tree()
    connect = _find_func(tree, "connect")
    assert connect is not None, "gui.py 里找不到 connect()"

    direct = _direct_calls(connect)
    offenders = sorted(n for n in BLOCKING if n in direct)
    assert not offenders, (
        "这些阻塞调用又回到 Tk 主线程上了：" + ", ".join(offenders)
        + "（必须放进 connect() 内部的 work() 里，用 run_bg 跑）"
    )


def test_blocking_calls_live_inside_a_background_worker():
    """三个阻塞调用仍要在（说明功能没被删掉），只是位置在嵌套工作函数里。"""
    tree = _gui_tree()
    connect = _find_func(tree, "connect")
    assert connect is not None

    nested = [n for n in ast.walk(connect) if isinstance(n, ast.FunctionDef) and n.name == "work"]
    assert nested, "connect() 里找不到后台工作函数 work()"
    calls = set()
    for fn in nested:
        calls |= _direct_calls(fn)

    for want in ("probe_server", "enroll", "start"):
        assert want in calls, f"后台 work() 里没有调用 {want}（功能被删了？）"


def test_run_bg_schedules_callback_on_main_thread():
    """`run_bg` 必须用 `after(0, ...)` 回主线程 —— 不能直接在后台线程碰控件。"""
    src = (ROOT / "core" / "src" / "zw_core" / "gui.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    run_bg = _find_func(tree, "run_bg")
    assert run_bg is not None, "找不到 run_bg()"
    seg = ast.get_source_segment(src, run_bg) or ""
    assert "root.after(0" in seg, "run_bg 没有用 root.after(0, ...) 回主线程"
    assert "threading.Thread" in seg, "run_bg 没有起后台线程"

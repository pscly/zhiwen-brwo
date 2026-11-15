"""孤儿浏览器进程的识别规则。

真实场景：core 被 kill -9、数据库被重置、手动删了环境目录 ——
浏览器却还在跑，占着内存、端口，还让同名环境下次启动撞 SingletonLock。
"""

from __future__ import annotations

from pathlib import Path

from zw_core.services import engines


def test_extract_udd_from_space_joined():
    cmd = "/path/chrome --type=renderer --user-data-dir=/srv/zw/profiles/abc/udd --no-sandbox"
    # 子进程也会带这个参数，所以判断函数只认"根进程"
    assert engines._extract_udd(cmd) == "/srv/zw/profiles/abc/udd"
    assert engines._is_child_process(cmd) is True


def test_extract_udd_missing():
    assert engines._extract_udd("/path/chrome --no-sandbox") is None


def test_extract_udd_with_quotes():
    assert engines._extract_udd(r'chrome --user-data-dir="C:\data\zw\profiles\x\udd"') == r"C:\data\zw\profiles\x\udd"


def test_root_process_detected():
    cmd = "/path/chrome --user-data-dir=/srv/zw/profiles/abc/udd --remote-debugging-port=21001"
    assert engines._is_child_process(cmd) is False


def test_decide_orphans_rules():
    from zw_core.config import get_settings
    from zw_core.services.manager import decide_orphans

    root = Path(get_settings().profiles_dir).resolve()
    procs = [
        {"pid": 1, "udd": str(root / "running1" / "udd")},
        {"pid": 2, "udd": str(root / "stopped1" / "udd")},
        {"pid": 3, "udd": str(root / "已删除的环境" / "udd")},
        {"pid": 4, "udd": "/tmp/别人的目录/udd"},        # 不在我们的目录下，别碰
    ]
    known = {"running1": "running", "stopped1": "stopped"}
    out = decide_orphans(procs, known)
    pids = {o["pid"] for o in out}
    assert pids == {2, 3}, f"只应清 stopped1 与已删除的环境，实际 {pids}"

    by_pid = {o["pid"]: o for o in out}
    assert by_pid[2]["profile_id"] == "stopped1"
    assert "stopped" in by_pid[2]["reason"]
    assert by_pid[3]["reason"] == "环境不在库里"


def test_decide_orphans_keeps_running():
    from zw_core.config import get_settings
    from zw_core.services.manager import decide_orphans

    root = Path(get_settings().profiles_dir).resolve()
    procs = [{"pid": 10, "udd": str(root / "p" / "udd")}]
    assert decide_orphans(procs, {"p": "running"}) == [], "正在运行的环境绝不能被杀"

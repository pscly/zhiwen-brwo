"""客户端身份（机器码）不能莫名其妙地变。

用户反馈「覆盖安装后机器码变了」——服务器上多出一台新设备、需要重新授权。
机器码变了只有一个来源：`ensure_device_id()` 从配置里**读不到** `device_id`，
于是生成一个新的。所以"读不到"这件事必须被穷尽排掉。

这条文件盯两件事：
1. 配置目录必须归一化 8.3 短名（`ADMINI~1` 与 `Administrator` 是同一个物理目录，
   但拼出来的路径字符串不同 → 读出两套配置）
2. 配置文件读不出来时必须**留档**，而不是静默返回空字典
"""

from __future__ import annotations

import json
import pathlib


def _cl(tmp_path, monkeypatch):
    from zw_core import client as cl

    monkeypatch.setenv("ZW_CLIENT_HOME", str(tmp_path / "cfg"))
    return cl


def test_config_dir_is_resolved(tmp_path, monkeypatch):
    """`config_dir()` 必须返回**归一化**后的路径。

    Windows 上 `%LOCALAPPDATA%` 可能是 `C:\\Users\\ADMINI~1\\AppData\\Local`，
    而正常启动是 `C:\\Users\\Administrator\\AppData\\Local` —— 同一目录、两个字符串。
    不归一化就会写出两份 `client.json`，机器码自然对不上。
    """
    cl = _cl(tmp_path, monkeypatch)
    d = cl.config_dir()
    assert d == d.resolve() or str(d) == str(d.resolve()), f"未归一化：{d} vs {d.resolve()}"


def test_broken_config_is_backed_up_not_silently_dropped(tmp_path, monkeypatch):
    """`client.json` 坏了 → 备份成 `.broken-*` 并报警，不能静默当"没配置"。"""
    cl = _cl(tmp_path, monkeypatch)
    p = cl.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ 这不是合法 JSON", encoding="utf-8")

    got = cl.load_config()
    assert got == {}

    backups = list(p.parent.glob("client.json.broken-*"))
    assert backups, "坏配置没留档 —— 机器码丢了都无法人工救回"
    assert not p.exists(), "坏文件还在原地，下次还会踩"


def test_good_config_round_trips_device_id(tmp_path, monkeypatch):
    """正常路径：写进去的机器码必须原样读回来（身份稳定的基础）。"""
    cl = _cl(tmp_path, monkeypatch)
    cl.save_config({"device_id": "zwd_AAAAbbbbCCCCdddd", "server": "http://192.168.3.5:8700"})
    assert cl.ensure_device_id() == "zwd_AAAAbbbbCCCCdddd"
    again = cl.load_config()
    assert again["device_id"] == "zwd_AAAAbbbbCCCCdddd"
    assert again["server"] == "http://192.168.3.5:8700"


def test_non_dict_json_is_not_used(tmp_path, monkeypatch):
    """`client.json` 里存了个数组之类 → 当没用，别让后续 `.get` 崩。"""
    cl = _cl(tmp_path, monkeypatch)
    p = cl.config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert cl.load_config() == {}


def test_device_id_is_stable_within_one_run_even_if_save_fails(tmp_path, monkeypatch):
    """写盘失败时，同一次运行内的机器码必须**稳定**。

    ## 为什么（2026-11-20）

    `ensure_device_id()` 原来是：

    ```python
    did = "zwd_" + secrets.token_urlsafe(24)[:32]
    cfg["device_id"] = did
    save_config(cfg)      # ← 抛 OSError 时，did 没落盘
    return did
    ```

    写盘失败（目录只读、磁盘满、被安全软件锁）时，新机器码没进文件，
    下次调用又生成一个**不同的** —— 服务器上就会不断冒"新设备"，
    每个都要重新授权。用户看到的是"机器码一直在变"。

    现在：先把值记进内存（本次运行内稳定），再写盘；写盘失败**不吞**
    （冒给调用方，界面能看到原因），并打一条说明后果的 ERROR 日志。
    """
    cl = _cl(tmp_path, monkeypatch)
    cl._DEVICE_ID_FALLBACK["value"] = ""      # 清掉上个用例的兜底值

    real_save = cl.save_config
    calls = {"n": 0}

    def boom(cfg):
        calls["n"] += 1
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cl, "save_config", boom)

    first = None
    try:
        first = cl.ensure_device_id()
    except OSError:
        first = cl._DEVICE_ID_FALLBACK["value"]   # 写盘失败：值在兜底里
        assert calls["n"] == 1
    else:
        raise AssertionError("写盘失败应该抛出来，不能静默")

    # 再调一次：必须还是同一个（内存里记住了），不再尝试写盘
    again = cl.ensure_device_id()
    assert again == first, f"同一次运行内机器码变了：{first} → {again}"
    assert calls["n"] == 1, "第二次不该再写盘"

    monkeypatch.setattr(cl, "save_config", real_save)

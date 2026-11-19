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

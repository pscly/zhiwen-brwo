"""服务器端设备注册表。

这是"客户端只填 IP 就能用、服务器端还能踢掉某台电脑"的核心。几条必须守住的：
1. 被踢的设备**重连也不能自愈**（否则踢下线没意义）；
2. 踢下线只影响那一台，别的设备照常；
3. 机器码不合法的输入要被挡住，不能往库里写垃圾。
"""

from __future__ import annotations

import time

import pytest

from zw_hub.devices import DeviceError, DeviceStore


@pytest.fixture()
def store(tmp_path):
    return DeviceStore(tmp_path / "devices.db")


def test_enroll_creates_active_device(store):
    rec = store.enroll("zwd_abcdefgh12345678", hostname="PC-A", platform="Windows", arch="AMD64",
                       app_version="0.3.0", ip="192.168.3.9")
    assert rec["status"] == "active"
    assert rec["hostname"] == "PC-A"
    assert rec["online"] is True
    assert rec["label"] == "PC-A"      # 没给 label 时用 hostname


def test_enroll_is_idempotent_but_keeps_identity(store):
    a = store.enroll("zwd_abcdefgh12345678", hostname="PC-A")
    b = store.enroll("zwd_abcdefgh12345678", hostname="PC-A-RENAMED")
    assert a["first_seen"] == b["first_seen"]
    assert b["hostname"] == "PC-A-RENAMED"
    assert len(store.listing()) == 1


def test_revoked_device_cannot_revive_by_re_enrolling(store):
    """核心回归：被踢之后重连必须仍然被拦。"""
    store.enroll("zwd_abcdefgh12345678", hostname="PC-A")
    store.revoke("zwd_abcdefgh12345678", actor="pscly")
    # 重新注册（模拟客户端重启）
    rec = store.enroll("zwd_abcdefgh12345678", hostname="PC-A")
    assert rec["status"] == "revoked", "重新注册不能把状态改回 active"
    with pytest.raises(DeviceError) as exc:
        store.require_active("zwd_abcdefgh12345678")
    assert exc.value.code == "DEVICE_REVOKED"
    assert exc.value.status == 403


def test_restore_brings_device_back(store):
    store.enroll("zwd_abcdefgh12345678")
    store.revoke("zwd_abcdefgh12345678")
    store.restore("zwd_abcdefgh12345678", actor="pscly")
    store.require_active("zwd_abcdefgh12345678")   # 不抛就对了


def test_revoke_one_does_not_affect_others(store):
    store.enroll("zwd_aaaaaaaaaaaaaaaa", hostname="A")
    store.enroll("zwd_bbbbbbbbbbbbbbbb", hostname="B")
    store.revoke("zwd_aaaaaaaaaaaaaaaa")
    store.require_active("zwd_bbbbbbbbbbbbbbbb")
    with pytest.raises(DeviceError):
        store.require_active("zwd_aaaaaaaaaaaaaaaa")


def test_unknown_device_is_rejected(store):
    with pytest.raises(DeviceError) as exc:
        store.require_active("zwd_never_seen_device")
    assert exc.value.code == "DEVICE_UNKNOWN"


def test_empty_device_id_is_rejected(store):
    with pytest.raises(DeviceError) as exc:
        store.require_active("")
    assert exc.value.code == "DEVICE_REQUIRED"


def test_invalid_device_id_shape_rejected(store):
    for bad in ("", "short", "x" * 200):
        with pytest.raises(DeviceError):
            store.enroll(bad)


def test_online_window(store, monkeypatch):
    rec = store.enroll("zwd_abcdefgh12345678")
    assert rec["online"] is True
    # 手工把 last_seen 退到很久以前
    import sqlite3

    c = sqlite3.connect(store.path)
    c.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (time.time() - 9999, "zwd_abcdefgh12345678"))
    c.commit()
    c.close()
    got = store.get("zwd_abcdefgh12345678")
    assert got["online"] is False
    assert got["last_seen_ago"] > 9000


def test_revoked_shows_offline(store):
    store.enroll("zwd_abcdefgh12345678")
    store.revoke("zwd_abcdefgh12345678")
    got = store.get("zwd_abcdefgh12345678")
    assert got["status"] == "revoked"
    assert got["online"] is False, "被踢的设备不该再算在线，哪怕它刚刚还心跳过"


def test_label_and_events(store):
    store.enroll("zwd_abcdefgh12345678", hostname="PC-A")
    store.set_label("zwd_abcdefgh12345678", "客厅那台")
    assert store.get("zwd_abcdefgh12345678")["label"] == "客厅那台"
    store.revoke("zwd_abcdefgh12345678", actor="pscly", note="换人了")
    ev = store.events()
    actions = [e["action"] for e in ev]
    assert "enroll" in actions and "revoke" in actions
    assert ev[0]["actor"] == "pscly"


def test_delete_removes_device(store):
    store.enroll("zwd_abcdefgh12345678")
    assert store.delete("zwd_abcdefgh12345678") is True
    assert store.get("zwd_abcdefgh12345678") is None
    assert store.delete("zwd_abcdefgh12345678") is False


def test_stats(store):
    store.enroll("zwd_aaaaaaaaaaaaaaaa")
    store.enroll("zwd_bbbbbbbbbbbbbbbb")
    store.revoke("zwd_bbbbbbbbbbbbbbbb")
    st = store.stats()
    assert st["total"] == 2
    assert st["active"] == 1
    assert st["revoked"] == 1
    assert st["online"] == 1

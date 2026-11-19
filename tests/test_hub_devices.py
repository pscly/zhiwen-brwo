

def test_heartbeat_reports_autostart_state(tmp_path):
    """心跳要带 `autostart`，让主控台能区分"设备为什么离线"。

    实际情况：设备突然从面板上消失，可能是
      · 机器重启了，客户端没开自启 → 人去点一下图标就回来
      · 机器真关机 / 断网       → 只能等
    两种处置完全不同。不报这个字段，用户只能猜。

    三态语义要保住：没上报 = None（旧客户端），不能当成 False（"确定没开"）。
    """
    from zw_hub.devices import DeviceStore

    store = DeviceStore(tmp_path / "d.sqlite3")
    # 方法叫 `enroll`（不是 register）
    dev = store.enroll("zwd_auto_test", ip="10.0.0.9")

    # 上报「开了自启」
    store.touch(dev["device_id"], ip="10.0.0.9", stats={"autostart": True})
    got = store.get(dev["device_id"])
    assert got["autostart"] is True, got.get("autostart")

    # 上报「没开」
    store.touch(dev["device_id"], ip="10.0.0.9", stats={"autostart": False})
    assert store.get(dev["device_id"])["autostart"] is False

    # 之后的心跳没带这个字段 → 保持原值（COALESCE），不能变回 None
    store.touch(dev["device_id"], ip="10.0.0.9", stats={"profiles": 3})
    assert store.get(dev["device_id"])["autostart"] is False

    # 全新设备、从没上报过 → None（未知，不是 False）
    dev2 = store.enroll("zwd_auto_never", ip="10.0.0.10")
    assert store.get(dev2["device_id"])["autostart"] is None

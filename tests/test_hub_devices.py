

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


def test_shell_submit_warns_when_core_unreachable():
    """core 不可达时投递命令，返回里必须带 `warning`。

    ## 为什么要（2026-11-20）

    心跳由客户端 GUI 发，core 是它起的子服务。**core 死了心跳照发** ——
    服务器上看着"在线"，但命令永远拉不到。

    以前 `POST /v1/devices/{id}/shell` 只回 `{"status": "queued"}`，
    调用方会一直等、以为在排队，实际没人领。我自己就干等过两轮。

    现在：排队保留（设备可能恢复），但把原因写进 `warning`，让人知道该去那台机器点一下。
    """
    import inspect

    from zw_hub import main as hub_main

    src = inspect.getsource(hub_main.shell_submit)
    assert "core_reachable" in src, "没检查 core 是否可达"
    assert "warning" in src, "没给 warning"
    assert "排队" in src, "没说清『命令会一直排队』"
    # **必须现算**，不能读 `dev["core_reachable"]` —— 那个键只存在于首屏视图里，
    # 设备原始行没有。第一版就是直接读，永远 None，警告一次都没出现（实测）。
    assert "_probe_core" in src, "没有现算可达性"
    # 去掉注释再判断（我自己的注释里提到过那个错误写法，会误伤）
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert 'dev.get("core_reachable")' not in code, "又在读不存在的键"

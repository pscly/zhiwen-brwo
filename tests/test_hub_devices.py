

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


def test_heartbeat_stores_and_exposes_core_error(tmp_path):
    """客户端报上来的 `core_error` 必须**存下来并暴露**。

    ## 为什么（2026-11-20 卡了我好几轮）

    云电脑上 core 起不来，我从服务器侧只能看到"core 不可达"，**不知道为什么**。
    而客户端本来就在算这个原因（`local_summary()` 里有 `core_error`）——
    只是 hub 以前根本不收这个字段，等于白报。

    补上之后：主控台首屏能看到原因，投递命令的 warning 里也会附上，
    不用跑到机器前才知道是"端口被占"还是"杀软拦截"。
    """
    from zw_hub.devices import DeviceStore

    store = DeviceStore(tmp_path / "d.sqlite3")
    dev = store.enroll("zwd_cerr", ip="10.0.0.5")

    reason = "本地服务 45 秒内没起来（端口 8730）：端口被别的程序占了"
    store.touch(dev["device_id"], ip="10.0.0.5",
                stats={"core_alive": False, "core_error": reason})
    got = store.get(dev["device_id"])
    assert got["core_error"] == reason, got.get("core_error")

    # 恢复正常时应当被清空（否则会一直挂着一个过期的旧原因）
    store.touch(dev["device_id"], ip="10.0.0.5", stats={"core_alive": True, "core_error": ""})
    assert store.get(dev["device_id"])["core_error"] == ""


def test_overview_row_carries_core_error():
    """控制台首屏的设备行要带 `core_error`。"""
    import inspect

    from zw_hub import main as hub_main

    src = inspect.getsource(hub_main.console_overview)
    assert '"core_error"' in src, "首屏没带 core_error"


def test_shell_warning_appends_reported_reason():
    """投递命令的 warning 要把客户端报的原因附上。"""
    import inspect

    from zw_hub import main as hub_main

    code = "\n".join(ln for ln in inspect.getsource(hub_main.shell_submit).splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "客户端报告的失败原因" in code


def test_heartbeat_stores_host_notice(tmp_path):
    """core 自动降级的原因（`host_notice`）必须存下来。

    ## 为什么（2026-11-20 排查了十几轮）

    云电脑上 core 的 `gateway_public_base` 报的是 `http://127.0.0.1:8730`
    而不是 LAN IP —— 这只在「没有 device_id 也没有 admin_token」触发降级时发生。
    降级之后外部再也连不上 8730，但心跳照发，于是服务器看到"在线却够不着"。

    这个原因本来就在 `Settings.host_notice` 里，但**只在日志打一行**，
    从来没进过心跳。我因此只能靠猜。

    现在存下来并暴露，主控台能直接说清。
    """
    from zw_hub.devices import DeviceStore

    store = DeviceStore(tmp_path / "d.sqlite3")
    dev = store.enroll("zwd_notice", ip="10.0.0.6")

    notice = "ZW_HOST=0.0.0.0 但没有 ZW_ADMIN_TOKEN 或 ZW_DEVICE_ID，已自动降级为只监听 127.0.0.1"
    store.touch(dev["device_id"], ip="10.0.0.6", stats={"host_notice": notice})
    got = store.get(dev["device_id"])
    assert got["host_notice"] == notice, got.get("host_notice")

    # 恢复正常时清空
    store.touch(dev["device_id"], ip="10.0.0.6", stats={"host_notice": ""})
    assert store.get(dev["device_id"])["host_notice"] == ""


def test_overview_and_warning_carry_host_notice():
    import inspect

    from zw_hub import main as hub_main

    ov = "\n".join(ln for ln in inspect.getsource(hub_main.console_overview).splitlines()
                   if not ln.lstrip().startswith("#"))
    assert '"host_notice"' in ov, "首屏没带 host_notice"

    sh = "\n".join(ln for ln in inspect.getsource(hub_main.shell_submit).splitlines()
                   if not ln.lstrip().startswith("#"))
    assert "host_notice" in sh, "投递命令的 warning 没带降级原因"

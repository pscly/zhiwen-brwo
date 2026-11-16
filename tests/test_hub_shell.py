"""hub 侧远程命令行：队列状态机 + 六条 HTTP 路由 + 心跳里的开关落库。

协议契约见 `docs/13-远程命令行协议.md`。这里要守住的几条性质：

1. **完整链路**：投递 → 设备长轮询取走 → 回传结果 → 管理员查到终态；
2. **长轮询不是忙轮询**：无命令时按 `wait` 返回（不提前、不超太久），
   投递后立刻被唤醒（用真实线程验证，看唤醒延迟而不是看"能拿到"）；
3. **越权不可见**：不同设备的 job 互相 404，连错误码都不该让人推断出"这个 id 存在"；
4. **截断是 UTF-8 安全的**：中文输出截断后仍然是合法 UTF-8，不能切出半个汉字；
5. **路由顺序**：`/v1/devices/shell/next` 与 `.../shell/history` 不能被
   `{device_id}` / `{job_id}` 通配段吃掉（这个仓库已经被同类问题坑过两次）。

整个文件刻意跑得快：所有长轮询都用 0.2~0.3 秒级别的等待。
"""

from __future__ import annotations

import re
import shutil
import threading
import time

import pytest
from fastapi.testclient import TestClient

DEV_A = "zwd_shella00000001"
DEV_B = "zwd_shellb00000002"


@pytest.fixture(scope="module")
def db_templates(tmp_path_factory):
    """空库模板：整个文件只建一次，每个用例复制一份。

    为什么不用"每个用例从零建库"：新建 sqlite 文件 + 建表 + 切 WAL 要 40~70ms，
    19 个用例光这一项就 1.5 秒以上；复制一个空库不到 1 毫秒。
    要求是"整个文件 < 5 秒"，这项省出来的时间最实在。
    """
    from zw_hub.devices import DeviceStore
    from zw_hub.shell import ShellQueue

    d = tmp_path_factory.mktemp("dbtpl")
    DeviceStore(d / "devices.db")
    ShellQueue(d / "shell.db").close()      # close 会把 WAL 落回主库，只复制 .db 就够
    return d


@pytest.fixture()
def hub(tmp_path, monkeypatch, db_templates):
    for name in ("devices.db", "shell.db"):
        shutil.copyfile(db_templates / name, tmp_path / name)

    monkeypatch.setenv("ZW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ZW_HUB_TOKEN", "")
    monkeypatch.setenv("ZW_ALLOWED_CIDRS", "")
    monkeypatch.setenv("ZW_PANEL_USER", "testadmin")
    monkeypatch.setenv("ZW_PANEL_PASSWORD", "test-panel-pass")
    monkeypatch.delenv("ZW_SHELL_ENABLED", raising=False)
    monkeypatch.delenv("ZW_SHELL_MAX_OUTPUT", raising=False)
    monkeypatch.delenv("ZW_SHELL_QUEUE_LIMIT", raising=False)

    from zw_hub import config

    config.reset_settings()
    from zw_hub import main as hub_main

    hub_main._store = None
    hub_main._devices = None
    hub_main._shell_queue = None
    # 用上下文管理器保持同一个 blocking portal：否则每个请求都要新起一个事件循环，
    # 100 多次请求光这一项就要多花 0.3 秒以上。
    with TestClient(hub_main.app) as client:
        yield client
    # 收尾把三个模块级单例都清掉：别的测试文件会换 ZW_DATA_DIR，
    # 留着旧实例会指向一个已经被删掉的临时目录。
    if hub_main._shell_queue is not None:
        hub_main._shell_queue.close()
    hub_main._store = None
    hub_main._devices = None
    hub_main._shell_queue = None
    config.reset_settings()


# ── 小工具 ────────────────────────────────────────────────
def enroll(client: TestClient, did: str = DEV_A) -> None:
    r = client.post("/v1/devices/enroll", json={"device_id": did, "hostname": "PC-A"})
    assert r.status_code == 200, r.text


def panel(client: TestClient) -> None:
    r = client.post("/panel/api/login", json={"username": "testadmin", "password": "test-panel-pass"})
    assert r.status_code == 200, r.text


def submit(client: TestClient, did: str = DEV_A, **payload):
    body = {"command": "ipconfig /all", **payload}
    return client.post(f"/v1/devices/{did}/shell", json=body)


def take(client: TestClient, did: str = DEV_A, wait: float = 0):
    """设备侧取一条命令（wait=0 表示不挂起，测试里只用这个或小 wait）。"""
    return client.get("/v1/devices/shell/next", params={"wait": wait}, headers={"X-ZW-Device": did})


def report(client: TestClient, job_id: str, did: str = DEV_A, **payload):
    body = {"rc": 0, "stdout": "", "stderr": "", "duration_ms": 5, "truncated": False, "error": ""}
    body.update(payload)
    return client.post(f"/v1/devices/shell/{job_id}/result", json=body, headers={"X-ZW-Device": did})


def shell_of(client: TestClient) -> dict:
    return client.get("/panel/api/stats").json()["data"]["shell"]


# ── 主链路 ────────────────────────────────────────────────
def test_submit_next_result_then_admin_sees_done(hub):
    enroll(hub)
    panel(hub)

    r = submit(hub, command="ipconfig /all")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "queued"
    job_id = data["job_id"]
    assert re.fullmatch(r"sh_[0-9a-f]{12}", job_id), job_id

    r = take(hub)
    assert r.status_code == 200, r.text
    job = r.json()["data"]["job"]
    assert job["id"] == job_id
    assert job["command"] == "ipconfig /all"
    assert job["cwd"] == ""
    assert job["timeout"] == 60
    assert job["created_at"] > 0

    # 取走之后是 running，且未完成时输出必须是空串（不能把上一条的输出带出来）
    got = hub.get(f"/v1/devices/{DEV_A}/shell/{job_id}", params={"wait": 0}).json()["data"]["job"]
    assert got["status"] == "running"
    assert got["stdout"] == "" and got["stderr"] == ""
    assert got["started_at"] is not None and got["finished_at"] is None

    r = report(hub, job_id, rc=0, stdout="Windows IP 配置\n", stderr="", duration_ms=123)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"accepted": True}

    job = hub.get(f"/v1/devices/{DEV_A}/shell/{job_id}", params={"wait": 0}).json()["data"]["job"]
    for key in (
        "id", "device_id", "command", "status", "rc", "stdout", "stderr",
        "duration_ms", "truncated", "error", "created_at", "started_at", "finished_at",
    ):
        assert key in job, f"协议要求的状态里有 {key}"
    assert job["status"] == "done"
    assert job["device_id"] == DEV_A
    assert job["rc"] == 0
    assert job["stdout"] == "Windows IP 配置\n"
    assert job["stderr"] == ""
    assert job["duration_ms"] == 123
    assert job["truncated"] is False
    assert job["error"] == ""
    assert job["created_at"] <= job["started_at"] <= job["finished_at"]

    # 历史里也在，且按 created_at 倒序
    jobs = hub.get(f"/v1/devices/{DEV_A}/shell/history").json()["data"]["jobs"]
    assert [j["id"] for j in jobs] == [job_id]


def test_nonzero_rc_is_still_done_but_error_is_error(hub):
    """跑完但退出码非 0 ≠ 没跑起来。面板要能区分这两件事。"""
    enroll(hub)
    panel(hub)
    j1 = submit(hub, command="grep 找不到").json()["data"]["job_id"]
    take(hub)
    report(hub, j1, rc=1, stderr="no match")
    got = hub.get(f"/v1/devices/{DEV_A}/shell/{j1}", params={"wait": 0}).json()["data"]["job"]
    assert got["status"] == "done" and got["rc"] == 1

    j2 = submit(hub, command="cd /不存在 && ls").json()["data"]["job_id"]
    take(hub)
    report(hub, j2, rc=-1, error="SHELL_DISABLED")
    got = hub.get(f"/v1/devices/{DEV_A}/shell/{j2}", params={"wait": 0}).json()["data"]["job"]
    assert got["status"] == "error" and got["error"] == "SHELL_DISABLED"

    j3 = submit(hub, command="sleep 99").json()["data"]["job_id"]
    take(hub)
    report(hub, j3, rc=-1, error="SHELL_TIMEOUT")
    got = hub.get(f"/v1/devices/{DEV_A}/shell/{j3}", params={"wait": 0}).json()["data"]["job"]
    assert got["status"] == "timeout", "设备侧超时要能和一般失败区分开"


# ── 长轮询 ────────────────────────────────────────────────
def test_long_poll_returns_none_after_wait(hub):
    """没命令时按 wait 返回，既不立刻空转回来，也不挂过头。"""
    enroll(hub)
    t0 = time.monotonic()
    r = take(hub, wait=0.2)
    spent = time.monotonic() - t0
    assert r.status_code == 200, r.text
    assert r.json()["data"]["job"] is None
    assert spent >= 0.15, f"wait=0.2 却不等待（{spent:.3f}s），说明实现是忙轮询或者忽略了 wait"
    assert spent < 2.0, f"wait=0.2 却挂了 {spent:.3f}s"


def test_long_poll_wakes_up_immediately_on_submit(hub):
    """投递后必须立刻被唤醒 —— 这才能证明用的是 Condition 而不是 sleep 轮询。"""
    from zw_hub.main import shell_queue

    q = shell_queue()
    box: dict = {}

    def waiter() -> None:
        t0 = time.monotonic()
        box["job"] = q.next_job(DEV_A, wait=5.0)
        box["spent"] = time.monotonic() - t0

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.15)                     # 确保它已经挂在 Condition.wait 上
    q.submit(DEV_A, "echo 醒醒")
    t.join(timeout=5)
    assert not t.is_alive(), "投递之后等待方没有被唤醒"
    assert box["job"]["command"] == "echo 醒醒"
    assert box["spent"] < 1.0, f"投递后应立刻唤醒，实际等了 {box['spent']:.3f}s"


def test_admin_get_wait_returns_as_soon_as_terminal(hub):
    """管理员侧 ?wait= 也是一样：结果落终态就立刻返回，不用等满。"""
    enroll(hub)
    panel(hub)
    job_id = submit(hub).json()["data"]["job_id"]
    take(hub)

    from zw_hub.main import shell_queue

    def finish() -> None:
        time.sleep(0.15)
        shell_queue().complete(job_id, DEV_A, rc=0, stdout="好了")

    t = threading.Thread(target=finish)
    t.start()
    t0 = time.monotonic()
    got = hub.get(f"/v1/devices/{DEV_A}/shell/{job_id}", params={"wait": 5}).json()["data"]["job"]
    spent = time.monotonic() - t0
    t.join(timeout=5)
    assert got["status"] == "done" and got["stdout"] == "好了"
    assert spent < 2.0, f"终态之后还挂着（{spent:.3f}s），长轮询没被 notify 唤醒"


# ── 队列上限与参数校验 ────────────────────────────────────
def test_queue_full_returns_429_and_is_per_device(hub, monkeypatch):
    enroll(hub)
    enroll(hub, DEV_B)
    panel(hub)
    from zw_hub.config import get_settings
    from zw_hub import main as hub_main

    monkeypatch.setattr(get_settings(), "shell_queue_limit", 3)
    hub_main._shell_queue = None                     # 让工厂按新上限重建

    for i in range(3):
        assert submit(hub, command=f"echo {i}").status_code == 200
    r = submit(hub)
    assert r.status_code == 429, r.text
    assert r.json()["code"] == "SHELL_QUEUE_FULL"

    # 上限是按设备算的：另一台设备照常能投
    assert submit(hub, DEV_B, command="echo b").status_code == 200
    # 设备把命令取走之后队列就腾出来了
    assert take(hub).json()["data"]["job"] is not None
    assert submit(hub, command="echo 又一条").status_code == 200


def test_command_length_and_timeout_validation(hub):
    enroll(hub)
    panel(hub)
    r = submit(hub, command="x" * 4001)
    assert r.status_code == 400 and r.json()["code"] == "SHELL_COMMAND_TOO_LONG"
    # 正好 4000 是允许的（边界值不能被误杀）
    assert submit(hub, command="x" * 4000).status_code == 200
    assert submit(hub, command="   ").status_code == 400
    assert submit(hub, command="echo hi") .json()["data"]["status"] == "queued"
    for bad in (0, 601, "abc"):
        r = submit(hub, command="echo hi", timeout=bad)
        assert r.status_code == 400 and r.json()["code"] == "SHELL_TIMEOUT_INVALID", bad
    assert submit(hub, command="echo hi", timeout=600).status_code == 200


# ── 鉴权与越权 ────────────────────────────────────────────
def test_revoked_device_cannot_take_or_submit(hub):
    enroll(hub)
    panel(hub)
    job_id = submit(hub).json()["data"]["job_id"]
    hub.post(f"/panel/api/devices/{DEV_A}/revoke", json={})

    r = take(hub)
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_REVOKED"
    r = report(hub, job_id)
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_REVOKED", "被踢的设备也不能回传结果"
    r = submit(hub)
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_REVOKED", "被踢的设备不该再收新命令"


def test_jobs_are_isolated_between_devices(hub):
    enroll(hub)
    enroll(hub, DEV_B)
    panel(hub)
    job_id = submit(hub, DEV_A).json()["data"]["job_id"]

    r = hub.get(f"/v1/devices/{DEV_B}/shell/{job_id}", params={"wait": 0})
    assert r.status_code == 404 and r.json()["code"] == "SHELL_JOB_NOT_FOUND"
    # 别人的命令也不该被取到
    assert take(hub, DEV_B).json()["data"]["job"] is None
    # 设备 A 取走后，B 也不能替它回传结果
    take(hub, DEV_A)
    r = report(hub, job_id, DEV_B)
    assert r.status_code == 404 and r.json()["code"] == "SHELL_JOB_NOT_FOUND"
    # 历史互不可见
    assert hub.get(f"/v1/devices/{DEV_B}/shell/history").json()["data"]["jobs"] == []
    # 不存在的 job 一律 404，不区分"别人的"和"没有的"
    assert hub.get(f"/v1/devices/{DEV_A}/shell/sh_ffffffffffff", params={"wait": 0}).status_code == 404


def test_admin_auth_accepts_bearer_when_token_set(hub, monkeypatch):
    enroll(hub)
    from zw_hub.config import get_settings

    monkeypatch.setattr(get_settings(), "token", "secret-token")
    r = submit(hub)
    assert r.status_code == 401 and r.json()["code"] == "UNAUTHENTICATED"
    r = hub.post(f"/v1/devices/{DEV_A}/shell", json={"command": "whoami"},
                 headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200, r.text


def test_empty_hub_token_does_not_accept_any_bearer(hub):
    """令牌留空 = 这台服务器不要令牌，不是"任何 Bearer 都算数"。"""
    enroll(hub)
    for header in ("Bearer ", "Bearer anything", "Bearer test-token"):
        r = hub.post(f"/v1/devices/{DEV_A}/shell", json={"command": "whoami"},
                     headers={"Authorization": header})
        assert r.status_code == 401, header


def test_cancel_only_works_before_dispatch(hub):
    enroll(hub)
    panel(hub)

    j1 = submit(hub).json()["data"]["job_id"]
    assert hub.post(f"/v1/devices/{DEV_A}/shell/{j1}/cancel").json()["data"] == {"canceled": True}
    assert take(hub).json()["data"]["job"] is None, "取消掉的命令不能再下发给设备"
    got = hub.get(f"/v1/devices/{DEV_A}/shell/{j1}", params={"wait": 0}).json()["data"]["job"]
    assert got["status"] == "canceled"

    j2 = submit(hub).json()["data"]["job_id"]
    take(hub)                                        # 已经下发
    assert hub.post(f"/v1/devices/{DEV_A}/shell/{j2}/cancel").json()["data"] == {"canceled": False}
    # 别人的 job 不能取消
    r = hub.post(f"/v1/devices/{DEV_B}/shell/{j2}/cancel")
    assert r.status_code == 404


# ── 输出截断 ──────────────────────────────────────────────
def test_long_output_truncated_on_utf8_boundary(hub, monkeypatch):
    enroll(hub)
    panel(hub)
    from zw_hub.config import get_settings
    from zw_hub import main as hub_main

    monkeypatch.setattr(get_settings(), "shell_max_output", 10)   # 10 字节 ≈ 3 个汉字
    hub_main._shell_queue = None

    job_id = submit(hub, command="dir").json()["data"]["job_id"]
    take(hub)
    report(hub, job_id, stdout="一二三四五六七八九十", stderr="警告：磁盘快满了" * 5)

    job = hub.get(f"/v1/devices/{DEV_A}/shell/{job_id}", params={"wait": 0}).json()["data"]["job"]
    assert job["truncated"] is True
    stdout = job["stdout"]
    assert stdout == "一二三", stdout
    assert len(stdout.encode("utf-8")) <= 10
    assert stdout.encode("utf-8").decode("utf-8") == stdout      # 没有半个字
    assert "\ufffd" not in stdout and "\ufffd" not in job["stderr"]
    assert job["stderr"].startswith("警告")
    # 短输出不受影响
    j2 = submit(hub, command="echo 短").json()["data"]["job_id"]
    take(hub)
    report(hub, j2, stdout="好的")
    job2 = hub.get(f"/v1/devices/{DEV_A}/shell/{j2}", params={"wait": 0}).json()["data"]["job"]
    assert job2["truncated"] is False and job2["stdout"] == "好的"


# ── 心跳里的开关 ──────────────────────────────────────────
def test_heartbeat_persists_shell_enabled(hub):
    enroll(hub)
    panel(hub)

    r = hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": DEV_A},
                 json={"profiles": 2, "running": 1, "shell_enabled": True})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["device"]["shell_enabled"] is True

    def panel_row() -> dict:
        rows = hub.get("/panel/api/devices").json()["data"]["devices"]
        return next(d for d in rows if d["device_id"] == DEV_A)

    assert panel_row()["shell_enabled"] is True

    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": DEV_A}, json={"shell_enabled": False})
    assert panel_row()["shell_enabled"] is False

    # 不带这个字段的心跳不能把状态清掉（否则面板会随机显示成"关"）
    hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": DEV_A}, json={"profiles": 3})
    assert panel_row()["shell_enabled"] is False
    assert panel_row()["profiles"] == 3


# ── 开关、统计、状态机细节 ────────────────────────────────
def test_hub_switch_off_rejects_everything_admin_side(hub, monkeypatch):
    enroll(hub)
    panel(hub)
    from zw_hub.config import get_settings

    monkeypatch.setattr(get_settings(), "shell_enabled", False)
    r = submit(hub)
    assert r.status_code == 403 and r.json()["code"] == "SHELL_DISABLED"
    assert hub.get(f"/v1/devices/{DEV_A}/shell/history").status_code == 403
    assert hub.get(f"/v1/devices/{DEV_A}/shell/sh_000000000000").status_code == 403
    # 设备侧也不干活（否则它会白挂 25 秒）
    assert take(hub).status_code == 403


def test_panel_stats_and_manifest_list_shell(hub):
    enroll(hub)
    panel(hub)
    j1 = submit(hub, command="echo a").json()["data"]["job_id"]
    submit(hub, command="echo b")
    take(hub)
    report(hub, j1, rc=0)

    st = shell_of(hub)
    assert st["done"] == 1
    assert st["queued"] == 1
    assert st["running"] == 0
    assert st["devices_with_pending"] == 1

    paths = {e["path"] for e in hub.get("/v1/ai/manifest").json()["data"]["endpoints"]}
    for want in (
        "/v1/devices/shell/next",
        "/v1/devices/shell/{job_id}/result",
        "/v1/devices/{device_id}/shell",
        "/v1/devices/{device_id}/shell/{job_id}",
        "/v1/devices/{device_id}/shell/history",
        "/v1/devices/{device_id}/shell/{job_id}/cancel",
    ):
        assert want in paths, f"AI 自描述清单里缺 {want}"


def test_expire_stale_writes_terminal_state(hub):
    from zw_hub.main import shell_queue

    q = shell_queue()
    job = q.submit(DEV_A, "等了没人取")
    assert q.expire_stale(max_age=-1) >= 1
    got = q.get(job["id"], DEV_A)
    assert got["status"] == "expired"
    assert q.next_job(DEV_A, wait=0) is None, "过期作废的命令不能再下发"


def test_history_survives_restart(tmp_path):
    """内存为主，但终态历史落 SQLite —— 换个实例（模拟重启）还能查到。"""
    from zw_hub.shell import ShellQueue

    path = tmp_path / "shell.db"
    q1 = ShellQueue(path, max_output=1024, queue_limit=10)
    try:
        job = q1.submit(DEV_A, "echo 记住我")
        assert q1.next_job(DEV_A, wait=1)["command"] == "echo 记住我"
        q1.complete(job["id"], DEV_A, rc=0, stdout="ok")
    finally:
        q1.close()

    q2 = ShellQueue(path, max_output=1024, queue_limit=10)
    try:
        got = q2.get(job["id"], DEV_A)
        assert got["status"] == "done" and got["stdout"] == "ok"
        assert [j["id"] for j in q2.history(DEV_A)] == [job["id"]]
        assert q2.stats()["done"] == 1
        # 排队中的命令不落库：重启后就没了（这是刻意的，见 shell.py 顶部）
        q2.submit(DEV_A, "这条别指望重启后还在")
    finally:
        q2.close()
    q3 = ShellQueue(path, max_output=1024, queue_limit=10)
    try:
        assert q3.next_job(DEV_A, wait=0) is None
        assert q3.stats()["queued"] == 0
    finally:
        q3.close()


def test_fixed_paths_are_not_swallowed_by_wildcards(hub):
    """路由顺序回归：被这个仓库的 `{ref}` 通配段坑过两次了。"""
    enroll(hub)
    panel(hub)
    submit(hub, command="echo 1")

    # history 必须命中"历史"，而不是被当成 job_id 去查一条不存在的命令
    r = hub.get(f"/v1/devices/{DEV_A}/shell/history")
    assert r.status_code == 200, r.text
    assert len(r.json()["data"]["jobs"]) == 1

    # 设备侧的固定路径必须命中设备处理器
    r = take(hub)
    assert r.status_code == 200 and r.json()["data"]["job"]["command"] == "echo 1"

    # 老的固定路径仍然活着（没被新的通配段吃掉）
    enroll(hub, DEV_B)
    assert hub.post("/v1/devices/heartbeat", headers={"X-ZW-Device": DEV_B}).status_code == 200


# ── 配置项 ────────────────────────────────────────────────
def test_shell_config_defaults_and_env(monkeypatch):
    """`ZW_SHELL_*` 三个配置项：默认值必须和协议一致，且能被环境变量覆盖。"""
    from zw_hub.config import Settings

    s = Settings()
    assert s.shell_enabled is True
    assert s.shell_max_output == 65536
    assert s.shell_queue_limit == 200

    monkeypatch.setenv("ZW_SHELL_ENABLED", "false")
    monkeypatch.setenv("ZW_SHELL_MAX_OUTPUT", "1234")
    monkeypatch.setenv("ZW_SHELL_QUEUE_LIMIT", "7")
    s2 = Settings()
    assert s2.shell_enabled is False
    assert s2.shell_max_output == 1234
    assert s2.shell_queue_limit == 7

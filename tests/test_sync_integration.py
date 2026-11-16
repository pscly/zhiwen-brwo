"""同步集成测试：不用 mock，真起一个 hub 进程，走完整的 push / pull 链路。

为什么值得这么重：跨机同步是这个项目最容易"看起来对、实际毁数据"的部分。
0.1.0 里就有三个只有真跑才会暴露的问题：

1. 包内 manifest 不带环境设置 → 拉到新机器上代理、扩展、窗口全丢；
2. `pull` 在本地没有该环境时会先建环境再判断 noop，导致空环境被当成"已同步"；
3. hub 的 latest 指针非单调时抛 409，客户端直接失败，而正确做法是让用户先对齐。

这里用 **subprocess 起真 hub**（uvicorn 监听临时端口），core 侧直接调 service 函数
（不走 HTTP，省掉一层），数据目录全部落在 tmp_path 里，互不干扰。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


@pytest.fixture(scope="module")
def hub_url(tmp_path_factory):
    """起一个真 hub。返回 (base_url, token)。"""
    data = tmp_path_factory.mktemp("hubbase")
    port = _free_port()
    token = "hub-test-token-abc"
    env = {
        **os.environ,
        "ZW_DATA_DIR": str(data / "data"),
        "ZW_HUB_PORT": str(port),
        "ZW_HUB_HOST": "127.0.0.1",
        "ZW_HUB_TOKEN": token,
        "ZW_LOG_LEVEL": "WARNING",
        "ZW_KEEP_REVISIONS": "5",
    }
    # 千万不要用 stdout=subprocess.PIPE：uvicorn 的访问日志会持续写入，
    # 管道缓冲区写满（Windows 上只有几十 KB）后子进程就阻塞在 write 上，
    # 表现是"hub 起不来"或整个测试挂死。CI 在 Windows 上真的卡过 40 分钟。
    # 用临时文件当 sink 就没有这个问题，失败时还能读出来看。
    log_file = data / "hub-test.log"
    log_fh = open(log_file, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "zw_hub.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/healthz", timeout=1.0)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()
        out = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else ""
        pytest.fail(f"hub 没起来：\n{out[-2000:]}")
    yield base, token
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_fh.close()


@pytest.fixture(scope="module")
def core_env(tmp_path_factory, hub_url):
    """把 core 的 settings 指到一个独立数据目录 + 上面那个 hub。"""
    base, token = hub_url
    root = tmp_path_factory.mktemp("coreA")
    keys = ("ZW_ROOT", "ZW_HUB_URL", "ZW_HUB_TOKEN", "ZW_SYNC_PASSWORD",
            "ZW_ADMIN_TOKEN", "ZW_ALLOWED_CIDRS", "ZW_HOST", "ZW_GATEWAY_PUBLIC_BASE")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(
        {
            "ZW_ROOT": str(root),
            "ZW_HUB_URL": base,
            "ZW_HUB_TOKEN": token,
            "ZW_SYNC_PASSWORD": "",
            "ZW_ADMIN_TOKEN": "",
            "ZW_ALLOWED_CIDRS": "",
            # 外部环境可能设了 ZW_HOST=0.0.0.0（给跨机验收用）；
            # 这里必须回到回环，否则会触发"公开监听必须配令牌"的保护而整套失败。
            "ZW_HOST": "127.0.0.1",
            "ZW_GATEWAY_PUBLIC_BASE": "",
        }
    )
    from zw_core import config, db

    config.reset_settings()
    db.reset_db()
    yield root
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    config.reset_settings()
    db.reset_db()


PW = "test-master-pass-2026"


def _make_profile(name: str, **kw):
    from zw_core.services import manager as mgr

    payload = {"name": name, "fp_timezone": "Asia/Shanghai", "fp_lang": "zh-CN", "note": "集成测试"}
    payload.update(kw)
    return mgr.create_profile(payload)


def test_push_requires_password(core_env):
    from zw_core.services import sync as syncsvc

    p = _make_profile("sync-nopw")
    with pytest.raises(syncsvc.SyncError) as exc:
        syncsvc.push(p["id"])
    assert exc.value.code == "PASSWORD_REQUIRED"


def test_push_then_diff(core_env, hub_url):
    from zw_core.services import sync as syncsvc

    p = _make_profile("sync-a", proxy="http://10.9.9.9:7890", window_w=1440, window_h=900,
                      extensions=[], fp_hardware=8, fp_platform="windows", fp_platform_version="10.0.0")
    out = syncsvc.push(p["id"], password=PW, ensure_running=False)
    assert out["revision"] == 1
    assert out["bytes"] > 0
    assert out["pw_fp"]

    d = syncsvc.diff(p["id"], password=PW)
    assert d["remote_revision"] == 1
    assert d["local_revision"] == 1
    assert d["needs_pull"] is False
    assert d["same_password_as_last_push"] is True


def test_pull_into_fresh_machine_keeps_settings(core_env, hub_url):
    """关键回归：新机器拉过去，代理/窗口/指纹参数必须一起过去，不能是个"裸环境"。"""
    from zw_core import config
    from zw_core.services import manager as mgr
    from zw_core.services import sync as syncsvc

    src = _make_profile("sync-b", proxy="http://10.8.8.8:1080", window_w=1600, window_h=1000,
                        group_name="迁移组", tags=["t1", "t2"], fp_hardware=12,
                        fp_platform="windows", fp_platform_version="11.0.0", headless=False)
    pushed = syncsvc.push(src["id"], password=PW, ensure_running=False)
    assert pushed["revision"] == 1

    # 模拟"另一台机器"：换一个空数据目录，但连同一个 hub
    base, token = hub_url
    other = Path(str(core_env) + "-B")
    other.mkdir(exist_ok=True)
    old_root = os.environ["ZW_ROOT"]
    os.environ["ZW_ROOT"] = str(other)
    config.reset_settings()
    from zw_core import db

    db.reset_db()
    try:
        pulled = syncsvc.pull(src["id"], password=PW, create_if_missing=True)
        assert pulled["imported"] is True
        assert pulled["revision"] == 1
        fresh = mgr.get_profile(src["id"])
        assert fresh["proxy"].endswith("10.8.8.8:1080"), fresh["proxy"]
        assert fresh["window_w"] == 1600 and fresh["window_h"] == 1000
        assert fresh["fp_hardware"] == 12
        assert fresh["fp_platform"] == "windows"
        assert fresh["fp_platform_version"] == "11.0.0"
        assert fresh["group_name"] == "迁移组"
        assert fresh["tags"] == ["t1", "t2"]
        assert fresh["fp_seed"] == src["fp_seed"], "指纹种子必须一致，否则等于换了台设备"
        assert fresh["sync_revision"] == 1
    finally:
        os.environ["ZW_ROOT"] = old_root
        config.reset_settings()
        db.reset_db()


def test_wrong_password_blocked_before_write(core_env, hub_url):
    """错主密码必须在**落盘前**拦下，且本地目录内容不变。"""
    from zw_core import config
    from zw_core.services import manager as mgr
    from zw_core.services import sync as syncsvc

    src = _make_profile("sync-c", note="原始内容")
    syncsvc.push(src["id"], password=PW, ensure_running=False)

    base, token = hub_url
    other = Path(str(core_env) + "-C")
    other.mkdir(exist_ok=True)
    old_root = os.environ["ZW_ROOT"]
    os.environ["ZW_ROOT"] = str(other)
    config.reset_settings()
    from zw_core import db

    db.reset_db()
    try:
        with pytest.raises(syncsvc.SyncError) as exc:
            syncsvc.pull(src["id"], password="完全不对的口令", create_if_missing=True)
        assert exc.value.code == "KEY_FINGERPRINT_MISMATCH"
        assert exc.value.status == 412
        # 本地不该因为失败而产生半成品环境
        assert mgr.list_profiles() == [], f"失败的 pull 不应该留下环境：{mgr.list_profiles()}"
        # staging 也要清干净
        from zw_core.config import get_settings

        assert list(Path(get_settings().staging_dir).glob("*")) == []
    finally:
        os.environ["ZW_ROOT"] = old_root
        config.reset_settings()
        db.reset_db()


def test_revision_conflict_is_reported(core_env, hub_url):
    """两台机器各自递增版本会撞；必须给出可理解的错误码而不是 500。"""
    from zw_core.services import sync as syncsvc
    from zw_core.services.hub import HubClient
    from zw_core.config import get_settings

    p = _make_profile("sync-d")
    first = syncsvc.push(p["id"], password=PW, ensure_running=False)
    assert first["revision"] == 1

    s = get_settings()
    hub = HubClient(s.hub_url, s.hub_token)
    # 手工在 hub 上造一个更后面的版本，模拟"另一台机器先推了"
    hub.put_blob(p["id"], 2, b"occupado", key_fp="deadbeefdeadbeef")
    hub.publish_latest(p["id"], 2)

    second = syncsvc.push(p["id"], password=PW, ensure_running=False)
    assert second["revision"] == 3, "版本号必须取 本地/远端 的较大者 +1"


def test_remote_history_and_index(core_env):
    from zw_core.services import sync as syncsvc

    p = _make_profile("sync-e")
    syncsvc.push(p["id"], password=PW, ensure_running=False)
    hist = syncsvc.remote_history(p["id"])
    assert hist["local_revision"] >= 1
    assert hist["latest"]["revision"] >= 1
    idx = syncsvc.remote_list()
    assert any(i["profile_id"] == p["id"] for i in idx)


def test_export_import_roundtrip(core_env, tmp_path):
    """单机搬家：导出成 .zwp 再导入，设置与种子都要还原。"""
    from zw_core.services import manager as mgr

    src = _make_profile("exp-a", note="导出用", window_w=1111, window_h=777,
                        fp_timezone="America/Los_Angeles", fp_lang="en-US")
    blob, filename = mgr.export_profile(src["id"], password=PW)
    assert blob[:4] == b"ZWBN"
    assert filename.endswith(".zwp")

    # 删掉原环境，导入回来（此处不能断言 list_profiles()==[]：同一个数据目录里
    # 还有本模块其它用例建的环境，断言"全空"是测试自己的错）
    mgr.delete_profile(src["id"], purge=True)
    assert all(p["id"] != src["id"] for p in mgr.list_profiles())

    out = mgr.import_profile(blob, password=PW)
    assert out["profile"]["id"] == src["id"]
    assert out["profile"]["fp_seed"] == src["fp_seed"]
    assert out["profile"]["window_w"] == 1111
    assert out["profile"]["fp_timezone"] == "America/Los_Angeles"


def test_export_rejects_wrong_password_on_import(core_env):
    from zw_core.services import manager as mgr

    src = _make_profile("exp-b")
    blob, _ = mgr.export_profile(src["id"], password=PW)
    mgr.delete_profile(src["id"], purge=True)
    with pytest.raises(mgr.ProfileError) as exc:
        mgr.import_profile(blob, password="错的口令")
    assert exc.value.code == "KEY_FINGERPRINT_MISMATCH"
    assert exc.value.status == 412


def test_export_invalid_blob(core_env):
    from zw_core.services import manager as mgr

    with pytest.raises(mgr.ProfileError) as exc:
        mgr.import_profile(b"not a bundle at all", password=PW)
    assert exc.value.code == "IMPORT_INVALID"


def test_ephemeral_profile_protected(core_env):
    from zw_core.services import manager as mgr
    from zw_core.services import sync as syncsvc

    p = _make_profile("eph-a", ephemeral=True)
    with pytest.raises(syncsvc.SyncError) as exc:
        syncsvc.push(p["id"], password=PW, ensure_running=False)
    assert exc.value.code == "EPHEMERAL_PROTECTED"
    with pytest.raises(mgr.ProfileError) as exc2:
        mgr.export_profile(p["id"], password=PW)
    assert exc2.value.code == "EPHEMERAL_PROTECTED"


def test_hub_cas_blocks_overwrite(core_env):
    """同一版本重复上传必须被 CAS 挡住，不能静默覆盖别人的数据。"""
    from zw_core.config import get_settings
    from zw_core.services.hub import HubClient, HubError

    s = get_settings()
    hub = HubClient(s.hub_url, s.hub_token)
    pid = "cas-test-0001"
    hub.put_blob(pid, 1, b"first", key_fp="aaaaaaaaaaaaaaaa")
    with pytest.raises(HubError) as exc:
        hub.put_blob(pid, 1, b"second", key_fp="aaaaaaaaaaaaaaaa")
    assert exc.value.code == "REVISION_EXISTS"
    assert hub.get_blob(pid, 1)[0] == b"first"


def test_hub_lease_conflict(core_env):
    from zw_core.config import get_settings
    from zw_core.services.hub import HubClient, HubError

    s = get_settings()
    hub = HubClient(s.hub_url, s.hub_token)
    pid = "lease-test-0001"
    hub.acquire_lease(pid, "machine-a:1:aaa", kind="sync", ttl=60)
    with pytest.raises(HubError) as exc:
        hub.acquire_lease(pid, "machine-b:2:bbb", kind="sync", ttl=60)
    assert exc.value.code == "PROFILE_BUSY"
    # owner 自己可以重入
    hub.acquire_lease(pid, "machine-a:1:aaa", kind="sync", ttl=60)
    hub.release_lease(pid, "machine-a:1:aaa")
    assert hub.get_lease(pid) is None

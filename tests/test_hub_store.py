"""hub 存储层：CAS、租约、指针单调。"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from zw_hub.store import HubError, Store


@pytest.fixture()
def store() -> Store:
    root = Path(tempfile.mkdtemp())
    s = Store(root, keep_revisions=3)
    yield s
    shutil.rmtree(root, ignore_errors=True)


def test_cas_blocks_overwrite(store: Store):
    store.put_blob("p001", 1, b"data", key_fp="fp1")
    with pytest.raises(HubError) as exc:
        store.put_blob("p001", 1, b"other", key_fp="fp1")
    assert exc.value.code == "REVISION_EXISTS"
    # 显式关闭 CAS 才允许覆盖
    store.put_blob("p001", 1, b"other", key_fp="fp1", cas=False)
    assert store.get_blob("p001", 1) == b"other"


def test_latest_monotonic(store: Store):
    for r in (1, 2):
        store.put_blob("p001", r, b"x", key_fp="fp")
    store.set_latest("p001", 2)
    with pytest.raises(HubError) as exc:
        store.set_latest("p001", 1)
    assert exc.value.code == "REVISION_NOT_MONOTONIC"
    store.set_latest("p001", 1, force=True)
    assert store.get_latest("p001")["revision"] == 1


def test_lease_conflict_and_expiry(store: Store):
    store.acquire_lease("p001", "owner-a", "sync", ttl=60)
    with pytest.raises(HubError) as exc:
        store.acquire_lease("p001", "owner-b", "run", ttl=60)
    assert exc.value.code == "PROFILE_BUSY"
    # 同 owner 可以重复拿
    store.acquire_lease("p001", "owner-a", "sync", ttl=60)
    # 过期的租约可以被别人接管
    store.acquire_lease("p001", "owner-a", "sync", ttl=1)
    import time

    time.sleep(1.2)
    store.acquire_lease("p001", "owner-c", "sync", ttl=60)
    assert store.get_lease("p001")["owner"] == "owner-c"


def test_prune_keeps_newest(store: Store):
    for r in range(1, 6):
        store.put_blob("p001", r, b"x", key_fp="fp")
    assert store.list_revisions("p001") == [3, 4, 5]


def test_invalid_id_rejected(store: Store):
    with pytest.raises(HubError) as exc:
        store.put_blob("../evil", 1, b"x", key_fp="fp")
    assert exc.value.code == "INVALID_ID"

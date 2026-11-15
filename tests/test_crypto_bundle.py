"""加解密与打包的核心断言。

这些是"做错了会毁数据"的地方，所以测试写得比其他模块啰嗦。
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from zw_core.services import bundle, crypto


def test_key_fingerprint_stable():
    salt = b"\x00" * 16
    k1 = crypto.derive_key("same-pass", salt)
    k2 = crypto.derive_key("same-pass", salt)
    assert k1 == k2
    assert crypto.key_fingerprint(k1) == crypto.key_fingerprint(k2)
    assert len(crypto.key_fingerprint(k1)) == 16


def test_password_fingerprint_is_salt_independent():
    a = crypto.password_fingerprint("hello")
    b = crypto.password_fingerprint("hello")
    c = crypto.password_fingerprint("hello!")
    assert a == b != c
    assert len(a) == 16


def test_aead_roundtrip_and_tamper():
    key = crypto.derive_key("pw", crypto.new_salt())
    nonce, ct = crypto.encrypt(key, b"payload", aad=b"header")
    assert crypto.decrypt(key, nonce, ct, aad=b"header") == b"payload"
    # 改一个字节必须失败
    broken = bytearray(ct)
    broken[0] ^= 0xFF
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(key, nonce, bytes(broken), aad=b"header")
    # AAD 变了也必须失败
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(key, nonce, ct, aad=b"other")


@pytest.fixture()
def fake_udd() -> Path:
    tmp = Path(tempfile.mkdtemp())
    udd = tmp / "udd"
    (udd / "Default/Local Storage/leveldb").mkdir(parents=True)
    (udd / "Default/Local Storage/leveldb/000003.log").write_bytes(b"x" * 2048)
    (udd / "Default/Preferences").write_text('{"x":1}')
    (udd / "Default/Cookies").write_bytes("DPAPI-密文".encode())
    (udd / "Default/Network").mkdir(parents=True)
    (udd / "Default/Network/Cookies").write_bytes("密文".encode())
    (udd / "Default/Cache").mkdir(parents=True)
    (udd / "Default/Cache/data_0").write_bytes(b"cache")
    (udd / "DevToolsActivePort").write_text("21001\n/devtools/browser/x")
    (udd / "Local State").write_text("{}")
    yield udd
    shutil.rmtree(tmp, ignore_errors=True)


def test_exclusions(fake_udd: Path):
    files, _ = bundle.collect_files(fake_udd, cross_machine=True)
    rels = {f.relative_to(fake_udd).as_posix() for f in files}
    assert "Default/Local Storage/leveldb/000003.log" in rels, "LevelDB 的数据文件不能被当日志误杀"
    assert "Default/Preferences" in rels
    for bad in ("Default/Cookies", "Default/Network/Cookies", "DevToolsActivePort", "Default/Cache/data_0"):
        assert bad not in rels, f"{bad} 必须被排除"


def test_pack_unpack_roundtrip(fake_udd: Path):
    blob, man = bundle.pack(
        fake_udd, "pid1", "环境一", 3, "pw-123456",
        fingerprint={"seed": 7, "platform": "linux"},
        cookies=[{"name": "a", "value": "b"}],
        storage={"https://x.com": {"k": "v"}},
        source_node="node-test",
    )
    header = bundle.inspect_blob(blob)
    assert header["revision"] == 3
    raw, man2 = bundle.unpack(blob, "pw-123456", expected_key_fp=header["key_fp"])
    assert man2.name == "环境一"
    assert man2.cookie_count == 1
    assert man2.fingerprint["seed"] == 7

    dest = fake_udd.parent / "out"
    root = bundle.extract_to(raw, dest)
    cookies, storage, manifest = bundle.read_side_files(dest)
    assert cookies[0]["name"] == "a"
    assert list(storage) == ["https://x.com"]
    assert manifest["revision"] == 3
    assert (root / "Default/Preferences").exists()


def test_wrong_password_rejected_before_write(fake_udd: Path):
    blob, _ = bundle.pack(fake_udd, "pid1", "x", 1, "right-pass", fingerprint={"seed": 1})
    with pytest.raises(bundle.BundleError) as exc:
        bundle.unpack(blob, "wrong-pass")
    assert exc.value.code == "KEY_FINGERPRINT_MISMATCH"


def test_unsafe_path_rejected():
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        payload = b"evil"
        info = tarfile.TarInfo(name="udd/../../etc/passwd")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        man = json.dumps({"profile_id": "p", "name": "n", "revision": 1, "created_at": 0,
                          "source_node": "", "engine_version": "", "fingerprint": {}}).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(man)
        tar.addfile(info, io.BytesIO(man))

    with pytest.raises(bundle.BundleError) as exc:
        bundle.extract_to(buf.getvalue(), Path(tempfile.mkdtemp()))
    assert exc.value.code == "UNSAFE_PATH"

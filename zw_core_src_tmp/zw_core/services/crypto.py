"""加密原语：主密码 → 密钥 → AEAD。

选择的理由（都写在文档里，这里只重复一句）：
- **Argon2id**：当前口令派生的标准，抗 GPU/ASIC。
- **XChaCha20-Poly1305**：192 位随机 nonce，不需要额外维护计数器，随机 nonce 不会撞。
- **密钥指纹**：`sha256(key)[:8]` 的 16 位 hex。它写进快照头，**不泄漏密钥**，
  但能让目标机在读任何数据之前就判断"这个包我能不能解"。这一步是硬门禁：
  解不开就立刻中止，绝不能让浏览器带着错密钥启动（否则 Chromium 会把 cookie 当损坏删掉）。
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass

from argon2.low_level import Type, hash_secret_raw
from nacl.bindings import crypto_aead_xchacha20poly1305_ietf_decrypt, crypto_aead_xchacha20poly1305_ietf_encrypt

# Argon2id 参数：单次派生约 0.4–1.2 秒，按机器性能浮动
DEFAULT_M_COST_KIB = 256 * 1024  # 256 MiB
DEFAULT_T_COST = 3
DEFAULT_PARALLELISM = 4
SALT_BYTES = 16
KEY_BYTES = 32
NONCE_BYTES = 24


class CryptoError(Exception):
    """解密 / 校验类错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class KdfParams:
    m_cost_kib: int = DEFAULT_M_COST_KIB
    t_cost: int = DEFAULT_T_COST
    parallelism: int = DEFAULT_PARALLELISM

    def to_dict(self) -> dict[str, int]:
        return {"m_cost_kib": self.m_cost_kib, "t_cost": self.t_cost, "parallelism": self.parallelism}


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_BYTES)


def derive_key(password: str, salt: bytes, params: KdfParams | None = None) -> bytes:
    """从主密码派生 32 字节密钥。同样的 password+salt+params 一定得到同一个 key。"""
    if not password:
        raise CryptoError("EMPTY_PASSWORD", "主密码为空，无法派生密钥")
    p = params or KdfParams()
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=p.t_cost,
        memory_cost=p.m_cost_kib,
        parallelism=p.parallelism,
        hash_len=KEY_BYTES,
        type=Type.ID,
    )


def password_fingerprint(password: str) -> str:
    """**稳定的**口令指纹：同一串口令永远得到同一个值，与 salt 无关。

    用途：跨机判断"这台机器用的是不是你那个主密码"，以及 hub 侧展示/对比。
    注意它和下面 `key_fingerprint` 不同 —— 后者是"某个包用某个 salt 派生出的密钥"的指纹，
    每打包一次都不一样（因为 salt 是随机的，这是必要的：salt 必须随包走，对端才能解密）。
    """
    return hashlib.sha256(b"zhiwen/pw-fingerprint/v1:" + password.encode("utf-8")).hexdigest()[:16]


def key_fingerprint(key: bytes) -> str:
    """密钥指纹：16 位 hex。用于"我能不能解这个包"的前置判断。"""
    return hashlib.sha256(key).digest()[:8].hex()


def encrypt(key: bytes, plaintext: bytes, aad: bytes = b"") -> tuple[bytes, bytes]:
    """返回 (nonce, ciphertext)。nonce 必须随包一起存。"""
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct = crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, aad, nonce, key)
    return nonce, ct


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes:
    try:
        return crypto_aead_xchacha20poly1305_ietf_decrypt(ciphertext, aad, nonce, key)
    except Exception as exc:  # nacl 的异常类型随版本变化，统一收敛
        raise CryptoError("DECRYPT_FAILED", "解密失败：主密码不对，或这个包不是用这台机器的主密码加密的") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: os.PathLike[str] | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

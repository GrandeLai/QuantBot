"""AES-256-GCM 加密/解密工具.

用于策略文件的本地加密存储。
密钥通过 keyring 或环境变量管理，不明文写入文件。
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-256 需要 32 字节密钥
KEY_SIZE = 32
# GCM nonce 大小
NONCE_SIZE = 12


def generate_key() -> bytes:
    """生成随机 AES-256 密钥."""
    return os.urandom(KEY_SIZE)


def encrypt(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM 加密.

    Args:
        data: 明文字节
        key: 32 字节 AES 密钥

    Returns:
        加密后的字节：nonce(12B) + ciphertext + tag(16B)
    """
    if len(key) != KEY_SIZE:
        msg = f"密钥长度必须为 {KEY_SIZE} 字节，实际 {len(key)} 字节"
        raise ValueError(msg)

    aesgcm = AESGCM(key)
    nonce = os.urandom(NONCE_SIZE)
    ciphertext = aesgcm.encrypt(nonce, data, associated_data=None)
    return nonce + ciphertext


def decrypt(data: bytes, key: bytes) -> bytes:
    """AES-256-GCM 解密.

    Args:
        data: 密文字节（nonce + ciphertext + tag）
        key: 32 字节 AES 密钥

    Returns:
        解密后的明文字节
    """
    if len(key) != KEY_SIZE:
        msg = f"密钥长度必须为 {KEY_SIZE} 字节，实际 {len(key)} 字节"
        raise ValueError(msg)
    if len(data) < NONCE_SIZE:
        raise ValueError("密文过短，无法解密")

    nonce = data[:NONCE_SIZE]
    ciphertext = data[NONCE_SIZE:]

    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None)


def encrypt_text(text: str, key: bytes) -> str:
    """加密文本，返回 base64 编码的密文."""
    encrypted = encrypt(text.encode("utf-8"), key)
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_text(encoded: str, key: bytes) -> str:
    """解密 base64 编码的密文，返回明文字符串."""
    encrypted = base64.b64decode(encoded.encode("ascii"))
    return decrypt(encrypted, key).decode("utf-8")


def derive_key_from_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    """从密码派生 AES-256 密钥（PBKDF2-HMAC-SHA256）.

    Args:
        password: 用户密码
        salt: 16 字节盐值（None 则随机生成）

    Returns:
        (key, salt) 元组
    """
    import hashlib

    if salt is None:
        salt = os.urandom(16)

    key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations=100_000,
        dklen=KEY_SIZE,
    )
    return key, salt

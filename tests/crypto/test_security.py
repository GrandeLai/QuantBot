"""安全模块测试 — T-1.6 验收."""

import os
from unittest.mock import patch

import pytest

from quantbot.common.security.encryption import (
    KEY_SIZE,
    decrypt,
    decrypt_text,
    derive_key_from_password,
    encrypt,
    encrypt_text,
    generate_key,
)
from quantbot.common.security.keystore import load_api_key, save_api_key


class TestEncryption:
    def test_generate_key_length(self) -> None:
        key = generate_key()
        assert len(key) == KEY_SIZE  # 32 bytes

    def test_encrypt_decrypt_roundtrip(self) -> None:
        key = generate_key()
        plaintext = b"Hello, QuantBot! \x00\x01\x02"
        ciphertext = encrypt(plaintext, key)
        decrypted = decrypt(ciphertext, key)
        assert decrypted == plaintext

    def test_ciphertext_differs_from_plaintext(self) -> None:
        key = generate_key()
        plaintext = b"secret data"
        ciphertext = encrypt(plaintext, key)
        assert ciphertext != plaintext

    def test_encrypt_twice_produces_different_ciphertext(self) -> None:
        """每次加密使用不同 nonce，密文应不同."""
        key = generate_key()
        plaintext = b"same data"
        c1 = encrypt(plaintext, key)
        c2 = encrypt(plaintext, key)
        assert c1 != c2  # nonce 随机，密文不同

    def test_wrong_key_raises(self) -> None:
        key1 = generate_key()
        key2 = generate_key()
        plaintext = b"secret"
        ciphertext = encrypt(plaintext, key1)
        with pytest.raises(Exception):  # noqa: B017 — InvalidTag from cryptography
            decrypt(ciphertext, key2)

    def test_invalid_key_length_raises(self) -> None:
        with pytest.raises(ValueError, match="密钥长度"):
            encrypt(b"data", b"short_key")

    def test_encrypt_decrypt_text(self) -> None:
        key = generate_key()
        text = "策略代码：def on_bar(bar): pass\n中文内容 🚀"
        encoded = encrypt_text(text, key)
        decoded = decrypt_text(encoded, key)
        assert decoded == text

    def test_large_data(self) -> None:
        """测试大文件加密（模拟策略文件）."""
        key = generate_key()
        large_data = b"x" * 100_000  # 100KB
        encrypted = encrypt(large_data, key)
        decrypted = decrypt(encrypted, key)
        assert decrypted == large_data

    def test_derive_key_from_password(self) -> None:
        key, salt = derive_key_from_password("my_secure_password")
        assert len(key) == KEY_SIZE
        assert len(salt) == 16

    def test_derive_key_deterministic_with_same_salt(self) -> None:
        password = "test_password"
        salt = os.urandom(16)
        key1, _ = derive_key_from_password(password, salt)
        key2, _ = derive_key_from_password(password, salt)
        assert key1 == key2

    def test_derive_key_different_passwords_differ(self) -> None:
        salt = os.urandom(16)
        key1, _ = derive_key_from_password("password1", salt)
        key2, _ = derive_key_from_password("password2", salt)
        assert key1 != key2


class TestKeystore:
    @patch("keyring.set_password")
    @patch("keyring.get_password")
    def test_save_and_load_via_keyring(
        self, mock_get: object, mock_set: object
    ) -> None:
        import keyring  # type: ignore[import]
        keyring.get_password.return_value = "sk-test-api-key-12345"  # type: ignore[attr-defined]

        save_api_key("openai", "sk-test-api-key-12345")
        result = load_api_key("openai")
        assert result == "sk-test-api-key-12345"

    def test_load_from_env_when_keyring_fails(self) -> None:
        """keyring 不可用时从环境变量读取."""
        with patch("keyring.get_password", side_effect=Exception("keyring unavailable")):
            with patch.dict(os.environ, {"QUANTBOT_OPENAI": "env-api-key"}):
                result = load_api_key("openai")
                assert result == "env-api-key"

    def test_load_returns_none_when_not_found(self) -> None:
        with patch("keyring.get_password", return_value=None):
            result = load_api_key("nonexistent_provider_xyz")
            assert result is None

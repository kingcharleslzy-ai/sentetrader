"""快期密码托管：AES-256-GCM，AAD 绑定 user_id，主密钥只来自密钥文件。

密码明文只出现在 bind 请求与池的 start/verify 调用里；库里只有密文+nonce。
没有密钥文件就拒绝托管（bind 直接不可用），绝不降级成明文存储。
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CREDENTIAL_KEY_FILE_ENV = "AI_TRADER_CREDENTIAL_KEY_FILE"
KEY_BYTES = 32
NONCE_BYTES = 12
MAX_PASSWORD_BYTES = 400


class CredentialKeyError(RuntimeError):
    """主密钥缺失/格式错误，或密文无法在该 user_id 下打开。"""


class CredentialBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise CredentialKeyError("credential_key_must_be_32_bytes")
        self._aesgcm = AESGCM(key)

    @classmethod
    def from_env(cls) -> CredentialBox | None:
        """`AI_TRADER_CREDENTIAL_KEY_FILE` 指向 32 字节 base64 文件；未配置返回
        None（池未启用），配置了但读不了/解不出是硬错误。"""
        path = os.getenv(CREDENTIAL_KEY_FILE_ENV, "")
        if not path:
            return None
        try:
            key = base64.b64decode(Path(path).read_bytes().strip(), validate=True)
        except (OSError, ValueError) as error:
            raise CredentialKeyError("credential_key_file_invalid") from error
        return cls(key)

    def seal(self, *, user_id: str, password: str) -> tuple[bytes, bytes]:
        if not user_id:
            raise CredentialKeyError("credential_user_required")
        if not password or len(password.encode()) > MAX_PASSWORD_BYTES:
            raise CredentialKeyError("credential_password_invalid")
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, password.encode(), user_id.encode())
        return ciphertext, nonce

    def open(self, *, user_id: str, ciphertext: bytes, nonce: bytes) -> str:
        try:
            return self._aesgcm.decrypt(nonce, ciphertext, user_id.encode()).decode()
        except (InvalidTag, UnicodeDecodeError) as error:
            raise CredentialKeyError("credential_decrypt_failed") from error

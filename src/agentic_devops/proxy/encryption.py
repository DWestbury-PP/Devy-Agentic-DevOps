"""Symmetric encryption for secrets at rest (Phase 9b).

Per-host MCP bearer tokens are encrypted with Fernet (AES-128-CBC + HMAC) using a
key from ``DEVY_ENCRYPTION_KEY`` (a urlsafe-base64 32-byte key — generate with
``agentic-devops admin gen-key``). If the key is unset the cipher is *disabled*:
hosts can still be registered, but a host that needs a token can't be stored or
reached until a key is configured.
"""

from __future__ import annotations

import os
from typing import Optional


class TokenCipher:
    def __init__(self, key: Optional[str]) -> None:
        self._fernet = None
        if key:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: Optional[str]) -> Optional[bytes]:
        if not plaintext:
            return None
        if self._fernet is None:
            raise RuntimeError("DEVY_ENCRYPTION_KEY is not set; cannot store secrets")
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, blob: Optional[bytes]) -> Optional[str]:
        if not blob:
            return None
        if self._fernet is None:
            raise RuntimeError("DEVY_ENCRYPTION_KEY is not set; cannot read stored secrets")
        return self._fernet.decrypt(bytes(blob)).decode("utf-8")


def cipher_from_env() -> TokenCipher:
    return TokenCipher(os.environ.get("DEVY_ENCRYPTION_KEY"))

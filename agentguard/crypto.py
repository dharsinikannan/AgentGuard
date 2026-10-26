"""Encryption at rest for stored OAuth tokens, and hashing for tokens we only verify.

Two different needs:
  - Tokens we must *use* later (a GitHub token, the gateway's token for a downstream
    server) are encrypted (Fernet: AES-128-CBC + HMAC). A Redis dump alone is useless
    without the key, which lives in the environment, not in Redis.
  - Tokens we only *check* (access/refresh tokens we issued) are stored as SHA-256
    hashes, like passwords: a dump can't be replayed, and we never need the original.

Generate a key:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, key: str | bytes):
        self._fernet = Fernet(key)

    @classmethod
    def from_env(cls, var: str) -> TokenCipher:
        key = os.environ.get(var)
        if not key:
            raise RuntimeError(
                f"{var} is not set. Generate one with:\n  python -c \"from cryptography.fernet "
                "import Fernet; print(Fernet.generate_key().decode())\"\nand put it in .env"
            )
        return cls(key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str | None:
        """None if the data was encrypted with another key or tampered with."""
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            return None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

import secrets

import anyio
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet

_PASSWORD_HASHER = PasswordHasher()
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


class SecretVault:
    """Encrypt credentials that providers need in their original form."""

    def __init__(self, encryption_key: str) -> None:
        self._fernet = Fernet(encryption_key.encode("ascii"))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")


async def hash_password(password: str) -> str:
    # Password hashing is deliberately costly; do not block the HTTP event loop.
    return await anyio.to_thread.run_sync(_PASSWORD_HASHER.hash, password)


async def verify_password(password: str, password_hash: str | None) -> bool:
    def verify() -> bool:
        try:
            _PASSWORD_HASHER.verify(password_hash or _DUMMY_PASSWORD_HASH, password)
        except VerificationError, InvalidHashError:
            return False
        return password_hash is not None

    return await anyio.to_thread.run_sync(verify)

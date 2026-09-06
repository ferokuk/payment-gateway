import pytest
from cryptography.fernet import Fernet, InvalidToken
from src.contexts.merchants.infrastructure.secrets import (
    SecretVault,
    hash_password,
    verify_password,
)


def test_provider_secrets_can_be_recovered_only_with_the_configured_key() -> None:
    encryption_key = Fernet.generate_key().decode("ascii")
    vault = SecretVault(encryption_key)
    secret = " provider-secret-with-significant-whitespace "
    first = vault.encrypt(secret)
    second = vault.encrypt(secret)
    assert secret not in first
    assert first != second
    assert vault.decrypt(first) == secret
    assert vault.decrypt(second) == secret
    assert encryption_key not in repr(vault)
    with pytest.raises(InvalidToken):
        SecretVault(Fernet.generate_key().decode("ascii")).decrypt(first)


def test_modified_provider_ciphertext_is_rejected() -> None:
    vault = SecretVault(Fernet.generate_key().decode("ascii"))
    encrypted = vault.encrypt("provider-secret")
    with pytest.raises(InvalidToken):
        vault.decrypt(encrypted[:20] + ("A" if encrypted[20] != "A" else "B") + encrypted[21:])


@pytest.mark.anyio
async def test_password_hashes_are_salted_and_preserve_whitespace() -> None:
    password = " exact merchant password "
    first = await hash_password(password)
    second = await hash_password(password)
    assert first != second
    assert password not in first
    assert first.startswith("$argon2id$")
    assert await verify_password(password, first)
    assert not await verify_password(password.strip(), first)
    assert not await verify_password("incorrect password", first)


@pytest.mark.anyio
@pytest.mark.parametrize("stored_hash", [None, "invalid-hash"])
async def test_unknown_or_corrupt_account_cannot_authenticate(stored_hash: str | None) -> None:
    assert not await verify_password("merchant-password", stored_hash)

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from src.contexts.merchants.application.authentication import AuthenticateAPIKey
from src.contexts.merchants.application.public import MerchantIdentity
from src.contexts.merchants.domain.api_key import (
    APIKey,
    credential_digest,
    credential_id,
    issue_api_key,
)
from src.contexts.merchants.domain.merchant import LEGACY_MERCHANT_ID


class MemoryCredentials:
    def __init__(self) -> None:
        self.keys: dict[UUID, APIKey] = {}
        self.active: dict[UUID, bool] = {}

    async def find_key(self, key_id: UUID) -> tuple[APIKey, bool] | None:
        key = self.keys.get(key_id)
        return (key, self.active.get(key.merchant_id, True)) if key is not None else None

    async def find_legacy_key(self, digest: str) -> tuple[APIKey, bool] | None:
        for key in self.keys.values():
            if key.is_legacy and key.secret_digest == digest:
                return key, self.active.get(key.merchant_id, True)
        return None


def test_issued_keys_have_public_id_and_256_bit_secret_with_safe_repr() -> None:
    merchant_id = uuid4()
    now = datetime.now(UTC)
    first = issue_api_key(merchant_id, "primary", now)
    second = issue_api_key(merchant_id, "rotation", now)
    assert credential_id(first.token) == first.key.id
    assert len(first.token.split(".")[1]) == 43
    assert first.key.merchant_id == merchant_id
    assert first.key.secret_digest == credential_digest(first.token)
    assert len(first.key.secret_digest) == 64
    assert first.token != second.token
    assert first.key.secret_digest != second.key.secret_digest
    assert first.token not in repr(first)
    assert first.key.secret_digest not in repr(first)
    assert first.key.secret_digest not in repr(first.key)


def test_identity_is_immutable() -> None:
    identity = MerchantIdentity(uuid4(), uuid4())
    with pytest.raises(FrozenInstanceError):
        identity.merchant_id = uuid4()  # type: ignore[misc]


@pytest.mark.parametrize(
    "expires_at",
    [datetime(2020, 1, 1), datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1)],
)
def test_issuance_rejects_past_and_naive_expiration(expires_at: datetime) -> None:
    with pytest.raises(ValueError):
        issue_api_key(uuid4(), "key", datetime(2026, 1, 1, tzinfo=UTC), expires_at)


def test_expiry_boundary_and_revocation_reject_credential() -> None:
    now = datetime.now(UTC)
    expiry = now + timedelta(minutes=1)
    issued = issue_api_key(uuid4(), "expiring", now, expiry)
    assert issued.key.authenticates(credential_digest(issued.token), now)
    assert not issued.key.authenticates(credential_digest(issued.token), expiry)
    assert not replace(issued.key, revoked_at=now).authenticates(issued.key.secret_digest, now)
    assert not issued.key.authenticates(credential_digest("wrong"), now)


@pytest.mark.anyio
async def test_rotation_preserves_merchant_and_revocation_takes_effect_immediately() -> None:
    repository = MemoryCredentials()
    authenticate = AuthenticateAPIKey(repository)
    now = datetime.now(UTC)
    owner = uuid4()
    old = issue_api_key(owner, "old", now)
    new = issue_api_key(owner, "new", now)
    other = issue_api_key(uuid4(), "other", now)
    for issued in (old, new, other):
        repository.keys[issued.key.id] = issued.key
    assert await authenticate(old.token) == MerchantIdentity(owner, old.key.id)
    assert await authenticate(new.token) == MerchantIdentity(owner, new.key.id)
    assert await authenticate(other.token) == MerchantIdentity(other.key.merchant_id, other.key.id)
    repository.keys[old.key.id] = replace(old.key, revoked_at=now)
    assert await authenticate(old.token) is None
    assert await authenticate(new.token) == MerchantIdentity(owner, new.key.id)
    repository.active[owner] = False
    assert await authenticate(new.token) is None
    assert await authenticate(other.token) is not None
    repository.active[owner] = True
    assert await authenticate(old.token) is None
    assert await authenticate(new.token) is not None


@pytest.mark.anyio
async def test_expired_key_cannot_authenticate() -> None:
    repository = MemoryCredentials()
    issued = issue_api_key(
        uuid4(),
        "expired",
        datetime.now(UTC) - timedelta(days=2),
        datetime.now(UTC) - timedelta(days=1),
    )
    repository.keys[issued.key.id] = issued.key
    assert await AuthenticateAPIKey(repository)(issued.token) is None


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "", "invalid", "ключ", "pg_invalid", "test-api-key"])
async def test_missing_unknown_malformed_and_unimported_legacy_are_rejected(
    token: str | None,
) -> None:
    assert await AuthenticateAPIKey(MemoryCredentials())(token) is None


@pytest.mark.anyio
async def test_public_key_id_without_correct_secret_cannot_impersonate_owner() -> None:
    repository = MemoryCredentials()
    first = issue_api_key(uuid4(), "first", datetime.now(UTC))
    second = issue_api_key(uuid4(), "second", datetime.now(UTC))
    repository.keys[first.key.id] = first.key
    repository.keys[second.key.id] = second.key
    forged = first.token.split(".")[0] + "." + second.token.split(".")[1]
    assert await AuthenticateAPIKey(repository)(forged) is None


@pytest.mark.anyio
@pytest.mark.parametrize("looks_like_new_token", [True, False])
async def test_only_explicit_legacy_import_authenticates_unchanged_header(
    looks_like_new_token: bool,
) -> None:
    repository = MemoryCredentials()
    token = (
        issue_api_key(uuid4(), "unrelated", datetime.now(UTC)).token
        if looks_like_new_token
        else "old arbitrary global key"
    )
    authenticate = AuthenticateAPIKey(repository)
    assert await authenticate(token) is None
    key = APIKey(
        id=uuid4(),
        merchant_id=LEGACY_MERCHANT_ID,
        secret_digest=credential_digest(token),
        label="migration",
        created_at=datetime.now(UTC),
        is_legacy=True,
    )
    repository.keys[key.id] = key
    assert await authenticate(token) == MerchantIdentity(LEGACY_MERCHANT_ID, key.id)
    repository.keys[key.id] = replace(key, revoked_at=datetime.now(UTC))
    assert await authenticate(token) is None

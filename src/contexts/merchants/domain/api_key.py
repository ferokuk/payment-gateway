import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

_TOKEN_PATTERN = re.compile(r"pg_([0-9a-f]{32})\.[A-Za-z0-9_-]{43}\Z")


def credential_digest(token: str) -> str:
    """Hash high-entropy credentials; never retain the plaintext in storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def credential_id(token: str) -> UUID | None:
    match = _TOKEN_PATTERN.fullmatch(token)
    return UUID(hex=match[1]) if match else None


def validate_expiration(created_at: datetime, expires_at: datetime | None) -> None:
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("Creation time must have a timezone")
    if expires_at is not None:
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("Expiration time must have a timezone")
        if expires_at <= created_at:
            raise ValueError("Expiration time must be in the future")


@dataclass(frozen=True, slots=True)
class APIKey:
    id: UUID
    merchant_id: UUID
    secret_digest: str = field(repr=False)
    label: str
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    is_legacy: bool = False

    def authenticates(self, digest: str, now: datetime) -> bool:
        # Compare even when revoked/expired. All failures have one public response.
        matches = secrets.compare_digest(self.secret_digest, digest)
        return (
            matches
            and self.revoked_at is None
            and (self.expires_at is None or now < self.expires_at)
        )


@dataclass(frozen=True, slots=True)
class IssuedAPIKey:
    key: APIKey
    token: str = field(repr=False)


def issue_api_key(
    merchant_id: UUID, label: str, now: datetime, expires_at: datetime | None = None
) -> IssuedAPIKey:
    validate_expiration(now, expires_at)
    key_id = uuid4()
    token = f"pg_{key_id.hex}.{secrets.token_urlsafe(32)}"
    return IssuedAPIKey(
        key=APIKey(
            id=key_id,
            merchant_id=merchant_id,
            secret_digest=credential_digest(token),
            label=label,
            created_at=now,
            expires_at=expires_at,
        ),
        token=token,
    )

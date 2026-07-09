from uuid import UUID

# .compat returns the standard uuid.UUID (not the native uuid_utils.UUID);
# otherwise Pydantic (UUID7) and SQLAlchemy (PG_UUID(as_uuid=True)) reject the value.
from uuid_utils.compat import uuid7


def new_uuid() -> UUID:
    """Generates an entity identifier as UUIDv7 (RFC 9562).

    v7 carries a Unix-time (ms) prefix -> values are nearly monotonic, giving
    better locality of the primary-key B-tree index in PostgreSQL than a
    random v4. The id generation strategy lives in a single place so it can
    be changed (v4/v7/other) without touching the domain or infrastructure.
    """
    return uuid7()

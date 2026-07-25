"""add not_accepted_by_provider refund failure reason

Revision ID: 8c113a4030ae
Revises: c72b9e889990
Create Date: 2026-07-25 15:27:32.769230

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c113a4030ae"
down_revision: str | Sequence[str] | None = "c72b9e889990"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # SQLAlchemy stores enum members by NAME, so the stored labels are uppercase.
    # ADD VALUE inside a transaction is allowed since PostgreSQL 12; the new
    # label only cannot be used before this transaction commits, and nothing
    # here writes it.
    op.execute("ALTER TYPE refundfailurereasons ADD VALUE IF NOT EXISTS 'NOT_ACCEPTED_BY_PROVIDER'")


def downgrade() -> None:
    """Downgrade schema."""
    # PostgreSQL cannot drop a single value from an enum, so the type is
    # rebuilt without it. Refunds already closed with this reason have no
    # equivalent in the old type: the cast fails loudly rather than silently
    # rewriting why the money came back.
    op.execute(
        "CREATE TYPE refundfailurereasons_old AS ENUM "
        "('TIMEOUT', 'CARD_UNAVAILABLE', 'INSUFFICIENT_MERCHANT_BALANCE')"
    )
    op.execute(
        "ALTER TABLE refunds ALTER COLUMN failure_reason TYPE refundfailurereasons_old "
        "USING failure_reason::text::refundfailurereasons_old"
    )
    op.execute("DROP TYPE refundfailurereasons")
    op.execute("ALTER TYPE refundfailurereasons_old RENAME TO refundfailurereasons")

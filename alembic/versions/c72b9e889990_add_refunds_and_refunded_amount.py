"""add refunds and refunded_amount

Revision ID: c72b9e889990
Revises: cbb6eea6f7a4
Create Date: 2026-07-17 22:34:35.092086

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c72b9e889990"
down_revision: str | Sequence[str] | None = "cbb6eea6f7a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "payments",
        sa.Column(
            "refunded_amount",
            sa.Numeric(precision=20, scale=2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_payments_refunded_amount_within_amount",
        "payments",
        "refunded_amount >= 0 AND refunded_amount <= amount",
    )
    op.create_table(
        "refunds",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(precision=20, scale=2), nullable=False),
        sa.Column(
            "status",
            sa.Enum("CREATED", "PENDING", "SUCCESS", "FAILED", "ERROR", name="refund_status"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "failure_reason",
            sa.Enum(
                "TIMEOUT",
                "CARD_UNAVAILABLE",
                "INSUFFICIENT_MERCHANT_BALANCE",
                name="refundfailurereasons",
            ),
            nullable=True,
        ),
        sa.Column("error_message", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_refunds_payment_id"), "refunds", ["payment_id"])
    op.create_table(
        "refund_idempotency_keys",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("refund_id", sa.UUID(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["refund_id"], ["refunds.id"]),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("refund_idempotency_keys")
    op.drop_index(op.f("ix_refunds_payment_id"), table_name="refunds")
    op.drop_table("refunds")
    op.drop_constraint("ck_payments_refunded_amount_within_amount", "payments")
    op.drop_column("payments", "refunded_amount")
    # drop_table does not drop the enum types; without these drops a repeated
    # upgrade fails on CREATE TYPE with DuplicateObjectError.
    sa.Enum(name="refund_status").drop(op.get_bind())
    sa.Enum(name="refundfailurereasons").drop(op.get_bind())

"""Schedule refund reconciliation retries.

Revision ID: f4b2a6c8d901
Revises: 8c113a4030ae
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4b2a6c8d901"
down_revision: str | Sequence[str] | None = "8c113a4030ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL uses created_at as the first due time, including existing refunds.
    op.add_column("refunds", sa.Column("next_reconcile_at", sa.DateTime(timezone=True)))
    op.add_column(
        "refunds",
        sa.Column("reconciliation_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_refunds_reconciliation_attempts", "refunds", "reconciliation_attempts >= 0"
    )
    op.create_index(
        "ix_refunds_reconciliation_due",
        "refunds",
        [sa.text("coalesce(next_reconcile_at, created_at)"), "id"],
        postgresql_where=sa.text("status IN ('CREATED', 'PENDING', 'ERROR')"),
    )


def downgrade() -> None:
    op.drop_index("ix_refunds_reconciliation_due", table_name="refunds")
    op.drop_constraint("ck_refunds_reconciliation_attempts", "refunds", type_="check")
    op.drop_column("refunds", "reconciliation_attempts")
    op.drop_column("refunds", "next_reconcile_at")

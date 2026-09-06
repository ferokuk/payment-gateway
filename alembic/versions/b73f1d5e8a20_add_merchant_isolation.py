"""Add merchant ownership and independently revocable API credentials.

Revision ID: b73f1d5e8a20
Revises: f4b2a6c8d901

Run with the API and workers stopped. No environment credential is read or
imported by this migration; an operator must explicitly import the old API key.
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "b73f1d5e8a20"
down_revision: str | Sequence[str] | None = "f4b2a6c8d901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_MERCHANT_ID = UUID("00000000-0000-4000-8000-000000000001")
OWNED_TABLES = ("payments", "refunds", "idempotency_keys", "refund_idempotency_keys")


def upgrade() -> None:
    op.create_table(
        "merchants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "merchant_api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("secret_digest", sa.String(64), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("is_legacy", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_merchant_api_keys_merchant_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("secret_digest", name="uq_merchant_api_keys_secret_digest"),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="ck_merchant_api_keys_expiry"
        ),
        sa.CheckConstraint(
            f"NOT is_legacy OR merchant_id = '{LEGACY_MERCHANT_ID}'::uuid",
            name="ck_merchant_api_keys_legacy_owner",
        ),
    )
    op.create_index("ix_merchant_api_keys_merchant_id", "merchant_api_keys", ["merchant_id"])
    op.create_index(
        "uq_merchant_api_keys_legacy",
        "merchant_api_keys",
        ["is_legacy"],
        unique=True,
        postgresql_where=sa.text("is_legacy"),
    )
    op.execute(
        sa.text("INSERT INTO merchants (id, name) VALUES (:id, :name)").bindparams(
            id=LEGACY_MERCHANT_ID, name="Legacy merchant"
        )
    )

    # Add nullable columns only while backfilling, and never add owner defaults.
    # Child ownership follows the parent so all snapshots and operation links
    # remain intact, even for initiated but still unresolved operations.
    for table in OWNED_TABLES:
        op.add_column(table, sa.Column("merchant_id", sa.UUID(), nullable=True))
    op.execute(sa.text("UPDATE payments SET merchant_id = :id").bindparams(id=LEGACY_MERCHANT_ID))
    op.execute(
        "UPDATE refunds AS r SET merchant_id = p.merchant_id "
        "FROM payments AS p WHERE p.id = r.payment_id"
    )
    op.execute(
        "UPDATE idempotency_keys AS k SET merchant_id = p.merchant_id "
        "FROM payments AS p WHERE p.id = k.payment_id"
    )
    op.execute(
        "UPDATE refund_idempotency_keys AS k SET merchant_id = r.merchant_id "
        "FROM refunds AS r WHERE r.id = k.refund_id"
    )
    for table in OWNED_TABLES:
        op.alter_column(table, "merchant_id", existing_type=sa.UUID(), nullable=False)
        op.create_foreign_key(
            f"fk_{table}_merchant_id",
            table,
            "merchants",
            ["merchant_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    op.create_unique_constraint("uq_payments_merchant_id_id", "payments", ["merchant_id", "id"])
    op.create_unique_constraint("uq_refunds_merchant_id_id", "refunds", ["merchant_id", "id"])
    op.create_index("ix_refunds_merchant_payment_id", "refunds", ["merchant_id", "payment_id"])

    for table, parent, column, constraint in (
        ("refunds", "payments", "payment_id", "fk_refunds_merchant_payment"),
        ("idempotency_keys", "payments", "payment_id", "fk_idempotency_keys_merchant_payment"),
        (
            "refund_idempotency_keys",
            "refunds",
            "refund_id",
            "fk_refund_idempotency_keys_merchant_refund",
        ),
    ):
        op.drop_constraint(f"{table}_{column}_fkey", table, type_="foreignkey")
        op.create_foreign_key(
            constraint, table, parent, ["merchant_id", column], ["merchant_id", "id"]
        )
    for table in ("idempotency_keys", "refund_idempotency_keys"):
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, ["merchant_id", "key"])


def downgrade() -> None:
    # A global-key schema cannot represent multiple tenants or rotated/new
    # credentials or revocation/expiry policy. Refuse before changing schema or
    # deleting data; old binaries must not resurrect deliberately blocked access.
    # An active, unexpired imported old key is recoverable from operator custody.
    op.execute(
        """
        DO $$
        DECLARE
            legacy_id CONSTANT uuid := '00000000-0000-4000-8000-000000000001';
        BEGIN
            IF EXISTS (SELECT 1 FROM merchants WHERE id <> legacy_id OR NOT is_active)
               OR EXISTS (SELECT 1 FROM merchant_api_keys WHERE NOT is_legacy OR revoked_at IS NOT NULL OR expires_at IS NOT NULL)
               OR EXISTS (SELECT 1 FROM payments WHERE merchant_id <> legacy_id)
               OR EXISTS (SELECT 1 FROM refunds WHERE merchant_id <> legacy_id)
               OR EXISTS (SELECT 1 FROM idempotency_keys WHERE merchant_id <> legacy_id)
               OR EXISTS (SELECT 1 FROM refund_idempotency_keys WHERE merchant_id <> legacy_id)
            THEN
                RAISE EXCEPTION 'Cannot downgrade merchant isolation: tenant data, new credentials or access restrictions cannot be preserved; export or explicitly migrate them first';
            END IF;
        END $$;
        """
    )

    for table in ("idempotency_keys", "refund_idempotency_keys"):
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, ["key"])
    for table, parent, column, constraint in (
        ("refunds", "payments", "payment_id", "fk_refunds_merchant_payment"),
        ("idempotency_keys", "payments", "payment_id", "fk_idempotency_keys_merchant_payment"),
        (
            "refund_idempotency_keys",
            "refunds",
            "refund_id",
            "fk_refund_idempotency_keys_merchant_refund",
        ),
    ):
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(f"{table}_{column}_fkey", table, parent, [column], ["id"])
    op.drop_index("ix_refunds_merchant_payment_id", table_name="refunds")
    op.drop_constraint("uq_refunds_merchant_id_id", "refunds", type_="unique")
    op.drop_constraint("uq_payments_merchant_id_id", "payments", type_="unique")
    for table in OWNED_TABLES:
        op.drop_constraint(f"fk_{table}_merchant_id", table, type_="foreignkey")
        op.drop_column(table, "merchant_id")
    op.drop_table("merchant_api_keys")
    op.drop_table("merchants")

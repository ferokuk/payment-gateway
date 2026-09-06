"""Add merchant accounts, provider credentials and profile sessions.

Revision ID: c91e4a7b6d20
Revises: b73f1d5e8a20

Existing merchants, API keys and payment ownership remain intact. Profile
accounts are created through registration; no login or secret is synthesized
for legacy records.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c91e4a7b6d20"
down_revision: str | Sequence[str] | None = "b73f1d5e8a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("merchants", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_table(
        "merchant_accounts",
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("api_key_id", sa.UUID(), nullable=False),
        sa.Column("encrypted_api_key", sa.Text(), nullable=False),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("webhook_url", sa.String(2048)),
        sa.Column("retry_max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column(
            "retry_window_seconds", sa.Integer(), nullable=False, server_default=sa.text("60")
        ),
        sa.PrimaryKeyConstraint("merchant_id"),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_merchant_accounts_merchant_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["merchant_api_keys.id"],
            name="fk_merchant_accounts_api_key_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("email", name="uq_merchant_accounts_email"),
        sa.CheckConstraint(
            "retry_max_attempts BETWEEN 1 AND 100", name="ck_merchant_accounts_retry_max_attempts"
        ),
        sa.CheckConstraint(
            "retry_window_seconds BETWEEN 1 AND 86400",
            name="ck_merchant_accounts_retry_window_seconds",
        ),
    )
    op.create_table(
        "merchant_provider_credentials",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_merchant_provider_credentials_merchant_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > created_at",
            name="ck_merchant_provider_credentials_expiry",
        ),
    )
    op.create_index(
        "ix_merchant_provider_credentials_merchant_id",
        "merchant_provider_credentials",
        ["merchant_id"],
    )
    op.create_index(
        "uq_merchant_provider_credentials_current",
        "merchant_provider_credentials",
        ["merchant_id"],
        unique=True,
        postgresql_where=sa.text("valid_until IS NULL"),
    )
    op.create_table(
        "merchant_sessions",
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("merchant_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("token_digest"),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name="fk_merchant_sessions_merchant_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_merchant_sessions_expiry"),
    )
    op.create_index("ix_merchant_sessions_merchant_id", "merchant_sessions", ["merchant_id"])


def downgrade() -> None:
    # Old code cannot retain profile credentials, sessions or soft-deletion
    # state. Refuse before dropping anything so account and access policy data
    # must be migrated explicitly by an operator.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM merchant_accounts)
               OR EXISTS (SELECT 1 FROM merchant_provider_credentials)
               OR EXISTS (SELECT 1 FROM merchant_sessions)
               OR EXISTS (SELECT 1 FROM merchants WHERE deleted_at IS NOT NULL)
            THEN
                RAISE EXCEPTION 'Cannot downgrade merchant profiles: accounts, credentials, sessions or deleted profiles cannot be preserved; export or explicitly migrate them first';
            END IF;
        END $$;
        """
    )
    op.drop_index("ix_merchant_sessions_merchant_id", table_name="merchant_sessions")
    op.drop_table("merchant_sessions")
    op.drop_index(
        "uq_merchant_provider_credentials_current", table_name="merchant_provider_credentials"
    )
    op.drop_index(
        "ix_merchant_provider_credentials_merchant_id", table_name="merchant_provider_credentials"
    )
    op.drop_table("merchant_provider_credentials")
    op.drop_table("merchant_accounts")
    op.drop_column("merchants", "deleted_at")

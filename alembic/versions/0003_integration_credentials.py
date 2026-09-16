"""Per-tenant integration credentials.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-15

One tenant-scoped table, so it needs the same three things every other
tenant-scoped table got: the grant to ``suliko_app``, RLS enabled and FORCEd,
and the ``tenant_isolation`` policy. A credentials table readable across
tenants would hand one bureau another's API keys, which makes this the single
worst table in the schema to get that wrong on.

Explicit operations, not ``create_all`` — see 0001 for why that revision is
the only one allowed to build from metadata, and what went wrong when it did
so unscoped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "suliko_app"

NEW_TABLES = ("integration_credentials",)

#: Kept in step with `IntegrationProvider`. Adding one is an ALTER of this
#: constraint in a new revision, which is the reason the column is a VARCHAR
#: with a CHECK rather than a native PostgreSQL enum.
PROVIDERS = (
    "google_drive",
    "bog_ecommerce",
    "bog_business",
    "sms_office",
    "smtp",
    "elevenlabs",
    "recaptcha",
    "api24",
)


def _existing_tables() -> set[str]:
    """Tables the database already has.

    Same guard as 0002: a database migrated before 0001 was scoped may already
    contain tables a later revision owns, and re-creating one is a hard
    failure. Empty when there is no real connection, so the migration-parity
    test still observes every call.
    """
    try:
        return set(sa.inspect(op.get_bind()).get_table_names())
    except Exception:
        return set()


def upgrade() -> None:
    existing = _existing_tables()

    def create_table(name: str, *args: Any, **kwargs: Any) -> None:
        if name not in existing:
            op.create_table(name, *args, **kwargs)

    def create_index(name: str, table: str, columns: list[str], **kwargs: Any) -> None:
        if table not in existing:
            op.create_index(name, table, columns, **kwargs)

    create_table(
        "integration_credentials",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # JSONB, not JSON: the non-secret config is queried and diffed, and
        # JSONB is the one that can be indexed if that is ever needed.
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # The encrypted blob: nonce || ciphertext || tag, from core.crypto.
        sa.Column("secrets", sa.LargeBinary(), nullable=True),
        sa.Column("secrets_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("secrets_updated_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_ok", sa.Boolean(), nullable=True),
        sa.Column("last_check_detail", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN (" + ", ".join(f"'{p}'" for p in PROVIDERS) + ")",
            name="ck_integration_credentials_integration_provider",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_integration_credentials_tenant_id_tenants",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["secrets_updated_by_user_id"],
            ["users.id"],
            name="fk_integration_credentials_secrets_updated_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration_credentials"),
        # One row per provider per tenant. Without this, a double-submitted
        # form gives a bureau two sets of Drive credentials and no way to know
        # which one the app will pick up.
        sa.UniqueConstraint("tenant_id", "provider", name="uq_integration_tenant_provider"),
    )
    create_index("ix_integration_credentials_tenant_id", "integration_credentials", ["tenant_id"])

    for table in NEW_TABLES:
        op.execute(sa.text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"))
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        # FORCE so the table owner is subject to the policy too — without it,
        # running as the owner silently disables isolation.
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"""
                CREATE POLICY tenant_isolation ON {table}
                USING (
                    tenant_id = NULLIF(current_setting('suliko.tenant_id', true), '')::bigint
                )
                WITH CHECK (
                    tenant_id = NULLIF(current_setting('suliko.tenant_id', true), '')::bigint
                )
                """
            )
        )


def downgrade() -> None:
    for table in NEW_TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.drop_index("ix_integration_credentials_tenant_id", table_name="integration_credentials")
    op.drop_table("integration_credentials")

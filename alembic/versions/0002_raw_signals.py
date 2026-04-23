"""raw_signals table

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-22

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "raw_signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column(
            "query_context",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source", "source_id", name="uq_raw_signals_source_id"),
    )
    op.create_index("ix_raw_signals_source", "raw_signals", ["source"])
    op.create_index(
        "ix_raw_signals_processed_at", "raw_signals", ["processed_at"]
    )
    # Partial index for the common "give me unprocessed rows" query in tier-1.
    op.create_index(
        "ix_raw_signals_unprocessed",
        "raw_signals",
        ["source", "created_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_raw_signals_unprocessed", table_name="raw_signals")
    op.drop_index("ix_raw_signals_processed_at", table_name="raw_signals")
    op.drop_index("ix_raw_signals_source", table_name="raw_signals")
    op.drop_table("raw_signals")

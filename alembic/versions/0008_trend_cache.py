"""trend_cache: 7-day cache of pytrends lookups

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-22

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "trend_cache",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("keyword", sa.String(200), nullable=False),
        sa.Column(
            "locale", sa.String(10), nullable=False, server_default=sa.text("'en-US'")
        ),
        sa.Column(
            "timeframe",
            sa.String(30),
            nullable=False,
            server_default=sa.text("'today 3-m'"),
        ),
        sa.Column("geo", sa.String(10), nullable=False, server_default=sa.text("''")),
        sa.Column("slope", sa.Float(asdecimal=False)),
        sa.Column(
            "raw_values",
            postgresql.ARRAY(sa.Float(asdecimal=False)),
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "keyword", "locale", "timeframe", "geo", name="uq_trend_cache_key"
        ),
    )
    op.create_index(
        "ix_trend_cache_expires_at", "trend_cache", ["expires_at"]
    )
    op.create_index("ix_trend_cache_keyword", "trend_cache", ["keyword"])


def downgrade() -> None:
    op.drop_index("ix_trend_cache_keyword", table_name="trend_cache")
    op.drop_index("ix_trend_cache_expires_at", table_name="trend_cache")
    op.drop_table("trend_cache")

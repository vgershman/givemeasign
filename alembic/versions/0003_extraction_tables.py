"""extraction tables: pain_signals + usage_log

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-22

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # pain_signals — raw SQL because VECTOR type isn't first-class in alembic.
    op.execute(
        """
        CREATE TABLE pain_signals (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            raw_signal_id UUID NOT NULL REFERENCES raw_signals(id) ON DELETE CASCADE,
            source_ref VARCHAR(100) NOT NULL,
            text TEXT NOT NULL,
            strength DOUBLE PRECISION NOT NULL,
            topic_tags VARCHAR[] NOT NULL DEFAULT ARRAY[]::VARCHAR[],
            locale VARCHAR(5) NOT NULL DEFAULT 'en',
            extractor_version VARCHAR(20) NOT NULL,
            embedding VECTOR(1536),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.create_index(
        "ix_pain_signals_raw_signal_id", "pain_signals", ["raw_signal_id"]
    )
    op.create_index("ix_pain_signals_created_at", "pain_signals", ["created_at"])
    op.create_index("ix_pain_signals_strength", "pain_signals", ["strength"])
    # Vector index deferred until M3 — we don't query by similarity yet.

    # usage_log — every LLM/API call, the foundation of the budget dashboard.
    op.create_table(
        "usage_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False, server_default="chat"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "usd_cost", sa.Float(asdecimal=False), nullable=False, server_default="0"
        ),
        sa.Column(
            "meta",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
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
    )
    op.create_index("ix_usage_log_stage", "usage_log", ["stage"])
    op.create_index("ix_usage_log_created_at", "usage_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_log_created_at", table_name="usage_log")
    op.drop_index("ix_usage_log_stage", table_name="usage_log")
    op.drop_table("usage_log")
    op.drop_index("ix_pain_signals_strength", table_name="pain_signals")
    op.drop_index("ix_pain_signals_created_at", table_name="pain_signals")
    op.drop_index("ix_pain_signals_raw_signal_id", table_name="pain_signals")
    op.execute("DROP TABLE pain_signals CASCADE")

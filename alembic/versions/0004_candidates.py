"""candidates + candidate_signals

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-22

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # candidates — synthesized startup ideas. Raw SQL for the VECTOR column.
    op.execute(
        """
        CREATE TABLE candidates (
            id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            concept TEXT NOT NULL,
            target_user TEXT,
            value_prop TEXT,
            angles VARCHAR[] NOT NULL DEFAULT ARRAY[]::VARCHAR[],
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            status VARCHAR(20) NOT NULL DEFAULT 'synthesized',
            synthesizer_version VARCHAR(30) NOT NULL,
            embedding VECTOR(1536),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.create_index("ix_candidates_created_at", "candidates", ["created_at"])
    op.create_index("ix_candidates_status", "candidates", ["status"])
    op.create_index("ix_candidates_confidence", "candidates", ["confidence"])
    # Vector similarity index for cross-day dedup lookups.
    op.execute(
        "CREATE INDEX ix_candidates_embedding "
        "ON candidates USING hnsw (embedding vector_cosine_ops)"
    )

    # candidate_signals — M:N link between a candidate and the pain_signals that formed it.
    op.create_table(
        "candidate_signals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "pain_signal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pain_signals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "candidate_id", "pain_signal_id", name="uq_candidate_pain_link"
        ),
    )
    op.create_index(
        "ix_candidate_signals_candidate_id",
        "candidate_signals",
        ["candidate_id"],
    )
    op.create_index(
        "ix_candidate_signals_pain_signal_id",
        "candidate_signals",
        ["pain_signal_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_candidate_signals_pain_signal_id", table_name="candidate_signals"
    )
    op.drop_index(
        "ix_candidate_signals_candidate_id", table_name="candidate_signals"
    )
    op.drop_table("candidate_signals")
    op.execute("DROP INDEX IF EXISTS ix_candidates_embedding")
    op.drop_index("ix_candidates_confidence", table_name="candidates")
    op.drop_index("ix_candidates_status", table_name="candidates")
    op.drop_index("ix_candidates_created_at", table_name="candidates")
    op.execute("DROP TABLE candidates CASCADE")

"""scoring: scores table + candidates scoring columns

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-22

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Extend candidates for scoring outcomes.
    op.add_column("candidates", sa.Column("aggregate_score", sa.Float(), nullable=True))
    op.add_column("candidates", sa.Column("gate_failed", sa.String(50), nullable=True))
    op.add_column(
        "candidates",
        sa.Column("dedup_of", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "candidates",
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_candidates_dedup_of",
        "candidates",
        "candidates",
        ["dedup_of"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_candidates_aggregate_score", "candidates", ["aggregate_score"]
    )
    op.create_index("ix_candidates_scored_at", "candidates", ["scored_at"])

    # scores — one row per (candidate × dimension × scorer_version).
    op.create_table(
        "scores",
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
        sa.Column("dimension", sa.String(30), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("reasoning", sa.Text()),
        sa.Column("scorer_version", sa.String(30), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
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
        sa.UniqueConstraint(
            "candidate_id",
            "dimension",
            "scorer_version",
            name="uq_score_cand_dim_ver",
        ),
    )
    op.create_index("ix_scores_candidate_id", "scores", ["candidate_id"])
    op.create_index("ix_scores_dimension", "scores", ["dimension"])


def downgrade() -> None:
    op.drop_index("ix_scores_dimension", table_name="scores")
    op.drop_index("ix_scores_candidate_id", table_name="scores")
    op.drop_table("scores")
    op.drop_index("ix_candidates_scored_at", table_name="candidates")
    op.drop_index("ix_candidates_aggregate_score", table_name="candidates")
    op.drop_constraint("fk_candidates_dedup_of", "candidates", type_="foreignkey")
    op.drop_column("candidates", "scored_at")
    op.drop_column("candidates", "dedup_of")
    op.drop_column("candidates", "gate_failed")
    op.drop_column("candidates", "aggregate_score")

"""candidates.origin: tag the stream that produced each candidate

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-23

B-stream (default, 'pains'): Sonnet clustered pain_signals into a candidate.
A-stream ('hypothesis'): Sonnet ideated directly from a seed theme without
source pain threads. Used for future filtering + M7 weight-learner features.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "candidates",
        sa.Column(
            "origin",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pains'"),
        ),
    )
    op.create_index("ix_candidates_origin", "candidates", ["origin"])


def downgrade() -> None:
    op.drop_index("ix_candidates_origin", table_name="candidates")
    op.drop_column("candidates", "origin")

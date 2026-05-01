"""candidates.last_delivered_at + last_delivered_score (anti-repeat)

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-29

The daily deck used to re-send the same top-10 day after day because the only
exclusion was per-user swipes — anything the user *ignored* (didn't tap) came
back forever, and `aggregate_score` is frozen after first scoring.

Track per-candidate delivery so the deck-fetch can hard-exclude already-shown
ideas, with a single materially-improved-score escape hatch (so a candidate
that re-scores ≥10% higher can come back).

`last_delivered_at` doubles as a sweepable index for "stale delivery" cleanup
if we ever want to age out very old entries.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "candidates",
        sa.Column(
            "last_delivered_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "candidates",
        sa.Column("last_delivered_score", sa.Float(), nullable=True),
    )
    # Hot path: deck query filters by (status='scored' AND last_delivered_at IS NULL
    # OR aggregate_score > last_delivered_score * 1.10). A partial index on the
    # null subset is the most common case and keeps the query cheap.
    op.create_index(
        "ix_candidates_undelivered_scored",
        "candidates",
        ["aggregate_score"],
        postgresql_where=sa.text(
            "status = 'scored' AND last_delivered_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_candidates_undelivered_scored", table_name="candidates")
    op.drop_column("candidates", "last_delivered_score")
    op.drop_column("candidates", "last_delivered_at")

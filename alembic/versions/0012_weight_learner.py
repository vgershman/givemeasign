"""bot_settings: dimension_weights + weights_updated_at + weights_swipe_count

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-23

Stores learned per-dimension weights (a dict keyed by dimension name → float),
the timestamp of the last retrain, and the swipe count used for that retrain.
Empty dict = uniform (default).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "bot_settings",
        sa.Column(
            "dimension_weights",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "bot_settings",
        sa.Column("weights_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "bot_settings",
        sa.Column(
            "weights_swipe_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("bot_settings", "weights_swipe_count")
    op.drop_column("bot_settings", "weights_updated_at")
    op.drop_column("bot_settings", "dimension_weights")

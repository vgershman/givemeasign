"""bot_settings.trends_enabled: runtime toggle for Google Trends enrichment

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-22

pytrends gets rate-limited quickly from datacenter IPs. Trends enrichment was
also contributing almost no signal on niche B2B queries. Default the toggle
to FALSE — flip on only when a candidate shape (consumer product, high-volume
search terms) actually benefits, or swap Ahrefs as the evidence source.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "bot_settings",
        sa.Column(
            "trends_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("bot_settings", "trends_enabled")

"""translations: candidates.translations, research_packs.translations, bot_settings.display_locale

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-23

DB stays English for scoring + debugging + LLM prompts. `translations` is
keyed by locale code (e.g. "ru") and contains translated versions of the
fields rendered on Telegram. Populated lazily at delivery time.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "candidates",
        sa.Column(
            "translations",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "research_packs",
        sa.Column(
            "translations",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "bot_settings",
        sa.Column(
            "display_locale",
            sa.String(5),
            nullable=False,
            server_default=sa.text("'en'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("bot_settings", "display_locale")
    op.drop_column("research_packs", "translations")
    op.drop_column("candidates", "translations")

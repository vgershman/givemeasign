"""telegram: swipe_verdicts + bot_settings

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-22

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # swipe_verdicts — one row per (candidate × user). Right/left/super/snooze.
    op.create_table(
        "swipe_verdicts",
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
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.String(20), nullable=False),
        sa.Column("deck_date", sa.Date(), nullable=False),
        sa.Column(
            "swiped_at",
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
            "candidate_id", "user_id", name="uq_verdict_cand_user"
        ),
    )
    op.create_index(
        "ix_swipe_verdicts_user_id", "swipe_verdicts", ["user_id"]
    )
    op.create_index(
        "ix_swipe_verdicts_deck_date", "swipe_verdicts", ["deck_date"]
    )
    op.create_index(
        "ix_swipe_verdicts_verdict", "swipe_verdicts", ["verdict"]
    )

    # bot_settings — single-row table (id=1) for runtime-toggleable flags.
    op.create_table(
        "bot_settings",
        sa.Column("id", sa.Integer(), primary_key=True, default=1),
        sa.Column(
            "telegram_log_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "telegram_log_min_level",
            sa.String(10),
            nullable=False,
            server_default=sa.text("'INFO'"),
        ),
        sa.Column(
            "daily_deck_hour",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("23"),
        ),
        sa.Column(
            "daily_deck_minute",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "daily_deck_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
        sa.CheckConstraint("id = 1", name="ck_bot_settings_singleton"),
    )
    # Seed the singleton row.
    op.execute("INSERT INTO bot_settings (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("bot_settings")
    op.drop_index("ix_swipe_verdicts_verdict", table_name="swipe_verdicts")
    op.drop_index("ix_swipe_verdicts_deck_date", table_name="swipe_verdicts")
    op.drop_index("ix_swipe_verdicts_user_id", table_name="swipe_verdicts")
    op.drop_table("swipe_verdicts")

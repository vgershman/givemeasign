"""research_packs: Opus-generated deep-research packs

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-22

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "research_packs",
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
            unique=True,
        ),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "content_json",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("summary", sa.Text()),
        sa.Column("recommendation", sa.String(10)),
        sa.Column("generator_version", sa.String(30), nullable=False),
        sa.Column("model", sa.String(80)),
        sa.Column(
            "input_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "output_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "usd_cost",
            sa.Float(asdecimal=False),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "triggered_by_verdict_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("swipe_verdicts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
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
    op.create_index(
        "ix_research_packs_status", "research_packs", ["status"]
    )
    op.create_index(
        "ix_research_packs_candidate_id", "research_packs", ["candidate_id"]
    )
    op.create_index(
        "ix_research_packs_sent_at", "research_packs", ["sent_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_research_packs_sent_at", table_name="research_packs")
    op.drop_index("ix_research_packs_candidate_id", table_name="research_packs")
    op.drop_index("ix_research_packs_status", table_name="research_packs")
    op.drop_table("research_packs")

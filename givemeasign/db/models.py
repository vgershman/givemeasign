"""ORM models.

Append-only raw_signals is the foundation; extraction outputs pain_signals;
Sonnet synthesis clusters pains into candidates (linked via candidate_signals);
Haiku scoring produces per-dimension scores and aggregate_score; every
LLM/API call lands in usage_log for budget accounting.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from datetime import date as date_type
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""


class TimestampMixin:
    """created_at / updated_at columns managed by the DB."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UUIDPrimaryKey:
    """UUID primary key, application-generated (uuid4)."""

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)


class RawSignal(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "raw_signals"

    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text())
    query_context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_raw_signals_source_id"),
    )

    def __repr__(self) -> str:
        return f"<RawSignal {self.source}:{self.source_id}>"


class PainSignal(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "pain_signals"

    raw_signal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("raw_signals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_ref: Mapped[str] = mapped_column(String(100), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    strength: Mapped[float] = mapped_column(Float, nullable=False)
    topic_tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    locale: Mapped[str] = mapped_column(String(5), nullable=False, default="en")
    extractor_version: Mapped[str] = mapped_column(String(20), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)

    def __repr__(self) -> str:
        return f"<PainSignal {self.strength:.2f} {self.text[:40]!r}>"


class Candidate(Base, UUIDPrimaryKey, TimestampMixin):
    """A synthesized startup idea.

    status lifecycle:
      synthesized → scored | gated_out | deduplicated
      (later: shortlisted, presented, right_swiped, left_swiped, hidden)
    """

    __tablename__ = "candidates"

    concept: Mapped[str] = mapped_column(Text, nullable=False)
    target_user: Mapped[str | None] = mapped_column(Text)
    value_prop: Mapped[str | None] = mapped_column(Text)
    angles: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="synthesized")
    synthesizer_version: Mapped[str] = mapped_column(String(30), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)

    # Scoring outcomes (added in M4).
    aggregate_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    gate_failed: Mapped[str | None] = mapped_column(String(50), nullable=True)
    dedup_of: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="SET NULL"),
        nullable=True,
    )
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Candidate {self.status} agg={self.aggregate_score} {self.concept[:50]!r}>"


class CandidateSignal(Base, UUIDPrimaryKey):
    """M:N link between a candidate and the pain_signals that formed it."""

    __tablename__ = "candidate_signals"

    candidate_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pain_signal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("pain_signals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "candidate_id", "pain_signal_id", name="uq_candidate_pain_link"
        ),
    )


class Score(Base, UUIDPrimaryKey, TimestampMixin):
    """One score row per (candidate × dimension × scorer_version).

    Long-form for auditability — you can see exactly which prompt version
    produced which numbers, and re-score without dropping history.
    """

    __tablename__ = "scores"

    candidate_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dimension: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text)
    scorer_version: Mapped[str] = mapped_column(String(30), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "dimension",
            "scorer_version",
            name="uq_score_cand_dim_ver",
        ),
    )


class SwipeVerdict(Base, UUIDPrimaryKey, TimestampMixin):
    """One row per (candidate × user) — your decision on that candidate.

    verdict ∈ {right, left, super, snooze}.
    The (candidate_id, user_id) unique constraint prevents double-swipes;
    re-clicking the same button is a no-op at insert time.
    """

    __tablename__ = "swipe_verdicts"

    candidate_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    verdict: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    deck_date: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    swiped_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("candidate_id", "user_id", name="uq_verdict_cand_user"),
    )


class BotSettings(Base):
    """Singleton (id=1) row for runtime-toggleable bot config.

    Kept in DB instead of env vars so toggles take effect immediately
    without a process restart.
    """

    __tablename__ = "bot_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    telegram_log_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    telegram_log_min_level: Mapped[str] = mapped_column(
        String(10), nullable=False, default="INFO"
    )
    daily_deck_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=23)
    daily_deck_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_deck_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (CheckConstraint("id = 1", name="ck_bot_settings_singleton"),)


class ResearchPack(Base, UUIDPrimaryKey, TimestampMixin):
    """Opus-generated deep research pack, one per candidate.

    Created when user right/super-swipes. Status lifecycle:
      pending → generating → complete → sent  (happy path)
      pending → generating → failed           (Opus or render error)

    `content_json` is the raw structured output (tldr, incumbents, build_plan,
    etc.). `summary` and `recommendation` are short fields pulled out for the
    Telegram preview + future scoring use.
    """

    __tablename__ = "research_packs"

    candidate_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("candidates.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    content_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    summary: Mapped[str | None] = mapped_column(Text)
    recommendation: Mapped[str | None] = mapped_column(String(10))
    generator_version: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str | None] = mapped_column(String(80))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usd_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    triggered_by_verdict_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("swipe_verdicts.id", ondelete="SET NULL"),
        nullable=True,
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class TrendCache(Base, UUIDPrimaryKey):
    """7-day cache of pytrends lookups.

    Keyed by (keyword, locale, timeframe, geo). `slope` is a normalized 0–1
    value (0.5 = flat); `raw_values` stores the weekly interest series so
    we can recompute slope with a different algorithm later without
    re-querying pytrends.
    """

    __tablename__ = "trend_cache"

    keyword: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    locale: Mapped[str] = mapped_column(String(10), nullable=False, default="en-US")
    timeframe: Mapped[str] = mapped_column(String(30), nullable=False, default="today 3-m")
    geo: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    slope: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_values: Mapped[list[float] | None] = mapped_column(ARRAY(Float), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "keyword", "locale", "timeframe", "geo", name="uq_trend_cache_key"
        ),
    )


class UsageLog(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "usage_log"

    stage: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False, default="chat")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usd_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

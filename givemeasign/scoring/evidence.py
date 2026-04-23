"""Build the evidence pack the scoring LLM sees.

For M4a the only evidence source is pain_signals already linked to the
candidate — pure-DB, zero external calls. The evidence pack also carries
a mechanical `demand_baseline` derived from pain count + strength so the
scorer has a concrete anchor for the demand dimension, rather than
guessing a mid-range value.

M4b will extend this with Google Trends slopes, and the eventual Ahrefs
integration will add keyword-volume + KD data.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from givemeasign.db.models import Candidate, CandidateSignal, PainSignal
from givemeasign.llm.prompts.derive_keywords import (
    SYSTEM_PROMPT as DERIVE_KW_SYSTEM,
)
from givemeasign.llm.prompts.derive_keywords import (
    render_user_prompt as render_derive_kw_prompt,
)
from givemeasign.llm.router import LLMRouter, Tier
from givemeasign.observability.logging import logger
from givemeasign.observability.usage import record_usage
from givemeasign.sources.google_trends import fetch_trend_slopes


@dataclass
class EvidenceStats:
    """Mechanical summary of a candidate's pain_signals."""

    pain_count: int
    avg_strength: float
    max_strength: float
    # Demand baseline: geometric mean of a log-scaled count factor and avg strength.
    # Ranges roughly 0.2 (few weak pains) to 0.9 (many strong pains).
    demand_baseline: float


def compute_stats(pains: list[PainSignal]) -> EvidenceStats:
    if not pains:
        return EvidenceStats(0, 0.0, 0.0, 0.0)
    strengths = [p.strength for p in pains]
    pain_count = len(pains)
    avg_s = sum(strengths) / pain_count
    max_s = max(strengths)
    # Log-scaled count: 1 pain = 0.27, 3 = 0.63, 5 = 0.82, 8+ = 1.0.
    count_factor = min(1.0, math.log1p(pain_count) / math.log1p(8))
    demand_baseline = math.sqrt(count_factor * avg_s)
    return EvidenceStats(
        pain_count=pain_count,
        avg_strength=round(avg_s, 3),
        max_strength=round(max_s, 3),
        demand_baseline=round(demand_baseline, 2),
    )


@dataclass
class Evidence:
    candidate: Candidate
    pains: list[PainSignal] = field(default_factory=list)
    stats: EvidenceStats = field(default_factory=lambda: EvidenceStats(0, 0.0, 0.0, 0.0))
    # Populated by M4b+ as more evidence types come online.
    trend_slopes: dict[str, float] = field(default_factory=dict)
    keyword_volumes: dict[str, int] = field(default_factory=dict)


def build_evidence(s: Session, candidate_id: UUID) -> Evidence | None:
    """Load candidate + linked pains + stats from DB. Returns None if candidate missing."""
    c = s.get(Candidate, candidate_id)
    if c is None:
        return None
    pain_stmt = (
        select(PainSignal)
        .join(CandidateSignal, CandidateSignal.pain_signal_id == PainSignal.id)
        .where(CandidateSignal.candidate_id == candidate_id)
        .order_by(PainSignal.strength.desc())
    )
    pains = list(s.execute(pain_stmt).scalars().all())
    return Evidence(candidate=c, pains=pains, stats=compute_stats(pains))


def pains_for_prompt(pains: list[PainSignal], *, cap: int = 15) -> list[dict]:
    """Serialize pains to the dict shape the scoring prompt expects."""
    out = []
    for p in pains[:cap]:
        out.append(
            {
                "strength": p.strength,
                "topic_tags": p.topic_tags or [],
                "text": p.text,
            }
        )
    return out


# ---------- keyword derivation + trend enrichment ----------


def _parse_keyword_list(text: str) -> list[str]:
    """Lenient JSON-array parse of Haiku's keyword-derivation response."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    if not s.startswith("["):
        start = s.find("[")
        end = s.rfind("]")
        if start >= 0 and end > start:
            s = s[start : end + 1]
    try:
        data = json.loads(s)
    except json.JSONDecodeError as e:
        logger.warning(f"derive_keywords: JSON parse failed ({e}); raw={text[:150]!r}")
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        if not isinstance(item, str):
            continue
        kw = item.strip().lower()[:60]
        if 2 <= len(kw) <= 60:
            out.append(kw)
    # Dedup preserving order
    seen: set[str] = set()
    deduped = []
    for kw in out:
        if kw not in seen:
            seen.add(kw)
            deduped.append(kw)
    return deduped[:5]


async def derive_keywords(
    candidate: Candidate,
    router: LLMRouter,
) -> list[str]:
    """One Haiku call → 3–5 short search queries for this candidate."""
    try:
        response = await router.chat(
            tier=Tier.T1,
            system=DERIVE_KW_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": render_derive_kw_prompt(
                        concept=candidate.concept,
                        target_user=candidate.target_user,
                    ),
                }
            ],
            max_tokens=200,
            temperature=0.2,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"derive_keywords: LLM call failed: {e}")
        return []

    record_usage(
        stage="derive_keywords",
        provider="anthropic",
        model=response.model,
        operation="chat",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        usd_cost=response.usd_cost,
        meta={"candidate_id": str(candidate.id)},
    )
    return _parse_keyword_list(response.text)


# Slopes in this band (roughly "flat noise") carry no signal — Google Trends
# simply doesn't have enough search volume on niche B2B queries to detect a
# real trajectory. We drop them before handing evidence to the scorer so the
# LLM doesn't over-interpret 0.504 vs 0.518 as meaningful.
_TREND_NO_SIGNAL_LOW = 0.40
_TREND_NO_SIGNAL_HIGH = 0.60


async def enrich_with_trends(evidence: Evidence, router: LLMRouter) -> None:
    """Populate `evidence.trend_slopes` via keyword derivation + pytrends.

    Always safe to call. Every failure mode (LLM down, pytrends blocked,
    empty trend data) results in an empty `trend_slopes` dict and a warning
    log. The scoring prompt renders "Google Trends: (unavailable)" in that
    case — scoring continues normally.

    Slopes in the 0.40–0.60 "noise floor" band are dropped: on low-volume
    niche queries pytrends returns essentially-flat series and those slopes
    are not real signal.
    """
    try:
        keywords = await derive_keywords(evidence.candidate, router)
        if not keywords:
            logger.info(
                f"enrich_with_trends: no keywords derived for {evidence.candidate.id}"
            )
            return
        raw_slopes = await fetch_trend_slopes(keywords)
        informative = {
            kw: slope
            for kw, slope in raw_slopes.items()
            if slope < _TREND_NO_SIGNAL_LOW or slope > _TREND_NO_SIGNAL_HIGH
        }
        evidence.trend_slopes = informative
        logger.info(
            f"enrich_with_trends: {len(informative)} informative / "
            f"{len(raw_slopes)} total / {len(keywords)} derived "
            f"for {evidence.candidate.id}"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"enrich_with_trends failed for {evidence.candidate.id}: {e}")

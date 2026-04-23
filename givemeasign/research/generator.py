"""Opus-driven deep-research pack generator."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from givemeasign.db.models import (
    Candidate,
    CandidateSignal,
    PainSignal,
    ResearchPack,
    Score,
)
from givemeasign.db.session import session_scope
from givemeasign.llm.prompts.deep_research import (
    RESEARCH_VERSION,
    SYSTEM_PROMPT,
    render_user_prompt,
)
from givemeasign.llm.prompts.score_candidate import SCORER_VERSION
from givemeasign.llm.router import LLMRouter, Tier
from givemeasign.observability.logging import logger
from givemeasign.observability.usage import record_usage


@dataclass
class GenerationResult:
    pack_id: UUID
    candidate_id: UUID
    content: dict
    summary: str
    recommendation: str | None
    usd_cost: float
    model: str


# ---------- response parsing ----------


def _clean_json_block(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    if not s.startswith("{"):
        start = s.find("{")
        end = s.rfind("}")
        if start >= 0 and end > start:
            s = s[start : end + 1]
    return s


def _parse_response(text: str) -> dict:
    try:
        data = json.loads(_clean_json_block(text))
    except json.JSONDecodeError as e:
        logger.warning(f"research: JSON parse failed ({e}); raw={text[:300]!r}")
        return {}
    return data if isinstance(data, dict) else {}


# ---------- pack row lifecycle ----------


def ensure_pending_pack(
    candidate_id: UUID,
    triggered_by_verdict_id: UUID | None = None,
) -> UUID:
    """Insert a pending row (or return existing one's id). Idempotent."""
    with session_scope() as s:
        existing = s.execute(
            select(ResearchPack).where(ResearchPack.candidate_id == candidate_id)
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id

        row = ResearchPack(
            candidate_id=candidate_id,
            status="pending",
            generator_version=RESEARCH_VERSION,
            triggered_by_verdict_id=triggered_by_verdict_id,
        )
        s.add(row)
        s.flush()
        return row.id


def _load_inputs(candidate_id: UUID) -> tuple[dict, list[dict], dict] | None:
    """Build the dicts the prompt renderer expects. None if candidate missing."""
    with session_scope() as s:
        c = s.get(Candidate, candidate_id)
        if c is None:
            return None
        pains = list(
            s.execute(
                select(PainSignal)
                .join(CandidateSignal, CandidateSignal.pain_signal_id == PainSignal.id)
                .where(CandidateSignal.candidate_id == candidate_id)
                .order_by(PainSignal.strength.desc())
            ).scalars()
        )
        score_rows = list(
            s.execute(
                select(Score)
                .where(Score.candidate_id == candidate_id)
                .where(Score.scorer_version == SCORER_VERSION)
            ).scalars()
        )
        candidate_dict = {
            "concept": c.concept,
            "target_user": c.target_user,
            "value_prop": c.value_prop,
            "angles": list(c.angles or []),
            "confidence": c.confidence,
            "aggregate_score": c.aggregate_score,
        }
        pains_dicts = [
            {
                "strength": p.strength,
                "topic_tags": list(p.topic_tags or []),
                "text": p.text,
            }
            for p in pains
        ]
        scores_dict: dict = {row.dimension: row.value for row in score_rows}
        if c.aggregate_score is not None:
            scores_dict["__aggregate"] = c.aggregate_score
        return candidate_dict, pains_dicts, scores_dict


def _set_status(pack_id: UUID, status: str, **extra) -> None:
    """Update pack status + any extra columns in one tx."""
    with session_scope() as s:
        row = s.get(ResearchPack, pack_id)
        if row is None:
            return
        row.status = status
        for k, v in extra.items():
            setattr(row, k, v)


# ---------- main ----------


async def generate_pack(
    *,
    candidate_id: UUID,
    router: LLMRouter,
    triggered_by_verdict_id: UUID | None = None,
) -> GenerationResult | None:
    """Generate (or return cached) deep-research pack for one candidate.

    If a `complete` pack already exists, returns its GenerationResult without
    re-calling Opus. If `pending` or `generating`, we proceed to generate.
    If `failed`, we retry.
    """
    pack_id = ensure_pending_pack(
        candidate_id, triggered_by_verdict_id=triggered_by_verdict_id
    )

    # Short-circuit if already complete.
    with session_scope() as s:
        existing = s.get(ResearchPack, pack_id)
        if existing is None:
            logger.error(f"research: pack row {pack_id} disappeared")
            return None
        if existing.status in ("complete", "sent"):
            logger.info(
                f"research: pack {pack_id} already {existing.status}, "
                f"returning cached content"
            )
            return GenerationResult(
                pack_id=pack_id,
                candidate_id=candidate_id,
                content=existing.content_json or {},
                summary=existing.summary or "",
                recommendation=existing.recommendation,
                usd_cost=existing.usd_cost or 0.0,
                model=existing.model or "",
            )

    _set_status(pack_id, "generating")

    inputs = _load_inputs(candidate_id)
    if inputs is None:
        _set_status(pack_id, "failed", error_message="candidate not found")
        logger.error(f"research: candidate {candidate_id} not found")
        return None
    candidate_dict, pains_dicts, scores_dict = inputs

    user_prompt = render_user_prompt(
        candidate=candidate_dict, pains=pains_dicts, scores=scores_dict
    )

    try:
        response = await router.chat(
            tier=Tier.T4,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=6144,
            temperature=0.4,
        )
    except Exception as e:  # noqa: BLE001
        _set_status(
            pack_id,
            "failed",
            error_message=f"{type(e).__name__}: {e}",
        )
        logger.exception(f"research: Opus call failed for {candidate_id}: {e}")
        return None

    record_usage(
        stage="deep_research",
        provider="anthropic",
        model=response.model,
        operation="chat",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        usd_cost=response.usd_cost,
        meta={
            "candidate_id": str(candidate_id),
            "pack_id": str(pack_id),
            "generator_version": RESEARCH_VERSION,
        },
    )

    content = _parse_response(response.text)
    if not content:
        _set_status(
            pack_id,
            "failed",
            error_message="unparseable Opus response",
            model=response.model,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            usd_cost=response.usd_cost,
        )
        return None

    tldr = str(content.get("tldr") or "").strip()
    recommendation = str(content.get("recommendation") or "").strip().lower()
    if recommendation not in ("go", "maybe", "pass"):
        recommendation = None

    _set_status(
        pack_id,
        "complete",
        content_json=content,
        summary=tldr[:2000],
        recommendation=recommendation,
        model=response.model,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        usd_cost=response.usd_cost,
        generated_at=datetime.now(timezone.utc),
        error_message=None,
    )
    logger.info(
        f"research: pack {pack_id} complete ({response.model}) "
        f"cost=${response.usd_cost:.4f}"
    )
    return GenerationResult(
        pack_id=pack_id,
        candidate_id=candidate_id,
        content=content,
        summary=tldr,
        recommendation=recommendation,
        usd_cost=response.usd_cost,
        model=response.model,
    )

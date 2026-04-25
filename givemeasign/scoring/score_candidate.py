"""LLM-driven per-candidate scoring.

One Haiku call per candidate. Parses the 7-dimension JSON response; if the
response is malformed, returns None and the runner skips the candidate
(scored_at stays NULL so a later re-run can retry).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from givemeasign.llm.prompts.score_candidate import (
    SCORER_VERSION,
    SYSTEM_PROMPT,
    render_user_prompt,
)
from givemeasign.llm.router import LLMRouter, Tier
from givemeasign.observability.logging import logger
from givemeasign.observability.usage import record_usage
from givemeasign.scoring.dimensions import DIMENSIONS
from givemeasign.scoring.evidence import Evidence, pains_for_prompt


@dataclass
class ScoringResult:
    values: dict[str, float]       # dimension_name → 0.0–1.0
    reasonings: dict[str, str]     # dimension_name → short explanation
    usd_cost: float
    model: str


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


def _parse_scoring_response(text: str) -> tuple[dict[str, float], dict[str, str]]:
    """Parse the scoring JSON leniently. Missing dimensions default to 0.5."""
    try:
        data = json.loads(_clean_json_block(text))
    except json.JSONDecodeError as e:
        logger.warning(f"score_candidate: JSON parse failed ({e}); raw={text[:200]!r}")
        return {}, {}
    if not isinstance(data, dict):
        logger.warning(f"score_candidate: expected object, got {type(data).__name__}")
        return {}, {}

    values: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for dim in DIMENSIONS:
        entry = data.get(dim)
        if isinstance(entry, dict):
            try:
                v = max(0.0, min(1.0, float(entry.get("value", 0.5))))
            except (ValueError, TypeError):
                v = 0.5
            reason = str(entry.get("reason") or "").strip()[:500]
        elif isinstance(entry, (int, float)):
            # Tolerate a flat {"demand": 0.7} shape if the model collapses.
            v = max(0.0, min(1.0, float(entry)))
            reason = ""
        else:
            v = 0.5
            reason = ""
        values[dim] = v
        if reason:
            reasons[dim] = reason
    return values, reasons


async def score_candidate(
    evidence: Evidence,
    router: LLMRouter,
    *,
    tier: Tier = Tier.T1,
) -> ScoringResult | None:
    """Score one candidate. Returns None if the LLM response can't be parsed."""
    c = evidence.candidate
    user_prompt = render_user_prompt(
        concept=c.concept,
        target_user=c.target_user,
        value_prop=c.value_prop,
        angles=c.angles or [],
        synth_confidence=c.confidence,
        pains=pains_for_prompt(evidence.pains),
        pain_count=evidence.stats.pain_count,
        avg_strength=evidence.stats.avg_strength,
        demand_baseline=evidence.stats.demand_baseline,
        trend_slopes=evidence.trend_slopes or None,
        is_hypothesis=(getattr(c, "origin", "pains") == "hypothesis"),
    )
    response = await router.chat(
        tier=tier,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        max_tokens=1536,
        temperature=0.1,  # low for reproducibility
    )
    record_usage(
        stage="score_candidate",
        provider="anthropic",
        model=response.model,
        operation="chat",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        usd_cost=response.usd_cost,
        meta={
            "candidate_id": str(c.id),
            "scorer_version": SCORER_VERSION,
        },
    )
    values, reasons = _parse_scoring_response(response.text)
    if not values:
        return None
    return ScoringResult(
        values=values,
        reasonings=reasons,
        usd_cost=response.usd_cost,
        model=response.model,
    )

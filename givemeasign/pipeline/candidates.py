"""Tier-2 synthesis: unclustered pain_signals → candidates via Sonnet.

Chunked: one Sonnet call per ~40 pains. Each chunk is independently
retried (via router-level retries) and persisted, so a failed chunk
doesn't poison the rest of the run. Cross-chunk clustering is lost in
exchange for robustness; cross-candidate dedup (via embedding similarity)
picks up stragglers in M4+.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from givemeasign.db.models import Candidate, CandidateSignal, PainSignal
from givemeasign.db.session import session_scope
from givemeasign.llm.prompts.synthesize_candidates import (
    SYNTHESIZER_VERSION,
    SYSTEM_PROMPT,
    render_user_prompt,
)
from givemeasign.llm.router import LLMRouter, Tier
from givemeasign.observability.logging import logger
from givemeasign.observability.usage import record_usage

# Max pains per Sonnet call. Lower = safer + faster per-call; higher = more
# cross-pain clustering opportunity. 40 is a reasonable middle ground based
# on observed prompt sizes and failure rates.
SYNTH_CHUNK_SIZE = 40


@dataclass
class SynthesizedCandidate:
    concept: str
    target_user: str | None
    value_prop: str | None
    angles: list[str]
    confidence: float
    pain_ids: list[UUID]


@dataclass
class SynthSummary:
    input_pains: int
    candidates: int
    linked_pains: int
    orphan_pains: int
    total_cost_usd: float


@dataclass
class _ChunkResult:
    candidates: int
    linked_ids: set[UUID]
    cost: float


# ---------- Unclustered pain fetch ----------


def _fetch_unclustered_pains(s: Session, limit: int) -> list[PainSignal]:
    """Pains with no candidate_signals row yet, highest-strength first."""
    linked_ids = select(CandidateSignal.pain_signal_id)
    stmt = (
        select(PainSignal)
        .where(PainSignal.id.not_in(linked_ids))
        .order_by(PainSignal.strength.desc(), PainSignal.created_at.desc())
        .limit(limit)
    )
    return list(s.execute(stmt).scalars().all())


# ---------- LLM response parsing ----------


def _parse_synth_response(
    text: str, valid_pain_ids: set[UUID]
) -> list[SynthesizedCandidate]:
    """Parse Sonnet's output into SynthesizedCandidate objects, leniently."""
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
        logger.warning(f"synth: JSON parse failed ({e}); raw={text[:200]!r}")
        return []
    if not isinstance(data, list):
        logger.warning(f"synth: expected list, got {type(data).__name__}")
        return []

    out: list[SynthesizedCandidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            concept = str(item["concept"]).strip()[:500]
            if not concept:
                continue
            target_user = (
                str(item["target_user"]).strip()[:300]
                if item.get("target_user")
                else None
            )
            value_prop = (
                str(item["value_prop"]).strip()[:500]
                if item.get("value_prop")
                else None
            )
            angles_raw = item.get("angles") or []
            angles = [str(a).strip()[:150] for a in angles_raw if a][:3]
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5))))

            pain_id_strs = item.get("pain_ids") or []
            pain_uuids: list[UUID] = []
            for pid in pain_id_strs:
                try:
                    u = UUID(str(pid))
                    if u in valid_pain_ids:
                        pain_uuids.append(u)
                except (ValueError, TypeError):
                    continue
            if len(pain_uuids) < 2:
                logger.debug(
                    f"synth: dropping candidate with <2 valid pain_ids: {concept[:60]!r}"
                )
                continue

            out.append(
                SynthesizedCandidate(
                    concept=concept,
                    target_user=target_user,
                    value_prop=value_prop,
                    angles=angles,
                    confidence=confidence,
                    pain_ids=pain_uuids,
                )
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"synth: skipping invalid candidate item: {e}; item={item}")
            continue
    return out


# ---------- Per-chunk synthesis ----------


async def _synth_chunk(pains: list[PainSignal], router: LLMRouter) -> _ChunkResult:
    """One Sonnet call on one chunk; parse, embed, persist."""
    pain_dicts = [
        {
            "id": str(p.id),
            "strength": p.strength,
            "topic_tags": p.topic_tags,
            "text": p.text,
        }
        for p in pains
    ]
    user_prompt = render_user_prompt(pain_dicts)

    response = await router.chat(
        tier=Tier.T2,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        max_tokens=4096,
        temperature=0.3,
    )
    record_usage(
        stage="synthesize_candidates",
        provider="anthropic",
        model=response.model,
        operation="chat",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        usd_cost=response.usd_cost,
        meta={"pain_count": len(pains)},
    )

    valid_ids = {p.id for p in pains}
    candidates = _parse_synth_response(response.text, valid_pain_ids=valid_ids)
    if not candidates:
        return _ChunkResult(candidates=0, linked_ids=set(), cost=response.usd_cost)

    # Batch-embed the candidates so cross-day dedup works later.
    concept_blobs = [
        f"{c.concept}\n{c.value_prop or ''}\n{c.target_user or ''}" for c in candidates
    ]
    try:
        embeddings = await router.embed(concept_blobs)
    except Exception as e:  # noqa: BLE001 — fall back to no-vector inserts
        logger.warning(f"synth: embedding call failed ({e}); inserting without vectors")
        embeddings = [None] * len(candidates)
    else:
        record_usage(
            stage="synthesize_candidates",
            provider="openai",
            model="text-embedding-3-small",
            operation="embed",
            input_tokens=sum(len(b) for b in concept_blobs) // 4,
            output_tokens=0,
            usd_cost=0.0,
            meta={"candidate_count": len(candidates)},
        )

    # Persist chunk's candidates + links in a single transaction.
    linked_pain_ids: set[UUID] = set()
    with session_scope() as s:
        for cand, emb in zip(candidates, embeddings, strict=True):
            row = Candidate(
                concept=cand.concept,
                target_user=cand.target_user,
                value_prop=cand.value_prop,
                angles=cand.angles,
                confidence=cand.confidence,
                status="synthesized",
                synthesizer_version=SYNTHESIZER_VERSION,
                embedding=emb,
            )
            s.add(row)
            s.flush()  # need row.id for link rows
            for pid in cand.pain_ids:
                s.add(CandidateSignal(candidate_id=row.id, pain_signal_id=pid))
                linked_pain_ids.add(pid)

    return _ChunkResult(
        candidates=len(candidates),
        linked_ids=linked_pain_ids,
        cost=response.usd_cost,
    )


# ---------- Main batch runner ----------


async def synthesize_candidates_batch(
    *, pain_limit: int = 100, chunk_size: int = SYNTH_CHUNK_SIZE
) -> SynthSummary:
    """Cluster up to N unclustered pain_signals into candidate rows.

    Splits the batch into chunks of `chunk_size` pains and runs one Sonnet
    call per chunk. Chunk failures are logged and skipped; surviving chunks
    still persist their candidates.
    """
    router = LLMRouter()

    with session_scope() as s:
        pains = _fetch_unclustered_pains(s, pain_limit)
        for p in pains:
            s.expunge(p)

    if not pains:
        logger.info("synth: no unclustered pains")
        return SynthSummary(
            input_pains=0,
            candidates=0,
            linked_pains=0,
            orphan_pains=0,
            total_cost_usd=0.0,
        )

    chunks = [pains[i : i + chunk_size] for i in range(0, len(pains), chunk_size)]
    logger.info(
        f"synth: {len(pains)} pain(s) → {len(chunks)} chunk(s) of up to {chunk_size}"
    )

    total_cost = 0.0
    total_candidates = 0
    linked: set[UUID] = set()
    failed_chunks = 0

    for i, chunk in enumerate(chunks, start=1):
        logger.info(f"  chunk {i}/{len(chunks)}  pains={len(chunk)}")
        try:
            result = await _synth_chunk(chunk, router)
        except Exception as e:  # noqa: BLE001 — one bad chunk doesn't kill the rest
            logger.error(f"  chunk {i} failed: {type(e).__name__}: {e}")
            failed_chunks += 1
            continue
        total_cost += result.cost
        total_candidates += result.candidates
        linked.update(result.linked_ids)
        logger.info(
            f"    ✓ candidates={result.candidates} cost=${result.cost:.4f}"
        )

    if failed_chunks:
        logger.warning(f"synth: {failed_chunks}/{len(chunks)} chunk(s) failed")

    return SynthSummary(
        input_pains=len(pains),
        candidates=total_candidates,
        linked_pains=len(linked),
        orphan_pains=len(pains) - len(linked),
        total_cost_usd=total_cost,
    )

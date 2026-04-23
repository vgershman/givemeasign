"""Batch scoring runner: score → gate → aggregate → dedup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from givemeasign.db.models import Candidate, Score
from givemeasign.db.session import session_scope
from givemeasign.llm.prompts.score_candidate import SCORER_VERSION
from givemeasign.llm.router import LLMRouter
from givemeasign.observability.logging import logger
from givemeasign.scoring.aggregate import multiplicative_aggregate
from givemeasign.scoring.dedup import deduplicate_scored_candidates
from givemeasign.scoring.evidence import build_evidence, enrich_with_trends
from givemeasign.scoring.gates import evaluate_gates
from givemeasign.scoring.score_candidate import score_candidate


@dataclass
class ScoringSummary:
    scored: int
    gated: int
    failed: int
    deduplicated: int
    total_cost_usd: float


async def score_candidates_batch(
    limit: int = 50,
    *,
    dedup_similarity: float = 0.80,
    rescore: bool = False,
    force: bool = False,
) -> ScoringSummary:
    """Score up to N unscored candidates. Runs dedup at the end on all `scored` rows.

    `rescore=True` — reset candidates whose latest scores are at a STALE
    `scorer_version` (i.e. prompt changed). No-op if the prompt version
    hasn't moved.

    `force=True` — reset ALL scored/gated/deduplicated candidates regardless
    of scorer_version. Use when the scoring *evidence* changed shape (e.g.
    adding Google Trends slopes in M4b) but the prompt version is the same.

    Old Score rows stay in the table for audit across versions.
    """
    router = LLMRouter()

    with session_scope() as s:
        if force:
            reset_result = s.execute(
                update(Candidate)
                .where(
                    Candidate.status.in_(["scored", "gated_out", "deduplicated"])
                )
                .values(
                    status="synthesized",
                    aggregate_score=None,
                    gate_failed=None,
                    dedup_of=None,
                    scored_at=None,
                )
            )
            logger.info(
                f"force: reset {reset_result.rowcount} candidate(s) "
                f"(all scored/gated/deduped) → synthesized"
            )
        elif rescore:
            # Reset candidates that aren't at the current scorer_version.
            scored_at_current = (
                select(Score.candidate_id)
                .where(Score.scorer_version == SCORER_VERSION)
                .scalar_subquery()
            )
            reset_result = s.execute(
                update(Candidate)
                .where(
                    Candidate.status.in_(["scored", "gated_out", "deduplicated"])
                )
                .where(Candidate.id.not_in(scored_at_current))
                .values(
                    status="synthesized",
                    aggregate_score=None,
                    gate_failed=None,
                    dedup_of=None,
                    scored_at=None,
                )
            )
            logger.info(
                f"rescore: reset {reset_result.rowcount} candidate(s) "
                f"from stale scorer version → synthesized"
            )

        # Pick unscored candidates still in `synthesized` status.
        stmt = (
            select(Candidate.id)
            .where(Candidate.scored_at.is_(None))
            .where(Candidate.status == "synthesized")
            .order_by(Candidate.confidence.desc(), Candidate.created_at.desc())
            .limit(limit)
        )
        candidate_ids = list(s.execute(stmt).scalars().all())

    if not candidate_ids:
        logger.info("score: no unscored candidates")
        # Still run dedup — catches duplicates formed across earlier runs.
        deduped = deduplicate_scored_candidates(similarity_threshold=dedup_similarity)
        return ScoringSummary(
            scored=0, gated=0, failed=0, deduplicated=deduped, total_cost_usd=0.0
        )

    logger.info(f"score: {len(candidate_ids)} candidate(s) queued")

    scored = 0
    gated = 0
    failed = 0
    total_cost = 0.0

    for cid in candidate_ids:
        # Build evidence inside a short session, release before LLM call.
        with session_scope() as s:
            evidence = build_evidence(s, cid)
            if evidence is None:
                logger.warning(f"  candidate {cid} disappeared mid-batch; skipping")
                failed += 1
                continue
            # Detach so we can use evidence.candidate outside the session.
            s.expunge(evidence.candidate)
            for p in evidence.pains:
                s.expunge(p)

        logger.info(f"→ {str(cid)[:8]} {evidence.candidate.concept[:70]!r}")

        # M4b: augment evidence with Google Trends slopes before scoring.
        # enrich_with_trends never raises — worst case `trend_slopes` stays {}
        # and the scoring prompt renders "Trends unavailable".
        await enrich_with_trends(evidence, router)

        try:
            result = await score_candidate(evidence, router)
        except Exception as e:  # noqa: BLE001
            logger.error(f"  scoring failed: {type(e).__name__}: {e}")
            failed += 1
            continue

        if result is None:
            logger.warning("  LLM returned unparseable response; skipping (will retry on next run)")
            failed += 1
            continue

        # Persist per-dimension scores + apply gates + aggregate in one transaction.
        now = datetime.now(timezone.utc)
        gate_fired = evaluate_gates(result.values)

        try:
            with session_scope() as s:
                # Upsert per (candidate_id, dimension, scorer_version). Idempotent:
                # first-time scoring inserts; re-scoring (including --force at the
                # same version) overwrites value + reasoning + computed_at in place.
                # Avoids the autoflush ordering issues a delete-then-insert has.
                for dim, val in result.values.items():
                    stmt = (
                        pg_insert(Score)
                        .values(
                            candidate_id=cid,
                            dimension=dim,
                            value=val,
                            reasoning=result.reasonings.get(dim),
                            scorer_version=SCORER_VERSION,
                        )
                        .on_conflict_do_update(
                            constraint="uq_score_cand_dim_ver",
                            set_={
                                "value": val,
                                "reasoning": result.reasonings.get(dim),
                                "computed_at": func.now(),
                            },
                        )
                    )
                    s.execute(stmt)
                if gate_fired is not None:
                    s.execute(
                        update(Candidate)
                        .where(Candidate.id == cid)
                        .values(
                            status="gated_out",
                            gate_failed=gate_fired.name,
                            aggregate_score=None,
                            scored_at=now,
                        )
                    )
                    gated += 1
                    logger.info(
                        f"  ✗ GATED ({gate_fired.name}): {gate_fired.description}"
                    )
                else:
                    agg = multiplicative_aggregate(result.values)
                    s.execute(
                        update(Candidate)
                        .where(Candidate.id == cid)
                        .values(
                            status="scored",
                            aggregate_score=agg,
                            gate_failed=None,
                            scored_at=now,
                        )
                    )
                    scored += 1
                    # Compact dimension summary line.
                    dim_str = " ".join(
                        f"{d[:3]}={result.values[d]:.2f}" for d in result.values
                    )
                    logger.info(f"  ✓ agg={agg:.3f}  {dim_str}  cost=${result.usd_cost:.4f}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"  persist failed: {type(e).__name__}: {e}")
            failed += 1
            continue

        total_cost += result.usd_cost

    # Final dedup pass across the entire pool of scored candidates.
    deduped = deduplicate_scored_candidates(similarity_threshold=dedup_similarity)

    return ScoringSummary(
        scored=scored,
        gated=gated,
        failed=failed,
        deduplicated=deduped,
        total_cost_usd=total_cost,
    )

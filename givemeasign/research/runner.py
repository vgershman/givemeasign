"""Orchestrator for end-to-end research: generate → deliver.

Called from the bot as a background task on right/super swipe, and from the
CLI via `generate-research <candidate_id>`. Also exposes `resume_pending()`
to be called at bot startup so interrupted packs finish.
"""

from __future__ import annotations

from uuid import UUID

from aiogram import Bot
from sqlalchemy import select

from givemeasign.db.models import ResearchPack
from givemeasign.db.session import session_scope
from givemeasign.llm.router import LLMRouter
from givemeasign.observability.logging import logger
from givemeasign.research.delivery import deliver_pack
from givemeasign.research.generator import GenerationResult, generate_pack


async def run_research(
    candidate_id: UUID,
    *,
    bot: Bot | None = None,
    user_id: int | None = None,
    triggered_by_verdict_id: UUID | None = None,
) -> GenerationResult | None:
    """Full flow: generate (or reuse) → deliver. Idempotent on candidate_id."""
    router = LLMRouter()
    result = await generate_pack(
        candidate_id=candidate_id,
        router=router,
        triggered_by_verdict_id=triggered_by_verdict_id,
    )
    if result is None:
        return None
    # Deliver (also idempotent: won't re-send a pack already at status='sent').
    delivered = await deliver_pack(result.pack_id, bot=bot, user_id=user_id)
    if not delivered:
        logger.warning(f"research: delivery skipped/failed for pack {result.pack_id}")
    return result


async def resume_pending(
    *, bot: Bot | None = None, user_id: int | None = None
) -> int:
    """Re-process packs the bot interrupted.

    Resumes:
      - status='pending'    — generation never started
      - status='generating' — mid-flight crash
      - status='complete'   — generated but never sent (bot died between)
    Returns the number of packs processed.
    """
    with session_scope() as s:
        rows = list(
            s.execute(
                select(ResearchPack.id, ResearchPack.candidate_id, ResearchPack.status)
                .where(ResearchPack.status.in_(["pending", "generating", "complete"]))
                .order_by(ResearchPack.created_at)
            )
        )
    if not rows:
        return 0
    logger.info(f"research: resuming {len(rows)} interrupted pack(s)")
    processed = 0
    for pack_id, candidate_id, status in rows:
        try:
            if status == "complete":
                # Already generated — just deliver.
                await deliver_pack(pack_id, bot=bot, user_id=user_id)
            else:
                await run_research(candidate_id, bot=bot, user_id=user_id)
            processed += 1
        except Exception as e:  # noqa: BLE001
            logger.exception(f"research: resume failed for pack {pack_id}: {e}")
    return processed

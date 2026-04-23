"""End-to-end orchestrator: fetch → extract → synthesize.

Multi-source fetch with graceful skipping when a source isn't configured
(e.g. Product Hunt without a token). Real scheduling with retries, tier-3/4
deep analysis, and the bandit sampler lives in M4+.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from givemeasign.config import settings
from givemeasign.observability.logging import logger
from givemeasign.pipeline.candidates import SynthSummary, synthesize_candidates_batch
from givemeasign.pipeline.extract import BatchSummary, extract_pains_batch
from givemeasign.sources.devto import fetch_and_store as fetch_and_store_devto
from givemeasign.sources.hackernews import fetch_and_store as fetch_and_store_hn
from givemeasign.sources.product_hunt import fetch_and_store as fetch_and_store_ph


@dataclass
class FetchSummary:
    source: str
    inserted: int
    duplicates: int
    skipped_reason: str | None = None


@dataclass
class PipelineSummary:
    fetches: list[FetchSummary]
    extract: BatchSummary
    synth: SynthSummary

    def as_dict(self) -> dict:
        return {
            "fetches": [asdict(f) for f in self.fetches],
            "extract": asdict(self.extract),
            "synth": asdict(self.synth),
        }


async def run_pipeline(
    *,
    fetch: bool = True,
    hn_limit: int = 25,
    hn_tags: str = "ask_hn,story",
    devto_limit: int = 25,
    devto_tag: str = "discuss",
    ph_limit: int = 25,
    extract_limit: int = 100,
    synth_pain_limit: int = 100,
) -> PipelineSummary:
    """Fetch → extract → synthesize. Returns counters per stage."""
    fetches: list[FetchSummary] = []

    if fetch:
        # Hacker News (always on)
        logger.info(f"[fetch] hackernews tags={hn_tags} limit={hn_limit}")
        try:
            inserted, duplicates = await fetch_and_store_hn(tags=hn_tags, limit=hn_limit)
            fetches.append(FetchSummary(source="hackernews", inserted=inserted, duplicates=duplicates))
            logger.info(f"        → inserted={inserted} duplicates={duplicates}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"        hackernews fetch failed: {e}")
            fetches.append(FetchSummary(source="hackernews", inserted=0, duplicates=0, skipped_reason=str(e)))

        # Dev.to (always on, no auth)
        logger.info(f"[fetch] devto tag={devto_tag} limit={devto_limit}")
        try:
            inserted, duplicates = await fetch_and_store_devto(tag=devto_tag, limit=devto_limit)
            fetches.append(FetchSummary(source="devto", inserted=inserted, duplicates=duplicates))
            logger.info(f"        → inserted={inserted} duplicates={duplicates}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"        devto fetch failed: {e}")
            fetches.append(FetchSummary(source="devto", inserted=0, duplicates=0, skipped_reason=str(e)))

        # Product Hunt (only if token configured)
        if settings.product_hunt_token.get_secret_value():
            logger.info(f"[fetch] producthunt limit={ph_limit}")
            try:
                inserted, duplicates = await fetch_and_store_ph(limit=ph_limit)
                fetches.append(FetchSummary(source="producthunt", inserted=inserted, duplicates=duplicates))
                logger.info(f"        → inserted={inserted} duplicates={duplicates}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"        producthunt fetch failed: {e}")
                fetches.append(FetchSummary(source="producthunt", inserted=0, duplicates=0, skipped_reason=str(e)))
        else:
            logger.info("[fetch] producthunt skipped (PRODUCT_HUNT_TOKEN not set)")
            fetches.append(
                FetchSummary(
                    source="producthunt",
                    inserted=0,
                    duplicates=0,
                    skipped_reason="no token",
                )
            )
    else:
        logger.info("[fetch] skipped (--no-fetch)")

    logger.info(f"[extract] limit={extract_limit}")
    extract_summary = await extract_pains_batch(limit=extract_limit)
    logger.info(
        f"          → processed={extract_summary.processed} "
        f"skipped={extract_summary.skipped} "
        f"pains={extract_summary.total_pains} "
        f"cost=${extract_summary.total_cost_usd:.4f}"
    )

    logger.info(f"[synth] pain_limit={synth_pain_limit}")
    synth_summary = await synthesize_candidates_batch(pain_limit=synth_pain_limit)
    logger.info(
        f"        → input_pains={synth_summary.input_pains} "
        f"candidates={synth_summary.candidates} "
        f"linked={synth_summary.linked_pains} "
        f"orphan={synth_summary.orphan_pains} "
        f"cost=${synth_summary.total_cost_usd:.4f}"
    )

    return PipelineSummary(fetches=fetches, extract=extract_summary, synth=synth_summary)

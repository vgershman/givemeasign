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
from givemeasign.pipeline.ideate import IdeationSummary, ideate_from_seeds
from givemeasign.sources.devto import fetch_and_store as fetch_and_store_devto
from givemeasign.sources.hackernews import fetch_and_store as fetch_and_store_hn
from givemeasign.sources.hackernews import (
    fetch_and_store_search as fetch_and_store_hn_search,
)
from givemeasign.sources.product_hunt import fetch_and_store as fetch_and_store_ph


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


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
    ideate: IdeationSummary | None = None

    def as_dict(self) -> dict:
        return {
            "fetches": [asdict(f) for f in self.fetches],
            "extract": asdict(self.extract),
            "synth": asdict(self.synth),
            "ideate": asdict(self.ideate) if self.ideate is not None else None,
        }


async def run_pipeline(
    *,
    fetch: bool = True,
    hn_limit: int = 10,
    hn_tags: str = "ask_hn,story",
    hn_show_limit: int | None = None,
    hn_seeds: str | None = None,
    hn_search_per_seed: int | None = None,
    devto_tags: str | None = None,
    devto_per_tag: int | None = None,
    ph_limit: int = 25,
    extract_limit: int = 100,
    synth_pain_limit: int = 100,
    ideate: bool = False,
    ideate_seeds: str | None = None,
    ideate_chunk_size: int = 8,
) -> PipelineSummary:
    """Fetch → extract → synthesize. Returns counters per stage.

    Defaults to diversified fetching: HN Ask + HN Show + HN search across a
    set of non-AI-dev seed queries, Dev.to across multiple tags, and Product
    Hunt. Tune via the `HN_*` and `DEVTO_*` env vars or the CLI flags.
    """
    fetches: list[FetchSummary] = []

    # Resolve diversification knobs (env defaults unless explicitly passed).
    seeds = _split_csv(hn_seeds if hn_seeds is not None else settings.hn_search_seeds)
    per_seed = hn_search_per_seed if hn_search_per_seed is not None else settings.hn_search_per_seed
    show_limit = hn_show_limit if hn_show_limit is not None else settings.hn_show_limit
    tags = _split_csv(devto_tags if devto_tags is not None else settings.devto_tags)
    per_tag = devto_per_tag if devto_per_tag is not None else settings.devto_per_tag

    if fetch:
        # --- HN Ask ---
        logger.info(f"[fetch] hackernews tags={hn_tags} limit={hn_limit}")
        try:
            inserted, duplicates = await fetch_and_store_hn(tags=hn_tags, limit=hn_limit)
            fetches.append(
                FetchSummary(source="hn:ask", inserted=inserted, duplicates=duplicates)
            )
            logger.info(f"        → inserted={inserted} duplicates={duplicates}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"        hackernews fetch failed: {e}")
            fetches.append(
                FetchSummary(source="hn:ask", inserted=0, duplicates=0, skipped_reason=str(e))
            )

        # --- HN Show (launches — often consumer-facing) ---
        if show_limit > 0:
            logger.info(f"[fetch] hackernews show_hn limit={show_limit}")
            try:
                inserted, duplicates = await fetch_and_store_hn(
                    tags="show_hn,story", limit=show_limit
                )
                fetches.append(
                    FetchSummary(source="hn:show", inserted=inserted, duplicates=duplicates)
                )
                logger.info(f"        → inserted={inserted} duplicates={duplicates}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"        hackernews show fetch failed: {e}")
                fetches.append(
                    FetchSummary(source="hn:show", inserted=0, duplicates=0, skipped_reason=str(e))
                )

        # --- HN search across non-AI-dev seed queries ---
        for seed in seeds:
            logger.info(f"[fetch] hackernews search query={seed!r} limit={per_seed}")
            try:
                inserted, duplicates = await fetch_and_store_hn_search(
                    query=seed, limit=per_seed
                )
                fetches.append(
                    FetchSummary(
                        source=f"hn:search:{seed[:30]}",
                        inserted=inserted,
                        duplicates=duplicates,
                    )
                )
                logger.info(f"        → inserted={inserted} duplicates={duplicates}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"        hn search {seed!r} failed: {e}")
                fetches.append(
                    FetchSummary(
                        source=f"hn:search:{seed[:30]}",
                        inserted=0,
                        duplicates=0,
                        skipped_reason=str(e),
                    )
                )

        # --- Dev.to across multiple tags ---
        for tag in tags:
            logger.info(f"[fetch] devto tag={tag} limit={per_tag}")
            try:
                inserted, duplicates = await fetch_and_store_devto(tag=tag, limit=per_tag)
                fetches.append(
                    FetchSummary(
                        source=f"devto:{tag}", inserted=inserted, duplicates=duplicates
                    )
                )
                logger.info(f"        → inserted={inserted} duplicates={duplicates}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"        devto {tag!r} fetch failed: {e}")
                fetches.append(
                    FetchSummary(
                        source=f"devto:{tag}",
                        inserted=0,
                        duplicates=0,
                        skipped_reason=str(e),
                    )
                )

        # --- Product Hunt (only if token configured) ---
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

    ideate_summary: IdeationSummary | None = None
    if ideate:
        raw_seeds = ideate_seeds if ideate_seeds is not None else settings.hn_search_seeds
        seeds = _split_csv(raw_seeds)
        logger.info(f"[ideate] A-stream — {len(seeds)} seed(s), chunk={ideate_chunk_size}")
        try:
            ideate_summary = await ideate_from_seeds(seeds, chunk_size=ideate_chunk_size)
            logger.info(
                f"         → candidates_inserted={ideate_summary.candidates_inserted} "
                f"cost=${ideate_summary.total_cost_usd:.4f}"
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[ideate] failed: {e}")

    return PipelineSummary(
        fetches=fetches,
        extract=extract_summary,
        synth=synth_summary,
        ideate=ideate_summary,
    )

"""A-stream: hypothesis-first candidate generation from seed themes.

Takes seed strings (same list format as HN_SEARCH_SEEDS), chunks them, and
asks Sonnet to ideate product hypotheses per theme. Ideas land as Candidate
rows with origin='hypothesis' (no pain links). Scoring + dedup treat them
like any other candidate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from givemeasign.db.models import Candidate
from givemeasign.db.session import session_scope
from givemeasign.llm.prompts.ideate import (
    IDEATOR_VERSION,
    SYSTEM_PROMPT,
    render_user_prompt,
)
from givemeasign.llm.router import LLMRouter, Tier
from givemeasign.observability.logging import logger
from givemeasign.observability.usage import record_usage

IDEATE_DEFAULT_CHUNK = 8
IDEATE_MAX_PER_THEME = 3


@dataclass
class IdeationSummary:
    seeds_in: int
    chunks_run: int
    ideas_parsed: int
    candidates_inserted: int
    total_cost_usd: float


# ---------- helpers ----------


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _parse_response(text: str) -> list[dict]:
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
        logger.warning(f"ideate: JSON parse failed ({e}); raw tail={s[-200:]!r}")
        return []
    if not isinstance(data, list):
        logger.warning(f"ideate: expected list, got {type(data).__name__}")
        return []
    return [item for item in data if isinstance(item, dict)]


def _clean_idea(idea: dict) -> dict | None:
    concept = str(idea.get("concept") or "").strip()
    if not concept:
        return None
    concept = concept[:500]
    target_user = (str(idea.get("target_user") or "").strip() or None)
    if target_user:
        target_user = target_user[:300]
    value_prop = (str(idea.get("value_prop") or "").strip() or None)
    if value_prop:
        value_prop = value_prop[:500]
    angles_raw = idea.get("angles") or []
    angles = [str(a).strip()[:150] for a in angles_raw if str(a).strip()][:3]
    theme = str(idea.get("theme") or "").strip()[:120] or None
    return {
        "concept": concept,
        "target_user": target_user,
        "value_prop": value_prop,
        "angles": angles,
        "theme": theme,
    }


# ---------- per-chunk runner ----------


async def _ideate_chunk(
    themes: list[str], router: LLMRouter
) -> tuple[list[dict], float]:
    if not themes:
        return [], 0.0
    response = await router.chat(
        tier=Tier.T2,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": render_user_prompt(themes)}],
        max_tokens=6144,
        temperature=0.5,
    )
    record_usage(
        stage="ideate",
        provider="anthropic",
        model=response.model,
        operation="chat",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        usd_cost=response.usd_cost,
        meta={"theme_count": len(themes), "ideator_version": IDEATOR_VERSION},
    )
    raw = _parse_response(response.text)
    cleaned: list[dict] = []
    per_theme_count: dict[str, int] = {}
    for item in raw:
        c = _clean_idea(item)
        if c is None:
            continue
        theme_key = c.get("theme") or "(no-theme)"
        per_theme_count[theme_key] = per_theme_count.get(theme_key, 0) + 1
        if per_theme_count[theme_key] > IDEATE_MAX_PER_THEME:
            continue
        cleaned.append(c)
    logger.info(
        f"ideate: chunk of {len(themes)} theme(s) → {len(raw)} raw ideas / "
        f"{len(cleaned)} kept (cost=${response.usd_cost:.4f})"
    )
    return cleaned, response.usd_cost


# ---------- batch ----------


async def ideate_from_seeds(
    seeds: list[str],
    *,
    chunk_size: int = IDEATE_DEFAULT_CHUNK,
) -> IdeationSummary:
    """Generate hypothesis candidates from seed themes and insert into DB.

    Returns a summary. Candidates land with origin='hypothesis', confidence=0.5
    (neutral — not derived from pain strength), and embeddings batched from
    OpenAI after ideation so cross-day dedup later can catch overlap with
    pain-based candidates.
    """
    seeds = [s.strip() for s in seeds if s and s.strip()]
    if not seeds:
        logger.info("ideate: no seeds provided, nothing to do")
        return IdeationSummary(0, 0, 0, 0, 0.0)

    router = LLMRouter()
    all_ideas: list[dict] = []
    total_cost = 0.0
    chunks = _chunks(seeds, chunk_size)
    logger.info(
        f"ideate: {len(seeds)} seed(s) → {len(chunks)} chunk(s) of up to {chunk_size}"
    )
    for i, ch in enumerate(chunks, start=1):
        logger.info(f"  chunk {i}/{len(chunks)}  themes={len(ch)}")
        try:
            ideas, cost = await _ideate_chunk(ch, router)
        except Exception as e:  # noqa: BLE001 — one bad chunk doesn't kill the rest
            logger.error(f"  ideate chunk {i} failed: {e}")
            continue
        total_cost += cost
        all_ideas.extend(ideas)

    if not all_ideas:
        return IdeationSummary(
            seeds_in=len(seeds),
            chunks_run=len(chunks),
            ideas_parsed=0,
            candidates_inserted=0,
            total_cost_usd=total_cost,
        )

    # Embed in one batched OpenAI call.
    blobs = [
        f"{i['concept']}\n{i.get('value_prop') or ''}\n{i.get('target_user') or ''}"
        for i in all_ideas
    ]
    try:
        embeddings = await router.embed(blobs)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ideate: embedding call failed ({e}); inserting without vectors")
        embeddings = [None] * len(all_ideas)
    else:
        record_usage(
            stage="ideate",
            provider="openai",
            model="text-embedding-3-small",
            operation="embed",
            input_tokens=sum(len(b) for b in blobs) // 4,
            output_tokens=0,
            usd_cost=0.0,
            meta={"candidate_count": len(all_ideas)},
        )

    inserted = 0
    with session_scope() as s:
        for idea, emb in zip(all_ideas, embeddings, strict=True):
            row = Candidate(
                concept=idea["concept"],
                target_user=idea["target_user"],
                value_prop=idea["value_prop"],
                angles=idea["angles"],
                confidence=0.5,
                status="synthesized",
                synthesizer_version=IDEATOR_VERSION,
                embedding=emb,
                origin="hypothesis",
            )
            s.add(row)
            inserted += 1

    logger.info(
        f"ideate: inserted {inserted} hypothesis candidate(s) "
        f"total_cost=${total_cost:.4f}"
    )
    return IdeationSummary(
        seeds_in=len(seeds),
        chunks_run=len(chunks),
        ideas_parsed=len(all_ideas),
        candidates_inserted=inserted,
        total_cost_usd=total_cost,
    )

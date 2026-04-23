"""Tier-1 extraction: raw_signal → pain_signals via Haiku."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update

from givemeasign.db.models import PainSignal, RawSignal
from givemeasign.db.session import session_scope
from givemeasign.llm.prompts.extract_pains import (
    EXTRACTOR_VERSION,
    SYSTEM_PROMPT,
    render_user_prompt,
)
from givemeasign.llm.router import LLMRouter, Tier
from givemeasign.observability.logging import logger
from givemeasign.observability.usage import record_usage


@dataclass
class ExtractedPain:
    text: str
    strength: float
    topic_tags: list[str]
    source_ref: str


@dataclass
class BatchSummary:
    processed: int
    skipped: int
    total_pains: int
    total_cost_usd: float


# ---------- Payload unpacking (source-agnostic, shape-based) ----------


def _unpack_payload(raw: RawSignal) -> tuple[str, str, list[dict]]:
    """Return (title, body, flat_comments) normalized across source shapes.

    Source shapes we handle:
      - reddit / producthunt / devto → payload["comments"] is a flat list with "body"
      - hackernews → payload["children"] is a nested tree with "text"
    New sources that add either shape get handled automatically.
    """
    payload = raw.payload or {}
    header = payload.get("post") or payload.get("story") or {}
    title = (header.get("title") or "").strip()
    body = (
        (header.get("body") or "").strip()
        or (header.get("selftext") or "").strip()
        or (header.get("text") or "").strip()
    )

    # Flat comments branch (reddit / producthunt / devto).
    if payload.get("comments") is not None:
        raw_comments = payload.get("comments") or []
        flat: list[dict] = []
        for c in raw_comments:
            text = (c.get("body") or c.get("text") or "").strip()
            if not text:
                continue
            flat.append(
                {
                    "id": c.get("id"),
                    "author": c.get("author"),
                    "text": text,
                    "depth": c.get("depth", 0),
                }
            )
    # Nested children branch (hackernews).
    elif payload.get("children") is not None:
        flat = []

        def walk(nodes: list[dict], depth: int) -> None:
            if depth >= 2:
                return
            for n in nodes[:15]:
                text = (n.get("text") or "").strip()
                if text and len(text) > 15:
                    flat.append(
                        {
                            "id": n.get("id"),
                            "author": n.get("author"),
                            "text": text,
                            "depth": depth,
                        }
                    )
                walk(n.get("children") or [], depth + 1)

        walk(payload.get("children") or [], 0)
    else:
        flat = []

    flat = flat[:20]  # cap total comments to bound token cost
    return title, body, flat


# ---------- LLM response parsing ----------


def _parse_pain_response(text: str) -> list[ExtractedPain]:
    """Parse Haiku's output as JSON, leniently."""
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
        logger.warning(f"extract_pains: JSON parse failed ({e}); raw={text[:200]!r}")
        return []
    if not isinstance(data, list):
        logger.warning(f"extract_pains: expected list, got {type(data).__name__}")
        return []

    out: list[ExtractedPain] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            text_val = str(item["text"]).strip()[:500]
            if not text_val:
                continue
            strength = max(0.0, min(1.0, float(item["strength"])))
            tags_raw = item.get("topic_tags") or []
            topic_tags = [str(t)[:30] for t in tags_raw][:3]
            source_ref = str(item.get("source_ref") or "post")[:100]
            out.append(
                ExtractedPain(
                    text=text_val,
                    strength=strength,
                    topic_tags=topic_tags,
                    source_ref=source_ref,
                )
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.debug(f"skipping invalid pain item: {e}; item={item}")
            continue
    return out


# ---------- Per-raw extraction ----------


async def extract_pains_for_raw(
    raw: RawSignal, router: LLMRouter
) -> tuple[list[ExtractedPain], float]:
    """Extract pains from a single raw_signal. Returns (pains, usd_cost)."""
    title, body, comments = _unpack_payload(raw)
    if not (title or body or comments):
        logger.info(f"{raw.source}:{raw.source_id}: empty content, skipping")
        return [], 0.0

    user_prompt = render_user_prompt(
        source=raw.source,
        url=raw.source_url or "",
        title=title,
        body=body,
        comments=comments,
    )
    response = await router.chat(
        tier=Tier.T1,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
        max_tokens=2048,
        temperature=0.2,
    )
    record_usage(
        stage="extract_pains",
        provider="anthropic",
        model=response.model,
        operation="chat",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        usd_cost=response.usd_cost,
        meta={
            "raw_signal_id": str(raw.id),
            "source": raw.source,
            "source_id": raw.source_id,
        },
    )
    pains = _parse_pain_response(response.text)
    return pains, response.usd_cost


# ---------- Batch runner ----------


async def extract_pains_batch(limit: int = 20) -> BatchSummary:
    """Run tier-1 extraction against up to N unprocessed raw_signals.

    Each raw is processed in its own transaction so a mid-batch crash
    doesn't lose earlier work.
    """
    router = LLMRouter()

    with session_scope() as s:
        stmt = (
            select(RawSignal)
            .where(RawSignal.processed_at.is_(None))
            .order_by(RawSignal.created_at)
            .limit(limit)
        )
        raws: list[RawSignal] = list(s.execute(stmt).scalars().all())
        for r in raws:
            _ = r.payload  # force load before detach
            _ = r.source
            s.expunge(r)

    if not raws:
        logger.info("extract_pains_batch: no unprocessed raw_signals found")
        return BatchSummary(processed=0, skipped=0, total_pains=0, total_cost_usd=0.0)

    logger.info(f"extract_pains_batch: {len(raws)} raw_signal(s) queued")

    processed = 0
    skipped = 0
    total_pains = 0
    total_cost = 0.0

    for raw in raws:
        logger.info(f"→ {raw.source}:{raw.source_id}")
        try:
            pains, cost = await extract_pains_for_raw(raw, router)
        except Exception as e:  # noqa: BLE001
            logger.error(f"  extraction failed: {e}")
            skipped += 1
            continue

        try:
            with session_scope() as s:
                for p in pains:
                    s.add(
                        PainSignal(
                            raw_signal_id=raw.id,
                            source_ref=p.source_ref,
                            text=p.text,
                            strength=p.strength,
                            topic_tags=p.topic_tags,
                            locale="en",
                            extractor_version=EXTRACTOR_VERSION,
                        )
                    )
                s.execute(
                    update(RawSignal)
                    .where(RawSignal.id == raw.id)
                    .values(processed_at=datetime.now(timezone.utc))
                )
        except Exception as e:  # noqa: BLE001
            logger.error(f"  persist failed: {e}")
            skipped += 1
            continue

        processed += 1
        total_pains += len(pains)
        total_cost += cost
        logger.info(f"  ✓ {len(pains)} pain(s)  cost=${cost:.4f}")

    return BatchSummary(
        processed=processed,
        skipped=skipped,
        total_pains=total_pains,
        total_cost_usd=total_cost,
    )

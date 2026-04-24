"""Lazy translation of candidate + research_pack content into a target locale.

DB keeps English as the source of truth (scoring, prompts, debugging all
work against the English text). When Telegram needs to render in a non-
English locale, we translate the minimal set of fields once via Haiku and
cache the result in the row's `translations` JSONB column.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from givemeasign.db.models import Candidate, ResearchPack
from givemeasign.db.session import session_scope
from givemeasign.llm.prompts.translate import SYSTEM_PROMPT, render_user_prompt
from givemeasign.llm.router import LLMRouter, Tier
from givemeasign.observability.logging import logger
from givemeasign.observability.usage import record_usage


# ---------- JSON parsing ----------


def _parse_json_response(text: str) -> Any | None:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        logger.warning(f"translator: JSON parse failed ({e}); raw={s[:200]!r}")
        return None


# ---------- One Haiku call per source ----------


async def _translate_json(
    source: dict | list,
    *,
    target_locale: str,
    router: LLMRouter,
    meta: dict[str, Any] | None = None,
) -> Any | None:
    """Translate all string values in `source` to `target_locale`. Returns
    the translated structure, or None on failure. Structure (keys/ordering)
    preserved by the prompt."""
    try:
        response = await router.chat(
            tier=Tier.T1,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": render_user_prompt(source, target_locale),
                }
            ],
            max_tokens=4096,
            temperature=0.3,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"translator: LLM call failed ({target_locale}): {e}")
        return None

    record_usage(
        stage="translate",
        provider="anthropic",
        model=response.model,
        operation="chat",
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        usd_cost=response.usd_cost,
        meta={
            "target_locale": target_locale,
            **(meta or {}),
        },
    )
    parsed = _parse_json_response(response.text)
    if not isinstance(parsed, (dict, list)):
        logger.warning(
            f"translator: expected JSON dict/list, got {type(parsed).__name__}"
        )
        return None
    return parsed


# ---------- Candidate ----------


CANDIDATE_TRANSLATED_KEYS = ("concept", "target_user", "value_prop", "angles")


async def ensure_candidate_translation(
    candidate_id: UUID,
    locale: str,
    router: LLMRouter,
) -> dict[str, Any]:
    """Return the translated candidate fields for `locale`.

    If a cached translation exists, return it. Otherwise translate via one
    Haiku call, cache on the candidate row, and return. On failure, return
    English originals (render never blocks).
    """
    # First check cache + snapshot English source.
    with session_scope() as s:
        cand = s.get(Candidate, candidate_id)
        if cand is None:
            return {}
        cached = (cand.translations or {}).get(locale)
        if cached:
            return cached
        source = {
            "concept": cand.concept,
            "target_user": cand.target_user or "",
            "value_prop": cand.value_prop or "",
            "angles": list(cand.angles or []),
        }

    translated = await _translate_json(
        source,
        target_locale=locale,
        router=router,
        meta={"entity": "candidate", "candidate_id": str(candidate_id)},
    )
    if not isinstance(translated, dict):
        return source  # fall back to English

    # Keep only the fields we asked for, with sane types.
    clean: dict[str, Any] = {}
    for k in CANDIDATE_TRANSLATED_KEYS:
        v = translated.get(k)
        if k == "angles":
            clean[k] = [str(x) for x in (v or []) if x]
        else:
            clean[k] = str(v) if v is not None else source.get(k, "")

    # Merge-write cache.
    with session_scope() as s:
        cand = s.get(Candidate, candidate_id)
        if cand is not None:
            merged = dict(cand.translations or {})
            merged[locale] = clean
            cand.translations = merged
    return clean


# ---------- Research pack ----------


async def ensure_research_translation(
    pack_id: UUID,
    locale: str,
    router: LLMRouter,
) -> dict[str, Any]:
    """Return the translated research content_json for `locale`.

    Same lazy + cache pattern as candidate translation. The structured
    shape (tldr, market_context, incumbents, build_plan_90d, risks, etc.)
    is preserved by the prompt.
    """
    with session_scope() as s:
        pack = s.get(ResearchPack, pack_id)
        if pack is None:
            return {}
        cached = (pack.translations or {}).get(locale)
        if cached:
            return cached
        source = dict(pack.content_json or {})

    if not source:
        return {}

    translated = await _translate_json(
        source,
        target_locale=locale,
        router=router,
        meta={"entity": "research_pack", "pack_id": str(pack_id)},
    )
    if not isinstance(translated, dict):
        return source  # fall back to English

    with session_scope() as s:
        pack = s.get(ResearchPack, pack_id)
        if pack is not None:
            merged = dict(pack.translations or {})
            merged[locale] = translated
            pack.translations = merged
    return translated

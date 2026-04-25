"""Lazy translation of candidate + research_pack content into a target locale.

DB keeps English as the source of truth (scoring, prompts, debugging all
work against the English text). When Telegram needs to render in a non-
English locale, we translate the minimal set of fields once via Haiku and
cache the result in the row's `translations` JSONB column.

Research packs can be ~3-5k English tokens; Russian output is ~30-40%
larger due to Cyrillic + word structure. A single translate call with
conservative max_tokens was truncating output and failing the JSON parse.
Mitigations in order:
  1. max_tokens=8192 for research packs (plenty of headroom).
  2. If the single call still fails to parse, split the dict into
     top-level keys and translate each independently (per-key pieces are
     tiny → guaranteed to fit). Partial failure only loses the keys whose
     pieces also failed, not the whole pack.
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
        logger.warning(
            f"translator: JSON parse failed ({e}); raw tail={s[-200:]!r}"
        )
        return None


# ---------- Single LLM attempt ----------


async def _single_attempt(
    source: dict | list,
    *,
    target_locale: str,
    router: LLMRouter,
    tier: Tier,
    max_tokens: int,
    meta: dict[str, Any] | None = None,
) -> dict | list | None:
    try:
        response = await router.chat(
            tier=tier,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": render_user_prompt(source, target_locale),
                }
            ],
            max_tokens=max_tokens,
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
            f"translator: expected JSON dict/list, got {type(parsed).__name__}; "
            f"raw length={len(response.text)}"
        )
        return None
    return parsed


# ---------- Translate with per-key split fallback ----------


async def _translate_json(
    source: dict | list,
    *,
    target_locale: str,
    router: LLMRouter,
    tier: Tier = Tier.T1,
    max_tokens: int = 4096,
    allow_split: bool = False,
    meta: dict[str, Any] | None = None,
) -> dict | list | None:
    """Translate a JSON document to target_locale.

    If `allow_split=True` and the single call fails to parse (e.g. output
    truncated on a big research pack), retries by translating each
    top-level key of a dict independently. Per-key pieces are much
    smaller and always fit. Per-key failures only lose that key.
    """
    parsed = await _single_attempt(
        source,
        target_locale=target_locale,
        router=router,
        tier=tier,
        max_tokens=max_tokens,
        meta=meta,
    )
    if parsed is not None:
        return parsed

    if not allow_split or not isinstance(source, dict):
        return None

    logger.info(
        f"translator: single call failed, splitting into {len(source)} per-key "
        f"pieces for {target_locale}"
    )
    result: dict = {}
    for key, value in source.items():
        piece_result = await _single_attempt(
            {key: value},
            target_locale=target_locale,
            router=router,
            tier=tier,
            max_tokens=max_tokens,
            meta={**(meta or {}), "split_key": key},
        )
        if isinstance(piece_result, dict) and key in piece_result:
            result[key] = piece_result[key]
        else:
            # Per-key fallback: keep the English value for just this key,
            # rather than losing the whole pack.
            result[key] = value
            logger.warning(
                f"translator: per-key split failed for key={key!r}, keeping English"
            )
    return result


# ---------- Candidate ----------


CANDIDATE_TRANSLATED_KEYS = ("concept", "target_user", "value_prop", "angles")


async def ensure_candidate_translation(
    candidate_id: UUID,
    locale: str,
    router: LLMRouter,
) -> dict[str, Any]:
    """Return the translated candidate fields for `locale`.

    Card content is small (~300 input / ~500 output tokens), so a single
    Haiku call with max_tokens=2048 always fits. No split fallback.
    """
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
        tier=Tier.T1,
        max_tokens=2048,
        allow_split=False,
        meta={"entity": "candidate", "candidate_id": str(candidate_id)},
    )
    if not isinstance(translated, dict):
        return source  # fall back to English

    clean: dict[str, Any] = {}
    for k in CANDIDATE_TRANSLATED_KEYS:
        v = translated.get(k)
        if k == "angles":
            clean[k] = [str(x) for x in (v or []) if x]
        else:
            clean[k] = str(v) if v is not None else source.get(k, "")

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

    Research packs are large (~3-5k English tokens → ~5-7k Russian tokens).
    Uses max_tokens=8192 and enables per-key split fallback so truncation
    in the single call only degrades one or two keys, not the whole pack.
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
        tier=Tier.T1,
        max_tokens=8192,
        allow_split=True,
        meta={"entity": "research_pack", "pack_id": str(pack_id)},
    )
    if not isinstance(translated, dict):
        return source  # total failure → fall back to English

    with session_scope() as s:
        pack = s.get(ResearchPack, pack_id)
        if pack is not None:
            merged = dict(pack.translations or {})
            merged[locale] = translated
            pack.translations = merged
    return translated

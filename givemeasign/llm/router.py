"""LLM provider router.

One interface (`chat`, `embed`) hides Anthropic vs OpenAI, picks the right
model per tier, and returns a normalized response with usage + estimated cost.
Swap in litellm or a self-hosted model by replacing this class — nothing else
in the codebase should import provider SDKs directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import anthropic
import httpx
from openai import AsyncOpenAI

from givemeasign.config import settings


class Tier(IntEnum):
    """Pipeline tier, used to route to the right-priced model."""

    T1 = 1  # bulk extraction / normalization / cluster prep
    T2 = 2  # synthesis, cross-validation
    T3 = 3  # presentation-ready analysis
    T4 = 4  # post-right-swipe deep research


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    usd_cost: float


# Rough per-1M-token pricing (Anthropic list, Apr 2026).
# Update when you have vendor contracts or when prices move.
_ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-6": (15.00, 75.00),
}


def _model_for_tier(tier: Tier) -> str:
    return {
        Tier.T1: settings.llm_model_tier1,
        Tier.T2: settings.llm_model_tier2,
        Tier.T3: settings.llm_model_tier3,
        Tier.T4: settings.llm_model_tier4,
    }[tier]


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = _ANTHROPIC_PRICING.get(model, (0.0, 0.0))
    return (input_tokens * in_price + output_tokens * out_price) / 1_000_000


class LLMRouter:
    """Thin provider router. Instantiate once per process."""

    def __init__(self) -> None:
        # Anthropic's default max_retries=2 is sometimes not enough during
        # transient server disconnects (RemoteProtocolError on large prompts).
        # Bump to 6 and use an explicit read-timeout generous enough for
        # 60-90s Sonnet responses on chunked synth prompts.
        self._anthropic = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            max_retries=6,
            timeout=httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=15.0),
        )
        self._openai = AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            max_retries=3,
        )

    async def chat(
        self,
        *,
        tier: Tier,
        messages: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> LLMResponse:
        model = _model_for_tier(tier)
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        response = await self._anthropic.messages.create(**kwargs)
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        usage = response.usage
        cost = _estimate_cost(model, usage.input_tokens, usage.output_tokens)
        return LLMResponse(
            text=text,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            usd_cost=cost,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._openai.embeddings.create(
            model=settings.embedding_model, input=texts
        )
        return [d.embedding for d in resp.data]

    # ----- Smoke tests used by `givemeasign doctor` -----

    async def ping_anthropic(self) -> str:
        resp = await self.chat(
            tier=Tier.T1,
            messages=[{"role": "user", "content": "Reply with the single word: pong"}],
            max_tokens=10,
        )
        return resp.text.strip()

    async def ping_openai(self) -> int:
        vecs = await self.embed(["ping"])
        return len(vecs[0])

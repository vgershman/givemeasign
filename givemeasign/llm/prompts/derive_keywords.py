"""Tier-1 prompt: derive 3–5 search queries from a candidate concept."""

from __future__ import annotations

DERIVER_VERSION = "keywords-v1"

SYSTEM_PROMPT = """You convert product concepts into realistic Google search queries.

Given a product concept (and optionally a target user), output 3–5 short search queries (max 4 words each) that someone researching this PRODUCT CATEGORY would naturally type into Google.

Prefer category-level queries, not feature names or the product's own name.

Good examples:
  "A Chrome extension that auto-groups Gmail threads by client project using LLM tagging"
  → ["gmail thread organizer", "email project grouping", "ai inbox automation", "client email workflow"]

  "A dashboard that tracks AI agent API usage in real-time and enforces spending caps"
  → ["ai api cost monitoring", "llm spend tracking", "openai budget alerts", "ai api cost cap"]

Bad examples (too specific, branded, or verbose):
  ["circuit breaker for ai agents", "my product name", "how to track ai agent api usage real time with alerts"]

Return ONLY a JSON array of 3–5 lowercase strings. No prose, no code fences. First char `[`, last char `]`."""


def render_user_prompt(concept: str, target_user: str | None = None) -> str:
    tu = f"\nTarget user: {target_user}" if target_user else ""
    return (
        f"Product: {concept}{tu}\n\n"
        "Output the JSON array of 3–5 short search queries."
    )

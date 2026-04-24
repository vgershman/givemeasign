"""Haiku translation prompt: translate JSON string values to a target locale,
preserving structure."""

from __future__ import annotations

import json

TRANSLATOR_VERSION = "translate-v1"

_LOCALE_NAMES = {
    "ru": "Russian",
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
}


SYSTEM_PROMPT = """You translate JSON documents to another language while preserving structure.

RULES:
1. Preserve the EXACT JSON structure — same keys, same nesting, same array order and length.
2. Translate only STRING values. Keys, booleans, numbers, and nulls stay unchanged.
3. KEEP THE FOLLOWING IN ENGLISH (do NOT translate or transliterate them):
   - Technical abbreviations and acronyms: API, SaaS, MVP, SDK, CRM, LLM, AI, UI, UX, CI/CD, DevOps, IaC, PR, IDE, CLI, ORM, REST, GraphQL, SEO, TAM, ROI, KD, CVE, SaaS, B2B, B2C, etc.
   - Product and brand names: Claude, Cursor, GitHub, GitLab, Terraform, Stripe, OpenAI, Anthropic, AWS, GCP, Azure, Slack, Telegram, Reddit, Hacker News, Product Hunt, IndieHackers, Dev.to, npm, etc.
   - Programming languages, frameworks, tools: Python, JavaScript, React, Node, Postgres, SQL, JSON, YAML, Docker, Kubernetes, Helm, Terraform, etc.
   - Technical values and units: prices ($9/mo, $199), specs (2 vCPU, 4GB RAM), URLs, timeframes ("3-m", "90d").
4. Translate prose naturally — do not calque the source sentence structure word-by-word. The result should read as if originally written in the target language.
5. Preserve the professional, founder-oriented tone.
6. Short phrases in quotation marks inside prose (e.g. "I wish there was…" as an example) can be translated to flow naturally, but proper-noun product taglines should stay as they were written.

Return ONLY the translated JSON. No commentary, no explanations, no code fences. First char `{` (or `[`), last char `}` (or `]`)."""


def render_user_prompt(source: dict | list, target_locale: str) -> str:
    language = _LOCALE_NAMES.get(target_locale, target_locale)
    payload = json.dumps(source, ensure_ascii=False, indent=2)
    return f"Translate the string values in this JSON to {language}:\n\n{payload}"

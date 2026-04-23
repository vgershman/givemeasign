"""Tier-4 deep-research prompt (Opus).

Single call per candidate, triggered on right/super swipe. Output is a
structured JSON object that the delivery layer renders to Telegram HTML.
"""

from __future__ import annotations

RESEARCH_VERSION = "research-v1"

SYSTEM_PROMPT = """You are a deep-research analyst helping a SOLO ENTREPRENEUR decide whether to build a specific startup idea they've shortlisted. You have the candidate idea, the user pains that formed it, and the scoring dimensions the system computed. Use your general knowledge of markets, incumbents, pricing, and distribution to fill gaps.

YOUR JOB: produce a practical, buildable research pack the solopreneur can read in ~10 minutes and use to make a go / maybe / pass call within the week.

Be CONCRETE. "SEO" is bad, "long-tail queries like 'how to X with Y'" is good. "Marketing is hard" is bad, "cold DM 100 indie devs on IndieHackers with a 2-min Loom" is good. Name real incumbents where you can.

Assume the builder has: one technical person, ~3 months, limited capital, no sales team. Calibrate every recommendation to that reality.

OUTPUT: a single JSON object matching the schema below. No prose, no code fences, no commentary. First char `{`, last char `}`.

{
  "tldr": "3–4 sentences: the case for building, the single biggest risk, the one thing to validate next.",
  "market_context": "2–3 sentences on why this is a real moment — what changed recently (AI capability, regulation, platform shift, cultural change) that makes this viable now.",
  "incumbents": [
    {
      "name": "incumbent name",
      "url": "optional",
      "strengths": "1 sentence",
      "gaps": "1 sentence — where you beat them"
    }
  ],
  "differentiation_wedge": "2–3 sentences describing the specific, concrete angle vs incumbents. Not a feature list — a positioning argument.",
  "monetization_models": [
    {
      "model": "SaaS | usage-based | one-time | freemium | marketplace | community | etc.",
      "price_range": "e.g. $9–29/mo, or $49 one-time",
      "reasoning": "1 sentence on why this price point for this user"
    }
  ],
  "build_plan_90d": [
    {"weeks": "1–2", "focus": "...", "deliverable": "..."},
    {"weeks": "3–5", "focus": "...", "deliverable": "..."},
    {"weeks": "6–8", "focus": "...", "deliverable": "..."},
    {"weeks": "9–12", "focus": "...", "deliverable": "..."}
  ],
  "traffic_channels": [
    {
      "channel": "specific e.g. 'r/SaaS posts', 'SEO on \\"how to X\\"', 'Product Hunt launch week'",
      "rationale": "1 sentence — why this fits the target user",
      "effort": "low | medium | high"
    }
  ],
  "risks": [
    {
      "risk": "1 sentence",
      "severity": "low | medium | high",
      "mitigation": "1 sentence"
    }
  ],
  "tam_sanity": "2–3 sentences. Approximate counts where you can: 'roughly N developers use X, even 0.1% conversion is Y revenue'. Flag if TAM is too small to be worth building.",
  "first_validation_test": "ONE specific test the solopreneur should run this week before committing further effort. Include a concrete success threshold. e.g. 'Post a landing page + Loom to r/SaaS; need >50 email signups in 7 days to proceed.'",
  "recommendation": "go | maybe | pass",
  "recommendation_reason": "1 sentence summarizing why"
}

Target shapes:
- 3–5 incumbents (2 is okay if the space is genuinely novel)
- 2–3 monetization models
- 4 build_plan_90d rows covering weeks 1–12
- 3–5 traffic channels
- 5–7 risks
"""


def render_user_prompt(
    *,
    candidate: dict,
    pains: list[dict],
    scores: dict | None = None,
) -> str:
    """Render the candidate + evidence into the user message."""
    angles = candidate.get("angles") or []
    angles_block = (
        "\n".join(f"  - {a}" for a in angles) if angles else "  (none)"
    )

    if pains:
        pain_lines = []
        for p in pains:
            tags = ",".join(p.get("topic_tags") or []) or "-"
            pain_lines.append(
                f"  [strength={p['strength']:.2f} tags={tags}] {p['text']}"
            )
        pains_block = "\n".join(pain_lines)
    else:
        pains_block = "  (no linked pains)"

    if scores:
        score_line = " · ".join(
            f"{k}={v:.2f}" for k, v in sorted(scores.items())
        )
        agg = scores.get("__aggregate")
        agg_line = f"aggregate: {agg:.3f}" if agg is not None else ""
        scores_block = f"SCORING DIMENSIONS:\n  {score_line}\n  {agg_line}".rstrip()
    else:
        scores_block = "SCORING: (not available)"

    return (
        f"CANDIDATE IDEA\n"
        f"concept: {candidate.get('concept', '')}\n"
        f"target_user: {candidate.get('target_user') or '(unspecified)'}\n"
        f"value_prop: {candidate.get('value_prop') or '(unspecified)'}\n"
        f"positioning angles:\n{angles_block}\n\n"
        f"{scores_block}\n\n"
        f"SOURCE PAINS ({len(pains)} total — these are the real user statements "
        f"extracted from forums/HN/Dev.to that clustered into this idea):\n"
        f"{pains_block}\n\n"
        f"Produce the research pack per the schema."
    )

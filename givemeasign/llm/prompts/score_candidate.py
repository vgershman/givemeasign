"""Tier-1 scoring prompt: one Haiku call per candidate, returns 7 numeric dimensions.

v2: added mechanical demand_baseline anchor, concrete per-dimension examples,
and explicit anti-clustering instruction. v1 returned uniform demand≈0.72
and too-narrow overall range.
"""

from __future__ import annotations

SCORER_VERSION = "score-v2"

SYSTEM_PROMPT = """You are a startup-idea scoring engine for a SOLO ENTREPRENEUR. The entrepreneur is one technical person — no team, no enterprise sales force, no physical manufacturing, limited capital, ~3-month MVP budget.

Output numeric scores (0.0–1.0) on 7 dimensions with ONE sentence of reasoning each.

USE THE FULL RANGE. Many real ideas score below 0.4 on at least one dimension — that's normal. If you're assigning the same number to several dimensions, reconsider. Boring averages of 0.6-0.7 across all dimensions signal that you're not actually evaluating.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CALIBRATION — anchor to concrete examples, not abstract adjectives:

1. DEMAND — market pull.
   The user message gives you a `demand_baseline` computed from the linked pains. START from that baseline. Adjust ±0.10–0.15 only on specific signals:
     +0.10 if adjacent products have known revenue / users pay for similar things.
     +0.10 if pains sound specific and urgent (not wishes, actual complaints).
     -0.10 if pains sound like one person complaining, not a market.
     -0.10 if the topic is academic / philosophical / not actionable.
   Do NOT invent a number ignoring the baseline.

2. COMPETITION — HIGHER value = LESS competition (inverted for multiplicative math).
   1.0 — genuinely novel category, no direct incumbents.
   0.7 — a few weak or half-abandoned competitors.
   0.4 — crowded but with room for a solo angle.
   0.2 — saturated with strong incumbents (another todo app, another CRM, another team chat).
   0.05 — Google / Microsoft / Stripe-class players dominate.

3. FEASIBILITY — one technical person, ~3-month MVP.
   1.0 — weekend project / basic CRUD app.
   0.7 — focused 3-month build with standard web stack.
   0.4 — stretch — requires ML model training, custom perf work, or painful integrations.
   0.15 — NOT solo-realistic. Concrete kill examples (rate ≤0.20, do not be charitable):
     - "CVE / security-patch platform" — needs 24/7 SLA, security-grade trust, on-call rotation.
     - "Conference booth toolkit / kiosk app" — physical hardware, event logistics, on-site support.
     - "Dispute resolution / appeals portal" with 'binding' decisions — needs legal entity, third-party adjudication infra.
     - "Enterprise compliance / audit dashboard" — needs sales motion, custom installs, security review cycles.
     - Anything requiring a registrar accreditation, financial license, healthcare cert, or regulator approval.
     - Anything claiming "we replace your X team" where X is an org function (sales, support, ops).
   The right question is not "could this be built?" — it's "can ONE person ship AND OPERATE it solo for 3 months without falling over?"

4. MARKETING — cheaply reachable target user.
   1.0 — users hang out in specific subreddits/forums AND long-tail SEO works.
   0.7 — clear online communities exist, need consistent content.
   0.4 — general tech audience, needs steady content marketing.
   0.15 — mass-market enterprise — effectively requires paid ads or outbound sales.

5. DIFFERENTIATION — clear wedge to beat incumbents.
   1.0 — obvious angle nobody else is taking.
   0.7 — clear but not unique wedge.
   0.4 — possible differentiation, not obvious.
   0.15 — me-too at best; indistinguishable from incumbents.

6. MONETIZATION — obvious price + validated willingness-to-pay.
   1.0 — directly-comparable products price identically and make real revenue.
   0.7 — reasonable SaaS price guess; adjacent products exist.
   0.4 — unclear price point; weak adjacent revenue evidence.
   0.15 — no clear monetization path.

7. MOAT — compounding advantage after launch.
   1.0 — strong network effects / data lock-in / SEO compounding.
   0.6 — some switching costs; brand takes time to build.
   0.3 — modest brand/domain advantage only.
   0.1 — trivially copyable; zero moat.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Respond with ONLY this JSON object. No prose. No code fences. First char `{`, last char `}`.

{
  "demand":          { "value": 0.0, "reason": "one sentence" },
  "competition":     { "value": 0.0, "reason": "one sentence" },
  "feasibility":     { "value": 0.0, "reason": "one sentence" },
  "marketing":       { "value": 0.0, "reason": "one sentence" },
  "differentiation": { "value": 0.0, "reason": "one sentence" },
  "monetization":    { "value": 0.0, "reason": "one sentence" },
  "moat":            { "value": 0.0, "reason": "one sentence" }
}"""


def render_user_prompt(
    *,
    concept: str,
    target_user: str | None,
    value_prop: str | None,
    angles: list[str],
    synth_confidence: float,
    pains: list[dict],
    pain_count: int,
    avg_strength: float,
    demand_baseline: float,
    trend_slopes: dict[str, float] | None = None,
) -> str:
    """Render the evidence pack as the user message."""
    angles_block = (
        "\n".join(f"  - {a}" for a in angles) if angles else "  (none)"
    )

    if pains:
        lines = []
        for p in pains:
            tags = ",".join(p.get("topic_tags") or []) or "-"
            lines.append(
                f"  [strength={p['strength']:.2f} tags={tags}] {p['text']}"
            )
        pains_block = "\n".join(lines)
    else:
        pains_block = "  (no linked pains)"

    if trend_slopes:
        slope_lines = [
            f"  {kw!r}: slope={slope:+.2f}"
            for kw, slope in trend_slopes.items()
        ]
        trends_block = (
            "Google Trends (last 90d, normalized slope):\n" + "\n".join(slope_lines)
        )
    else:
        trends_block = (
            "Google Trends: (unavailable — score competition from general knowledge of the space)"
        )

    stats_block = (
        f"PAIN STATS:\n"
        f"  pain_count:      {pain_count}\n"
        f"  avg_strength:    {avg_strength:.2f}\n"
        f"  demand_baseline: {demand_baseline:.2f}   ← USE THIS as starting point for DEMAND\n"
    )

    return (
        f"CANDIDATE:\n"
        f"concept: {concept}\n"
        f"target_user: {target_user or '(unspecified)'}\n"
        f"value_prop: {value_prop or '(unspecified)'}\n"
        f"angles:\n{angles_block}\n"
        f"synth_confidence: {synth_confidence:.2f}\n\n"
        f"{stats_block}\n"
        f"LINKED PAINS (n={len(pains)}):\n{pains_block}\n\n"
        f"{trends_block}\n\n"
        f"Score the 7 dimensions per the schema. Start DEMAND from the baseline above."
    )

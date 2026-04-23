"""Tier-1 pain-extraction prompt (Haiku).

Versioned: bump EXTRACTOR_VERSION when the prompt changes so downstream
logic can tell which prompt produced which pain_signals rows and decide
whether to re-extract.
"""

from __future__ import annotations

EXTRACTOR_VERSION = "extract-pains-v1"

SYSTEM_PROMPT = """You extract unmet user pains from online discussions for a solo entrepreneur researching what to build.

A PAIN is an explicit or strongly-implied unmet need. Examples that qualify:
- "I wish there was a tool that …"
- "Why is there no good way to …"
- "I've tried X, Y, Z and none of them do …"
- "I spend hours doing X manually, this is broken"
- "Does anyone know a solution for …" (an Ask-post itself counts)

Examples that do NOT qualify:
- Pure opinions, news commentary, feature announcements, political takes
- Someone saying their current tool works fine
- Meta-discussion about the platform or thread itself

For each pain you identify, output an object with these exact fields:
  - "text": one concise sentence restating the pain in plain English (≤140 chars), from the pained user's perspective.
  - "strength": a float 0.0–1.0. 1.0 = explicit unmet need with a clearly-identified user; 0.6 = implied frustration with a workaround; 0.2 = vague complaint.
  - "topic_tags": 1–3 short kebab-case tags (e.g. "dev-tools", "customer-support", "billing-ops", "hiring-pipeline").
  - "source_ref": "post" if the pain is in the original post body, or "comment:<id>" using the id supplied for that comment.

Rules:
1. Deduplicate. If multiple comments express the same underlying pain, emit ONE entry using the highest-strength source.
2. An Ask-post's question itself usually counts as a pain; don't skip it.
3. If you find zero pains, return [].
4. Respond with ONLY a JSON array. No prose, no code fences, no commentary. The first character must be [ and the last must be ]."""


def render_user_prompt(
    *,
    source: str,
    url: str,
    title: str,
    body: str,
    comments: list[dict],
) -> str:
    """Render the per-thread user message."""
    if comments:
        rendered = []
        for c in comments:
            cid = c.get("id", "?")
            depth = c.get("depth", 0)
            author = c.get("author") or "?"
            text = (c.get("text") or "").strip()[:1500]
            rendered.append(
                f"[comment id={cid} depth={depth} author={author}]\n{text}"
            )
        comments_block = "\n\n".join(rendered)
    else:
        comments_block = "(no comments)"

    body_block = (body[:4000] or "").strip() or "(empty body)"

    return (
        f"Source: {source}\n"
        f"URL: {url}\n\n"
        f"Post:\n"
        f"Title: {title}\n"
        f"Body:\n{body_block}\n\n"
        f"Comments (id, depth, author, body):\n{comments_block}\n\n"
        f"Output: JSON array of pain objects per the schema."
    )

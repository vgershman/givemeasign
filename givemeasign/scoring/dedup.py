"""Cross-candidate dedup via pgvector cosine similarity.

Two candidates are duplicates if their concept embeddings have cosine
similarity >= threshold. We use union-find to cluster pairwise-similar
candidates transitively (so if A~B and B~C, all three end up in one
cluster even if A and C weren't directly paired). Within each cluster,
the highest aggregate_score wins; others are marked `deduplicated` with
`dedup_of` pointing at the winner.

Runs on `scored` candidates only — no point deduping things that haven't
been evaluated yet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text, update

from givemeasign.db.models import Candidate
from givemeasign.db.session import session_scope
from givemeasign.observability.logging import logger


def _find_components(pairs: list[tuple[UUID, UUID]]) -> list[set[UUID]]:
    """Union-find: return connected components with size ≥ 2."""
    parent: dict[UUID, UUID] = {}

    def find(x: UUID) -> UUID:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])  # path compression
            x = parent[x]
        return x

    def union(a: UUID, b: UUID) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in pairs:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        union(a, b)

    groups: dict[UUID, set[UUID]] = {}
    for node in list(parent.keys()):
        root = find(node)
        groups.setdefault(root, set()).add(node)
    return [g for g in groups.values() if len(g) > 1]


def deduplicate_scored_candidates(
    *,
    similarity_threshold: float = 0.80,
) -> int:
    """Collapse near-duplicate scored candidates.

    Returns the number of candidates marked `deduplicated`.
    """
    # pgvector's `<=>` is cosine DISTANCE (1 - similarity). Convert threshold.
    max_distance = 1.0 - similarity_threshold

    with session_scope() as s:
        result = s.execute(
            text(
                """
                SELECT c1.id AS c1_id, c2.id AS c2_id,
                       (c1.embedding <=> c2.embedding) AS dist
                FROM candidates c1, candidates c2
                WHERE c1.id < c2.id
                  AND c1.status = 'scored'
                  AND c2.status = 'scored'
                  AND c1.embedding IS NOT NULL
                  AND c2.embedding IS NOT NULL
                  AND (c1.embedding <=> c2.embedding) <= :max_dist
                """
            ),
            {"max_dist": max_distance},
        )
        pairs: list[tuple[UUID, UUID]] = [(row.c1_id, row.c2_id) for row in result]

    if not pairs:
        logger.info("dedup: no near-duplicates above threshold")
        return 0

    components = _find_components(pairs)
    logger.info(
        f"dedup: {len(pairs)} similar-pair(s) → {len(components)} cluster(s) "
        f"(threshold similarity ≥ {similarity_threshold:.2f})"
    )

    if not components:
        return 0

    # For each cluster: fetch candidates, pick winner, mark losers.
    deduped = 0
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        for group in components:
            # Order by aggregate_score desc; NULL at the end.
            members = (
                s.query(Candidate)
                .filter(Candidate.id.in_(group))
                .filter(Candidate.status == "scored")
                .all()
            )
            if len(members) < 2:
                continue
            members.sort(key=lambda c: c.aggregate_score or 0.0, reverse=True)
            winner = members[0]
            losers = members[1:]
            logger.info(
                f"  cluster of {len(members)}: winner={winner.aggregate_score:.3f} "
                f"{winner.concept[:60]!r}"
            )
            for loser in losers:
                s.execute(
                    update(Candidate)
                    .where(Candidate.id == loser.id)
                    .values(
                        status="deduplicated",
                        dedup_of=winner.id,
                        scored_at=now,
                    )
                )
                deduped += 1
                logger.info(
                    f"    → dedup loser={loser.aggregate_score:.3f} "
                    f"{loser.concept[:60]!r}"
                )
    return deduped

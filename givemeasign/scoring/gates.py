"""Hard gates — categorical filters applied before aggregation.

A candidate that fails ANY gate is marked `gated_out` and does not receive
an aggregate score. Gates encode constraints that can't be meaningfully
traded off against other dimensions: no amount of demand fixes a project
that literally cannot be built solo.

Thresholds are deliberately low so that only clear kill-shots trigger —
borderline feasibility issues should land in the aggregate, not the gate.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Gate:
    name: str
    dimension: str
    threshold: float
    description: str


DEFAULT_GATES: tuple[Gate, ...] = (
    Gate(
        name="solo_infeasible",
        dimension="feasibility",
        threshold=0.30,
        description="Not buildable solo in ~3 months (e.g. requires physical goods, regulatory certification, enterprise ops, or 24/7 on-call).",
    ),
    Gate(
        name="enterprise_sales",
        dimension="marketing",
        threshold=0.20,
        description="Target user is unreachable without a sales team or heavy paid acquisition.",
    ),
    Gate(
        name="no_demand",
        dimension="demand",
        threshold=0.28,
        description="Insufficient evidence of market pull — barely anyone seems to want this.",
    ),
    Gate(
        name="unmonetizable",
        dimension="monetization",
        threshold=0.20,
        description="No obvious willingness-to-pay path — users wouldn't pay or adjacent products don't monetize.",
    ),
)


def evaluate_gates(
    dimension_scores: dict[str, float],
    gates: tuple[Gate, ...] = DEFAULT_GATES,
) -> Gate | None:
    """Return the first gate that fires, or None if all pass."""
    for g in gates:
        v = dimension_scores.get(g.dimension)
        if v is None:
            continue
        if v < g.threshold:
            return g
    return None

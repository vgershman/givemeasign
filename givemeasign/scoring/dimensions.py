"""The 7 scoring dimensions and their default weights.

Dimension names are strings because they're stored in the scores table.
Keep this list in sync with the scoring prompt schema.
"""

from __future__ import annotations

DIMENSIONS: tuple[str, ...] = (
    "demand",
    "competition",      # HIGHER = LESS competition (inverted for multiplicative math)
    "feasibility",
    "marketing",
    "differentiation",
    "monetization",
    "moat",
)

# Default equal weights. Feedback learner will tune these in M7 from swipe data.
DEFAULT_WEIGHTS: dict[str, float] = {d: 1.0 for d in DIMENSIONS}

# Floor used during aggregation to prevent one noisy 0.0 from catastrophically
# collapsing the score. 0.05 still punishes near-zero strongly while leaving
# headroom for the multiplicative math to discriminate.
AGGREGATE_FLOOR: float = 0.05

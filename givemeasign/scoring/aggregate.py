"""Multiplicative aggregate (weighted geometric mean).

Variant 2 of the scoring algorithms discussed in design. A single near-zero
dimension strongly suppresses the aggregate, which matches our intuition:
an idea with great demand but zero feasibility is not actually a good idea.
"""

from __future__ import annotations

import math

from givemeasign.scoring.dimensions import AGGREGATE_FLOOR, DEFAULT_WEIGHTS, DIMENSIONS


def multiplicative_aggregate(
    scores: dict[str, float],
    weights: dict[str, float] | None = None,
    *,
    floor: float = AGGREGATE_FLOOR,
) -> float:
    """Weighted geometric mean of dimension scores.

    score = exp( sum(w_i * log(max(s_i, floor))) / sum(w_i) )

    Result is in [floor, 1.0]. Equal-weighted and no-missing-dimensions
    degenerates to the plain geometric mean.
    """
    if weights is None:
        weights = DEFAULT_WEIGHTS

    log_sum = 0.0
    w_sum = 0.0
    for dim in DIMENSIONS:
        w = weights.get(dim, 1.0)
        if w <= 0.0:
            continue
        val = max(scores.get(dim, 0.5), floor)
        log_sum += w * math.log(val)
        w_sum += w
    if w_sum == 0.0:
        return 0.0
    return math.exp(log_sum / w_sum)

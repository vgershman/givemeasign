"""Swipe-driven weight learner.

For each dimension, learns a weight by comparing average scores of
right-swiped vs left-swiped candidates. Blends toward uniform when sample
is small so early swipes don't overfit.

No ML library required — the math is diff-of-means + sigmoid confidence
blending. Works from swipe #1 (uniform), improves gracefully through
~30 swipes to a stable learned vector, recomputable in microseconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from givemeasign.db.models import BotSettings, Score, SwipeVerdict
from givemeasign.db.session import session_scope
from givemeasign.llm.prompts.score_candidate import SCORER_VERSION
from givemeasign.observability.logging import logger
from givemeasign.scoring.dimensions import DIMENSIONS

# Verdicts that count as positive/negative for the learner.
# 'super' is a strong positive. 'snooze' is deliberately excluded —
# it's "maybe later", not a preference signal.
_POSITIVE_VERDICTS = frozenset({"right", "super"})
_NEGATIVE_VERDICTS = frozenset({"left"})

# Confidence grows as 1 - exp(-n/SAMPLE_SCALE). At n=10 swipes each side,
# confidence ≈ 0.63; at n=30, ≈ 0.95. Below ~5 swipes each, weights stay
# very close to uniform.
_SAMPLE_SCALE = 10.0

# Diff-to-weight-delta scaling. A perfect diff of ±0.3 (a dimension where
# right-swipes average 0.3 higher than left-swipes) shifts weight by ±1.0
# at full confidence, before normalization.
_DIFF_SCALE = 3.33

# Weight floor so no dimension gets fully ignored (prevents catastrophic
# collapse of the multiplicative aggregate on dimensions the user merely
# underweights).
_WEIGHT_FLOOR = 0.1

# Below this many total swipes, weights are treated as uninformative and
# we return uniform. Prevents noisy "learning" from the first 3 swipes.
_MIN_SWIPES_FOR_LEARNING = 5


@dataclass
class LearnedWeights:
    weights: dict[str, float] = field(default_factory=dict)
    pos_count: int = 0
    neg_count: int = 0
    total_swipes: int = 0
    confidence: float = 0.0
    diffs: dict[str, float] = field(default_factory=dict)

    @property
    def is_learned(self) -> bool:
        return self.total_swipes >= _MIN_SWIPES_FOR_LEARNING and bool(self.weights)


def _uniform() -> dict[str, float]:
    return {d: 1.0 for d in DIMENSIONS}


def compute_weights(
    pos_values: dict[str, list[float]],
    neg_values: dict[str, list[float]],
) -> LearnedWeights:
    """Pure-function version — takes already-aggregated per-dimension value lists."""
    n_pos = max(len(v) for v in pos_values.values()) if pos_values else 0
    n_neg = max(len(v) for v in neg_values.values()) if neg_values else 0
    total = n_pos + n_neg

    if total < _MIN_SWIPES_FOR_LEARNING or n_pos == 0 or n_neg == 0:
        return LearnedWeights(
            weights=_uniform(),
            pos_count=n_pos,
            neg_count=n_neg,
            total_swipes=total,
            confidence=0.0,
            diffs={d: 0.0 for d in DIMENSIONS},
        )

    confidence = 1.0 - math.exp(-min(n_pos, n_neg) / _SAMPLE_SCALE)

    diffs: dict[str, float] = {}
    weights: dict[str, float] = {}
    for d in DIMENSIONS:
        pos = pos_values.get(d) or []
        neg = neg_values.get(d) or []
        if not pos or not neg:
            diffs[d] = 0.0
            weights[d] = 1.0
            continue
        avg_pos = sum(pos) / len(pos)
        avg_neg = sum(neg) / len(neg)
        diff = avg_pos - avg_neg
        diffs[d] = diff
        weights[d] = max(_WEIGHT_FLOOR, 1.0 + confidence * diff * _DIFF_SCALE)

    # Normalize so sum = len(DIMENSIONS) (same total "budget" as uniform).
    target_sum = float(len(DIMENSIONS))
    current_sum = sum(weights.values())
    if current_sum > 0:
        scale = target_sum / current_sum
        weights = {d: w * scale for d, w in weights.items()}

    return LearnedWeights(
        weights=weights,
        pos_count=n_pos,
        neg_count=n_neg,
        total_swipes=total,
        confidence=confidence,
        diffs=diffs,
    )


def fetch_training_data() -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """Pull per-dimension score values per swipe verdict from the DB.

    Joins swipe_verdicts × scores on candidate_id; only uses scores at the
    current SCORER_VERSION (older versions would have different value
    calibration). Returns ({dim: [values_for_positive_swipes]}, same for negative).
    """
    pos: dict[str, list[float]] = {d: [] for d in DIMENSIONS}
    neg: dict[str, list[float]] = {d: [] for d in DIMENSIONS}

    with session_scope() as s:
        stmt = (
            select(SwipeVerdict.candidate_id, SwipeVerdict.verdict, Score.dimension, Score.value)
            .join(Score, Score.candidate_id == SwipeVerdict.candidate_id)
            .where(Score.scorer_version == SCORER_VERSION)
        )
        rows = s.execute(stmt).all()

    for _cid, verdict, dim, value in rows:
        if dim not in pos:
            continue
        if verdict in _POSITIVE_VERDICTS:
            pos[dim].append(float(value))
        elif verdict in _NEGATIVE_VERDICTS:
            neg[dim].append(float(value))

    return pos, neg


def retrain_and_persist() -> LearnedWeights:
    """Query swipes + scores from DB, learn weights, persist to bot_settings.

    Always safe to call. With insufficient data, persists an empty dict
    (signifying "use uniform") and returns a LearnedWeights with is_learned=False.
    """
    pos, neg = fetch_training_data()
    result = compute_weights(pos, neg)

    with session_scope() as s:
        row = s.get(BotSettings, 1)
        if row is None:
            row = BotSettings(id=1)
            s.add(row)
        if result.is_learned:
            row.dimension_weights = result.weights
        else:
            row.dimension_weights = {}  # empty = uniform
        row.weights_updated_at = datetime.now(timezone.utc)
        row.weights_swipe_count = result.total_swipes

    return result


def load_current_weights() -> dict[str, float] | None:
    """Return the currently-persisted weights dict, or None if uniform.

    Callers pass this straight to multiplicative_aggregate — which treats
    None as uniform. No blending logic here; blending happens at learn time.
    """
    with session_scope() as s:
        row = s.get(BotSettings, 1)
        if row is None:
            return None
        raw = row.dimension_weights or {}
    if not raw:
        return None
    # Defensive: fill any missing dimensions with 1.0.
    return {d: float(raw.get(d, 1.0)) for d in DIMENSIONS}


def reset_weights() -> None:
    """Clear learned weights back to uniform."""
    with session_scope() as s:
        row = s.get(BotSettings, 1)
        if row is None:
            return
        row.dimension_weights = {}
        row.weights_updated_at = datetime.now(timezone.utc)
        row.weights_swipe_count = 0

"""One write per LLM/API call — the foundation of the budget dashboard.

Errors are swallowed on purpose: logging must never kill a pipeline run.
"""

from __future__ import annotations

from typing import Any

from givemeasign.db.models import UsageLog
from givemeasign.db.session import session_scope
from givemeasign.observability.logging import logger


def record_usage(
    *,
    stage: str,
    provider: str,
    model: str,
    operation: str = "chat",
    input_tokens: int = 0,
    output_tokens: int = 0,
    usd_cost: float = 0.0,
    meta: dict[str, Any] | None = None,
) -> None:
    """Insert one row into usage_log. Safe to call from hot paths."""
    try:
        with session_scope() as s:
            s.add(
                UsageLog(
                    stage=stage,
                    provider=provider,
                    model=model,
                    operation=operation,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    usd_cost=usd_cost,
                    meta=meta or {},
                )
            )
    except Exception:
        logger.exception("failed to record usage — continuing")

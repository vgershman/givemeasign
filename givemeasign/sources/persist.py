"""Shared RawSignalPayload → raw_signals persistence.

Every source adapter emits RawSignalPayloads. Use this helper to drain an
async iterator into the DB with ON CONFLICT DO NOTHING on (source, source_id),
so re-fetching the same item is idempotent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.dialects.postgresql import insert

from givemeasign.db.models import RawSignal
from givemeasign.db.session import session_scope
from givemeasign.sources.base import RawSignalPayload


async def persist_payloads(
    payloads: AsyncIterator[RawSignalPayload],
) -> tuple[int, int]:
    """Drain `payloads` into raw_signals. Returns (inserted, skipped_duplicates)."""
    inserted = 0
    duplicates = 0
    with session_scope() as s:
        async for p in payloads:
            stmt = (
                insert(RawSignal)
                .values(
                    source=p.source,
                    source_id=p.source_id,
                    source_url=p.source_url,
                    query_context=p.query_context,
                    payload=p.payload,
                )
                .on_conflict_do_nothing(constraint="uq_raw_signals_source_id")
                .returning(RawSignal.id)
            )
            result = s.execute(stmt).scalar_one_or_none()
            if result is not None:
                inserted += 1
            else:
                duplicates += 1
    return inserted, duplicates

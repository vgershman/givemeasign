"""Source adapter interface + the in-memory payload shape."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawSignalPayload:
    """What a source adapter emits.

    Persistence layer turns this into a RawSignal row (with ON CONFLICT DO
    NOTHING on (source, source_id) so re-fetching is idempotent).
    """

    source: str
    source_id: str
    source_url: str | None = None
    query_context: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


class Source(ABC):
    """Base for all external data sources.

    Each adapter owns auth, rate limiting, retries, and caching for its
    specific source. Downstream only sees normalized RawSignalPayloads.
    """

    name: str = ""

    @abstractmethod
    async def close(self) -> None:
        """Release any persistent resources (HTTP clients, sessions, etc.)."""
        ...

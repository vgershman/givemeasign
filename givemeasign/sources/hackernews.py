"""Hacker News source adapter via the Algolia search API.

No auth required. Generous rate limits. Ask HN / Show HN threads are
extremely dense with explicit user pains — ideal for the B-pipeline.
Docs: https://hn.algolia.com/api
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import httpx

from givemeasign.observability.logging import logger
from givemeasign.sources.base import RawSignalPayload, Source
from givemeasign.sources.persist import persist_payloads

_BASE = "https://hn.algolia.com/api/v1"


class HackerNewsSource(Source):
    """Hacker News adapter backed by the Algolia API."""

    name = "hackernews"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "givemeasign/0.1 (contact via github)"},
        )

    async def _search(
        self,
        *,
        tags: str,
        limit: int,
        since_days: int | None,
    ) -> list[dict]:
        params: dict[str, str | int] = {"tags": tags, "hitsPerPage": min(limit, 100)}
        if since_days is not None:
            since = int(time.time()) - since_days * 86_400
            params["numericFilters"] = f"created_at_i>{since}"
        r = await self._client.get(f"{_BASE}/search_by_date", params=params)
        r.raise_for_status()
        return r.json().get("hits", [])

    async def _get_item(self, object_id: str) -> dict:
        r = await self._client.get(f"{_BASE}/items/{object_id}")
        r.raise_for_status()
        return r.json()

    async def _search_by_query(
        self,
        *,
        query: str,
        tags: str,
        limit: int,
        since_days: int | None,
    ) -> list[dict]:
        """Hit /search (relevance-ranked) with a free-text query."""
        params: dict[str, str | int] = {
            "query": query,
            "tags": tags,
            "hitsPerPage": min(limit, 100),
        }
        if since_days is not None:
            since = int(time.time()) - since_days * 86_400
            params["numericFilters"] = f"created_at_i>{since}"
        r = await self._client.get(f"{_BASE}/search", params=params)
        r.raise_for_status()
        return r.json().get("hits", [])

    async def fetch(
        self,
        *,
        tags: str = "ask_hn,story",
        limit: int = 25,
        since_days: int = 30,
    ) -> AsyncIterator[RawSignalPayload]:
        """Yield HN threads as RawSignalPayloads. The full nested comment tree
        is stored under payload['children']; extract walks it.
        """
        hits = await self._search(tags=tags, limit=limit, since_days=since_days)
        logger.info(f"hn: search returned {len(hits)} hit(s) for tags={tags!r}")
        async for payload in self._yield_threads(hits, ctx={"tags": tags, "since_days": since_days}):
            yield payload

    async def search(
        self,
        *,
        query: str,
        limit: int = 10,
        since_days: int = 180,
        tags: str = "story",
    ) -> AsyncIterator[RawSignalPayload]:
        """Yield HN threads matching a free-text query (e.g. 'small business tools').

        Uses HN Algolia's /search (relevance-ranked) endpoint, not /search_by_date.
        Time window defaults to 180 days so domain-specific searches have enough
        results — most real pain threads are older than the last 30 days.
        """
        hits = await self._search_by_query(
            query=query, tags=tags, limit=limit, since_days=since_days
        )
        logger.info(
            f"hn: search('{query}') returned {len(hits)} hit(s) (tags={tags}, {since_days}d)"
        )
        async for payload in self._yield_threads(
            hits, ctx={"search_query": query, "tags": tags, "since_days": since_days}
        ):
            yield payload

    async def _yield_threads(
        self,
        hits: list[dict],
        *,
        ctx: dict,
    ) -> AsyncIterator[RawSignalPayload]:
        """Shared per-hit item-fetch + payload construction for fetch() and search()."""
        for hit in hits:
            object_id = hit.get("objectID")
            if not object_id:
                continue
            try:
                thread = await self._get_item(object_id)
            except httpx.HTTPError as e:
                logger.warning(f"hn: skipping {object_id}: {e}")
                continue
            yield RawSignalPayload(
                source="hackernews",
                source_id=f"hn_{object_id}",
                source_url=f"https://news.ycombinator.com/item?id={object_id}",
                query_context=dict(ctx),
                payload={
                    "post": {
                        "id": thread.get("id"),
                        "title": thread.get("title"),
                        "body": thread.get("text") or "",
                        "author": thread.get("author"),
                        "score": thread.get("points"),
                        "created_at": thread.get("created_at"),
                        "url": thread.get("url"),
                    },
                    "children": thread.get("children", []),
                },
            )

    async def close(self) -> None:
        await self._client.aclose()


async def fetch_and_store(
    *,
    tags: str = "ask_hn,story",
    limit: int = 25,
    since_days: int = 30,
) -> tuple[int, int]:
    source = HackerNewsSource()
    try:
        return await persist_payloads(
            source.fetch(tags=tags, limit=limit, since_days=since_days)
        )
    finally:
        await source.close()


async def fetch_and_store_search(
    query: str,
    *,
    limit: int = 10,
    since_days: int = 180,
    tags: str = "story",
) -> tuple[int, int]:
    """Search HN by keyword query and insert matching threads into raw_signals."""
    source = HackerNewsSource()
    try:
        return await persist_payloads(
            source.search(query=query, limit=limit, since_days=since_days, tags=tags)
        )
    finally:
        await source.close()

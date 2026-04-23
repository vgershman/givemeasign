"""Product Hunt source adapter (GraphQL v2).

Fetches recent launches plus their comment threads. Launches themselves are
weaker pain signals than the comments under them — those surface "I tried
the existing version and it still doesn't X" gaps that candidate synthesis
can turn into differentiated ideas.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from givemeasign.config import settings
from givemeasign.observability.logging import logger
from givemeasign.sources.base import RawSignalPayload, Source
from givemeasign.sources.persist import persist_payloads

_ENDPOINT = "https://api.producthunt.com/v2/api/graphql"

# Single query pulling posts + top comments. Page with the cursor.
_POSTS_QUERY = """
query($first: Int!, $after: String) {
  posts(order: RANKING, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id slug name tagline description url
        votesCount commentsCount createdAt
        topics(first: 5) { edges { node { name slug } } }
        makers { name }
        comments(first: 20) {
          edges {
            node {
              id body votesCount createdAt
              user { name }
            }
          }
        }
      }
    }
  }
}
"""


class ProductHuntSource(Source):
    name = "producthunt"

    def __init__(self) -> None:
        token = settings.product_hunt_token.get_secret_value()
        if not token:
            raise RuntimeError(
                "PRODUCT_HUNT_TOKEN is not set — create a developer token at "
                "https://www.producthunt.com/v2/oauth/applications and add it to .env"
            )
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "givemeasign/0.1",
            },
        )

    async def fetch_recent(self, *, limit: int = 25) -> AsyncIterator[RawSignalPayload]:
        cursor: str | None = None
        remaining = limit
        while remaining > 0:
            batch = min(remaining, 20)
            try:
                r = await self._client.post(
                    _ENDPOINT,
                    json={
                        "query": _POSTS_QUERY,
                        "variables": {"first": batch, "after": cursor},
                    },
                )
                logger.debug(f"producthunt: status={r.status_code}")
                r.raise_for_status()
                data = r.json()
            except httpx.HTTPStatusError as e:
                body = ""
                try:
                    body = e.response.text[:500]
                except Exception:
                    pass
                logger.error(
                    f"producthunt: HTTP {e.response.status_code} — "
                    f"likely invalid/expired PRODUCT_HUNT_TOKEN or wrong token type "
                    f"(need the Developer Token, not API Key). Body: {body!r}"
                )
                return
            except httpx.HTTPError as e:
                logger.error(f"producthunt: network error: {e}")
                return

            if data.get("errors"):
                logger.error(f"producthunt: GraphQL errors: {data['errors']}")
                return
            posts = (data.get("data") or {}).get("posts") or {}
            edges = posts.get("edges") or []
            if not edges:
                if cursor is None:
                    logger.warning(
                        "producthunt: first page returned 0 posts with no error — "
                        "possible auth/scope issue even with 200 OK. "
                        f"response keys={list(data.keys())}"
                    )
                return
            for edge in edges:
                node = edge.get("node") or {}
                comments_raw = (node.get("comments") or {}).get("edges") or []
                comments = []
                for c in comments_raw:
                    cn = c.get("node") or {}
                    body = (cn.get("body") or "").strip()
                    if not body:
                        continue
                    comments.append(
                        {
                            "id": cn.get("id"),
                            "author": (cn.get("user") or {}).get("name"),
                            "body": body,
                            "score": cn.get("votesCount", 0),
                            "created_at": cn.get("createdAt"),
                        }
                    )
                topics = [
                    (t.get("node") or {}).get("slug")
                    for t in (node.get("topics") or {}).get("edges", [])
                    if (t.get("node") or {}).get("slug")
                ]
                tagline = (node.get("tagline") or "").strip()
                description = (node.get("description") or "").strip()
                body_combined = "\n\n".join(filter(None, [tagline, description]))
                yield RawSignalPayload(
                    source="producthunt",
                    source_id=f"ph_{node['id']}",
                    source_url=f"https://www.producthunt.com/posts/{node.get('slug', '')}",
                    query_context={"order": "RANKING"},
                    payload={
                        "post": {
                            "id": node.get("id"),
                            "slug": node.get("slug"),
                            "title": node.get("name"),
                            "tagline": tagline,
                            "body": body_combined,
                            "external_url": node.get("url"),
                            "score": node.get("votesCount", 0),
                            "num_comments": node.get("commentsCount", 0),
                            "topics": topics,
                            "makers": [m.get("name") for m in (node.get("makers") or []) if m.get("name")],
                            "created_at": node.get("createdAt"),
                        },
                        "comments": comments,
                    },
                )
                remaining -= 1
                if remaining <= 0:
                    return
            page = posts.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return
            cursor = page.get("endCursor")

    async def close(self) -> None:
        await self._client.aclose()


async def fetch_and_store(*, limit: int = 25) -> tuple[int, int]:
    source = ProductHuntSource()
    try:
        return await persist_payloads(source.fetch_recent(limit=limit))
    finally:
        await source.close()

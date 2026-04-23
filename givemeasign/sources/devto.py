"""Dev.to source adapter (public Forem API, no auth).

Docs: https://developers.forem.com/api

For pain signals, the most useful tags are `discuss`, `beginners`, and
`help` — those bias toward questions and complaints rather than pure
tutorials/announcements.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx

from givemeasign.observability.logging import logger
from givemeasign.sources.base import RawSignalPayload, Source
from givemeasign.sources.persist import persist_payloads

_BASE = "https://dev.to/api"

# Rough HTML stripper for comment body_html. Good enough for extraction;
# if we ever need rich rendering we swap in BeautifulSoup.
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_ENTITY_MAP = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&nbsp;": " ",
}


def _strip_html(html: str) -> str:
    if not html:
        return ""
    stripped = _SCRIPT_RE.sub("", html)
    text = _TAG_RE.sub(" ", stripped)
    for k, v in _ENTITY_MAP.items():
        text = text.replace(k, v)
    return _WS_RE.sub(" ", text).strip()


def _flatten_comments(raw: list[dict], *, max_depth: int = 2, cap: int = 20) -> list[dict]:
    """Dev.to returns comments as a nested tree with body_html."""
    flat: list[dict] = []

    def walk(nodes: list[dict], depth: int) -> None:
        if depth >= max_depth:
            return
        for c in nodes or []:
            text = _strip_html(c.get("body_html") or "")
            if text:
                flat.append(
                    {
                        "id": c.get("id_code"),
                        "author": (c.get("user") or {}).get("name"),
                        "body": text,
                        "depth": depth,
                    }
                )
            walk(c.get("children") or [], depth + 1)

    walk(raw, 0)
    return flat[:cap]


class DevtoSource(Source):
    name = "devto"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Accept": "application/json",
                "User-Agent": "givemeasign/0.1",
            },
        )

    async def _list_articles(self, *, tag: str, per_page: int, top_days: int) -> list[dict]:
        params: dict[str, str | int] = {
            "tag": tag,
            "per_page": per_page,
            "top": top_days,  # top over last N days
        }
        r = await self._client.get(f"{_BASE}/articles", params=params)
        r.raise_for_status()
        return r.json() or []

    async def _get_article(self, article_id: int) -> dict | None:
        try:
            r = await self._client.get(f"{_BASE}/articles/{article_id}")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            logger.warning(f"devto: article {article_id} fetch failed: {e}")
            return None

    async def _get_comments(self, article_id: int) -> list[dict]:
        try:
            r = await self._client.get(f"{_BASE}/comments", params={"a_id": article_id})
            r.raise_for_status()
            return r.json() or []
        except httpx.HTTPError as e:
            logger.warning(f"devto: comments for {article_id} fetch failed: {e}")
            return []

    async def fetch(
        self,
        *,
        tag: str = "discuss",
        limit: int = 25,
        top_days: int = 7,
    ) -> AsyncIterator[RawSignalPayload]:
        articles = await self._list_articles(tag=tag, per_page=min(limit, 30), top_days=top_days)
        logger.info(f"devto: listed {len(articles)} article(s) tag={tag!r}")
        for stub in articles[:limit]:
            article_id = stub.get("id")
            if not article_id:
                continue
            article = await self._get_article(article_id)
            if not article:
                continue
            raw_comments = await self._get_comments(article_id)
            comments = _flatten_comments(raw_comments)
            body_md = article.get("body_markdown") or article.get("description") or ""
            yield RawSignalPayload(
                source="devto",
                source_id=f"devto_{article_id}",
                source_url=article.get("url"),
                query_context={"tag": tag, "top_days": top_days},
                payload={
                    "post": {
                        "id": article_id,
                        "title": article.get("title"),
                        "body": body_md[:8000],
                        "description": article.get("description"),
                        "author": (article.get("user") or {}).get("name"),
                        "tags": article.get("tag_list") or [],
                        "score": article.get("positive_reactions_count", 0),
                        "num_comments": article.get("comments_count", 0),
                        "created_at": article.get("published_at"),
                    },
                    "comments": comments,
                },
            )

    async def close(self) -> None:
        await self._client.aclose()


async def fetch_and_store(
    *,
    tag: str = "discuss",
    limit: int = 25,
    top_days: int = 7,
) -> tuple[int, int]:
    source = DevtoSource()
    try:
        return await persist_payloads(source.fetch(tag=tag, limit=limit, top_days=top_days))
    finally:
        await source.close()

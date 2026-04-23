"""Reddit source adapter (asyncpraw)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal

import asyncpraw

from givemeasign.config import settings
from givemeasign.observability.logging import logger
from givemeasign.sources.base import RawSignalPayload, Source
from givemeasign.sources.persist import persist_payloads

SortMode = Literal["hot", "new", "top", "rising"]


class RedditSource(Source):
    """asyncpraw-backed Reddit adapter. Read-only — we never post."""

    name = "reddit"

    def __init__(self) -> None:
        self._reddit = asyncpraw.Reddit(
            client_id=settings.reddit_client_id.get_secret_value(),
            client_secret=settings.reddit_client_secret.get_secret_value(),
            user_agent=settings.reddit_user_agent,
        )
        self._reddit.read_only = True

    async def fetch_subreddit(
        self,
        name: str,
        *,
        limit: int = 25,
        sort: SortMode = "hot",
        top_comments: int = 10,
        time_filter: str = "week",
    ) -> AsyncIterator[RawSignalPayload]:
        """Yield posts from `r/{name}` as RawSignalPayloads, with top-N comments."""
        sub = await self._reddit.subreddit(name)
        if sort == "hot":
            stream = sub.hot(limit=limit)
        elif sort == "new":
            stream = sub.new(limit=limit)
        elif sort == "rising":
            stream = sub.rising(limit=limit)
        elif sort == "top":
            stream = sub.top(limit=limit, time_filter=time_filter)
        else:
            raise ValueError(f"unsupported sort: {sort!r}")

        async for post in stream:
            try:
                await post.load()
                await post.comments.replace_more(limit=0)
                comments_sorted = sorted(
                    post.comments,
                    key=lambda c: getattr(c, "score", 0) or 0,
                    reverse=True,
                )
                comments = [
                    {
                        "id": c.id,
                        "author": str(c.author) if c.author else None,
                        "body": c.body or "",
                        "score": c.score or 0,
                        "created_utc": c.created_utc,
                    }
                    for c in comments_sorted[:top_comments]
                ]
                yield RawSignalPayload(
                    source="reddit",
                    source_id=f"t3_{post.id}",
                    source_url=f"https://reddit.com{post.permalink}",
                    query_context={
                        "subreddit": name,
                        "sort": sort,
                        "time_filter": time_filter if sort == "top" else None,
                    },
                    payload={
                        "post": {
                            "id": post.id,
                            "title": post.title,
                            "body": post.selftext or "",
                            "author": str(post.author) if post.author else None,
                            "score": post.score or 0,
                            "num_comments": post.num_comments or 0,
                            "created_utc": post.created_utc,
                            "upvote_ratio": post.upvote_ratio or 0.0,
                            "flair": post.link_flair_text,
                        },
                        "comments": comments,
                    },
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"reddit: skipping post due to error: {e}")
                continue

    async def close(self) -> None:
        await self._reddit.close()


async def fetch_and_store(
    subreddit: str,
    *,
    limit: int = 25,
    sort: SortMode = "hot",
    top_comments: int = 10,
) -> tuple[int, int]:
    """Fetch a subreddit and insert rows into raw_signals."""
    source = RedditSource()
    try:
        return await persist_payloads(
            source.fetch_subreddit(
                subreddit, limit=limit, sort=sort, top_comments=top_comments
            )
        )
    finally:
        await source.close()

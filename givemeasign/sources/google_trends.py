"""Google Trends adapter (pytrends) with 7-day DB cache.

pytrends is unofficial — it scrapes Google's Trends UI endpoints and is
rate-limited, geofenced, and occasionally broken. Everything here fails
soft: on any error, callers get an empty dict and carry on. The scoring
prompt already handles "trends unavailable" gracefully.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from givemeasign.db.models import TrendCache
from givemeasign.db.session import session_scope
from givemeasign.observability.logging import logger


def _patch_urllib3_retry() -> None:
    """pytrends (4.9.x) still passes Retry(method_whitelist=...) which was
    renamed to allowed_methods in urllib3 1.26 and removed in 2.x. Translate
    at Retry.__init__ so pytrends keeps working across urllib3 versions.
    """
    try:
        from urllib3.util.retry import Retry  # type: ignore
    except ImportError:
        return
    if getattr(Retry, "_givemeasign_method_whitelist_patched", False):
        return
    _orig_init = Retry.__init__

    def _init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "method_whitelist" in kwargs:
            kwargs["allowed_methods"] = kwargs.pop("method_whitelist")
        return _orig_init(self, *args, **kwargs)

    Retry.__init__ = _init  # type: ignore[assignment]
    Retry._givemeasign_method_whitelist_patched = True  # type: ignore[attr-defined]


_patch_urllib3_retry()

TRENDS_CACHE_TTL_DAYS = 7
_PYTRENDS_BATCH_DELAY_SEC = 2.5  # politeness between consecutive Trends calls
_DEFAULT_TIMEFRAME = "today 3-m"
_DEFAULT_LOCALE = "en-US"
_DEFAULT_GEO = ""  # worldwide


# ---------- slope math ----------


def compute_slope(values: list[float]) -> float:
    """Linear-regression slope of weekly interest values, normalized to 0–1.

    0.5 = flat, >0.5 = rising, <0.5 = declining.
    Mapping: slope of +2 points/week → 1.0; −2 points/week → 0.0.
    """
    if not values or len(values) < 3:
        return 0.5
    y = np.array(values, dtype=float)
    if y.std() < 0.01:
        return 0.5
    x = np.arange(len(y), dtype=float)
    slope_per_step, _ = np.polyfit(x, y, 1)
    normalized = 0.5 + float(slope_per_step) / 4.0
    return max(0.0, min(1.0, normalized))


# ---------- cache ----------


def _read_cache(
    keywords: list[str],
    *,
    locale: str,
    timeframe: str,
    geo: str,
) -> tuple[dict[str, float], list[str]]:
    """Return (cached_slopes, keywords_to_fetch)."""
    if not keywords:
        return {}, []
    now = datetime.now(timezone.utc)
    cached: dict[str, float] = {}
    missing: list[str] = []
    with session_scope() as s:
        for kw in keywords:
            row = s.execute(
                select(TrendCache)
                .where(TrendCache.keyword == kw)
                .where(TrendCache.locale == locale)
                .where(TrendCache.timeframe == timeframe)
                .where(TrendCache.geo == geo)
                .where(TrendCache.expires_at > now)
            ).scalar_one_or_none()
            if row is not None and row.slope is not None:
                cached[kw] = row.slope
            else:
                missing.append(kw)
    return cached, missing


def _write_cache(
    results: dict[str, tuple[float, list[float]]],
    *,
    locale: str,
    timeframe: str,
    geo: str,
) -> None:
    if not results:
        return
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=TRENDS_CACHE_TTL_DAYS)
    with session_scope() as s:
        for kw, (slope, values) in results.items():
            stmt = (
                pg_insert(TrendCache)
                .values(
                    keyword=kw,
                    locale=locale,
                    timeframe=timeframe,
                    geo=geo,
                    slope=slope,
                    raw_values=values,
                    fetched_at=now,
                    expires_at=expires,
                )
                .on_conflict_do_update(
                    constraint="uq_trend_cache_key",
                    set_={
                        "slope": slope,
                        "raw_values": values,
                        "fetched_at": now,
                        "expires_at": expires,
                    },
                )
            )
            s.execute(stmt)


# ---------- blocking pytrends call ----------


def _fetch_sync(
    keywords: list[str],
    *,
    timeframe: str,
    locale: str,
    geo: str,
) -> dict[str, tuple[float, list[float]]]:
    """Blocking pytrends fetch. Returns {keyword: (slope, weekly_values)}.

    pytrends caps at 5 keywords per call; we batch accordingly.
    """
    try:
        from pytrends.request import TrendReq
    except ImportError as e:
        logger.warning(f"pytrends not installed: {e}")
        return {}

    try:
        pytrends = TrendReq(hl=locale, tz=0, timeout=(5, 25), retries=2, backoff_factor=0.5)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"pytrends: init failed: {e}")
        return {}

    results: dict[str, tuple[float, list[float]]] = {}
    for i in range(0, len(keywords), 5):
        batch = keywords[i : i + 5]
        try:
            pytrends.build_payload(batch, cat=0, timeframe=timeframe, geo=geo)
            df = pytrends.interest_over_time()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"pytrends: fetch failed for {batch!r}: {e}")
            continue
        if df is None or df.empty:
            continue
        for kw in batch:
            if kw not in df.columns:
                continue
            values = df[kw].astype(float).tolist()
            slope = compute_slope(values)
            results[kw] = (slope, values)
        if i + 5 < len(keywords):
            time.sleep(_PYTRENDS_BATCH_DELAY_SEC)
    return results


# ---------- public async API ----------


async def fetch_trend_slopes(
    keywords: list[str],
    *,
    locale: str = _DEFAULT_LOCALE,
    timeframe: str = _DEFAULT_TIMEFRAME,
    geo: str = _DEFAULT_GEO,
    use_cache: bool = True,
) -> dict[str, float]:
    """Return {keyword: slope ∈ [0,1]}. Empty dict on total failure."""
    if not keywords:
        return {}

    if use_cache:
        cached, missing = _read_cache(
            keywords, locale=locale, timeframe=timeframe, geo=geo
        )
    else:
        cached, missing = {}, list(keywords)

    if not missing:
        return cached

    try:
        fresh = await asyncio.to_thread(
            _fetch_sync,
            missing,
            timeframe=timeframe,
            locale=locale,
            geo=geo,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"fetch_trend_slopes: unexpected failure: {e}")
        fresh = {}

    if fresh:
        try:
            _write_cache(fresh, locale=locale, timeframe=timeframe, geo=geo)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"fetch_trend_slopes: cache write failed: {e}")

    out = dict(cached)
    for kw, (slope, _values) in fresh.items():
        out[kw] = slope
    return out

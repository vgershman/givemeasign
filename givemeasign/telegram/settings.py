"""Read/write the singleton bot_settings row.

Caching: the loguru sink polls is_telegram_log_enabled() on every log record,
so we cache the read for 5s to avoid hammering the DB.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select

from givemeasign.db.models import BotSettings
from givemeasign.db.session import session_scope

_CACHE_TTL_SEC = 5.0


@dataclass
class _CachedSettings:
    snapshot: dict
    fetched_at: float


_cache: _CachedSettings | None = None


def _read_fresh() -> dict:
    with session_scope() as s:
        row = s.get(BotSettings, 1)
        if row is None:
            # Defensive: row should always exist (seeded by migration).
            return {
                "telegram_log_enabled": False,
                "telegram_log_min_level": "INFO",
                "daily_deck_hour": 23,
                "daily_deck_minute": 0,
                "daily_deck_enabled": True,
            }
        return {
            "telegram_log_enabled": row.telegram_log_enabled,
            "telegram_log_min_level": row.telegram_log_min_level,
            "daily_deck_hour": row.daily_deck_hour,
            "daily_deck_minute": row.daily_deck_minute,
            "daily_deck_enabled": row.daily_deck_enabled,
        }


def get_settings(*, force_refresh: bool = False) -> dict:
    global _cache
    now = time.monotonic()
    if (
        not force_refresh
        and _cache is not None
        and now - _cache.fetched_at < _CACHE_TTL_SEC
    ):
        return _cache.snapshot
    snapshot = _read_fresh()
    _cache = _CachedSettings(snapshot=snapshot, fetched_at=now)
    return snapshot


def is_telegram_log_enabled() -> bool:
    return bool(get_settings().get("telegram_log_enabled", False))


def get_telegram_log_min_level() -> str:
    return str(get_settings().get("telegram_log_min_level", "INFO")).upper()


def get_daily_deck_schedule() -> tuple[int, int, bool]:
    """Return (hour, minute, enabled)."""
    s = get_settings()
    return (
        int(s.get("daily_deck_hour", 23)),
        int(s.get("daily_deck_minute", 0)),
        bool(s.get("daily_deck_enabled", True)),
    )


def set_telegram_log(*, enabled: bool, min_level: str | None = None) -> None:
    """Toggle Telegram logging. Cache is invalidated on next read."""
    global _cache
    with session_scope() as s:
        row = s.get(BotSettings, 1)
        if row is None:
            row = BotSettings(id=1, telegram_log_enabled=enabled)
            s.add(row)
        else:
            row.telegram_log_enabled = enabled
            if min_level:
                row.telegram_log_min_level = min_level.upper()
    _cache = None  # force next read to refresh


def set_daily_deck_schedule(
    *, hour: int | None = None, minute: int | None = None, enabled: bool | None = None
) -> None:
    global _cache
    with session_scope() as s:
        row = s.get(BotSettings, 1)
        if row is None:
            row = BotSettings(id=1)
            s.add(row)
        if hour is not None:
            row.daily_deck_hour = hour
        if minute is not None:
            row.daily_deck_minute = minute
        if enabled is not None:
            row.daily_deck_enabled = enabled
    _cache = None

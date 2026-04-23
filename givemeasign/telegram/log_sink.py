"""Loguru sink that posts log records to Telegram when toggled on.

The toggle lives in bot_settings (DB), polled with a short cache. The sink
swallows all errors — logging must NEVER kill the pipeline. Uses raw httpx
(not aiogram) so it has zero dependency on a running bot process.
"""

from __future__ import annotations

import html as html_mod
import threading
import time

import httpx

from givemeasign.config import settings
from givemeasign.telegram.settings import (
    get_telegram_log_min_level,
    is_telegram_log_enabled,
)

# Loguru level numeric values for ordering. Same order as loguru's defaults.
_LEVEL_ORDER = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

# Per-process throttle to avoid Telegram per-chat rate limit (1 msg/sec/chat).
_send_lock = threading.Lock()
_last_send_at = 0.0
_MIN_GAP = 0.6  # seconds between posts to the same chat


def _record_passes_filter(record_level_name: str) -> bool:
    if not is_telegram_log_enabled():
        return False
    threshold = _LEVEL_ORDER.get(get_telegram_log_min_level(), 20)
    return _LEVEL_ORDER.get(record_level_name, 20) >= threshold


def _format_for_telegram(record) -> str:
    level = record["level"].name
    name = record["name"]
    func = record["function"]
    line = record["line"]
    msg = record["message"]
    icon = {
        "DEBUG": "🔍",
        "INFO": "ℹ️",
        "SUCCESS": "✅",
        "WARNING": "⚠️",
        "ERROR": "❌",
        "CRITICAL": "🚨",
    }.get(level, "•")
    safe_msg = html_mod.escape(msg)[:3500]
    safe_loc = html_mod.escape(f"{name}.{func}:{line}")
    return f"{icon} <b>{level}</b> <code>{safe_loc}</code>\n<pre>{safe_msg}</pre>"


def _throttle() -> None:
    """Block briefly so we don't exceed Telegram's per-chat rate limit."""
    global _last_send_at
    with _send_lock:
        now = time.monotonic()
        wait = (_last_send_at + _MIN_GAP) - now
        if wait > 0:
            time.sleep(wait)
        _last_send_at = time.monotonic()


def telegram_log_sink(message) -> None:
    """Loguru sink. `message` is a loguru.Message (str-like with .record)."""
    record = message.record
    try:
        if not _record_passes_filter(record["level"].name):
            return
        token = settings.telegram_bot_token.get_secret_value()
        chat_id = settings.telegram_user_id
        if not token or chat_id == 0:
            return
        text = _format_for_telegram(record)
        _throttle()
        # Sync POST — loguru sinks run in their own thread by default.
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=5.0,
        )
    except Exception:
        # Never propagate from a logging sink.
        return

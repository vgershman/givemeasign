"""loguru bootstrap. Call `configure_logging()` once at process start."""

from __future__ import annotations

import logging
import sys

from loguru import logger

from givemeasign.config import settings

_CONFIGURED = False


class _InterceptHandler(logging.Handler):
    """Route stdlib `logging` records through loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging(*, with_telegram_sink: bool = False) -> None:
    """Bootstrap loguru.

    `with_telegram_sink=True` wires a sink that forwards records to Telegram
    when the runtime toggle in bot_settings is on. Pipeline CLI invocations
    pass True so progress logs route to Telegram; ad-hoc commands can stay
    stderr-only by default.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
            "<level>{level: <8}</level> "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
            "| <level>{message}</level>"
        ),
        backtrace=True,
        diagnose=settings.environment != "production",
    )
    if with_telegram_sink:
        # Lazy import: avoids pulling Telegram deps in non-bot contexts.
        from givemeasign.telegram.log_sink import telegram_log_sink

        # Sink handles its own enabled/level filtering against bot_settings.
        logger.add(telegram_log_sink, level="DEBUG", enqueue=True)
    # Bridge stdlib logging (sqlalchemy, httpx, aiogram, anthropic, openai all use it).
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for name in ("sqlalchemy.engine", "httpx", "httpcore", "aiogram.event"):
        logging.getLogger(name).setLevel(logging.WARNING)
    _CONFIGURED = True


__all__ = ["configure_logging", "logger"]

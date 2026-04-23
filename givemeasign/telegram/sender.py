"""Pick today's deck of candidates and send them as Telegram cards."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.orm import Session

from givemeasign.config import settings
from givemeasign.db.models import Candidate, Score, SwipeVerdict
from givemeasign.db.session import session_scope
from givemeasign.llm.prompts.score_candidate import SCORER_VERSION
from givemeasign.observability.logging import logger
from givemeasign.telegram.cards import format_card_html, make_keyboard


@dataclass
class DeckResult:
    sent: int
    no_new: bool


def _today_in_tz() -> date:
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


def _fetch_deck_candidates(s: Session, user_id: int, limit: int) -> list[Candidate]:
    """Top-N scored candidates not yet swiped by this user."""
    swiped = (
        select(SwipeVerdict.candidate_id)
        .where(SwipeVerdict.user_id == user_id)
        .scalar_subquery()
    )
    stmt = (
        select(Candidate)
        .where(Candidate.status == "scored")
        .where(Candidate.id.not_in(swiped))
        .order_by(Candidate.aggregate_score.desc().nulls_last())
        .limit(limit)
    )
    return list(s.execute(stmt).scalars().all())


def _fetch_scores(s: Session, candidate_id: UUID) -> list[Score]:
    return list(
        s.execute(
            select(Score)
            .where(Score.candidate_id == candidate_id)
            .where(Score.scorer_version == SCORER_VERSION)
            .order_by(Score.dimension)
        )
        .scalars()
        .all()
    )


async def send_deck(
    *,
    bot: Bot | None = None,
    user_id: int | None = None,
    limit: int = 10,
    inter_message_delay: float = 0.25,
) -> DeckResult:
    """Send today's top-N scored candidates as Telegram cards.

    If `bot` is None, a temporary Bot is created from settings.telegram_bot_token.
    If `user_id` is None, settings.telegram_user_id is used.
    """
    target_user = user_id or settings.telegram_user_id
    if target_user == 0:
        raise RuntimeError("TELEGRAM_USER_ID not set — can't deliver deck")

    own_bot = False
    if bot is None:
        token = settings.telegram_bot_token.get_secret_value()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set — can't send deck")
        bot = Bot(token=token)
        own_bot = True

    today = _today_in_tz()

    try:
        # Pre-flight: fail fast with a helpful message if we can't reach the chat,
        # rather than firing 10 identical "chat not found" errors for each card.
        try:
            await bot.get_chat(target_user)
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                try:
                    me = await bot.get_me()
                    bot_handle = f"@{me.username}" if me.username else "your bot"
                except Exception:
                    bot_handle = "your bot"
                raise RuntimeError(
                    f"Telegram can't reach user_id={target_user}. "
                    f"Open {bot_handle} in Telegram and send /start (or any message) "
                    f"first — bots can only DM users who initiated a chat. "
                    f"Also double-check TELEGRAM_USER_ID via @userinfobot."
                ) from e
            raise

        # Pick + materialize candidates within a session, then release before async sends.
        with session_scope() as s:
            candidates = _fetch_deck_candidates(s, target_user, limit)
            if not candidates:
                logger.info("send_deck: no new scored candidates for this user")
                try:
                    await bot.send_message(
                        chat_id=target_user,
                        text="No new candidates today. Pipeline will refill overnight.",
                    )
                except TelegramAPIError as e:
                    logger.warning(f"send_deck: telegram error on empty notice: {e}")
                return DeckResult(sent=0, no_new=True)

            cards = []
            for rank, c in enumerate(candidates, start=1):
                scores = _fetch_scores(s, c.id)
                cards.append(
                    {
                        "candidate_id": str(c.id),
                        "html": format_card_html(c, scores, rank=rank, total=len(candidates)),
                    }
                )
            logger.info(f"send_deck: sending {len(cards)} card(s) to user_id={target_user}")

        # Send a header message first so the user knows a fresh deck arrived.
        try:
            await bot.send_message(
                chat_id=target_user,
                text=f"🎴 <b>Daily deck</b> — {today.isoformat()} · {len(cards)} candidate(s)",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError as e:
            logger.warning(f"send_deck: header send failed: {e}")

        sent = 0
        for card in cards:
            try:
                await bot.send_message(
                    chat_id=target_user,
                    text=card["html"],
                    parse_mode=ParseMode.HTML,
                    reply_markup=make_keyboard(card["candidate_id"]),
                    disable_web_page_preview=True,
                )
                sent += 1
            except TelegramAPIError as e:
                logger.error(f"send_deck: failed to send card {card['candidate_id']}: {e}")
            await asyncio.sleep(inter_message_delay)
        return DeckResult(sent=sent, no_new=False)
    finally:
        if own_bot:
            await bot.session.close()

"""Pick today's deck of candidates and send them as Telegram cards.

Anti-repeat (0013): once a candidate is included in a delivered deck, its
`last_delivered_at` and `last_delivered_score` are stamped. Future deck
selections hard-exclude already-delivered candidates UNLESS their current
`aggregate_score` has improved by `REPEAT_SCORE_IMPROVE_RATIO` (default 1.10
= +10%) on a later scoring pass — that's the single escape hatch.

Pre-0013 the deck only filtered swiped candidates, so anything the user
ignored re-appeared every day. With a static pool + frozen scores, the same
top-10 came back forever.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from givemeasign.config import settings
from givemeasign.db.models import Candidate, Score, SwipeVerdict
from givemeasign.db.session import session_scope
from givemeasign.i18n.strings import label
from givemeasign.i18n.translator import ensure_candidate_translation
from givemeasign.llm.prompts.score_candidate import SCORER_VERSION
from givemeasign.llm.router import LLMRouter
from givemeasign.observability.logging import logger
from givemeasign.telegram.cards import format_card_html, make_keyboard
from givemeasign.telegram.settings import get_display_locale

# How much aggregate_score must improve (multiplicatively) over the score at
# last delivery before we'll re-deliver. 1.10 = score must climb by ≥10%.
REPEAT_SCORE_IMPROVE_RATIO = 1.10


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
    """Top-N scored candidates eligible for delivery to this user.

    Eligibility rules:
      - status == 'scored'
      - not previously swiped by this user
      - either never delivered, OR aggregate_score has improved by
        ≥REPEAT_SCORE_IMPROVE_RATIO since the last delivery (re-score escape)
    """
    swiped = (
        select(SwipeVerdict.candidate_id)
        .where(SwipeVerdict.user_id == user_id)
        .scalar_subquery()
    )
    stmt = (
        select(Candidate)
        .where(Candidate.status == "scored")
        .where(Candidate.id.not_in(swiped))
        .where(
            or_(
                Candidate.last_delivered_at.is_(None),
                Candidate.aggregate_score
                > Candidate.last_delivered_score * REPEAT_SCORE_IMPROVE_RATIO,
            )
        )
        .order_by(Candidate.aggregate_score.desc().nulls_last())
        .limit(limit)
    )
    return list(s.execute(stmt).scalars().all())


def _mark_delivered(s: Session, candidate_ids: list[UUID]) -> None:
    """Stamp last_delivered_at + last_delivered_score on the just-shipped batch.

    Snapshot the candidate's CURRENT aggregate_score so the "improved by 10%"
    check on the next deck has a stable baseline.
    """
    if not candidate_ids:
        return
    s.execute(
        update(Candidate)
        .where(Candidate.id.in_(candidate_ids))
        .values(
            last_delivered_at=datetime.now(timezone.utc),
            last_delivered_score=Candidate.aggregate_score,
        )
    )


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

        locale = get_display_locale()
        router = LLMRouter() if locale != "en" else None

        # Pick + materialize candidates within a session, then release before async sends.
        with session_scope() as s:
            candidates = _fetch_deck_candidates(s, target_user, limit)
            if not candidates:
                logger.info("send_deck: no new scored candidates for this user")
                try:
                    await bot.send_message(
                        chat_id=target_user,
                        text=label("no_new_candidates", locale),
                    )
                except TelegramAPIError as e:
                    logger.warning(f"send_deck: telegram error on empty notice: {e}")
                return DeckResult(sent=0, no_new=True)

            # Materialize candidate ids + scores (detach so we can await outside the session).
            staged: list[tuple[Candidate, list[Score]]] = []
            for c in candidates:
                scores = _fetch_scores(s, c.id)
                for sc in scores:
                    s.expunge(sc)
                s.expunge(c)
                staged.append((c, scores))
            logger.info(
                f"send_deck: sending {len(staged)} card(s) to user_id={target_user} locale={locale}"
            )

        # Build card payloads. If non-English locale, translate each card lazily.
        cards = []
        for rank, (c, scores) in enumerate(staged, start=1):
            translated: dict | None = None
            if router is not None:
                try:
                    translated = await ensure_candidate_translation(c.id, locale, router)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"send_deck: translation failed for {c.id}, falling back to English: {e}"
                    )
                    translated = None
            cards.append(
                {
                    "candidate_id": str(c.id),
                    "html": format_card_html(
                        c,
                        scores,
                        rank=rank,
                        total=len(staged),
                        locale=locale,
                        translated=translated,
                    ),
                }
            )

        # Header message.
        header = (
            f"🎴 <b>{label('deck_header', locale)}</b> — "
            f"{today.isoformat()} · {len(cards)} {label('candidates_suffix', locale)}"
        )
        try:
            await bot.send_message(
                chat_id=target_user, text=header, parse_mode=ParseMode.HTML
            )
        except TelegramAPIError as e:
            logger.warning(f"send_deck: header send failed: {e}")

        sent = 0
        delivered_ids: list[UUID] = []
        for card in cards:
            try:
                await bot.send_message(
                    chat_id=target_user,
                    text=card["html"],
                    parse_mode=ParseMode.HTML,
                    reply_markup=make_keyboard(card["candidate_id"], locale=locale),
                    disable_web_page_preview=True,
                )
                sent += 1
                delivered_ids.append(UUID(card["candidate_id"]))
            except TelegramAPIError as e:
                logger.error(f"send_deck: failed to send card {card['candidate_id']}: {e}")
            await asyncio.sleep(inter_message_delay)
        # Stamp delivery so these candidates won't reappear in tomorrow's deck
        # unless their aggregate_score improves by ≥10% on a re-score.
        if delivered_ids:
            with session_scope() as s:
                _mark_delivered(s, delivered_ids)
            logger.info(f"send_deck: marked {len(delivered_ids)} candidate(s) as delivered")
        return DeckResult(sent=sent, no_new=False)
    finally:
        if own_bot:
            await bot.session.close()

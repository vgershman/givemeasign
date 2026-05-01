"""Long-running aiogram bot. Polls Telegram for updates.

Handles:
  - /start    : greet + show user_id
  - /deck     : send today's deck on demand
  - /log_on   : enable Telegram logging
  - /log_off  : disable Telegram logging
  - /log_status : show current logging state
  - /status   : compact pipeline counters
  - /help     : list commands
  - callback queries on verdict buttons (right/left/super/snooze)

Also runs an internal scheduler that fires the daily deck at the configured
hour:minute in the configured timezone, unless daily_deck_enabled is false.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from givemeasign.config import settings
from givemeasign.db.models import (
    Candidate,
    PainSignal,
    RawSignal,
    SwipeVerdict,
)
from givemeasign.db.session import session_scope
from givemeasign.observability.logging import logger
from givemeasign.research.runner import resume_pending, run_research
from givemeasign.telegram.cards import (
    VERDICT_RIGHT,
    VERDICT_SUPER,
    format_decision_footer,
    parse_callback_data,
)
from givemeasign.telegram.settings import get_display_locale
from givemeasign.telegram.sender import _today_in_tz, send_deck
from givemeasign.telegram.settings import (
    get_daily_deck_schedule,
    is_telegram_log_enabled,
    set_telegram_log,
)


def _is_authorized(user_id: int | None) -> bool:
    """Single-tenant: only the configured user_id is allowed."""
    return user_id is not None and user_id == settings.telegram_user_id


async def _safe_answer(
    query: CallbackQuery, text: str = "", *, show_alert: bool = False
) -> None:
    """query.answer() but swallow the 'query too old' error.

    Callback answers must reach Telegram within ~15 minutes of the click.
    If the bot was restarted or the user clicked an old card, the answer
    call will 400. The DB write that happens earlier in cb_verdict has
    already succeeded, so we just log and move on.
    """
    try:
        await query.answer(text, show_alert=show_alert)
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "query is too old" in msg or "query id is invalid" in msg:
            logger.info(f"callback answer skipped (stale query): {e}")
            return
        logger.warning(f"callback answer failed: {e}")
    except TelegramAPIError as e:
        logger.warning(f"callback answer failed: {e}")


# ------------------------------------------------------------------ commands


async def cmd_start(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    text = (
        "👋 <b>givemeasign</b>\n\n"
        f"Your Telegram user_id is <code>{uid}</code>.\n"
        f"Configured user_id is <code>{settings.telegram_user_id}</code>.\n\n"
        "Commands:\n"
        "  /deck — fetch today's top-N candidates\n"
        "  /status — pipeline counters\n"
        "  /log_on /log_off /log_status — Telegram log routing\n"
        "  /help — this list\n"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


async def cmd_help(message: Message) -> None:
    await cmd_start(message)


async def cmd_deck(message: Message, bot: Bot) -> None:
    if not _is_authorized(message.from_user.id if message.from_user else None):
        await message.answer("Not authorized.")
        return
    try:
        result = await send_deck(bot=bot)
        if result.no_new:
            await message.answer("No new candidates right now.")
        else:
            await message.answer(f"Sent {result.sent} card(s).")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"/deck failed: {e}")
        await message.answer(f"Deck send failed: {e}")


async def cmd_status(message: Message) -> None:
    if not _is_authorized(message.from_user.id if message.from_user else None):
        await message.answer("Not authorized.")
        return
    with session_scope() as s:
        raw_total = s.execute(select(func.count(RawSignal.id))).scalar() or 0
        raw_unproc = (
            s.execute(
                select(func.count(RawSignal.id)).where(RawSignal.processed_at.is_(None))
            ).scalar()
            or 0
        )
        pain_total = s.execute(select(func.count(PainSignal.id))).scalar() or 0
        cand_total = s.execute(select(func.count(Candidate.id))).scalar() or 0
        cand_scored = (
            s.execute(
                select(func.count(Candidate.id)).where(Candidate.status == "scored")
            ).scalar()
            or 0
        )
        cand_gated = (
            s.execute(
                select(func.count(Candidate.id)).where(Candidate.status == "gated_out")
            ).scalar()
            or 0
        )
        cand_dedup = (
            s.execute(
                select(func.count(Candidate.id)).where(
                    Candidate.status == "deduplicated"
                )
            ).scalar()
            or 0
        )
        verdict_count = s.execute(select(func.count(SwipeVerdict.id))).scalar() or 0

    text = (
        "📊 <b>Pipeline status</b>\n"
        f"raw_signals: {raw_total} (unprocessed: {raw_unproc})\n"
        f"pain_signals: {pain_total}\n"
        f"candidates: {cand_total} "
        f"(scored: {cand_scored}, gated: {cand_gated}, deduped: {cand_dedup})\n"
        f"swipe_verdicts: {verdict_count}\n"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


async def cmd_log_on(message: Message) -> None:
    if not _is_authorized(message.from_user.id if message.from_user else None):
        await message.answer("Not authorized.")
        return
    set_telegram_log(enabled=True)
    await message.answer("✅ Telegram log routing: <b>ON</b>", parse_mode=ParseMode.HTML)


async def cmd_log_off(message: Message) -> None:
    if not _is_authorized(message.from_user.id if message.from_user else None):
        await message.answer("Not authorized.")
        return
    set_telegram_log(enabled=False)
    await message.answer("🔕 Telegram log routing: <b>OFF</b>", parse_mode=ParseMode.HTML)


async def cmd_log_status(message: Message) -> None:
    state = "ON" if is_telegram_log_enabled() else "OFF"
    await message.answer(f"Telegram log routing is <b>{state}</b>", parse_mode=ParseMode.HTML)


# ------------------------------------------------------------------ callbacks


async def cb_verdict(query: CallbackQuery) -> None:
    if not _is_authorized(query.from_user.id if query.from_user else None):
        await _safe_answer(query, "Not authorized.", show_alert=True)
        return

    parsed = parse_callback_data(query.data or "")
    if parsed is None:
        await _safe_answer(query, "Bad callback.", show_alert=True)
        return
    verdict, candidate_id_str = parsed
    try:
        candidate_uuid = UUID(candidate_id_str)
    except ValueError:
        await _safe_answer(query, "Bad candidate id.", show_alert=True)
        return

    today = _today_in_tz()
    inserted = False
    try:
        with session_scope() as s:
            stmt = (
                pg_insert(SwipeVerdict)
                .values(
                    candidate_id=candidate_uuid,
                    user_id=query.from_user.id,
                    verdict=verdict,
                    deck_date=today,
                )
                .on_conflict_do_nothing(constraint="uq_verdict_cand_user")
                .returning(SwipeVerdict.id)
            )
            inserted = s.execute(stmt).scalar_one_or_none() is not None
    except Exception as e:  # noqa: BLE001
        logger.exception(f"verdict persist failed: {e}")
        await _safe_answer(query, "DB error — try again.", show_alert=True)
        return

    # Strip buttons + append decision footer so the user sees confirmation.
    try:
        original_html = query.message.html_text or query.message.text or ""
    except Exception:
        original_html = ""
    new_text = original_html + format_decision_footer(verdict, locale=get_display_locale())
    try:
        await query.message.edit_text(
            text=new_text,
            parse_mode=ParseMode.HTML,
            reply_markup=None,
        )
    except TelegramAPIError as e:
        logger.warning(f"edit_text failed: {e}")

    if inserted:
        await _safe_answer(query, "Recorded.")
    else:
        await _safe_answer(query, "Already recorded — no change.")

    # Right / super swipe → spawn deep-research in the background.
    # Fire-and-forget: the task has its own error handling and doesn't
    # block the callback handler.
    if verdict in (VERDICT_RIGHT, VERDICT_SUPER):
        bot = query.bot
        uid = query.from_user.id if query.from_user else settings.telegram_user_id

        async def _run_research_task() -> None:
            try:
                logger.info(
                    f"research: kicking off for {candidate_uuid} (verdict={verdict})"
                )
                await run_research(candidate_uuid, bot=bot, user_id=uid)
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    f"research: background task failed for {candidate_uuid}: {e}"
                )

        asyncio.create_task(_run_research_task())


# ------------------------------------------------------------------ scheduler


def _seconds_until(hour: int, minute: int, tz_name: str) -> float:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()


async def _run_auto_pipeline() -> None:
    """Run the daily fetch+extract+synth pipeline, then score new candidates.

    Best-effort: any exception is logged and swallowed so a flaky source can't
    block the deck delivery 30 min later. Cost-bounded by `auto_score_limit`.
    """
    # Lazy imports — these touch httpx + LLMs and aren't needed for normal
    # bot operation.
    from givemeasign.pipeline.run import run_pipeline
    from givemeasign.scoring.runner import score_candidates_batch

    logger.info("auto-pipeline: fetch+extract+synth starting")
    try:
        summary = await run_pipeline()
        logger.info(f"auto-pipeline: pipeline summary={summary.as_dict()}")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"auto-pipeline: pipeline failed: {e}")
        return

    logger.info(
        f"auto-pipeline: scoring up to {settings.auto_score_limit} new candidate(s)"
    )
    try:
        scoring = await score_candidates_batch(limit=settings.auto_score_limit)
        logger.info(
            f"auto-pipeline: scored={scoring.scored} gated={scoring.gated} "
            f"deduped={scoring.deduplicated} cost=${scoring.total_cost_usd:.4f}"
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"auto-pipeline: scoring failed: {e}")


async def _daily_deck_loop(bot: Bot) -> None:
    """Sleep until the deck time, optionally run pipeline+score N min before, send deck.

    `pipeline_lead_minutes` env controls the lead window. 0 = disabled (deck
    only, no auto-pipeline — handy for dev). The lead-time window is computed
    relative to the configured deck hour:minute so flipping deck time in
    bot_settings just shifts both stages forward.
    """
    logger.info("daily-deck scheduler started")
    while True:
        try:
            hour, minute, enabled = get_daily_deck_schedule()
            lead_min = max(0, settings.pipeline_lead_minutes)
            deck_wait_s = _seconds_until(hour, minute, settings.timezone)

            if lead_min > 0:
                # Sleep until pipeline_time = deck_time - lead_min, run pipeline,
                # then sleep the remaining lead_min before delivery.
                pipeline_wait_s = deck_wait_s - lead_min * 60
                if pipeline_wait_s < 0:
                    # Deck fires within `lead_min` — too late to run a fresh
                    # pipeline this cycle; send what we have.
                    logger.info(
                        f"daily-deck: deck fires in {deck_wait_s/60:.1f}m, "
                        f"skipping auto-pipeline this cycle"
                    )
                    pipeline_wait_s = None
                else:
                    logger.info(
                        f"daily-deck: auto-pipeline in {pipeline_wait_s/3600:.2f}h, "
                        f"deck in {deck_wait_s/3600:.2f}h "
                        f"({hour:02d}:{minute:02d} {settings.timezone}, enabled={enabled})"
                    )
                if pipeline_wait_s is not None:
                    await asyncio.sleep(pipeline_wait_s)
                    # Re-read enabled — if the deck got disabled while we slept,
                    # skip the pipeline cost too.
                    _, _, enabled = get_daily_deck_schedule()
                    if enabled:
                        await _run_auto_pipeline()
                    else:
                        logger.info("auto-pipeline: skipped (deck disabled)")
                    # Sleep the remaining minutes until deck time.
                    deck_wait_s = _seconds_until(hour, minute, settings.timezone)
            else:
                logger.info(
                    f"daily-deck: next fire in {deck_wait_s/3600:.2f}h at "
                    f"{hour:02d}:{minute:02d} {settings.timezone} "
                    f"(enabled={enabled}, auto-pipeline=off)"
                )

            await asyncio.sleep(deck_wait_s)
            _, _, enabled = get_daily_deck_schedule()
            if not enabled:
                logger.info("daily-deck: skipped (disabled in bot_settings)")
                await asyncio.sleep(61)  # avoid tight loop on the same minute
                continue
            try:
                result = await send_deck(bot=bot)
                logger.info(
                    f"daily-deck: sent={result.sent} no_new={result.no_new}"
                )
            except Exception as e:  # noqa: BLE001
                logger.exception(f"daily-deck: send failed: {e}")
        except asyncio.CancelledError:
            logger.info("daily-deck scheduler cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(f"daily-deck loop error: {e}")
            await asyncio.sleep(60)  # don't tight-loop on errors


# ------------------------------------------------------------------ main


def _build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_deck, Command("deck"))
    dp.message.register(cmd_status, Command("status"))
    dp.message.register(cmd_log_on, Command("log_on"))
    dp.message.register(cmd_log_off, Command("log_off"))
    dp.message.register(cmd_log_status, Command("log_status"))
    dp.callback_query.register(cb_verdict, F.data.startswith("v:"))
    return dp


async def run_bot() -> None:
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set — can't start bot")
    if settings.telegram_user_id == 0:
        logger.warning(
            "TELEGRAM_USER_ID is 0 — commands will be rejected as unauthorized"
        )
    bot = Bot(token=token)
    dp = _build_dispatcher()
    scheduler_task = asyncio.create_task(_daily_deck_loop(bot))

    # Resume any research packs left in pending/generating/complete-unsent
    # state from a previous bot invocation (crash, restart, etc.).
    async def _resume_task() -> None:
        try:
            n = await resume_pending(bot=bot, user_id=settings.telegram_user_id)
            if n:
                logger.info(f"research: resume_pending processed {n} pack(s)")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"research: resume_pending failed: {e}")

    resume_task = asyncio.create_task(_resume_task())

    try:
        logger.info("givemeasign bot starting (polling)…")
        # drop_pending_updates=True: on startup, discard updates queued by
        # Telegram while the bot was offline. Old callback_query IDs would
        # be stale anyway and would all fail with "query is too old".
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        scheduler_task.cancel()
        resume_task.cancel()
        for t in (scheduler_task, resume_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await bot.session.close()

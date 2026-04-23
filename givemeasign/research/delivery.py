"""Render a research_pack and deliver it to Telegram in <=4096-char chunks."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from uuid import UUID

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError

from givemeasign.config import settings
from givemeasign.db.models import Candidate, ResearchPack
from givemeasign.db.session import session_scope
from givemeasign.observability.logging import logger

TELEGRAM_MAX = 4096
_SOFT_MAX = 3800  # headroom for split safety


_RECOMMENDATION_ICON = {
    "go": "🟢",
    "maybe": "🟡",
    "pass": "🔴",
}


def _h(text: object) -> str:
    return escape(str(text) if text is not None else "")


def _effort_icon(effort: str | None) -> str:
    return {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(
        (effort or "").lower(), "◾"
    )


def _severity_icon(severity: str | None) -> str:
    return {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(
        (severity or "").lower(), "◾"
    )


def _header_section(candidate: Candidate, pack: ResearchPack, content: dict) -> str:
    tldr = _h(content.get("tldr") or pack.summary or "")
    rec = (content.get("recommendation") or pack.recommendation or "").lower()
    rec_icon = _RECOMMENDATION_ICON.get(rec, "⚪")
    rec_reason = _h(content.get("recommendation_reason") or "")
    agg = candidate.aggregate_score
    agg_str = f"{agg:.3f}" if agg is not None else "—"
    ts = (pack.generated_at or datetime.now(timezone.utc)).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    return (
        f"🔬 <b>Deep research</b>\n"
        f"<b>{_h(candidate.concept)}</b>\n"
        f"score {agg_str} · generated {ts}\n\n"
        f"<b>TL;DR</b>\n{tldr}\n\n"
        f"{rec_icon} <b>Recommendation:</b> {_h(rec.upper() or '—')} — {rec_reason}"
    )


def _market_section(content: dict) -> str:
    ctx = _h(content.get("market_context") or "")
    incumbents = content.get("incumbents") or []
    inc_lines = []
    for inc in incumbents[:6]:
        name = _h(inc.get("name") or "?")
        strengths = _h(inc.get("strengths") or "")
        gaps = _h(inc.get("gaps") or "")
        url = inc.get("url")
        name_html = (
            f'<a href="{_h(url)}">{name}</a>' if url and str(url).startswith("http") else f"<b>{name}</b>"
        )
        inc_lines.append(f"• {name_html}\n  ✅ {strengths}\n  ❌ {gaps}")
    incs_block = "\n\n".join(inc_lines) if inc_lines else "(none listed)"
    wedge = _h(content.get("differentiation_wedge") or "")
    return (
        f"<b>Market context</b>\n{ctx}\n\n"
        f"<b>Incumbents</b>\n{incs_block}\n\n"
        f"<b>Differentiation wedge</b>\n{wedge}"
    )


def _plan_section(content: dict) -> str:
    plan = content.get("build_plan_90d") or []
    plan_lines = []
    for row in plan[:6]:
        weeks = _h(row.get("weeks") or "?")
        focus = _h(row.get("focus") or "")
        deliv = _h(row.get("deliverable") or "")
        plan_lines.append(f"• <b>Weeks {weeks}</b>: {focus}\n  → {deliv}")
    plan_block = "\n".join(plan_lines) if plan_lines else "(no plan)"

    models = content.get("monetization_models") or []
    mon_lines = []
    for m in models[:4]:
        model_type = _h(m.get("model") or "?")
        price = _h(m.get("price_range") or "")
        reason = _h(m.get("reasoning") or "")
        mon_lines.append(f"• <b>{model_type}</b> ({price}): {reason}")
    mon_block = "\n".join(mon_lines) if mon_lines else "(none suggested)"

    return (
        f"<b>90-day build plan</b>\n{plan_block}\n\n"
        f"<b>Monetization</b>\n{mon_block}"
    )


def _channels_risks_section(content: dict) -> str:
    channels = content.get("traffic_channels") or []
    ch_lines = []
    for ch in channels[:6]:
        channel = _h(ch.get("channel") or "?")
        rationale = _h(ch.get("rationale") or "")
        effort = _effort_icon(ch.get("effort"))
        ch_lines.append(f"• {effort} <b>{channel}</b>: {rationale}")
    ch_block = "\n".join(ch_lines) if ch_lines else "(none)"

    risks = content.get("risks") or []
    risk_lines = []
    for r in risks[:8]:
        sev = _severity_icon(r.get("severity"))
        risk = _h(r.get("risk") or "")
        mit = _h(r.get("mitigation") or "")
        risk_lines.append(f"• {sev} {risk}\n   ↳ {mit}")
    risk_block = "\n\n".join(risk_lines) if risk_lines else "(none)"

    tam = _h(content.get("tam_sanity") or "")
    first_test = _h(content.get("first_validation_test") or "")

    return (
        f"<b>Traffic channels</b>\n{ch_block}\n\n"
        f"<b>Risks</b>\n{risk_block}\n\n"
        f"<b>TAM sanity</b>\n{tam}\n\n"
        f"🎯 <b>First validation test (this week)</b>\n{first_test}"
    )


def build_message_chunks(candidate: Candidate, pack: ResearchPack) -> list[str]:
    """Return 3–4 Telegram HTML chunks, each under Telegram's size limit."""
    content = pack.content_json or {}
    sections = [
        _header_section(candidate, pack, content),
        _market_section(content),
        _plan_section(content),
        _channels_risks_section(content),
    ]
    # Enforce hard cap defensively — split oversized sections at blank lines.
    out: list[str] = []
    for sec in sections:
        if len(sec) <= _SOFT_MAX:
            out.append(sec)
            continue
        buf = ""
        for para in sec.split("\n\n"):
            piece = (buf + "\n\n" + para) if buf else para
            if len(piece) > _SOFT_MAX and buf:
                out.append(buf)
                buf = para
            else:
                buf = piece
        if buf:
            out.append(buf)
    return out


# ---------- sender ----------


async def deliver_pack(
    pack_id: UUID,
    *,
    bot: Bot | None = None,
    user_id: int | None = None,
    inter_message_delay: float = 0.35,
) -> bool:
    """Render a completed pack and send it as multi-message Telegram flow.

    Returns True on success. On success, marks sent_at on the pack.
    """
    target_user = user_id or settings.telegram_user_id
    if target_user == 0:
        logger.error("deliver_pack: TELEGRAM_USER_ID not set")
        return False

    own_bot = False
    if bot is None:
        token = settings.telegram_bot_token.get_secret_value()
        if not token:
            logger.error("deliver_pack: TELEGRAM_BOT_TOKEN not set")
            return False
        bot = Bot(token=token)
        own_bot = True

    try:
        # Load pack + candidate inside one session; detach for async send.
        with session_scope() as s:
            pack = s.get(ResearchPack, pack_id)
            if pack is None:
                logger.error(f"deliver_pack: pack {pack_id} not found")
                return False
            if pack.status != "complete":
                logger.warning(
                    f"deliver_pack: pack {pack_id} status={pack.status}, skipping"
                )
                return False
            candidate = s.get(Candidate, pack.candidate_id)
            if candidate is None:
                logger.error(
                    f"deliver_pack: candidate {pack.candidate_id} missing for pack {pack_id}"
                )
                return False
            _ = pack.content_json  # force-load before expunge
            s.expunge(pack)
            s.expunge(candidate)

        chunks = build_message_chunks(candidate, pack)
        logger.info(
            f"deliver_pack: sending {len(chunks)} chunk(s) for {pack.candidate_id}"
        )
        import asyncio

        for chunk in chunks:
            try:
                await bot.send_message(
                    chat_id=target_user,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except TelegramAPIError as e:
                logger.error(f"deliver_pack: send failed: {e}")
                return False
            await asyncio.sleep(inter_message_delay)

        with session_scope() as s:
            fresh = s.get(ResearchPack, pack_id)
            if fresh is not None:
                fresh.status = "sent"
                fresh.sent_at = datetime.now(timezone.utc)
        return True
    finally:
        if own_bot:
            await bot.session.close()

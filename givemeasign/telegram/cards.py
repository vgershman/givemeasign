"""Render a Candidate as a Telegram message + inline keyboard."""

from __future__ import annotations

from html import escape
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from givemeasign.db.models import Candidate, Score
from givemeasign.i18n.strings import label

# Verdict tokens used in callback_data and stored in swipe_verdicts.verdict.
VERDICT_RIGHT = "right"
VERDICT_LEFT = "left"
VERDICT_SUPER = "super"
VERDICT_SNOOZE = "snooze"

CALLBACK_PREFIX = "v"  # short to fit Telegram's 64-byte callback_data limit


def make_callback_data(verdict: str, candidate_id: str) -> str:
    return f"{CALLBACK_PREFIX}:{verdict}:{candidate_id}"


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """Return (verdict, candidate_id) or None if malformed."""
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    return parts[1], parts[2]


def make_keyboard(candidate_id: str, *, locale: str = "en") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label("verdict_right", locale),
                    callback_data=make_callback_data(VERDICT_RIGHT, candidate_id),
                ),
                InlineKeyboardButton(
                    text=label("verdict_left", locale),
                    callback_data=make_callback_data(VERDICT_LEFT, candidate_id),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=label("verdict_super", locale),
                    callback_data=make_callback_data(VERDICT_SUPER, candidate_id),
                ),
                InlineKeyboardButton(
                    text=label("verdict_snooze", locale),
                    callback_data=make_callback_data(VERDICT_SNOOZE, candidate_id),
                ),
            ],
        ]
    )


def format_card_html(
    candidate: Candidate,
    scores: list[Score],
    *,
    rank: int,
    total: int,
    locale: str = "en",
    translated: dict[str, Any] | None = None,
) -> str:
    """Telegram-HTML formatted card body.

    When `translated` is provided (from i18n.translator.ensure_candidate_translation),
    its concept/target_user/value_prop/angles are used instead of the English
    originals. Static labels (User, Value, Angles) come from the locale bundle.
    """
    agg = candidate.aggregate_score
    agg_str = f"{agg:.3f}" if agg is not None else "—"

    source = translated or {}
    concept = escape(str(source.get("concept") or candidate.concept or ""))
    target_user = escape(
        str(source.get("target_user") or candidate.target_user or "—")
    )
    value_prop = escape(
        str(source.get("value_prop") or candidate.value_prop or "—")
    )
    angles_list = source.get("angles") if source else candidate.angles
    angles_list = list(angles_list or [])

    angles_block = ""
    if angles_list:
        angle_lines = "\n".join(f"  • {escape(str(a))}" for a in angles_list[:3])
        angles_block = f"\n\n🎬 <b>{label('card_angles', locale)}</b>\n{angle_lines}"

    # Compact dimension row (keep dimension names in English — they're
    # internal identifiers, not user-facing prose).
    if scores:
        dim_pairs = [f"{escape(s.dimension)} {s.value:.2f}" for s in scores]
        scores_line = " · ".join(dim_pairs)
    else:
        scores_line = "(no dimension scores)"

    user_label = label("card_user", locale)
    value_label = label("card_value", locale)
    score_word = label("card_score", locale)

    return (
        f"🎯 <b>{rank}/{total}</b> · {score_word} <b>{agg_str}</b>\n\n"
        f"<b>{concept}</b>\n\n"
        f"👤 <b>{user_label}</b>: {target_user}\n\n"
        f"💡 <b>{value_label}</b>: {value_prop}"
        f"{angles_block}\n\n"
        f"📊 {scores_line}"
    )


def format_decision_footer(verdict: str, *, locale: str = "en") -> str:
    """Short line appended to the card after the user clicks a verdict."""
    verdict_key = {
        VERDICT_RIGHT: "verdict_right",
        VERDICT_LEFT: "verdict_left",
        VERDICT_SUPER: "verdict_super",
        VERDICT_SNOOZE: "verdict_snooze",
    }.get(verdict, "verdict_right")
    return f"\n\n<i>✓ {label('decision_recorded', locale)}: {label(verdict_key, locale)}</i>"

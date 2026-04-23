"""Render a Candidate as a Telegram message + inline keyboard."""

from __future__ import annotations

from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from givemeasign.db.models import Candidate, Score

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


def make_keyboard(candidate_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👍 Yes",
                    callback_data=make_callback_data(VERDICT_RIGHT, candidate_id),
                ),
                InlineKeyboardButton(
                    text="👎 No",
                    callback_data=make_callback_data(VERDICT_LEFT, candidate_id),
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⭐ Deep dive",
                    callback_data=make_callback_data(VERDICT_SUPER, candidate_id),
                ),
                InlineKeyboardButton(
                    text="💤 Later",
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
) -> str:
    """Telegram-HTML formatted card body."""
    agg = candidate.aggregate_score
    agg_str = f"{agg:.3f}" if agg is not None else "—"
    concept = escape(candidate.concept or "")
    target_user = escape(candidate.target_user or "—")
    value_prop = escape(candidate.value_prop or "—")
    angles = candidate.angles or []
    angles_block = ""
    if angles:
        angle_lines = "\n".join(f"  • {escape(a)}" for a in angles[:3])
        angles_block = f"\n\n🎬 <b>Angles</b>\n{angle_lines}"

    # Compact dimension row.
    if scores:
        dim_pairs = [f"{escape(s.dimension)} {s.value:.2f}" for s in scores]
        scores_line = " · ".join(dim_pairs)
    else:
        scores_line = "(no dimension scores)"

    return (
        f"🎯 <b>{rank}/{total}</b> · score <b>{agg_str}</b>\n\n"
        f"<b>{concept}</b>\n\n"
        f"👤 <b>User</b>: {target_user}\n\n"
        f"💡 <b>Value</b>: {value_prop}"
        f"{angles_block}\n\n"
        f"📊 {scores_line}"
    )


def format_decision_footer(verdict: str) -> str:
    """Short line appended to the card after the user clicks a verdict."""
    label = {
        VERDICT_RIGHT: "👍 Yes",
        VERDICT_LEFT: "👎 No",
        VERDICT_SUPER: "⭐ Deep dive",
        VERDICT_SNOOZE: "💤 Later",
    }.get(verdict, verdict)
    return f"\n\n<i>✓ Recorded: {label}</i>"

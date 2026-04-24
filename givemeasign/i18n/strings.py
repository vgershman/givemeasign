"""Static UI labels rendered alongside translated content.

Keys are grouped by the surface that renders them. Add a new locale by
extending the inner dicts.
"""

from __future__ import annotations

_LABELS: dict[str, dict[str, str]] = {
    "en": {
        # Deck header
        "deck_header": "Daily deck",
        "candidates_suffix": "candidate(s)",
        "no_new_candidates": "No new candidates today. Pipeline will refill overnight.",
        # Card
        "card_score": "score",
        "card_user": "User",
        "card_value": "Value",
        "card_angles": "Angles",
        # Card decision footer
        "decision_recorded": "Recorded",
        # Research header
        "research_header": "Deep research",
        "research_generated_at": "generated",
        "research_tldr": "TL;DR",
        "research_recommendation": "Recommendation",
        "research_market_context": "Market context",
        "research_incumbents": "Incumbents",
        "research_wedge": "Differentiation wedge",
        "research_build_plan": "90-day build plan",
        "research_monetization": "Monetization",
        "research_traffic": "Traffic channels",
        "research_risks": "Risks",
        "research_tam": "TAM sanity",
        "research_first_test": "First validation test (this week)",
        "research_none": "(none)",
        "research_none_listed": "(none listed)",
        "research_none_suggested": "(none suggested)",
        "research_no_plan": "(no plan)",
        # Verdict labels (button feedback)
        "verdict_right": "👍 Yes",
        "verdict_left": "👎 No",
        "verdict_super": "⭐ Deep dive",
        "verdict_snooze": "💤 Later",
    },
    "ru": {
        # Deck header
        "deck_header": "Подборка идей",
        "candidates_suffix": "идей",
        "no_new_candidates": "Новых кандидатов сегодня нет. Пайплайн пополнит их к утру.",
        # Card
        "card_score": "балл",
        "card_user": "Пользователь",
        "card_value": "Ценность",
        "card_angles": "Позиционирование",
        "decision_recorded": "Записано",
        # Research header
        "research_header": "Глубокое исследование",
        "research_generated_at": "сгенерировано",
        "research_tldr": "TL;DR",
        "research_recommendation": "Рекомендация",
        "research_market_context": "Контекст рынка",
        "research_incumbents": "Конкуренты",
        "research_wedge": "Отличие от конкурентов",
        "research_build_plan": "План на 90 дней",
        "research_monetization": "Монетизация",
        "research_traffic": "Каналы привлечения",
        "research_risks": "Риски",
        "research_tam": "Оценка TAM",
        "research_first_test": "Первый тест (на этой неделе)",
        "research_none": "(нет)",
        "research_none_listed": "(не перечислены)",
        "research_none_suggested": "(не предложены)",
        "research_no_plan": "(нет плана)",
        # Verdict labels
        "verdict_right": "👍 Да",
        "verdict_left": "👎 Нет",
        "verdict_super": "⭐ Исследовать",
        "verdict_snooze": "💤 Позже",
    },
}


def label(key: str, locale: str = "en") -> str:
    """Return a UI string for the given locale, falling back to English."""
    bundle = _LABELS.get(locale) or _LABELS["en"]
    if key in bundle:
        return bundle[key]
    return _LABELS["en"].get(key, key)


def supported_locales() -> list[str]:
    return list(_LABELS.keys())

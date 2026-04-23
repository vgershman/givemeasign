"""Smoke tests for env, DB, LLM, and Telegram.

Run via `givemeasign doctor`. Designed to be the first thing you run after
setting up .env. Each check is independent — a failure in one doesn't prevent
the others from executing, so you see the full picture at once.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from sqlalchemy import text

from givemeasign.config import settings
from givemeasign.db.session import engine
from givemeasign.llm.router import LLMRouter
from givemeasign.observability.logging import logger


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


async def _check_config() -> Check:
    missing: list[str] = []
    if not settings.anthropic_api_key.get_secret_value():
        missing.append("ANTHROPIC_API_KEY")
    if not settings.openai_api_key.get_secret_value():
        missing.append("OPENAI_API_KEY")
    if not settings.telegram_bot_token.get_secret_value():
        missing.append("TELEGRAM_BOT_TOKEN")
    if settings.telegram_user_id == 0:
        missing.append("TELEGRAM_USER_ID")
    if missing:
        return Check("config", False, f"missing env vars: {', '.join(missing)}")
    return Check("config", True, "all required env vars present")


def _check_db() -> Check:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            ext = conn.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
        if not ext:
            return Check(
                "database",
                False,
                "pgvector extension missing — run `alembic upgrade head`",
            )
        return Check("database", True, "postgres reachable, pgvector installed")
    except Exception as e:
        return Check("database", False, f"connection failed: {e}")


async def _check_anthropic(router: LLMRouter) -> Check:
    try:
        reply = await router.ping_anthropic()
        return Check("anthropic", True, f"tier-1 model replied: {reply!r}")
    except Exception as e:
        return Check("anthropic", False, f"call failed: {e}")


async def _check_openai(router: LLMRouter) -> Check:
    try:
        dim = await router.ping_openai()
        return Check("openai", True, f"embedding dimension: {dim}")
    except Exception as e:
        return Check("openai", False, f"call failed: {e}")


async def _check_telegram() -> Check:
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        return Check("telegram", False, "no token set")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
            r.raise_for_status()
            data = r.json()
        if not data.get("ok"):
            return Check("telegram", False, f"api error: {data}")
        bot = data["result"]
        return Check("telegram", True, f"bot @{bot['username']} (id={bot['id']})")
    except Exception as e:
        return Check("telegram", False, f"call failed: {e}")


async def run_all() -> list[Check]:
    router = LLMRouter()
    config_check = await _check_config()
    db_check = _check_db()
    checks: list[Check] = [config_check, db_check]
    # Only attempt remote calls if config looks sane — saves time and noise.
    if config_check.ok:
        anthropic_check, openai_check, tg_check = await asyncio.gather(
            _check_anthropic(router),
            _check_openai(router),
            _check_telegram(),
        )
        checks.extend([anthropic_check, openai_check, tg_check])
    return checks


def print_checks(checks: list[Check]) -> bool:
    """Log each check; return True iff all passed."""
    all_ok = True
    for c in checks:
        mark = "[OK]  " if c.ok else "[FAIL]"
        line = f"  {mark} {c.name:<10} {c.detail}"
        if c.ok:
            logger.info(line)
        else:
            logger.error(line)
            all_ok = False
    return all_ok

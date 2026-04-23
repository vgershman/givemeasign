"""Environment-driven settings, single source of truth for the app."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Environment ---
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    timezone: str = "UTC"

    # --- Database ---
    database_url: str = Field(
        default="postgresql+psycopg://givemeasign:givemeasign_dev@localhost:5432/givemeasign"
    )

    # --- LLM ---
    anthropic_api_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    llm_model_tier1: str = "claude-haiku-4-5-20251001"
    llm_model_tier2: str = "claude-sonnet-4-6"
    llm_model_tier3: str = "claude-sonnet-4-6"
    llm_model_tier4: str = "claude-opus-4-6"
    embedding_model: str = "text-embedding-3-small"

    # --- Sources ---
    reddit_client_id: SecretStr = SecretStr("")
    reddit_client_secret: SecretStr = SecretStr("")
    reddit_user_agent: str = "givemeasign/0.1"
    reddit_default_subreddits: str = (
        "Entrepreneur,SaaS,SideProject,indiebiz,startups,smallbusiness,indiehackers"
    )
    product_hunt_token: SecretStr = SecretStr("")
    ahrefs_api_key: SecretStr = SecretStr("")

    # --- Telegram ---
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_user_id: int = 0

    # --- Budgets (USD) ---
    daily_budget_usd: float = 5.00
    monthly_budget_usd: float = 150.00

    # --- Derived helpers ---

    @property
    def database_url_sync(self) -> str:
        """Sync SQLAlchemy URL — used by alembic and the default engine."""
        return self.database_url

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value()) and self.telegram_user_id != 0


settings = Settings()

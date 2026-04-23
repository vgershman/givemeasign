# givemeasign

Autonomous startup-idea scoring pipeline for solopreneurs. Mines community pain signals, validates with keyword-demand data, and delivers a daily top-10 to Telegram at 23:00 for swipe-based feedback. The system learns from swipes and biases future sourcing accordingly.

## Quick start

```bash
cp .env.example .env
# edit .env with API keys (Anthropic, OpenAI, Telegram)

docker compose up -d postgres
docker compose run --rm app alembic upgrade head
docker compose run --rm app givemeasign doctor
```

`doctor` validates env vars, DB connectivity + pgvector, Anthropic and OpenAI API keys, and the Telegram bot token. If it's green, the foundation is ready for the next milestone.

## Repo layout

- `givemeasign/config.py` — pydantic settings (env-driven).
- `givemeasign/db/` — SQLAlchemy models + session helpers.
- `givemeasign/llm/` — provider router (Anthropic + OpenAI).
- `givemeasign/sources/` — one adapter per external source (M2+).
- `givemeasign/pipeline/` — tier-1→4 stages (M2+).
- `givemeasign/scoring/` — multiplicative aggregate + hard gates (M4).
- `givemeasign/telegram/` — aiogram bot + card rendering (M5).
- `alembic/` — DB migrations.

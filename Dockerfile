FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Minimal system deps for M1. Playwright system deps come later with M3.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Dep-install layer — cached unless pyproject.toml or package root changes.
COPY pyproject.toml README.md ./
COPY givemeasign ./givemeasign
RUN uv pip install --system -e ".[dev]"

# Remaining source (alembic, scripts, tests, etc.)
COPY . .

# In dev, compose mounts ./:/app so live source wins. In prod, image ships source as-is.
CMD ["bash"]

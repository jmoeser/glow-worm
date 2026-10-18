# ---------- build stage ----------
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder

# Install uv for fast, reproducible dependency resolution.
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv

WORKDIR /app

# Copy only dependency files first (layer caching)
COPY pyproject.toml uv.lock README.md ./

# Install production dependencies into a virtual env
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source
COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini ./
COPY scripts/ scripts/

# Install the project itself
RUN uv sync --frozen --no-dev


# ---------- runtime stage ----------
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

RUN groupadd --system appuser && useradd --system --gid appuser --no-create-home appuser && \
    mkdir -p /data && chown appuser:appuser /data

WORKDIR /app

# Copy the virtual env and source from the builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app app/
COPY --from=builder /app/alembic alembic/
COPY --from=builder /app/alembic.ini ./
COPY --from=builder /app/scripts scripts/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# SQLite database lives in a volume so it persists across container restarts
VOLUME ["/data"]
ENV DATABASE_URL="sqlite:////data/glow-worm.db"

# Default to secure cookies in production
ENV SECURE_COOKIES="true"

EXPOSE 8000

# Run migrations then start the server
# Switch to non-root user for the running process
USER appuser

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

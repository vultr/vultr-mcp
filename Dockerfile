# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv for fast, reproducible installs from the locked deps.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps first (cached layer) using the lockfile, then the source.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY openapi.json ./
# The reviewed tool definitions. Without these the server still runs, it just
# falls back to the generated surface for every operation.
COPY interface ./interface
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    VULTR_MCP_TRANSPORT=http \
    SERVER_HOST=0.0.0.0 \
    SERVER_PORT=8080

EXPOSE 8080

# Non-root.
RUN useradd -u 1000 -m app && chown -R app:app /app
USER app

CMD ["python", "-m", "vultr_mcp"]

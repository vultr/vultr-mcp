# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv for fast, reproducible installs from the locked deps.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Precompile dependencies to .pyc at build time. Kept because it is free and
# strictly better than shipping 3035 .py files with no bytecode, but be clear
# about what it does NOT buy: boot at 0.2 CPU measured 302s before this change
# and 309s after. Bytecode compilation was not the cost.
#
# The cost is create_http_app building 42 FastMCP servers -- the root plus one
# per category tag -- each running from_openapi over a 500-operation spec. That
# is 38s on an idle full core and ~300s throttled, and it is why the startup
# probe in k8s/deployment.yaml is sized the way it is. VULTR_MCP_CATEGORY_ENDPOINTS
# trims the category list and is the real lever if boot time needs to come down.
ENV UV_COMPILE_BYTECODE=1

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

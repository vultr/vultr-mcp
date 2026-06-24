# ============================================================
# Vultr MCP Server — Multi-stage Dockerfile
# ============================================================
# Supports both STDIO (local) and HTTP (remote) transport modes.
#
# Build:
#   docker build -t vultr/mcp:latest .
#
# Run (STDIO — local MCP client):
#   docker run --rm -i \
#     -e VULTR_API_KEY=*** \
#     vultr/mcp:latest
#
# Run (HTTP — hosted):
#   docker run --rm -p 8000:8000 \
#     -e VULTR_MCP_TRANSPORT=http \
#     -e VULTR_PER_USER_MODE=true \
#     vultr/mcp:latest
# ============================================================

# ---------------------------------------------------------------------------
# Stage 1: Build — use pre-installed vendor from host
# ---------------------------------------------------------------------------
FROM composer:2 AS build

WORKDIR /app

COPY composer.json composer.lock* ./
COPY vendor/ vendor/
COPY src/ src/
COPY bin/ bin/

RUN composer dump-autoload --no-dev --optimize
# ---------------------------------------------------------------------------
# Stage 2: Runtime — minimal PHP image
# ---------------------------------------------------------------------------
FROM php:8.4-cli-alpine AS runtime

# Install posix extension (for STDIO auto-detection) and opcache
RUN apk add --no-cache \
    linux-headers \
    && docker-php-ext-install \
        posix \
        opcache \
    && apk del linux-headers

# Production PHP config
COPY <<'EOF' /usr/local/etc/php/conf.d/production.ini
opcache.enable=1
opcache.enable_cli=1
opcache.jit=1255
opcache.jit_buffer_size=64M
opcache.validate_timestamps=0
opcache.max_accelerated_files=2000
memory_limit=128M
max_execution_time=30
display_errors=0
log_errors=1
error_log=/dev/stderr
EOF

WORKDIR /app

# Copy built application from build stage
COPY --from=build /app ./

# Copy non-code files
COPY openapi.json ./
COPY .env.example .env.example

# Default environment
ENV VULTR_MCP_TRANSPORT=stdio \
    VULTR_PER_USER_MODE=true \
    SSL_VERIFY=true

# Health check (only meaningful in HTTP mode)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD php -r "if(getenv('VULTR_MCP_TRANSPORT')==='http'){exit(@fsockopen('localhost',8000)!==false?0:1);}else{exit(0);}"

# Non-root user for security
RUN addgroup -S mcp && adduser -S mcp -G mcp \
    && chown -R mcp:mcp /app
USER mcp

# STDIO entrypoint — MCP clients spawn this process and communicate via STDIN/STDOUT
# HTTP entrypoint — starts the built-in PHP server for web requests
CMD ["php", "-S", "0.0.0.0:8000", "src/Server.php"]

# Default: STDIO mode. Override for HTTP:
#   docker run ... vultr/mcp:latest php src/Server.php
#   (the ENTRYPOINT already runs the server; just set VULTR_MCP_TRANSPORT=http)

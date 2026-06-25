# ============================================================
# Vultr MCP Server — FrankenPHP
# ============================================================
# Supports both STDIO (local) and HTTP (FrankenPHP worker) transport modes.
#
# Build:
#   docker build -t vultr/mcp:latest .
#
# Run (HTTP — FrankenPHP worker mode):
#   docker run --rm -p 8000:8000 \
#     -e VULTR_PER_USER_MODE=true \
#     vultr/mcp:latest
#
# Run (STDIO — local MCP client):
#   docker run --rm -i \
#     -e VULTR_API_KEY=*** \
#     vultr/mcp:latest php bin/console mcp:stdio
# ============================================================

# ---------------------------------------------------------------------------
# Stage 1: Build — install Composer dependencies
# ---------------------------------------------------------------------------
FROM composer:2 AS build

WORKDIR /app

COPY composer.json composer.lock* ./
COPY src/ src/
COPY bin/ bin/

RUN composer install --no-dev --optimize-autoloader --no-interaction --prefer-source

# ---------------------------------------------------------------------------
# Runtime — FrankenPHP with PHP 8.4
# ---------------------------------------------------------------------------
FROM dunglas/frankenphp:1-php8.4 AS runtime

RUN install-php-extensions posix opcache

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

# Copy entire application (including vendor/ from local composer install)
COPY . .

# Default environment — HTTP mode with per-user API keys
ENV VULTR_PER_USER_MODE=true \
    SSL_VERIFY=true

RUN groupadd -r mcp && useradd -r -g mcp mcp \
    && chown -R mcp:mcp /app
USER mcp

# Health check for K8s probes
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD php -r "if(@fsockopen('localhost',8080)!==false){exit(0);}else{exit(1);}"

# FrankenPHP worker mode — keeps the app booted in memory
EXPOSE 8080

CMD ["frankenphp", "php-server", "--root=/app/public", "--listen=:8080"]
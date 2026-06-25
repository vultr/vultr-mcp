# ============================================================
# Vultr MCP Server — FrankenPHP
# ============================================================

# ---------------------------------------------------------------------------
# Stage 1: Build — install Composer dependencies
# ---------------------------------------------------------------------------
FROM composer:2 AS build

WORKDIR /app

COPY composer.json composer.lock* ./
COPY src/ src/
COPY bin/ bin/

RUN COMPOSER_DISABLE_TLS=1 composer install --no-dev --optimize-autoloader --no-interaction --prefer-dist

# ---------------------------------------------------------------------------
# Runtime — FrankenPHP with PHP 8.4
# ---------------------------------------------------------------------------
FROM dunglas/frankenphp:1-php8.4 AS runtime

RUN install-php-extensions posix opcache

# Strip setcap from frankenphp binary — we listen on port 8080 (not privileged).
# The setcap causes "Operation not permitted" in K8s when capabilities are dropped.
RUN setcap -r /usr/local/bin/frankenphp

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

# Create non-root user and set ownership
RUN groupadd -r mcp && useradd -r -g mcp mcp \
    && chown -R mcp:mcp /app \
    && chown -R mcp:mcp /data/caddy /config/caddy

USER mcp

ENV VULTR_PER_USER_MODE=true \
    SSL_VERIFY=true

EXPOSE 8080

CMD ["frankenphp", "php-server", "--root=/app/public", "--listen=:8080"]
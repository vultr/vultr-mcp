# syntax=docker/dockerfile:1

# Two images from one file:
#
#   docker build .                        the server alone (the default)
#   docker build --target with-shipper .  the server plus the clicktail log
#                                          shipper, for the sidecar in
#                                          k8s/deployment.yaml
#
# The default never touches the shipper stages -- BuildKit builds only what the
# target needs -- so building this repo downloads nothing that is not public.

FROM python:3.12-slim AS app

# Links the image on ghcr.io to this repo, so the package inherits the repo's
# access rules -- and so a GitHub Actions job using GITHUB_TOKEN can push to it
# later, which it cannot for a package first pushed by hand without this link.
LABEL org.opencontainers.image.source="https://github.com/vultr/vultr-mcp"

# uv for fast, reproducible installs from the locked deps.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Precompile dependencies to .pyc at build time. Kept because it is free and
# strictly better than shipping 3035 .py files with no bytecode, but be clear
# about what it does NOT buy: boot at 0.2 CPU measured 302s before this change
# and 309s after. Bytecode compilation was not the cost.
#
# The boot cost that mattered was the interface layer being recompiled once per
# server -- 35 servers, 0.6s each, identical result every time. load_interface
# caches it now and boot is ~4.5s. from_openapi itself is 0.1s per server.
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


# clicktail, an internal log shipper. Where it comes from is not in this repo:
# the download URL is a build secret (id=clicktail_url), which BuildKit never
# writes into the image or its history the way it does build args. The bytes
# are pinned here, in review, by checksum -- a new binary is a diff to this
# line, not a quiet change to a secret.
#
#   docker build --target with-shipper --secret id=clicktail_url,env=CLICKTAIL_URL .
FROM python:3.12-slim AS clicktail
ARG CLICKTAIL_SHA256=0e66c08de5825f47b4f259dab438e348f37ba4cce750ceeb8f9187cbab6a782f
RUN --mount=type=secret,id=clicktail_url,required=true python - <<'EOF'
import sys
import urllib.request

# utf-8-sig and strip: a secret set by piping from PowerShell arrives as
# BOM + URL + CRLF, and the BOM alone makes urllib reject the URL (URLError).
url = open("/run/secrets/clicktail_url", encoding="utf-8-sig").read().strip()
try:
    urllib.request.urlretrieve(url, "/clicktail")
except Exception as exc:  # noqa: BLE001
    # The exception's text can carry the URL, and CI logs of a public repo are
    # public. The type is enough to act on.
    sys.exit(f"clicktail download failed: {type(exc).__name__}")
EOF
RUN echo "${CLICKTAIL_SHA256}  /clicktail" | sha256sum -c - && chmod 0755 /clicktail

# Baked in rather than fetched at pod start, so a pod never depends on where
# the binary is hosted. The server never runs or imports it.
FROM app AS with-shipper
COPY --from=clicktail /clicktail /usr/local/bin/clicktail


# Last, so it is what a plain `docker build .` produces.
FROM app

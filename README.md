# vultr-mcp

A **PHP MCP (Model Context Protocol) server** for the [Vultr](https://www.vultr.com/) cloud platform.

Exposes Vultr's REST API v2 as MCP tools so AI agents (Claude, Copilot, Cursor, etc.) can provision and manage cloud infrastructure in natural language.

Supports **both local (STDIO) and remote (HTTP)** transport modes.

## Requirements

- PHP 8.2+ (with `posix` extension for STDIO auto-detection)
- Composer

---

## Quick Start

### Local Use (STDIO) — Recommended for Individuals

The simplest way: Docker with your own API key.

```bash
docker run --rm -i \
  -e VULTR_API_KEY=YOUR_VULTR_API_KEY \
  vultr/mcp:latest
```

Or run directly with PHP:

```bash
git clone https://github.com/vultr/vultr-mcp.git
cd vultr-mcp
composer install
VULTR_API_KEY=YOUR_VULTR_API_KEY php bin/console mcp:stdio
```

The server auto-detects STDIO mode when launched by an MCP client.

### Remote Use (HTTP) — For Hosted / Team Deployments

```bash
docker run --rm -p 8080:8080 \
  -e VULTR_PER_USER_MODE=true \
  vultr/mcp:latest
```

Each user provides their own Vultr API key via the `X-Vultr-API-Key` header.

---

## Client Configuration

### Claude Desktop

Add to your `claude_desktop_config.json`:

**With Docker (recommended):**

```json
{
  "mcpServers": {
    "vultr": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-e", "VULTR_API_KEY", "vultr/mcp:latest"],
      "env": {
        "VULTR_API_KEY": "YOUR_VULTR_API_KEY"
      }
    }
  }
}
```

**With PHP:**

```json
{
  "mcpServers": {
    "vultr": {
      "command": "php",
      "args": ["/absolute/path/to/vultr-mcp/bin/console", "mcp:stdio"],
      "env": {
        "VULTR_API_KEY": "YOUR_VULTR_API_KEY"
      }
    }
  }
}
```

### VS Code Copilot

Add to your `.vscode/mcp.json` or VS Code settings:

```json
{
  "servers": {
    "vultr": {
      "type": "stdio",
      "command": "docker",
      "args": ["run", "--rm", "-i", "-e", "VULTR_API_KEY", "vultr/mcp:latest"],
      "env": {
        "VULTR_API_KEY": "YOUR_VULTR_API_KEY"
      }
    }
  }
}
```

### Cursor

Add to your `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "vultr": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-e", "VULTR_API_KEY", "vultr/mcp:latest"],
      "env": {
        "VULTR_API_KEY": "YOUR_VULTR_API_KEY"
      }
    }
  }
}
```

### Remote HTTP Endpoint

For AI clients that support remote MCP endpoints (Streamable HTTP transport):

```
URL:  https://vultrmcp.com
Auth: X-Vultr-API-Key: YOUR_VULTR_API_KEY
```

For clients that don't support custom headers, use the query parameter fallback:

```
URL:  https://vultrmcp.com/?api_key=YOUR_V…_KEY
```

### Hermes (Nous Research)

Hermes supports HTTP/SSE and STDIO transports. For HTTP/SSE, use the query parameter method since Hermes doesn't support custom headers in the dashboard UI:

```
Name:        vultr
Transport:   HTTP/SSE
URL:         https://vultrmcp.com/?api_key=YOUR_V…_KEY
```

Or via the config file (`config.yaml`):

```yaml
mcp_servers:
  vultr:
    type: http
    url: https://vultrmcp.com/?api_key=YOUR_V…_KEY
```

For local STDIO mode:

```
Name:        vultr
Transport:   stdio
Command:     docker
Args:        run --rm -i -e VULTR_API_KEY vultr/mcp:latest
Environment: VULTR_API_KEY=YOUR_V…_KEY
```

---

## Project Structure

```
vultr-mcp/
├── bin/
│   └── console                 # CLI entry point (STDIO mode + generator)
├── k8s/                        # Kubernetes manifests
│   ├── configmap.yaml           # Transport mode, per-user settings, Redis config
│   ├── deployment.yaml          # MCP server deployment (2 replicas)
│   ├── ingress.yaml             # Traefik ingress with TLS
│   ├── redis.yaml               # Redis deployment + service (session store)
│   ├── secret.yaml              # MCP_AUTH_TOKEN secret
│   ├── service.yaml             # ClusterIP service
│   └── traefik-values.yaml      # Traefik Helm values
├── public/
│   └── index.php                # HTTP entry point
├── src/
│   ├── Auth/                    # Authentication middleware
│   ├── Generator/               # OpenAPI → MCP tool code generator
│   ├── Http/                    # Health check middleware
│   ├── Tools/                   # Generated MCP tool classes
│   │   ├── InstanceTools.php    # VPS instance operations
│   │   └── BareMetalTools.php   # Bare metal operations
│   ├── Utils/                   # VultrClient, RateLimiter, RequestContext
│   └── Server.php               # Main server bootstrap
├── composer.json
├── composer.lock
├── Dockerfile                   # Multi-stage build (FrankenPHP + PHP 8.4)
├── Caddyfile                    # FrankenPHP/Caddy config
└── openapi.json                 # Vultr OpenAPI v3 spec (for tool generation)
```

---

## Available MCP Tools

### Instances (17 tools)

| Tool | Description |
|---|---|
| `list_instances` | List all VPS instances (with filters) |
| `create_instance` | Create a new VPS instance |
| `get_instance` | Get details for a single instance |
| `update_instance` | Update instance attributes (label, plan, tags…) |
| `delete_instance` | Permanently delete an instance |
| `start_instance` | Start a stopped instance |
| `reboot_instance` | Reboot an instance |
| `reinstall_instance` | Reinstall OS on an instance |
| `halt_instance` | Hard-power-off an instance |
| `start_instances` | Start multiple instances at once |
| `reboot_instances` | Reboot multiple instances at once |
| `halt_instances` | Halt multiple instances at once |
| `get_instance_bandwidth` | Bandwidth usage for an instance |
| `get_instance_upgrades` | Available plan upgrades |
| `get_instance_user_data` | User data (base64) attached to instance |
| `list_instance_ipv4` | IPv4 addresses on an instance |
| `list_instance_ipv6` | IPv6 addresses on an instance |

### Bare Metal (14 tools)

| Tool | Description |
|---|---|
| `list_bare_metals` | List all Bare Metal instances |
| `create_bare_metal` | Create a new Bare Metal server |
| `get_bare_metal` | Get details for a single Bare Metal server |
| `update_bare_metal` | Update Bare Metal attributes |
| `delete_bare_metal` | Permanently delete a Bare Metal server |
| `start_bare_metal` | Start a Bare Metal server |
| `reboot_bare_metal` | Reboot a Bare Metal server |
| `reinstall_bare_metal` | Reinstall OS on a Bare Metal server |
| `halt_bare_metal` | Hard-power-off a Bare Metal server |
| `get_bare_metal_ipv4` | IPv4 addresses on a Bare Metal server |
| `get_bare_metal_ipv6` | IPv6 addresses on a Bare Metal server |
| `get_bare_metal_bandwidth` | Bandwidth usage |
| `get_bare_metal_user_data` | User data attached to instance |
| `get_bare_metal_upgrades` | Available plan upgrades |

---

## Transport Modes

### STDIO (Local)

Default when launched by an MCP client (stdin is a pipe). Reads JSON-RPC from STDIN, writes responses to STDOUT.

- No authentication needed (local pipe, no network)
- API key from `VULTR_API_KEY` environment variable
- Used by Claude Desktop, VS Code Copilot, Cursor

### HTTP (Remote / Hosted)

Automatically enabled in the FrankenPHP Docker image. Provides a Streamable HTTP + SSE endpoint on port 8080.

- Per-user API keys via `X-Vultr-API-Key` header
- Optional `MCP_AUTH_TOKEN` bearer gate
- Built-in CORS, DNS rebinding protection, protocol version validation (from MCP SDK)
- Health check at `/healthz`
- Redis-backed session store for multi-replica horizontal scaling (see [Kubernetes Deployment](#kubernetes-deployment))

### Auto-Detection

If `VULTR_MCP_TRANSPORT` is not set, the server auto-detects:
- **STDIN is a pipe** (launched by MCP client) → STDIO mode
- **STDIN is a TTY / undefined** (interactive / Docker with `-p`) → HTTP mode

---

## Authentication

### Local (STDIO) Mode

Set `VULTR_API_KEY` in your environment. No additional auth — STDIO is a local pipe.

```bash
VULTR_API_KEY=YOUR_KEY php bin/console mcp:stdio
```

### Remote (HTTP) Mode — Per-User API Keys

When `VULTR_PER_USER_MODE=true` (default), each client provides their own Vultr API key. The server accepts the key via three methods, in priority order:

**1. Header (preferred):**

```
X-Vultr-API-Key: YOUR_VULTR_API_KEY
```

**2. Query parameter (for MCP clients that don't support custom headers):**

```
https://vultrmcp.com/?api_key=YOUR_V…_KEY
```

**3. Bearer token (when `MCP_AUTH_TOKEN` is not set):**

```
Authorization: Bearer YOUR_V…_KEY
```

This fallback only applies when `MCP_AUTH_TOKEN` is not configured. When it is set, the `Authorization` header is used for the MCP auth gate instead.

The key is held in memory only for the duration of the request — never logged, never persisted to disk.

### Optional MCP Auth Token Gate

Set `MCP_AUTH_TOKEN` in your environment to add a second auth layer. Clients must send both:

```
Authorization: Bearer <MCP_AUTH_TOKEN>
X-Vultr-API-Key: <user_vultr_key>
```

This is useful for deployments where access to the MCP server itself should be restricted, separate from each user's Vultr account.

For production, replace the static token check in `VultrAuth::validateToken()` with JWT verification from an OAuth 2.0 / PKCE provider.

---

## Rate Limiting

Vultr enforces 30 requests/second. The `RateLimiter` class automatically retries on HTTP 429 with exponential back-off (up to 5 attempts, up to 30 s delay, with ±10% jitter). The `Retry-After` response header is honoured when present.

---

## Docker

The Docker image uses [FrankenPHP](https://frankenphp.dev/) for high-performance PHP serving with built-in Caddy and opcache/JIT.

### Build

> **Note:** If you are behind a TLS-intercepting proxy or VPN, run `composer install` locally before building. The Docker build stage copies the pre-installed `vendor/` directory instead of running `composer install` inside the container, which avoids SSL certificate failures when downloading packages from GitHub.

```bash
composer install --no-dev --no-interaction --optimize-autoloader
docker build -t vultr/mcp:latest .
```

### Run (STDIO)

```bash
docker run --rm -i \
  -e VULTR_API_KEY=*** \
  vultr/mcp:latest
```

### Run (HTTP)

```bash
docker run --rm -p 8080:8080 \
  -e VULTR_PER_USER_MODE=true \
  vultr/mcp:latest
```

### With MCP Auth Token (HTTP)

```bash
docker run --rm -p 8080:8080 \
  -e VULTR_PER_USER_MODE=true \
  -e MCP_AUTH_TOKEN=*** \
  vultr/mcp:latest
```

---

## Kubernetes Deployment

Deployed on [Vultr VKE](https://www.vultr.com/kubernetes/) with Traefik ingress and Let's Encrypt TLS.

### Apply all manifests:

```bash
kubectl apply -f k8s/
```

This creates:
- **ConfigMap** — transport mode, per-user settings, Redis connection config
- **Secret** — MCP_AUTH_TOKEN (edit before applying)
- **Redis Deployment + Service** — session store for multi-replica support
- **Deployment** — 2 replicas, resource limits, health probes on port 8080
- **Service** — ClusterIP on port 8000 → targetPort 8080
- **Ingress** — Traefik ingress with TLS at `mcp.vrnd.io`

### Multi-Replica Session Handling

MCP's Streamable HTTP transport uses SSE for server-to-client streaming and POST requests for client-to-server messages. With multiple replicas, these requests may land on different pods — the Redis-backed session store (`Psr16SessionStore`) ensures session state is shared across all pods so any pod can handle any request for a given session.

The Redis connection is configured via the `REDIS_HOST` and `REDIS_PORT` environment variables in the ConfigMap. Sessions have a 1-hour TTL.

### Ingress Controller

Install Traefik with Let's Encrypt certificate resolver:

```bash
helm repo add traefik https://traefik.github.io/charts
helm repo update
helm install traefik traefik/traefik \
  --namespace traefik --create-namespace \
  --set certificatesResolvers.letsencrypt.acme.email=your-email@vrnd.io \
  --set certificatesResolvers.letsencrypt.acme.storage=/data/acme.json \
  --set certificatesResolvers.letsencrypt.acme.tlsChallenge=true
```

---

## Regenerating Tools from the OpenAPI Spec

Download the latest Vultr OpenAPI spec from https://www.vultr.com/api/ and save it as `openapi.json` in the project root, then run:

```bash
php bin/console mcp:generate
```

This parses the OpenAPI spec, filters by the `instances` and `baremetal` tags, and generates typed PHP tool classes in `src/Tools/`. Generated files are meant to be checked in and committed. Re-running the generator after a spec update will overwrite them.

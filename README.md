# vultr-mcp

A **PHP MCP (Model Context Protocol) server** for the [Vultr](https://www.vultr.com/) cloud platform.

Exposes Vultr's REST API v2 as MCP tools so AI agents (Claude, Copilot, Cursor, etc.) can provision and manage cloud infrastructure in natural language.

Supports **both local (STDIO) and remote (HTTP)** transport modes.

## Requirements

- PHP 8.1+ (with `posix` extension for STDIO auto-detection)
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
VULTR_API_KEY=YOUR_VULTR_API_KEY php src/Server.php
```

The server auto-detects STDIO mode when launched by an MCP client.

### Remote Use (HTTP) — For Hosted / Team Deployments

```bash
docker run --rm -p 8000:8000 \
  -e VULTR_MCP_TRANSPORT=http \
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
      "args": ["/absolute/path/to/vultr-mcp/src/Server.php"],
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
URL:  https://mcp.vultr.com
Auth: X-Vultr-API-Key: YOUR_VULTR_API_KEY
```

---

## Project Structure

```
vultr-mcp/
├── bin/
│   └── generate.php               ← CLI tool regenerator
├── k8s/
│   ├── configmap.yaml              ← K8s ConfigMap (transport, mode settings)
│   ├── deployment.yaml             ← K8s Deployment (2 replicas, resource limits)
│   ├── service.yaml                ← K8s ClusterIP Service
│   ├── ingress.yaml                ← K8s Ingress (TLS, domain)
│   └── secret.yaml                 ← K8s Secret (MCP_AUTH_TOKEN)
├── src/
│   ├── Server.php                  ← Entry point (STDIO + HTTP dual transport)
│   ├── Auth/
│   │   └── VultrAuth.php           ← PSR-15 auth middleware (per-user API keys)
│   ├── Generator/
│   │   └── OpenApiGenerator.php    ← Parses OpenAPI spec → PHP tool classes
│   ├── Http/
│   │   └── HealthCheckMiddleware.php ← /healthz endpoint for K8s probes
│   ├── Tools/
│   │   ├── InstanceTools.php       ← MCP tools for /v2/instances
│   │   └── BareMetalTools.php     ← MCP tools for /v2/bare-metals
│   └── Utils/
│       ├── VultrClient.php         ← Guzzle wrapper (auth, JSON, error handling)
│       ├── VultrClientFactory.php  ← Per-request client factory
│       ├── RequestContext.php      ← Request-scoped API key holder
│       ├── RateLimiter.php         ← Exponential back-off retry
│       └── RateLimitException.php
├── Dockerfile                      ← Multi-stage (build + runtime)
├── composer.json
├── .env.example
└── README.md
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

Set `VULTR_MCP_TRANSPORT=http` to enable. Provides a Streamable HTTP + SSE endpoint.

- Per-user API keys via `X-Vultr-API-Key` header
- Optional `MCP_AUTH_TOKEN` bearer gate
- Built-in CORS, DNS rebinding protection, protocol version validation (from MCP SDK)
- Health check at `/healthz`
- Stateless — no session persistence, safe for horizontal scaling

### Auto-Detection

If `VULTR_MCP_TRANSPORT` is not set, the server auto-detects:
- **STDIN is a pipe** (launched by MCP client) → STDIO mode
- **STDIN is a TTY** (interactive / Docker with `-p`) → HTTP mode

---

## Authentication

### Local (STDIO) Mode

Set `VULTR_API_KEY` in your environment. No additional auth — STDIO is a local pipe.

```bash
VULTR_API_KEY=YOUR_KEY php src/Server.php
```

### Remote (HTTP) Mode — Per-User API Keys

When `VULTR_PER_USER_MODE=true` (default), each client provides their own Vultr API key:

```
X-Vultr-API-Key: YOUR_VULTR_API_KEY
```

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

### Build

```bash
docker build -t vultr/mcp:latest .
```

### Run (STDIO)

```bash
docker run --rm -i \
  -e VULTR_API_KEY=YOUR_KEY \
  vultr/mcp:latest
```

### Run (HTTP)

```bash
docker run --rm -p 8000:8000 \
  -e VULTR_MCP_TRANSPORT=http \
  -e VULTR_PER_USER_MODE=true \
  vultr/mcp:latest
```

### With MCP Auth Token (HTTP)

```bash
docker run --rm -p 8000:8000 \
  -e VULTR_MCP_TRANSPORT=http \
  -e VULTR_PER_USER_MODE=true \
  -e MCP_AUTH_TOKEN=a-secret-token \
  vultr/mcp:latest
```

---

## Kubernetes Deployment

Apply all manifests:

```bash
kubectl apply -f k8s/
```

This creates:
- **ConfigMap** — transport mode, per-user settings
- **Secret** — MCP_AUTH_TOKEN (edit before applying)
- **Deployment** — 2 replicas, resource limits, health probes
- **Service** — ClusterIP on port 8000
- **Ingress** — TLS at `mcp.vultr.com` (adjust host and TLS secret)

Customize the Ingress for your ingress controller and cert-manager setup.

---

## Regenerating Tools from the OpenAPI Spec

Download the latest Vultr OpenAPI spec from https://www.vultr.com/api/ and save it as `openapi.json` in the project root, then run:

```bash
php bin/generate.php
# or:
php bin/generate.php --spec=openapi.json --tags=instances,baremetal --output=src/Tools/
```

Generated files are committed to the repository. Review the diff before committing.

---

## License

MIT

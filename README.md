# vultr-mcp

A **PHP MCP (Model Context Protocol) server** for the [Vultr](https://www.vultr.com/) cloud platform.

Exposes **494 tools** across **39 categories** of the Vultr REST API v2 as MCP tools, so AI agents (Claude, Copilot, Cursor, Hermes, etc.) can provision and manage cloud infrastructure in natural language.

Supports **both local (STDIO) and remote (HTTP)** transport modes, with **path-based tool filtering** for token-efficient AI connections.

## Requirements

- PHP 8.4+ (with `posix` extension for STDIO auto-detection)
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

## Path-Based Tool Filtering (HTTP Mode)

When deployed as a remote HTTP server, you can connect to specific category endpoints to limit which tools the AI sees — saving tokens and context window space.

### All Tools (One Connection)

```
URL:  https://vultrmcp.com/
```

Returns all 494 tools. Best for clients with limited MCP connections or free plans.

### Category Endpoints (Token-Efficient)

Connect to a category path to load only that category's tools:

| Endpoint | Tools | Description |
|---|---|---|
| `/instances` | 35 | VPS instances (create, list, start, halt, reboot, etc.) |
| `/baremetal` | 25 | Bare Metal servers |
| `/kubernetes` | 26 | VKE clusters and node pools |
| `/load-balancer` | 19 | Load balancers and forwarding rules |
| `/dns` | 13 | DNS domains and records |
| `/firewall` | 9 | Firewall groups and rules |
| `/block` | 12 | Block storage volumes |
| `/snapshot` | 6 | Snapshots |
| `/ssh` | 5 | SSH keys |
| `/iso` | 6 | ISO images |
| `/reserved-ip` | 8 | Reserved IPs |
| `/vpcs` | 21 | VPCs and NAT gateways |
| `/plans` | 2 | Available plans |
| `/region` | 2 | Available regions |
| `/os` | 1 | Available operating systems |
| `/account` | 6 | Account info and bandwidth |
| `/billing` | 6 | Billing history and invoices |
| `/users` | 19 | User management |
| `/api-keys` | 4 | API key management |
| `/backup` | 2 | Backups |
| `/startup` | 5 | Startup scripts |
| `/marketplace` | 2 | Marketplace apps |
| `/clusters` | 9 | Container clusters |
| `/container-registry` | 32 | Container registries |
| `/managed-databases` | 66 | Managed databases (MySQL, PostgreSQL, Kafka, etc.) |
| `/s3` | 14 | S3-compatible object storage |
| `/vfs` | 10 | Vultr File Storage |
| `/cdns` | 15 | CDN pull/push zones |
| `/storage-gateways` | 7 | Storage gateways and exports |
| `/serverless-inference` | 6 | Serverless inference endpoints |
| `/iam` | 51 | IAM roles, policies, groups, trust relationships |
| `/organizations` | 15 | Organizations and invitations |
| `/oidc` | 16 | OIDC providers and issuers |
| `/scim` | 11 | SCIM user/group provisioning |
| `/instance-templates` | 5 | Instance templates |
| `/application` | 1 | Application definitions |
| `/logs` | 1 | Log retrieval |
| `/subaccount` | 2 | Subaccounts |
| `/tickets` | 8 | Support tickets |

### Example Client Config (Category Endpoints)

**Claude Desktop:**

```json
{
  "mcpServers": {
    "vultr-instances": {
      "url": "https://vultrmcp.com/instances"
    },
    "vultr-kubernetes": {
      "url": "https://vultrmcp.com/kubernetes"
    },
    "vultr-dns": {
      "url": "https://vultrmcp.com/dns"
    }
  }
}
```

**Hermes (Nous Research):**

```yaml
mcp_servers:
  - name: vultr-instances
    transport: http
    url: https://vultrmcp.com/instances?api_key=YOUR_VULTR_API_KEY
  - name: vultr-kubernetes
    transport: http
    url: https://vultrmcp.com/kubernetes?api_key=YOUR_VULTR_API_KEY
```

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
URL:  https://vultrmcp.com/
Auth: X-Vultr-API-Key: YOUR_VULTR_API_KEY
```

Or use a category endpoint for fewer tokens:

```
URL:  https://vultrmcp.com/instances
Auth: X-Vultr-API-Key: YOUR_VULTR_API_KEY
```

---

## Authentication

### Local (STDIO) Mode

Set `VULTR_API_KEY` in your environment. No additional auth — STDIO is a local pipe.

```bash
VULTR_API_KEY=YOUR_KEY php bin/console mcp:stdio
```

### Remote (HTTP) Mode — Per-User API Keys

When `VULTR_PER_USER_MODE=true` (default), each client provides their own Vultr API key. Three methods are supported:

**1. Header (preferred):**
```
X-Vultr-API-Key: YOUR_VULTR_API_KEY
```

**2. Query parameter (for clients that don't support custom headers, e.g. Hermes):**
```
https://vultrmcp.com/instances?api_key=YOUR_VULTR_API_KEY
```

**3. Bearer token (when `MCP_AUTH_TOKEN` is not set):**
```
Authorization: Bearer YOUR_VULTR_API_KEY
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
- **Path-based tool filtering** — connect to `/instances`, `/kubernetes`, etc. for token-efficient connections
- Redis-backed session store for multi-replica horizontal scaling

### Auto-Detection

If `VULTR_MCP_TRANSPORT` is not set, the server auto-detects:
- **STDIN is a pipe** (launched by MCP client) → STDIO mode
- **STDIN is a TTY / undefined** (interactive / Docker with `-p`) → HTTP mode

---

## Docker

The Docker image uses [FrankenPHP](https://frankenphp.dev/) for high-performance PHP serving with built-in Caddy and opcache/JIT.

### Build

```bash
docker build -t vultr/mcp:latest .
```

> **Note:** Run `composer install` locally before building if you're behind a TLS-intercepting VPN/proxy. The Dockerfile copies `vendor/` from the local directory to avoid package download issues during the build.

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


---

## Regenerating Tools from the OpenAPI Spec

The tool classes in `src/Tools/` are auto-generated from the Vultr OpenAPI spec. To regenerate:

1. Download the latest spec from https://www.vultr.com/api/ and save it as `openapi.json` in the project root
2. Run:

```bash
php bin/console mcp:generate
```

This generates one `*Tools.php` class per API tag, with `#[McpTool]` attributes on each method. The generator handles:
- All 39 API tags (494 tools total)
- Duplicate `operationId` deduplication
- Path parameter and query parameter extraction
- Request body schema generation
- `VultrClientFactory` injection for per-request API key handling

Tool registration is fully automatic — `Server.php` scans `src/Tools/*Tools.php` via reflection and registers all `#[McpTool]`-attributed methods with the MCP SDK. No manual `addTool()` calls needed.

To add a new API tag, add it to `CLASS_NAMES` and `PATH_PREFIXES` in `src/Generator/OpenApiGenerator.php`, then regenerate.

# vultr-mcp

A **PHP MCP (Model Context Protocol) server** for the [Vultr](https://www.vultr.com/) cloud platform.

Exposes Vultr's REST API v2 as MCP tools so AI agents (Claude, Copilot, etc.) can provision and manage cloud infrastructure in natural language.

## Requirements

- PHP 8.1+
- Composer

---

## Quick Start

```bash
# 1. Clone and install dependencies
git clone https://github.com/your-org/vultr-mcp.git
cd vultr-mcp
composer install

# 2. Configure environment
cp .env.example .env
# Edit .env and set VULTR_API_KEY

# 3. Start the server
composer serve
# → Listening on http://0.0.0.0:8000
```

Point your MCP client at `http://localhost:8000`.

---

## Project Structure

```
vultr-mcp/
├── bin/
│   └── generate.php            ← CLI tool regenerator
├── src/
│   ├── Server.php              ← HTTP entry point / MCP server bootstrap
│   ├── Auth/
│   │   └── VultrAuth.php       ← PSR-15 bearer-token auth middleware
│   ├── Generator/
│   │   └── OpenApiGenerator.php ← Parses OpenAPI spec → PHP tool classes
│   ├── Tools/
│   │   ├── InstanceTools.php   ← MCP tools for /v2/instances
│   │   └── BareMetalTools.php  ← MCP tools for /v2/bare-metals
│   └── Utils/
│       ├── VultrClient.php     ← Guzzle wrapper (auth, JSON, error handling)
│       ├── RateLimiter.php     ← Exponential back-off retry
│       └── RateLimitException.php
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

## Regenerating Tools from the OpenAPI Spec

Download the latest Vultr OpenAPI spec from https://www.vultr.com/api/ and save it as `openapi.json` in the project root, then run:

```bash
php bin/generate.php
# or:
php bin/generate.php --spec=openapi.json --tags=instances,baremetal --output=src/Tools/
```

Generated files are committed to the repository. Review the diff before committing.

---

## Authentication

### Vultr API Key

Set `VULTR_API_KEY` in your `.env` file. The key is injected as `Authorization: Bearer <key>` on every Vultr API request.

### MCP Client Auth (for the HTTP endpoint)

Set `MCP_AUTH_TOKEN` in `.env` to require clients to present:

```
Authorization: Bearer <MCP_AUTH_TOKEN>
```

Leave it empty to disable authentication (development only).

For production, replace the static token check in `VultrAuth::validateToken()` with JWT verification from an OAuth 2.0 / PKCE provider. The PKCE flow (RFC 7636) is recommended for public clients:

1. Client generates `code_verifier` → derives `code_challenge = BASE64URL(SHA256(code_verifier))`
2. Client sends `code_challenge` + `code_challenge_method=S256` to your OAuth authorization server
3. User authenticates, server returns an auth code
4. Client exchanges code + `code_verifier` for an access token
5. Client includes `Authorization: Bearer <access_token>` in MCP requests

---

## Rate Limiting

Vultr enforces 30 requests/second. The `RateLimiter` class automatically retries on HTTP 429 with exponential back-off (up to 5 attempts, up to 30 s delay, with ±10 % jitter). The `Retry-After` response header is honoured when present.

---

## Claude Desktop Integration (STDIO mode)

For local use with Claude Desktop, run as a STDIO process instead of HTTP:

```json
{
  "mcpServers": {
    "vultr": {
      "command": "php",
      "args": ["/absolute/path/to/vultr-mcp/src/Server.php"],
      "env": {
        "VULTR_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

To switch to STDIO transport, replace the `StreamableHttpTransport` block in `Server.php` with:

```php
use Mcp\Server\Transport\StdioTransport;
$transport = new StdioTransport();
$status = $server->run($transport);
exit($status);
```

---

## License

MIT
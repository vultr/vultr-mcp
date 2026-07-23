# vultr-mcp

A **Python MCP (Model Context Protocol) server** for the [Vultr](https://www.vultr.com/) cloud platform, built on [FastMCP](https://gofastmcp.com).

Tools are generated directly from the Vultr OpenAPI spec, so AI agents (Claude, Cursor, VS Code Copilot, etc.) can provision and manage Vultr infrastructure in natural language. Supports **local (STDIO)** and **remote (HTTP)** transports, per-request auth, identity-tool exclusions, and token-efficient category endpoints.

> **v2** is a ground-up rewrite in Python/FastMCP. The original PHP server lives in this repo's git history.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

---

## Quick Start

### Local (STDIO)

```bash
uv sync
VULTR_API_KEY=YOUR_VULTR_API_KEY uv run python -m vultr_mcp
```

The server auto-detects STDIO when launched by an MCP client. In STDIO mode the credential comes from `VULTR_API_KEY`.

### Remote (HTTP)

```bash
VULTR_MCP_TRANSPORT=http uv run python -m vultr_mcp
```

Serves Streamable HTTP on port 8080 (configurable via `SERVER_PORT`), plus `/healthz`. Each user provides their own Vultr API key per request (see Authentication).

---

## Tool Surface

Tools are generated from `openapi.json` via `FastMCP.from_openapi()` — no hand-written tool code.

### Excluded categories

Identity and credential-management categories are **excluded by default** so they stay out of agent reach (the same posture as the GitHub/Stripe/DigitalOcean MCPs): `api-keys`, `users`, `iam`, `scim`, `organizations`, `oidc`. Enforcement of these permissions belongs in the IAM policy attached to the OAuth client app; excluding the tools is UX-layer hygiene.

Override with `VULTR_MCP_EXCLUDED_CATEGORIES` (comma-separated tags; empty string keeps everything — appropriate for local STDIO use).

### Category endpoints (token-efficient)

The **root endpoint gives every (non-excluded) tool through a single connection** — best when your client only lets you add one or two MCP servers, so you want everything in one slot. Each category also gets its **own endpoint exposing only that category's tools** — for when you have room to add several focused connections and prefer each one scoped:

```
https://vultrmcp.com/                    # all tools
https://vultrmcp.com/instances           # VPS instance tools only
https://vultrmcp.com/kubernetes          # VKE tools only
https://vultrmcp.com/dns                 # DNS tools only
https://vultrmcp.com/container-registry  # multi-word tags are slugified
```

Paths are lowercase, dash-separated, and **bare — no trailing slash needed**. Common categories: `instances`, `baremetal`, `kubernetes`, `dns`, `firewall`, `block`, `snapshot`, `ssh`, `iso`, `reserved-ip`, `load-balancer`, `managed-databases`, `container-registry`, `vpcs`, `s3`, `cdns`, `billing`, `account`, `plans`, `region`, `os`.

Add several category servers to your client to cover what you use without loading everything. Configure which endpoints are mounted with `VULTR_MCP_CATEGORY_ENDPOINTS` (comma-separated slugs; default: all non-excluded).

---

## Authentication

### Header / API key (works today)

Each request carries the user's Vultr API key, which is forwarded to `api.vultr.com`:

```
Authorization: Bearer YOUR_VULTR_API_KEY
```

`X-Vultr-API-Key: YOUR_VULTR_API_KEY` is also accepted when no OAuth layer is active.

### OAuth 2.1 / OIDC (zero-config)

Set `VULTR_OIDC_ENABLED=true` (plus the OAuth app credentials) to front Vultr's OIDC provider with FastMCP's `OAuthProxy`. This gives MCP clients the paste-a-URL experience — Dynamic Client Registration, no client ID/secret entered by the user — while the server holds the one approved client's credentials. A `DualTokenVerifier` accepts **both** OIDC JWTs and raw API keys concurrently, so the header path keeps working with OAuth enabled.

See `k8s/configmap.yaml` and `src/vultr_mcp/auth.py` for the required variables.

### Client config example (Claude Desktop, remote)

Claude Desktop's config supports remote servers via the `mcp-remote` bridge:

```json
{
  "mcpServers": {
    "vultr": {
      "command": "cmd",
      "args": ["/c", "npx", "-y", "mcp-remote",
               "https://vultrmcp.com/instances",
               "--header", "Authorization: Bearer YOUR_VULTR_API_KEY"]
    }
  }
}
```

(On non-Windows, drop the `"/c"` and use `"npx"` as the command.)

---

## Using multiple organizations

**Every Vultr credential is scoped to exactly one organization.** The Vultr API resolves the org from the credential itself — an API key belongs to one account, and an OAuth access token carries the org as a fixed `acctid` claim. There is no per-request "act as org X" parameter and no in-session org switch. So connecting a second org always means a **second credential**, presented as a second MCP connection.

### API key mode (recommended for multi-org)

Each org issues its own API key. Add the server twice with different names and different keys — you get two independent tool namespaces, one per org:

```json
{
  "mcpServers": {
    "vultr-org-a": {
      "command": "cmd",
      "args": ["/c", "npx", "-y", "mcp-remote", "https://vultrmcp.com/",
               "--header", "Authorization: Bearer ORG_A_API_KEY"]
    },
    "vultr-org-b": {
      "command": "cmd",
      "args": ["/c", "npx", "-y", "mcp-remote", "https://vultrmcp.com/",
               "--header", "Authorization: Bearer ORG_B_API_KEY"]
    }
  }
}
```

No server or platform changes required — this works today.

### OAuth mode

With OAuth there is **no org picker in the flow**. The org is captured implicitly from whichever account your Vultr console session is in at the moment you click **Authorize** — that session's `ACCTID` is frozen into the consent record and every token (including refreshes) minted from it.

To select an org: switch the Vultr console to the desired org **first**, then authorize.

Two limitations follow:

- **One org per connector.** Clients such as claude.ai key connectors by URL, so re-authorizing the same connector under a different org just overwrites the first binding — you can't hold both at once. A second org needs a second connector on a distinct URL (e.g. a `org-b.vultrmcp.com` ingress alias pointing at the same deployment), or simply use API-key mode above.
- **No confirmation of which org you granted.** Because selection is implicit in the console session, authorizing while in the *wrong* org silently connects that org's resources with no prompt naming it. Double-check your active org in the Vultr console before consenting.

---

## Configuration (environment)

| Variable | Purpose |
|---|---|
| `VULTR_MCP_TRANSPORT` | `stdio` (default when launched by a client) or `http` |
| `VULTR_API_KEY` | credential for STDIO/local mode |
| `VULTR_API_BASE_URL` | default `https://api.vultr.com/v2` |
| `SERVER_HOST` / `SERVER_PORT` | HTTP bind (default `0.0.0.0:8080`) |
| `SSL_VERIFY` | verify upstream TLS (default `true`) |
| `VULTR_MCP_EXCLUDED_CATEGORIES` | tags to drop (default identity set; empty disables) |
| `VULTR_MCP_CATEGORY_ENDPOINTS` | category endpoints to mount (default: all non-excluded) |
| `MCP_RESOURCE_URL` | public URL, used for OAuth + Host allow-list (default `https://vultrmcp.com`) |
| `MCP_ALLOWED_HOSTS` | extra hosts for DNS-rebinding protection |
| `MCP_ALLOWED_ORIGINS` | extra allowed `Origin` values for the OAuth consent/browser flow |
| `VULTR_OIDC_ENABLED` | enable the OAuthProxy (default `false`) |
| `VULTR_OIDC_PROVIDER_ID` / `VULTR_OAUTH_CLIENT_ID` / `VULTR_OAUTH_CLIENT_SECRET` | approved OAuth app credentials |
| `REDIS_HOST` / `REDIS_PORT` | Redis for OAuth DCR state (required for multi-replica OAuth) |

---

## Deployment

### Docker

```bash
docker build -t vultr-mcp:latest .
docker run --rm -p 8080:8080 -e VULTR_MCP_TRANSPORT=http vultr-mcp:latest
```

The image runs `python -m vultr_mcp` under uvicorn as a non-root user.

### Kubernetes

Manifests are in `k8s/` (deployment, service, configmap, `secret.yaml.example`, redis). Copy `secret.yaml.example` to `secret.yaml` (gitignored), fill in values, then apply:

```bash
cp k8s/secret.yaml.example k8s/secret.yaml   # then edit
kubectl apply -f k8s/
```

Runs 2 replicas behind a round-robin ingress; the server uses **stateless HTTP** so no session affinity is required. OAuth DCR state is shared via Redis.

---

## Development

```bash
uv sync
uv run pytest            # test suite
uv run python -m vultr_mcp   # run locally (STDIO)
```

Tools regenerate automatically from `openapi.json` on startup — to update the tool surface, replace `openapi.json` with the latest Vultr spec.

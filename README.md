# Vultr MCP Server

[![CI](https://github.com/vultr/vultr-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/vultr/vultr-mcp/actions/workflows/ci.yml)

The official [Model Context Protocol](https://modelcontextprotocol.io) server for [Vultr](https://www.vultr.com/). Connect Claude, Cursor, VS Code, Codex or any other MCP client to your Vultr account and ask about your infrastructure in plain language: which instances are running in Amsterdam, what you spent last month, whether a Kubernetes cluster has an upgrade waiting.

> [!NOTE]
> **The server is read-only.** It can look at everything your credential can see, but it has no tools that create, change or delete anything, and none that spend money. Ask it to resize a server and it will tell you it can't.

- [Quick start](#quick-start)
- [Authentication](#authentication)
- [Endpoints](#endpoints)
- [Client setup](#client-setup)
- [What you can ask](#what-you-can-ask)
- [Safety](#safety)
- [Running it yourself](#running-it-yourself)
- [Configuration](#configuration)
- [Development](#development)

## Quick start

Vultr hosts the server, so there is nothing to install. Add this URL to your MCP client:

```
https://vultrmcp.com/mcp
```

Your client opens a browser window to sign in to Vultr, and the tools are ready once you approve. For example, in Claude Code:

```bash
claude mcp add --transport http vultr https://vultrmcp.com/mcp
```

Then run `/mcp` inside Claude Code, choose **vultr** and sign in. Setup for other clients is under [Client setup](#client-setup), and a browser pointed at [vultrmcp.com](https://vultrmcp.com) shows the same guide with every endpoint and tool listed.

## Authentication

Sign in with your Vultr account through OAuth. Add the server with no credentials, and your client opens a browser window where you sign in to Vultr and approve access. There is nothing to create, copy or rotate, and the server keeps no credentials of its own: every call goes to `api.vultr.com` as you.

- Access renews every hour on its own. You sign in again only after 30 days without use, or if access is revoked.
- Most MCP clients support this, including Claude, Cursor, VS Code, Codex CLI and OpenClaw.

### One organization per sign-in

Each sign-in is tied to one Vultr organization: whichever one your Vultr console has selected when you click **Authorize**. Switch organizations in the console first, then sign in, and ask "What Vultr account am I connected as?" to confirm where you landed.

To work with two organizations at once, add the server twice under different names and sign in to each separately.

## Endpoints

`https://vultrmcp.com/mcp` serves all 186 tools. Some clients struggle with that many, and a model picks the right tool more often from a shorter list. Each product area also has its own endpoint serving only its tools. Add the area to the end of `/mcp`:

| Endpoint | Tools | Covers |
|---|---:|---|
| `/mcp/compute` | 33 | Cloud Compute instances, Bare Metal, snapshots, backups, startup scripts, instance templates and Compute Clusters |
| `/mcp/network` | 38 | DNS, firewalls, load balancers, VPCs and NAT gateways, reserved IPs, CDN, and legacy private networks |
| `/mcp/databases` | 29 | Managed Databases: clusters, users, databases, pools, topics, connectors, maintenance and alerts |
| `/mcp/account` | 23 | Account details, billing and invoices, activity logs, SSH keys, sub-accounts and support tickets |
| `/mcp/catalog` | 19 | What you can deploy and where: plans, regions, operating systems, ISOs, One-Click apps and Marketplace |
| `/mcp/storage` | 16 | Block Storage, Object Storage, Vultr File System and storage gateways |
| `/mcp/registry` | 14 | Container registries, repositories, artifacts, robots and replication |
| `/mcp/kubernetes` | 11 | Vultr Kubernetes Engine clusters, node pools, versions and upgrades |
| `/mcp/inference` | 3 | Serverless Inference subscriptions and usage |

Each endpoint's name matches its tools: `/mcp/compute` serves the `vultr_compute_*` tools, and so on. Kubernetes is kept apart from compute on purpose, because VKE clusters and Compute Clusters are easy to confuse.

You can add as many as you like, each as its own entry. `https://vultrmcp.com/` works the same as `/mcp`. Older per-category paths, such as `/mcp/instances` or `/instances`, still work and serve the endpoint their category now belongs to.

## Client setup

Every example uses the full endpoint. Swap in one of the [endpoints above](#endpoints) to load fewer tools.

<details>
<summary><b>Claude.ai and Claude Desktop</b></summary>

1. **Settings → Connectors → Add custom connector**.
2. Name it `Vultr`, set the URL to `https://vultrmcp.com/mcp`, and click **Add**.
3. Click **Connect**, sign in to Vultr and approve.

A connector added once works in both Claude Desktop and claude.ai. On Team and Enterprise plans an Owner adds it first under **Organization settings → Connectors**, then each person connects from their own Settings. Claude Desktop and claude.ai sign in with OAuth only.

</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add --transport http vultr https://vultrmcp.com/mcp
```

Then run `/mcp`, choose **vultr** and sign in. Add `--scope user` to make it available in every folder, not just the current one.

</details>

<details>
<summary><b>Cursor</b></summary>

In **Settings → Tools & MCP → Add custom MCP**, edit `mcp.json`:

```json
{
  "mcpServers": {
    "vultr": {
      "type": "http",
      "url": "https://vultrmcp.com/mcp"
    }
  }
}
```

Save, then click **Needs login** to sign in.

</details>

<details>
<summary><b>VS Code</b></summary>

In `.vscode/mcp.json`, or from the **MCP Servers** panel with **+** and an HTTP server:

```json
{
  "servers": {
    "vultr": {
      "type": "http",
      "url": "https://vultrmcp.com/mcp"
    }
  }
}
```

Sign in when VS Code asks.

</details>

<details>
<summary><b>Codex CLI</b></summary>

```bash
codex mcp add vultr --url https://vultrmcp.com/mcp
codex mcp login vultr
```

</details>

<details>
<summary><b>OpenClaw</b></summary>

```bash
openclaw mcp add vultr --url https://vultrmcp.com/mcp --transport streamable-http --auth oauth
openclaw mcp login vultr
```

On a remote machine the browser may show an error page with an authorization code instead of finishing. Copy the code and run `openclaw mcp login vultr --code THE_CODE`.

</details>

<details>
<summary><b>opencode</b></summary>

In `opencode.json`. It starts the sign-in on its own:

```json
{
  "mcp": {
    "vultr": {
      "type": "remote",
      "url": "https://vultrmcp.com/mcp",
      "enabled": true
    }
  }
}
```

</details>

<details>
<summary><b>Hermes Agent</b></summary>

In `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  vultr:
    url: "https://vultrmcp.com/mcp"
    auth: oauth
```

</details>

<details>
<summary><b>Other clients</b></summary>

Any client that supports remote MCP over Streamable HTTP works: give it the URL, and leave credentials empty, and it signs in with OAuth. Clients that only launch local servers can connect through [`mcp-remote`](https://github.com/geelen/mcp-remote), which signs in with OAuth:

```bash
npx -y mcp-remote https://vultrmcp.com/mcp
```

On a network that inspects TLS traffic, point Node at your company's CA with `NODE_EXTRA_CA_CERTS=/path/to/ca.pem` rather than turning off certificate checks.

</details>

## What you can ask

A few examples of questions the tools answer:

- "Which instances are running in Amsterdam, and on what plans?"
- "How much bandwidth have I used this month?"
- "Does my Kubernetes cluster have an upgrade available?"
- "Do any of my managed databases have maintenance pending?"
- "Which firewall rules allow SSH from anywhere?"
- "What was on last month's invoice?"
- "What DNS records point at this IP address?"

Start with "What Vultr account am I connected as?" to confirm which account and organization you're signed in to.

## Safety

- **Read-only.** Only reads are exposed. A handful of operations that change state despite using a read method, such as purging a CDN cache, are removed as well.
- **Your credential is the boundary.** Every call runs as you, and Vultr enforces what that credential may do. The server cannot see anything you can't.
- **No identity management.** Tools for API keys, users, IAM, SCIM, organizations and OIDC are left out, so an agent cannot read or change who has access.
- **No credentials.** Nothing returns something an agent could act with: no kubeconfig, no console link, no user data or startup script body, no passwords or keys. The agent gets information; access stays in the Vultr console.
- **Arguments are checked.** A tool called with an argument it doesn't have is refused, with a list of the ones it takes. A mistyped filter can't quietly return everything.
- **Errors are Vultr's own.** When `api.vultr.com` returns an error, you see its message. A 5xx means Vultr's API had a problem, so try again later. A 401 or 403 means the credential can't do that.
- **Descriptions are reviewed.** Each tool's name and description are written and checked by hand, including when *not* to use it, so a model doesn't mix up similar products such as Kubernetes clusters and Compute Clusters. [docs/interface-layer.md](docs/interface-layer.md) explains how.

## Running it yourself

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

**Local, over STDIO.** Your client launches the server and talks to it directly. It uses the Vultr API key in the `VULTR_API_KEY` environment variable, since there is no request to carry a sign-in:

```bash
uv sync
uv run python -m vultr_mcp
```

**Local, over HTTP.** Streamable HTTP on port 8080, plus `/healthz`. Each request carries its own credential, exactly like the hosted server:

```bash
VULTR_MCP_TRANSPORT=http uv run python -m vultr_mcp
```

Over HTTP, `VULTR_API_KEY` is ignored on purpose. Otherwise anyone who reached the server without a credential would be served as whoever deployed it.

**Docker and Kubernetes.** See [docs/self-hosting.md](docs/self-hosting.md) for the container image, the Kubernetes manifests the hosted server runs from, and the optional audit log pipeline.

Self-hosted servers can turn on write tools with `VULTR_MCP_WRITES_ENABLED=true`. This is a safety default, not a security boundary: anyone holding the credential can already make the same changes through the API.

## Configuration

| Variable | Purpose |
|---|---|
| `VULTR_MCP_TRANSPORT` | `stdio` (default) or `http` |
| `VULTR_API_KEY` | credential for STDIO. Ignored over HTTP |
| `VULTR_API_BASE_URL` | default `https://api.vultr.com/v2` |
| `SERVER_HOST` / `SERVER_PORT` | HTTP bind address (default `0.0.0.0:8080`) |
| `SSL_VERIFY` | verify upstream TLS (default `true`) |
| `VULTR_MCP_WRITES_ENABLED` | expose tools that change state (default `false`) |
| `VULTR_MCP_EXCLUDED_CATEGORIES` | categories to leave out (default: the identity set; empty keeps everything) |
| `VULTR_MCP_CATEGORY_ENDPOINTS` | endpoints to serve, by name (`compute,network`) or by an old category name, which selects its endpoint (default: all nine) |
| `VULTR_MCP_INTERFACE` | `off` serves only the tools generated from the spec (default on) |
| `VULTR_MCP_INTERFACE_DIR` | where the reviewed tool definitions live (default `interface/`) |
| `VULTR_MCP_INTERFACE_MAX_PAGES` | pages one filtered search may scan (default `10`) |
| `VULTR_MCP_OUTPUT_SCHEMAS` | advertise generated output schemas (default `false`) |
| `MCP_RESOURCE_URL` | the server's public URL, for OAuth and the allowed host list (default `https://vultrmcp.com`) |
| `MCP_ALLOWED_HOSTS` | extra hosts to accept |
| `MCP_ALLOWED_ORIGINS` | extra `Origin` values for the OAuth consent page |
| `VULTR_OIDC_ENABLED` | turn on OAuth (default `false`) |
| `VULTR_OIDC_PROVIDER_ID` / `VULTR_OAUTH_CLIENT_ID` / `VULTR_OAUTH_CLIENT_SECRET` | the approved OAuth app's credentials |
| `REDIS_HOST` / `REDIS_PORT` | shared OAuth state, required when running more than one replica |
| `VULTR_MCP_AUDIT_DIR` | also write audit records to files here, for a log shipper |

## Development

```bash
uv sync
uv run pytest
```

The tools come from two places. Vultr's OpenAPI spec (`openapi.json`) generates a baseline, and reviewed definitions in `interface/` replace them one for one with clearer names, descriptions and response shapes. To check the definitions against the spec, and to see what the spec added that nobody has reviewed yet:

```bash
uv run python -m vultr_mcp.interface --list
uv run python -m vultr_mcp.interface --drift
```

[docs/interface-layer.md](docs/interface-layer.md) covers how definitions are written, scaffolded and checked in CI.

```
src/vultr_mcp/      the server: HTTP app, auth, audit, runtime
interface/          reviewed tool definitions, one directory per product family
openapi.json        Vultr's API spec, the source of truth for every request
k8s/                the hosted deployment
tests/              pytest suite, run by CI on every push
docs/               deeper documentation
```

# vultr-mcp

A **Python MCP (Model Context Protocol) server** for the [Vultr](https://www.vultr.com/) cloud platform, built on [FastMCP](https://gofastmcp.com).

Tools are generated directly from the Vultr OpenAPI spec, so AI agents (Claude, Cursor, VS Code, Codex, etc.) can query Vultr infrastructure in natural language. Supports **local (STDIO)** and **remote (HTTP)** transports, per-request auth, identity-tool exclusions, and token-efficient category endpoints.

> **The tool surface is read-only.** State-changing operations are excluded unless writes are explicitly enabled — see [Read-only by default](#read-only-by-default).

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

Most tools are generated from `openapi.json` via `FastMCP.from_openapi()`. A small, growing set is defined by hand in `interface/` and **replaces** the generated tool for the same operation — see below.

### Interface layer

Generated tool definitions inherit whatever the spec says, which is written for developers reading docs, not for an agent choosing between 180 tools. That ambiguity has a measurable cost: asked to add a node to a Compute Cluster, Claude picked a VKE tool.

`interface/` is a versioned set of reviewed YAML files that decide what the agent sees — tool name, description, input schema, response shape — while `openapi.json` stays the source of truth for the HTTP call itself. One tool maps to exactly one `operationId`.

```yaml
- name: vultr_compute_clusters_search   # provider_productarea_resource_action
  access: read                          # cross-checked against the HTTP method
  operation: list-clusters
  description: |
    ... including an explicit "Do not use this tool for VKE clusters."
  input:
    properties:
      label:                            # GET /clusters can't filter, so the
        filter: {field: label, match: contains_ci}   # server does
      page_size:
        maps_to: per_page               # renamed on the way to the API
  output:
    include: [id, label, region, ...]   # everything else is dropped
  computed:
    instance_count: {from: length(instances)}
```

Every reference is resolved against `openapi.json` at build time, so a field the API stopped returning fails the build instead of a tool call:

```bash
uv run python -m vultr_mcp.interface --list
```

New product areas start from a draft rather than a blank file. The scaffolder derives what the spec can tell it — parameters and their real names, pagination, the response shape, the tool name from the file's declared family — and leaves the rest visibly unfinished:

```bash
uv run python -m vultr_mcp.interface --scaffold instances > interface/instances.yaml
```

Every drafted tool is `enabled: false` with a stub description, because the part that makes a tool worth having (say what it is for, and explicitly what it is *not* for) is the part no generator can write. Fields whose names look like credentials — `default_password`, `s3_secret_key`, the noVNC console link — are commented *out* of `output.include` rather than into it, so returning one is always a deliberate act.

Once an area is covered, the layer can tell you what the spec grew that nobody has looked at:

```bash
uv run python -m vultr_mcp.interface --drift
```

It reports only *covered* areas, and only operations that are neither served, drafted, nor explicitly declined — so a new endpoint shows up as one line rather than being buried under the dozen omissions that were deliberate. New endpoints are news rather than a build failure, so this exits 0; a reference to an operation the spec no longer has exits non-zero, and CI that wants the stricter gate can pass `--fail-on-unreviewed`.

Because these tools filter client-side, a filtered search pages through the collection itself (up to `VULTR_MCP_INTERFACE_MAX_PAGES` requests) and reports what it scanned in `meta.filtered`, so a count is never quietly taken from a partial scan. Set `VULTR_MCP_INTERFACE=off` to serve the generated surface alone.

### Read-only by default

**The server exposes read operations only.** Every state-changing operation is dropped, so a connected agent can inspect and report on infrastructure but cannot provision, modify, or destroy it — and cannot spend money. That is the posture the hosted server ships with, and it is the *default*, not a setting: writes are opt-in via `VULTR_MCP_WRITES_ENABLED=true`, so a deployment that configures nothing gets the safe surface.

The rule is "GET, plus an explicit allowlist". Everything else is excluded — including Vultr's two `OPTIONS` routes, which mint container-registry Docker credentials despite the verb. The allowlist (`READ_ONLY_METHOD_OVERRIDES` in `server.py`) currently holds one entry: `POST /databases/{database-id}/alerts`, which only lists a database's existing alerts but takes its filter in a request body.

This cuts the root listing from the full generated surface to **188 tools** (~19k tokens), which comfortably fits clients that previously choked on it.

Enable writes for local use:

```bash
VULTR_MCP_WRITES_ENABLED=true uv run python -m vultr_mcp
```

`GET /healthz` reports the live posture as `"read_only": true|false`, so a deployment's write behaviour is verifiable without listing tools.

> Per-user, per-org write access — a toggle in the Vultr console that promotes a specific user to the write surface — is the planned next step. The env flag is the mechanism it will drive.

### Keeping up with the spec

`openapi.json` moves because someone else shipped, so CI answers "what changed, and does it still work" on every push:

| Check | Catches |
|---|---|
| `pytest` | spec defects that stop the server parsing at all, plus a new OpenAPI tag nobody has assessed |
| `python -m vultr_mcp.interface` | a tool definition referencing something the spec no longer has |
| `python -m vultr_mcp.interface --drift` | operations added to a covered product area that nobody has reviewed |
| `scripts/smoke_interface.py` | the spec being *wrong* — a documented field that never arrives, an undocumented one that does |

The category check deserves a note. A new tag defaults to being exposed, so a spec update can put a product area on the tool surface with nobody having looked — that is how 24 OAuth client-management operations arrived, and an audit-log endpoint returning `s3_secret_key`. Every tag must now appear in either `DEFAULT_EXCLUDED_CATEGORIES` or `REVIEWED_CATEGORIES`, and a tag in neither fails the build.

The smoke test needs a `VULTR_API_KEY` secret and is skipped without one. Use a test account: whatever the API returns lands in the workflow log.

### Excluded categories

Identity and credential-management categories are **excluded by default** so they stay out of agent reach (the same posture as the GitHub/Stripe/DigitalOcean MCPs): `api-keys`, `users`, `iam`, `scim`, `organizations`, `oidc`, `oauth`, `logs`. Enforcement of these permissions belongs in the IAM policy attached to the OAuth client app; excluding the tools is UX-layer hygiene.

The last two joined with the 2026-08-28 spec. `oauth` covers OAuth client management, whose write half mints and regenerates client secrets. `logs` is blunter: `ListAuditLogs` returns `s3_access_key` and `s3_secret_key` for the audit-log delivery bucket, so the generated tool hands an agent a live credential pair. Excluding `logs` costs the useful `list-logs` read, and the right fix is a hand-authored tool that shapes the keys out — an interface tool replaces the generated one — after which the category can be re-admitted.

Override with `VULTR_MCP_EXCLUDED_CATEGORIES` (comma-separated tags; empty string keeps everything — appropriate for local STDIO use).

### Category endpoints (token-efficient)

The **root endpoint gives every (non-excluded) tool through a single connection** — best when your client only lets you add one or two MCP servers, so you want everything in one slot. At 180 read-only tools (~19k tokens) it now fits most clients, though those with a hard tool cap (VS Code allows 128) still need the category endpoints below. Each category also gets its **own endpoint exposing only that category's tools** — for when you have room to add several focused connections and prefer each one scoped:

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

### Header / API key

Each request carries the user's Vultr API key, which is forwarded to `api.vultr.com`:

```
Authorization: Bearer YOUR_VULTR_API_KEY
```

`X-Vultr-API-Key: YOUR_VULTR_API_KEY` is also accepted when no OAuth layer is active.

### OAuth 2.1 / OIDC (zero-config)

Set `VULTR_OIDC_ENABLED=true` (plus the OAuth app credentials) to front Vultr's OIDC provider with FastMCP's `OAuthProxy`. This gives MCP clients the paste-a-URL experience — Dynamic Client Registration, no client ID/secret entered by the user — while the server holds the one approved client's credentials. A `DualTokenVerifier` accepts **both** OIDC JWTs and raw API keys concurrently, so the header path keeps working with OAuth enabled.

See `k8s/configmap.yaml` and `src/vultr_mcp/auth.py` for the required variables.

---

## Connecting a client

The hosted server is **`https://vultrmcp.com/`** (mind the trailing slash — see the note below). Point any MCP client at it and authenticate with **OAuth** (browser sign-in, nothing to paste) or an **API-key** `Authorization: Bearer` header. The table shows the quickest path per client; drop the header and use the tool's OAuth switch for OAuth.

| Client | How to add |
|---|---|
| **Claude.ai** | Settings → Connectors → Add custom connector → `https://vultrmcp.com/` → Connect → sign in. |
| **Claude Desktop / stdio-only (Zed, …)** | `npx -y mcp-remote https://vultrmcp.com/` (add `--header "Authorization: Bearer KEY"` for API-key mode). |
| **Cursor** | Settings → Tools & MCP → Add custom MCP → `mcp.json`: `"type":"http"`, `"url":"https://vultrmcp.com/"`. |
| **VS Code** | MCP Servers panel → **+** (or edit `mcp.json`): HTTP server, URL `https://vultrmcp.com/`. |
| **Codex CLI** | `codex mcp add vultr --url https://vultrmcp.com/`, then `codex mcp login vultr` (OAuth). |
| **opencode** | `opencode.json` → `mcp.vultr` = `{ "type":"remote", "url":"https://vultrmcp.com/" }` (omit headers for auto-OAuth). |
| **Hermes Agent** | `~/.hermes/config.yaml` → `mcp_servers.vultr` with `url` + `auth: oauth` (or a `headers` block). |
| **OpenClaw** | `openclaw mcp add vultr --url https://vultrmcp.com --transport streamable-http --auth oauth`, then `openclaw mcp login vultr`. |

The server's root doubles as a **human docs page**: open <https://vultrmcp.com/> in a browser for a full walkthrough (OAuth flow diagram, per-client steps, and every endpoint's tools). MCP clients hitting the same URL get the protocol, not the page.

> **Both `https://vultrmcp.com` and `https://vultrmcp.com/` connect.** The root serves the human docs page *only* to genuine top-level browser navigations (`Sec-Fetch-Mode: navigate`); everything else — including browser-based agent UIs that connect via `fetch`/XHR (`Sec-Fetch-Mode: cors`) — reaches the MCP protocol at the same URL, so the missing trailing slash no longer breaks the connection. The trailing-slash form is still the canonical one to configure.

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
| `VULTR_MCP_WRITES_ENABLED` | expose state-changing tools (default `false` — the surface is read-only) |
| `VULTR_MCP_EXCLUDED_CATEGORIES` | tags to drop (default identity set; empty disables) |
| `VULTR_MCP_CATEGORY_ENDPOINTS` | category endpoints to mount (default: all non-excluded) |
| `VULTR_MCP_INTERFACE` | `off` serves the generated surface alone, without the hand-reviewed `interface/` tools (default on) |
| `VULTR_MCP_INTERFACE_DIR` | where the interface layer lives (default: `interface/` beside `openapi.json`) |
| `VULTR_MCP_INTERFACE_MAX_PAGES` | requests one filtered search may make while scanning a collection (default `10`) |
| `VULTR_MCP_OUTPUT_SCHEMAS` | advertise generated `outputSchema` on tools (default `false` — they tripled the tool-listing size without helping agents) |
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

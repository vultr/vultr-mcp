# vultr-mcp

A **Python MCP (Model Context Protocol) server** for the [Vultr](https://www.vultr.com/) cloud platform, built on [FastMCP](https://gofastmcp.com).

It lets an AI agent answer questions about your Vultr infrastructure in natural language — "which instances are running in Amsterdam", "what's my bandwidth this month", "does this cluster have an upgrade available" — by turning the Vultr API into a set of reviewed, deliberately-named tools.

> **The tool surface is read-only.** Every state-changing operation is dropped unless writes are explicitly enabled, so a connected agent can inspect and report on your infrastructure but cannot provision, modify, destroy, or spend money. This is the default, not a setting you have to find.

**Two ways to run it.** Connect to the hosted server at **`https://vultrmcp.com/`**, or run it yourself locally over STDIO. Same server, same tools.

---

## Quick start

### Hosted (no install)

Point any MCP client at `https://vultrmcp.com/` and authenticate with **OAuth** (browser sign-in, nothing to paste) or a **Vultr API key**.

| Client | How to add |
|---|---|
| **Claude.ai** | Settings → Connectors → Add custom connector → `https://vultrmcp.com/` → Connect → sign in |
| **Claude Desktop** | `npx -y mcp-remote https://vultrmcp.com/` (add `--header "Authorization: Bearer KEY"` for API-key mode) |
| **Cursor** | Settings → Tools & MCP → Add custom MCP → `"type":"http"`, `"url":"https://vultrmcp.com/"` |
| **VS Code** | MCP Servers panel → **+** → HTTP server → `https://vultrmcp.com/` |
| **Codex CLI** | `codex mcp add vultr --url https://vultrmcp.com/`, then `codex mcp login vultr` |
| **opencode** | `opencode.json` → `mcp.vultr` = `{ "type":"remote", "url":"https://vultrmcp.com/" }` |

Opening `https://vultrmcp.com/` in a browser gives a docs page with the full walkthrough; MCP clients hitting the same URL get the protocol.

### Local (STDIO)

```bash
uv sync
VULTR_API_KEY=YOUR_VULTR_API_KEY uv run python -m vultr_mcp
```

Your client launches the server and talks over stdin/stdout. The credential comes from the environment, because there is no request to carry one.

### Local (HTTP)

```bash
VULTR_MCP_TRANSPORT=http uv run python -m vultr_mcp
```

Streamable HTTP on `:8080` plus `/healthz`. Each request carries its own credential, exactly as against the hosted server — use this when testing anything auth-related, since STDIO never exercises header handling.

**Requirements:** Python 3.12+ and [uv](https://docs.astral.sh/uv/).

---

## Authentication

Two credentials work concurrently, and both arrive the same way:

```
Authorization: Bearer YOUR_VULTR_API_KEY     # or an OAuth access token
```

**API key** is simplest and the right choice for scripts and automation. **OAuth** suits interactive users — the server acts as an OAuth proxy in front of Vultr's OIDC provider, so you sign in with a browser and paste nothing.

Whichever you use, **the credential is the boundary**. The server forwards it to `api.vultr.com` on every request; everything an agent can reach is exactly what that credential can reach, enforced by Vultr rather than by this server.

Over HTTP the server holds no credential of its own. A caller who presents none gets a 401 from Vultr — never somebody else's access. `VULTR_API_KEY` from the environment is consulted **only when there is no HTTP request in scope**, which is to say only under STDIO. That gate is deliberate and tested: without it, an HTTP deployment with a key in its environment would serve an anonymous caller as the operator.

### For automation

Use an API key belonging to a **service user** — API-only, no portal login, and structurally barred from root access. It belongs to the account rather than to a person, so it survives staff changes, and its ACLs are the real scope. Keep them narrow: the key works against `api.vultr.com` directly, so the read-only tool surface does not constrain it.

OAuth is the wrong tool for automation here. Vultr's provider advertises only `authorization_code` and `refresh_token` — there is no `client_credentials` grant — and every OAuth token is bound to a consenting user, so it stops working when that user's access does.

## One organization per credential

**Every Vultr credential is scoped to exactly one organization.** The API resolves the org from the credential itself — an API key belongs to one account, and an OAuth token carries the org as a fixed `acctid` claim. There is no per-request "act as org X" and no in-session switch.

So a second org means a second credential, presented as a second MCP connection. With API keys that is two entries with different keys. With OAuth it is harder: the org is captured implicitly from whichever account your Vultr console session is in **at the moment you click Authorize**, and clients key connectors by URL, so re-authorizing the same connector overwrites the first binding rather than adding to it. Switch the console to the org you want *before* authorizing, then confirm with `vultr_account_get`.

## Endpoints

```
https://vultrmcp.com/          # every tool — canonical
https://vultrmcp.com/mcp       # alias, identical surface
```

Each category also gets its own endpoint exposing only that category's tools — useful for clients with a hard tool cap, or when you want a narrower surface:

```
https://vultrmcp.com/instances           # VPS instance tools only
https://vultrmcp.com/kubernetes          # VKE tools only
https://vultrmcp.com/container-registry  # multi-word tags are slugified
```

Paths are lowercase, dash-separated, and **bare — no trailing slash needed**. Common categories: `instances`, `baremetal`, `kubernetes`, `dns`, `firewall`, `block`, `snapshot`, `ssh`, `iso`, `reserved-ip`, `load-balancer`, `managed-databases`, `container-registry`, `vpcs`, `s3`, `cdns`, `billing`, `account`, `plans`, `region`, `os`.

The read-only gate and the identity exclusions always apply on top, so a category endpoint can never expose something the root would not.

---

## The tool surface

Tools come from two places. `FastMCP.from_openapi()` generates them from `openapi.json`; `interface/` defines them by hand, and **a hand-authored tool replaces the generated one** for the same operation.

The read-only surface is **191 tools** — 179 hand-authored, plus 12 generated ones belonging to operations that were reviewed and deliberately declined. Every read operation the server exposes is hand-authored. The count does not grow as the layer does, because the replacement is one-for-one.

### Three states an operation can be in

| State | Generated tool | Meaning |
|---|---|---|
| **hand-authored** | replaced | a reviewed tool in `interface/` |
| **declined** | **still served** | reviewed, judged not worth hand-authoring |
| **excluded** | **removed** | serving it is itself the harm |

**Declining is bookkeeping, not suppression.** It exists so drift detection can tell "we looked and said no" from "nobody has looked yet" — the generated tool stays exposed and callable.

**Excluding removes it.** Reserved for cases where serving the operation is the problem, not merely where a tool would be redundant. One entry today: `purge-pullzone` empties a CDN cache — a state change Vultr serves over `GET`, so the read-only gate read it as safe and served it on a surface whose whole promise is that nothing on it changes anything.

Every problem with an exclusion fails the build, unlike a decline. A stale decline is untidy; a stale exclusion reads as protection that is not there.

### Read-only by default

The rule is "GET, plus an explicit allowlist". Everything else is dropped — including Vultr's two `OPTIONS` routes, which mint container-registry Docker credentials despite the verb. The allowlist (`READ_ONLY_METHOD_OVERRIDES` in `server.py`) holds one entry: `POST /databases/{database-id}/alerts`, which only lists existing alerts but takes its filter in a request body.

Writes are opt-in:

```bash
VULTR_MCP_WRITES_ENABLED=true uv run python -m vultr_mcp
```

`GET /healthz` reports the live posture as `"read_only": true|false`, so a deployment's behaviour is verifiable without listing tools.

Note what this flag is and is not. It is a **safety default** that stops a connected agent from destroying infrastructure. It is **not a security boundary**: anyone holding a credential can call `api.vultr.com` directly and do everything that credential permits. The boundary is the credential's own ACLs.

> Per-user, per-org write access — a toggle in the Vultr console promoting a specific user to the write surface — is the planned next step. This flag is the mechanism it will drive.

### Excluded categories

Identity and credential-management categories are excluded by default so they stay out of agent reach: `api-keys`, `users`, `iam`, `scim`, `organizations`, `oidc`, `oauth`. Enforcement of these permissions belongs in the IAM policy attached to the OAuth client app; excluding the tools is UX-layer hygiene, and keeps identity mutations out of prompt-injection range on every auth path.

`logs` was excluded for a while because `ListAuditLogs` returns `s3_access_key` and `s3_secret_key` for the delivery bucket. It is back, because `interface/account/logs.yaml` now covers every read in the category with a tool that withholds both. **Exclusion is per-category and blunt; shaping is per-operation and exact**, so a category returns as soon as the shaping exists.

Override with `VULTR_MCP_EXCLUDED_CATEGORIES` (comma-separated tags; empty string keeps everything).

---

## How the tools are defined

Generated tool definitions inherit whatever the spec says, which is written for developers reading docs, not for an agent choosing between 180 tools. That ambiguity has a measurable cost: asked to add a node to a Compute Cluster, an agent picked a VKE tool.

`interface/` is a versioned set of reviewed YAML files deciding what the agent sees — name, description, input schema, response shape — while `openapi.json` stays the source of truth for the HTTP call. One tool maps to exactly one `operationId`. It covers **33 product areas across 10 families**.

```yaml
- name: vultr_compute_clusters_list     # vultr_<family>_<resource>_<verb>
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

The **name** does the disambiguating work, not the file layout — the agent receives a flat tool list and never sees the directory tree. `family` is the second segment and a closed vocabulary; `kubernetes` is deliberately its own family rather than part of `compute`, because those two are the pair that actually gets confused. The verb vocabulary is closed too — `list` for collections, `get` for one thing, then CRUD and lifecycle verbs.

### Layout

Files are grouped into one directory per family, so the tree matches the `family` enum:

```
interface/
├── compute/
│   ├── instances/          # a large area splits into a directory
│   │   ├── instances.yaml  #   list, get
│   │   ├── networking.yaml #   ipv4, ipv6, vpcs
│   │   ├── configuration.yaml
│   │   └── operations.yaml
│   ├── baremetal/
│   └── clusters.yaml       # a small area stays one file
├── kubernetes/
├── network/
└── interface.yaml          # the manifest
```

A split area lists several files in the manifest and **stays one area**: drift matches an area's name against an OpenAPI tag, so siblings would carry names matching no tag and every operation under the real tag would report as undetected drift.

### Working on it

Every reference resolves against `openapi.json` at build time, so a field the API stopped returning fails the build instead of a tool call:

```bash
uv run python -m vultr_mcp.interface --list
```

New areas start from a draft. The scaffolder derives what the spec can tell it — parameters and their real names, pagination, response shape, the tool name from the declared family — and leaves the rest visibly unfinished:

```bash
uv run python -m vultr_mcp.interface --scaffold instances
```

Drafted tools are `enabled: false` with a stub description, because the part that makes a tool worth having (say what it is for, and explicitly what it is *not* for) is the part no generator can write. Fields whose names look like credentials — `default_password`, `s3_secret_key`, the noVNC console link — are commented *out* of `output.include` rather than into it, so returning one is always a deliberate act.

Once an area is covered, the layer reports what the spec grew that nobody has looked at:

```bash
uv run python -m vultr_mcp.interface --drift
```

It covers only *covered* areas, and only operations that are neither served, drafted, declined, nor excluded — so a new endpoint is one line rather than being buried under deliberate omissions. New endpoints exit 0; a reference to an operation the spec no longer has exits non-zero, and CI can demand the stricter gate with `--fail-on-unreviewed`.

Because these tools filter client-side, a filtered search pages through the collection itself (up to `VULTR_MCP_INTERFACE_MAX_PAGES` requests) and reports what it scanned in `meta.filtered`, so a count is never quietly taken from a partial scan. Set `VULTR_MCP_INTERFACE=off` to serve the generated surface alone.

### Keeping up with the spec

`openapi.json` moves because someone else shipped, so CI answers "what changed, and does it still work" on every push:

| Check | Catches |
|---|---|
| `pytest` | spec defects that stop the server parsing at all, plus a new OpenAPI tag nobody has assessed |
| `python -m vultr_mcp.interface` | a tool definition referencing something the spec no longer has |
| `python -m vultr_mcp.interface --drift` | operations added to a covered area that nobody has reviewed |
| `scripts/smoke_interface.py` | the spec being *wrong* — a documented field that never arrives, an undocumented one that does |

The category check deserves a note. A new tag defaults to being exposed, so a spec update can put a product area on the surface with nobody having looked — that is how 24 OAuth client-management operations arrived, and an audit-log endpoint returning `s3_secret_key`. Every tag must now appear in either `DEFAULT_EXCLUDED_CATEGORIES` or `REVIEWED_CATEGORIES`, and a tag in neither fails the build.

---

## Configuration

| Variable | Purpose |
|---|---|
| `VULTR_MCP_TRANSPORT` | `stdio` (default) or `http` |
| `VULTR_API_KEY` | credential for STDIO. Ignored on the HTTP path — see [Authentication](#authentication) |
| `VULTR_API_BASE_URL` | default `https://api.vultr.com/v2` |
| `SERVER_HOST` / `SERVER_PORT` | HTTP bind (default `0.0.0.0:8080`) |
| `SSL_VERIFY` | verify upstream TLS (default `true`) |
| `VULTR_MCP_WRITES_ENABLED` | expose state-changing tools (default `false`) |
| `VULTR_MCP_EXCLUDED_CATEGORIES` | tags to drop (default identity set; empty disables) |
| `VULTR_MCP_CATEGORY_ENDPOINTS` | category endpoints to mount (default: all non-excluded) |
| `VULTR_MCP_INTERFACE` | `off` serves the generated surface alone (default on) |
| `VULTR_MCP_INTERFACE_DIR` | where the interface layer lives (default: `interface/` beside `openapi.json`) |
| `VULTR_MCP_INTERFACE_MAX_PAGES` | requests one filtered search may make while scanning (default `10`) |
| `VULTR_MCP_OUTPUT_SCHEMAS` | advertise generated `outputSchema` (default `false` — tripled listing size without helping agents) |
| `MCP_RESOURCE_URL` | public URL, for OAuth + Host allow-list (default `https://vultrmcp.com`) |
| `MCP_ALLOWED_HOSTS` | extra hosts for DNS-rebinding protection |
| `MCP_ALLOWED_ORIGINS` | extra allowed `Origin` values for the OAuth consent flow |
| `VULTR_OIDC_ENABLED` | enable the OAuthProxy (default `false`) |
| `VULTR_OIDC_PROVIDER_ID` / `VULTR_OAUTH_CLIENT_ID` / `VULTR_OAUTH_CLIENT_SECRET` | approved OAuth app credentials |
| `REDIS_HOST` / `REDIS_PORT` | Redis for OAuth DCR state (required for multi-replica OAuth) |

## Self-hosting

```bash
docker build -t vultr-mcp:latest .
docker run --rm -p 8080:8080 -e VULTR_MCP_TRANSPORT=http vultr-mcp:latest
```

The image runs `python -m vultr_mcp` under uvicorn as a non-root user.

Kubernetes manifests are in `k8s/` (deployment, service, configmap, `secret.yaml.example`, redis, ingress):

```bash
cp k8s/secret.yaml.example k8s/secret.yaml   # then edit; gitignored
kubectl apply -f k8s/
```

Runs 2 replicas behind a round-robin ingress; the server uses **stateless HTTP**, so no session affinity is required. OAuth DCR state is shared via Redis.

## Development

```bash
uv sync
uv run pytest
```

To update the tool surface, replace `openapi.json` with the latest Vultr spec; tools regenerate on startup.

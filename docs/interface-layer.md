# The tool surface and the interface layer

How the 186 tools are produced, reviewed and kept in step with Vultr's OpenAPI spec. For using the server, start with the [README](../README.md).

## The tool surface

Tools come from two places. `FastMCP.from_openapi()` generates them from `openapi.json`; `interface/` defines them by hand, and **a hand-authored tool replaces the generated one** for the same operation.

The read-only surface is **186 tools** — 175 hand-authored, plus 11 generated ones belonging to operations that were reviewed and deliberately declined. Every read operation the server exposes is hand-authored. The count does not grow as the layer does, because the replacement is one-for-one.

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

Files are grouped into one directory per endpoint, so the tree matches what a client can load: `interface/compute/` holds everything `/mcp/compute` serves. That is the product family, except that Marketplace sits under `catalog/` and the database family's directory is `databases/`, both following the endpoints. A test checks every area lives under its endpoint's directory.

```
interface/
├── compute/                # /mcp/compute
│   ├── instances/          # a large area that shares its endpoint gets a directory
│   │   ├── instances.yaml  #   list, get
│   │   ├── networking.yaml #   ipv4, ipv6, vpcs
│   │   ├── configuration.yaml
│   │   └── operations.yaml
│   ├── baremetal/
│   └── clusters.yaml       # a small area stays one file
├── catalog/                # /mcp/catalog
│   ├── marketplace/        #   Marketplace joined catalog with the endpoints
│   └── plans.yaml
├── databases/              # /mcp/databases: the endpoint's only area, so its
│   └── clusters.yaml       #   files are the endpoint's directory
├── kubernetes/
├── network/
├── registry/
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

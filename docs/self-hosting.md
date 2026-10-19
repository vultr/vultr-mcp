# Self-hosting

Running your own copy of the server over HTTP, as the hosted deployment runs. For the environment variables, see [Configuration](../README.md#configuration) in the README.

```bash
docker build -t vultr-mcp:latest .
docker run --rm -p 8080:8080 -e VULTR_MCP_TRANSPORT=http vultr-mcp:latest
```

The image runs `python -m vultr_mcp` under uvicorn as a non-root user.

Kubernetes manifests are in `k8s/` (deployment, service, configmap, `secret.yaml.example`, redis, ingress) — the hosted deployment as it runs:

```bash
cp k8s/secret.yaml.example k8s/secret.yaml   # then edit; gitignored
kubectl apply -f k8s/
```

Runs 2 replicas behind a round-robin ingress; the server uses **stateless HTTP**, so no session affinity is required. OAuth DCR state is shared via Redis.

The ingress controller is Traefik, installed with Helm rather than `kubectl apply`, so its values live apart in `k8s/traefik/` (install command at the top of `values.yaml`).

## Audit records (optional)

The server writes an audit record for every tool call and sign-in as JSON, to stdout over HTTP and to stderr over STDIO, where stdout carries the protocol. Set `VULTR_MCP_AUDIT_DIR` to also write them as files there, one per event type, for a log shipper to tail; `VULTR_MCP_AUDIT_FILE_MAX_MB` (default 256) stops writing a file that has grown that large.

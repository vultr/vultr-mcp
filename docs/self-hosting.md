# Self-hosting

Running your own copy of the server over HTTP, as the hosted deployment runs. For the environment variables, see [Configuration](../README.md#configuration) in the README.

```bash
docker build -t vultr-mcp:latest .
docker run --rm -p 8080:8080 -e VULTR_MCP_TRANSPORT=http vultr-mcp:latest
```

The image runs `python -m vultr_mcp` under uvicorn as a non-root user. That default build is the server alone. The Dockerfile's other target, `with-shipper`, adds an internal log shipper for the audit sidecar described below; it needs a build secret this repo does not contain, and nothing else depends on it.

Kubernetes manifests are in `k8s/` (deployment, service, configmap, `secret.yaml.example`, redis, ingress) — the hosted deployment as it runs:

```bash
cp k8s/secret.yaml.example k8s/secret.yaml   # then edit; gitignored
kubectl apply -f k8s/
```

Runs 2 replicas behind a round-robin ingress; the server uses **stateless HTTP**, so no session affinity is required. OAuth DCR state is shared via Redis.

The ingress controller is Traefik, installed with Helm rather than `kubectl apply`, so its values live apart in `k8s/traefik/` (install command at the top of `values.yaml`).

## Audit records (optional)

Set `VULTR_MCP_AUDIT_DIR` and the server also writes its audit records there as files, one per event type, for a log shipper to tail. `k8s/deployment.yaml` runs one as a sidecar (from the `with-shipper` image) into the ClickHouse and Grafana in `k8s/observability/`, which `kubectl apply -f k8s/` does not include:

```bash
# ClickHouse logins (the shipper, and read-only Grafana) and Grafana's admin login.
# Needs openssl -- without it the values are silently empty (both then refuse to start).
kubectl create secret generic clickhouse-credentials   --from-literal=password="$(openssl rand -hex 24)"   --from-literal=grafana-password="$(openssl rand -hex 24)"
kubectl create secret generic grafana-admin --from-literal=password="$(openssl rand -hex 24)"
kubectl apply -f k8s/observability/
```

On Windows PowerShell, which has no openssl, create the secrets with .NET's generator instead:

```powershell
$g = { $b = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') }) }; kubectl create secret generic clickhouse-credentials --from-literal=password=$(& $g) --from-literal=grafana-password=$(& $g); kubectl create secret generic grafana-admin --from-literal=password=$(& $g)
```

Grafana is at `https://grafana.<your domain>` (the manifest says `grafana.vultrmcp.com`). There is no self sign-up: an admin creates an account for each person who needs one (Administration → Users → New user). The `admin` login is the break-glass account; its password is in the `grafana-admin` secret:

```powershell
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((kubectl get secret grafana-admin -o jsonpath="{.data.password}")))
```

The dashboards ("vultr-mcp audit" and "vultr-mcp eval tickets") are generated: edit `scripts/grafana_dashboard.py`, run it, and apply `k8s/observability/grafana.yaml`.

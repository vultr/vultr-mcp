"""Generate the Grafana dashboards into k8s/observability/grafana.yaml.

Edit the panels here, then from the repo root:

    uv run python scripts/grafana_dashboard.py

and apply grafana.yaml. Each dashboard becomes one key of the grafana-dashboards
ConfigMap, which is rewritten whole, so the script can be re-run after editing.
"""

import json
import re
import sys
from pathlib import Path

DS = {"type": "grafana-clickhouse-datasource", "uid": "clickhouse-vultr-mcp"}
TOOL = "vultr_mcp.log_mcp_tool_call"
UP = "vultr_mcp.log_mcp_upstream_call"
AUTH = "vultr_mcp.log_mcp_auth"
TIMESERIES, TABLE = 0, 1  # the plugin's format numbers (sqlutil.FormatQueryOption)
WHERE = "WHERE $__timeFilter(timestamp)"

f = lambda field: f"JSONExtractString(line, '{field}')"  # noqa: E731
num = lambda field: f"JSONExtractFloat(line, '{field}')"  # noqa: E731


class Board:
    """Panels for one dashboard; ids are per dashboard, as Grafana expects."""

    def __init__(self):
        self.panels = []

    def panel(self, kind, title, sql, fmt, x, y, w, h, description="", **extra):
        p = {
            "id": len(self.panels) + 1,
            "type": kind,
            "title": title,
            "description": description,
            "datasource": DS,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [
                {
                    "refId": "A",
                    "datasource": DS,
                    "editorType": "sql",
                    "format": fmt,
                    "queryType": "timeseries" if fmt == TIMESERIES else "table",
                    "rawSql": " ".join(sql.split()),
                }
            ],
        }
        p.update(extra)
        self.panels.append(p)

    def stat(self, title, sql, x, unit="short", description="", decimals=None, y=0, w=6, h=4):
        defaults = {"unit": unit}
        if decimals is not None:
            defaults["decimals"] = decimals
        self.panel(
            "stat", title, sql, TABLE, x, y, w, h, description,
            fieldConfig={"defaults": defaults, "overrides": []},
            options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                     "colorMode": "none", "graphMode": "none", "textMode": "value"},
        )

    def table(self, title, sql, x, y, w, h, description=""):
        self.panel("table", title, sql, TABLE, x, y, w, h, description,
                   fieldConfig={"defaults": {}, "overrides": []},
                   options={"showHeader": True, "cellHeight": "sm"})

    def series(self, title, sql, x, y, w, h, unit="short", description="", stack=False):
        custom = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "never"}
        if stack:
            custom.update({"stacking": {"mode": "normal", "group": "A"}, "drawStyle": "bars", "fillOpacity": 70})
        self.panel("timeseries", title, sql, TIMESERIES, x, y, w, h, description,
                   fieldConfig={"defaults": {"unit": unit, "custom": custom}, "overrides": []},
                   options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                            "tooltip": {"mode": "multi", "sort": "desc"}})

    def text(self, title, markdown, x, y, w, h):
        """No query: the ticket as filed, and what resolved looks like on the panels beside it."""
        global_id = len(self.panels) + 1
        self.panels.append({
            "id": global_id, "type": "text", "title": title,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": "markdown", "content": markdown},
        })

    def row(self, title, y):
        self.panels.append({"id": len(self.panels) + 1, "type": "row", "title": title,
                            "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})


def dashboard(uid, title, board, time_from, description=""):
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["vultr-mcp"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "refresh": "1m",
        "time": {"from": time_from, "to": "now"},
        "panels": board.panels,
        "templating": {"list": []},
        "annotations": {"list": []},
    }


# -- vultr-mcp audit: the whole service at a glance ---------------------------


def audit():
    b = Board()
    # Row 0: the four numbers.
    b.stat("Tool calls", f"SELECT count() AS calls FROM {TOOL} {WHERE}", 0)
    b.stat("Error rate",
           f"SELECT round(100 * countIf({f('outcome')} != 'ok') / greatest(count(), 1), 1) AS error_rate FROM {TOOL} {WHERE}",
           6, unit="percent", decimals=1,
           description="Share of tool calls whose outcome was not ok, including errors Vultr returned (401, 404, ...).")
    b.stat("p95 duration",
           f"SELECT round(quantile(0.95)({num('duration_ms')})) AS p95 FROM {TOOL} {WHERE}",
           12, unit="ms", description="Whole tool call, as the caller waited for it.")
    b.stat("Accounts",
           f"SELECT uniqExactIf(JSONExtractRaw(line, 'acctid'), JSONHas(line, 'acctid')) AS accounts FROM {TOOL} {WHERE}",
           18, description="Distinct Vultr accounts (acctid) that made a call. API-key callers carry no acctid.")

    # Row 1: volume and latency over time.
    b.series("Tool calls by outcome",
             f"""SELECT $__timeInterval(timestamp) AS time, {f('outcome')} AS outcome, count() AS calls
                 FROM {TOOL} {WHERE} GROUP BY time, outcome ORDER BY time""",
             0, 4, 12, 8, stack=True)
    b.series("Duration",
             f"""SELECT $__timeInterval(timestamp) AS time,
                        quantile(0.5)({num('duration_ms')}) AS p50_total,
                        quantile(0.95)({num('duration_ms')}) AS p95_total,
                        quantileIf(0.95)({num('overhead_ms')}, JSONHas(line, 'overhead_ms')) AS p95_overhead
                 FROM {TOOL} {WHERE} GROUP BY time ORDER BY time""",
             12, 4, 12, 8, unit="ms",
             description="overhead is time spent in this server rather than waiting on the Vultr API. A rising overhead is ours to explain.")

    # Row 2: what gets called, and how upstream answers.
    b.table("Tools",
            f"""SELECT {f('tool')} AS tool, count() AS calls,
                       countIf({f('outcome')} != 'ok') AS errors,
                       round(quantile(0.5)({num('duration_ms')})) AS p50_ms,
                       round(quantile(0.95)({num('duration_ms')})) AS p95_ms,
                       round(avg({num('upstream_calls')}), 1) AS avg_upstream_calls
                FROM {TOOL} {WHERE} GROUP BY tool ORDER BY calls DESC LIMIT 50""",
            0, 12, 12, 10)
    b.series("Upstream responses by status class",
             f"""SELECT $__timeInterval(timestamp) AS time,
                        if(JSONHas(line, 'status'), concat(toString(intDiv(JSONExtractInt(line, 'status'), 100)), 'xx'), 'no response') AS status,
                        count() AS responses
                 FROM {UP} {WHERE} GROUP BY time, status ORDER BY time""",
             12, 12, 12, 10, stack=True,
             description="Every call this server made to the Vultr API. 'no response' is a connection failure or timeout.")

    # Row 3: failures, with who to blame.
    b.table("Failed tool calls",
            f"""SELECT timestamp AS time, {f('tool')} AS tool, {f('fault')} AS fault,
                       {f('error_type')} AS error_type,
                       JSONExtractInt(line, 'upstream_status') AS upstream_status,
                       trim(BOTH '"' FROM JSONExtractRaw(line, 'acctid')) AS acctid,
                       {f('pod')} AS pod, {f('request_id')} AS request_id
                FROM {TOOL} {WHERE} AND {f('outcome')} != 'ok' ORDER BY timestamp DESC LIMIT 200""",
            0, 22, 24, 9,
            description="fault: upstream = Vultr erred, auth = bad or missing credential, request = the call was invalid, unreachable = no response, caller = an argument the tool does not have (the agent's mistake), mcp = this server. Match request_id against Upstream errors for the API's own message.")

    # Row 4: the upstream side.
    b.table("Upstream errors",
            f"""SELECT timestamp AS time, {f('tool')} AS tool, {f('method')} AS method, {f('path')} AS path,
                       JSONExtractInt(line, 'status') AS status, {f('upstream_error')} AS upstream_error,
                       {f('request_id')} AS request_id
                FROM {UP} {WHERE} AND (JSONExtractInt(line, 'status') >= 400 OR NOT JSONHas(line, 'status'))
                ORDER BY timestamp DESC LIMIT 200""",
            0, 31, 14, 10, description="The Vultr API's own error body for each non-2xx response.")
    b.table("Slowest upstream endpoints",
            f"""SELECT {f('method')} AS method, {f('path')} AS path, count() AS calls,
                       round(quantile(0.95)({num('duration_ms')})) AS p95_ms,
                       countIf(JSONExtractInt(line, 'status') >= 500) AS server_errors
                FROM {UP} {WHERE} GROUP BY method, path ORDER BY p95_ms DESC LIMIT 50""",
            14, 31, 10, 10, description="path is the API's path template, so IDs do not split one endpoint into many rows.")

    # Row 5: argument names the agent guessed wrong. Refused since 2.1.10, each one
    # a retry; the evidence for or against renaming a parameter (vke_id -> cluster_id).
    b.table("Arguments the tools don't have",
            f"""SELECT {f('tool')} AS tool, JSONExtractString(line, 'argument_names') AS arguments_passed,
                       count() AS refused, uniqExact({f('mcp_client_id')}) AS clients, max(timestamp) AS last_seen
                FROM {TOOL} {WHERE} AND has(JSONExtract(line, 'error_chain', 'Array(String)'), 'ArgumentError')
                GROUP BY tool, arguments_passed ORDER BY refused DESC LIMIT 50""",
            0, 41, 24, 8,
            description="Calls refused because an argument name is not one the tool has. The error named the right "
                        "parameter, so this is usually one retry. A name that recurs across clients is a naming problem.")

    return dashboard("vultr-mcp-audit", "vultr-mcp audit", b, "now-24h")


# -- vultr-mcp eval tickets: one row per ticket from the eval harness ---------

# Status class of one upstream response; 'no response' is a timeout or refused connection.
STATUS_CLASS = ("if(JSONHas(line, 'status'), concat(toString(intDiv(JSONExtractInt(line, 'status'), 100)), 'xx'), "
                "'no response')")

# (row title, ticket text, SQL predicate on the upstream path). The predicate uses the
# recorded path template, so every instance's calls for an endpoint land in one series.
TICKETS = [
    ("RND-165 — instance upgrades: 500",
     "**Filed:** `vultr_compute_instances_upgrades_get` (`type=plans`) returns 500.\n\n"
     "**Resolved when** the chart beside this shows only 2xx. The table shows Vultr's own "
     "message for each failure; `Error loading upgrade options.` is the API's text, not ours.\n\n"
     "The `type` argument is not recorded, so the chart cannot split plans from other types.",
     "JSONExtractString(line, 'path') LIKE '/v2/instances/%/upgrades'"),
    ("RND-163 — instance VPCs: 500",
     "**Filed:** `vultr_compute_instances_vpcs_list` returns 500.\n\n"
     "**Resolved when** the chart shows only 2xx. Includes the legacy `private-networks` "
     "endpoint, which fails the same way without sending `per_page` — so paging is not the cause. "
     "`/v2/vpcs` and VPC attachments are unaffected.",
     "(JSONExtractString(line, 'path') LIKE '/v2/instances/%/vpcs' OR JSONExtractString(line, 'path') LIKE '/v2/instances/%/private-networks')"),
    ("VKE — cluster resources: 404",
     "**Filed:** `vultr_kubernetes_clusters_resources_get` 404'd — the caller passed `cluster_id` "
     "where the tool takes `vke_id`, and the path went out with the placeholder unfilled.\n\n"
     "**Fixed in 2.1.8:** a missing path argument now fails before any API call, naming it. "
     "**Resolved when** this row shows calls with no 404. An empty row means it has not been retried.",
     "JSONExtractString(line, 'path') LIKE '/v2/kubernetes/clusters/%/resources'"),
    ("Not yet ticketed — instance IPv4 / IPv6",
     "**Seen 2026-09-23 on the eval instance:** IPv4 list 500 `Unable to retrieve instance information.`; "
     "IPv6 list 503 `IPv6 is currently available for this server, but a subnet has not been assigned`.\n\n"
     "Upstream in both cases. Worth checking whether the eval instance itself is in a bad state.",
     "(JSONExtractString(line, 'path') LIKE '/v2/instances/%/ipv4' OR JSONExtractString(line, 'path') LIKE '/v2/instances/%/ipv6')"),
]


def tickets():
    b = Board()
    y = 0

    # Overview: every ticketed endpoint in one table.
    b.table("All ticketed endpoints",
            f"""SELECT JSONExtractString(line, 'path') AS path, count() AS calls,
                       countIf(JSONExtractInt(line, 'status') BETWEEN 200 AND 299) AS ok,
                       round(100 * ok / greatest(calls, 1), 1) AS ok_pct,
                       max(timestamp) AS last_call,
                       argMaxIf({f('upstream_error')}, timestamp, JSONExtractInt(line, 'status') >= 400) AS latest_vultr_error
                FROM {UP} {WHERE}
                  AND ({' OR '.join('(' + p + ')' for _, _, p in TICKETS)}
                       OR JSONExtractString(line, 'path') LIKE '/v2/instances/%/bandwidth')
                GROUP BY path ORDER BY path""",
            0, y, 24, 7,
            description="One row per endpoint named in a ticket. ok_pct is the share of 2xx answers from the Vultr API.")
    y += 7

    for title, text, pred in TICKETS:
        b.row(title, y)
        y += 1
        b.text("The ticket", text, 0, y, 6, 8)
        b.series("Vultr's answers",
                 f"""SELECT $__timeInterval(timestamp) AS time, {STATUS_CLASS} AS status, count() AS responses
                     FROM {UP} {WHERE} AND {pred} GROUP BY time, status ORDER BY time""",
                 6, y, 9, 8, stack=True)
        b.table("Latest failures, in Vultr's words",
                f"""SELECT timestamp AS time, {f('tool')} AS tool, JSONExtractInt(line, 'status') AS status,
                           {f('upstream_error')} AS vultr_says, round({num('duration_ms')}) AS ms, {f('request_id')} AS request_id
                    FROM {UP} {WHERE} AND {pred}
                      AND (JSONExtractInt(line, 'status') >= 400 OR NOT JSONHas(line, 'status'))
                    ORDER BY timestamp DESC LIMIT 100""",
                15, y, 9, 8)
        y += 8

    # RND-166 is a 200 with nothing in it, so status is the wrong signal: size is.
    b.row("RND-166 — instance bandwidth: 200 but empty", y)
    y += 1
    b.text("The ticket",
           "**Filed:** `vultr_compute_instances_bandwidth_get` succeeds with no data.\n\n"
           "Every answer is a 2xx, so watch **size**. Sizes are wire bytes of a gzip body: "
           "**36 bytes is an empty `{\"bandwidth\":{}}`**. Account-level bandwidth (same API) returns real data.\n\n"
           "**Resolved when** instance responses grow past ~40 bytes.",
           0, y, 6, 8)
    b.series("Response size (wire bytes)",
             f"""SELECT $__timeInterval(timestamp) AS time,
                        maxIf(JSONExtractInt(line, 'response_bytes'), JSONExtractString(line, 'path') LIKE '/v2/instances/%/bandwidth') AS instance_bandwidth,
                        maxIf(JSONExtractInt(line, 'response_bytes'), JSONExtractString(line, 'path') = '/v2/account/bandwidth') AS account_bandwidth
                 FROM {UP} {WHERE} AND JSONExtractString(line, 'path') LIKE '%bandwidth'
                 GROUP BY time ORDER BY time""",
             6, y, 9, 8, unit="decbytes")
    b.table("Latest instance bandwidth calls",
            f"""SELECT timestamp AS time, JSONExtractInt(line, 'status') AS status,
                       JSONExtractInt(line, 'response_bytes') AS bytes, {f('response_encoding')} AS encoding,
                       if(bytes <= 40 AND encoding = 'gzip', 'empty', '') AS reads_as, {f('request_id')} AS request_id
                FROM {UP} {WHERE} AND JSONExtractString(line, 'path') LIKE '/v2/instances/%/bandwidth'
                ORDER BY timestamp DESC LIMIT 100""",
            15, y, 9, 8)

    y += 8

    # Forced re-sign-ins: tool-call records cannot show these, the auth records (2.1.9+) can.
    b.row("Sign-ins and token refreshes — \"re-auth a few times a day\"", y)
    y += 1
    b.text("The report",
           "**Reported:** having to sign in again several times a day.\n\n"
           "**Cause:** a client refreshing twice at once; Vultr revokes the whole sign-in when a refresh "
           "token is used twice. **2.1.9** makes duplicate refreshes share one call to Vultr.\n\n"
           "**Resolved when** refreshes stop failing and sign-ins drop to about one per client per month. "
           "`shared_*` refreshes are duplicates that would each have signed someone out.\n\n"
           "_Records start with 2.1.9; the row is empty before it._",
           0, y, 6, 9)
    b.series("Sign-ins and refreshes",
             f"""SELECT $__timeInterval(timestamp) AS time, concat({f('action')}, ' ', {f('outcome')}) AS event, count() AS n
                 FROM {AUTH} {WHERE} GROUP BY time, event ORDER BY time""",
             6, y, 9, 9, stack=True,
             description="authorize = a sign-in began; code_exchange = a sign-in completed; refresh = a silent token renewal.")
    b.series("How refreshes were resolved",
             f"""SELECT $__timeInterval(timestamp) AS time, {f('resolved_by')} AS resolved_by, count() AS n
                 FROM {AUTH} {WHERE} AND {f('action')} = 'refresh' AND JSONHas(line, 'resolved_by')
                 GROUP BY time, resolved_by ORDER BY time""",
             15, y, 9, 9, stack=True,
             description="upstream = the one real refresh; shared_inflight / shared_finished = a duplicate given that result "
                         "instead of reaching Vultr; direct = no coalescing (store unavailable).")
    y += 9
    b.table("Per client",
            f"""SELECT {f('client_id')} AS client, any({f('client_kind')}) AS kind,
                       groupUniqArrayIf(trim(BOTH '"' FROM JSONExtractRaw(line, 'acctid')), JSONHas(line, 'acctid')) AS accounts,
                       countIf({f('action')} = 'code_exchange' AND {f('outcome')} = 'ok') AS sign_ins,
                       countIf({f('action')} = 'refresh' AND {f('outcome')} = 'ok') AS refreshes_ok,
                       countIf({f('action')} = 'refresh' AND {f('outcome')} != 'ok') AS refreshes_failed,
                       countIf(startsWith({f('resolved_by')}, 'shared')) AS duplicates_absorbed,
                       maxIf(timestamp, {f('action')} = 'code_exchange') AS last_sign_in
                FROM {AUTH} {WHERE} GROUP BY client ORDER BY sign_ins DESC""",
            0, y, 12, 9,
            description="One row per OAuth client. A healthy client signs in rarely and refreshes hourly while in use.")
    b.table("Refresh failures",
            f"""SELECT timestamp AS time, {f('client_id')} AS client,
                       trim(BOTH '"' FROM JSONExtractRaw(line, 'acctid')) AS acctid, {f('outcome')} AS outcome,
                       {f('resolved_by')} AS resolved_by, {f('error')} AS error, {f('error_description')} AS detail
                FROM {AUTH} {WHERE} AND {f('action')} = 'refresh' AND {f('outcome')} != 'ok'
                ORDER BY timestamp DESC LIMIT 100""",
            12, y, 12, 9,
            description="refused = the token was unknown to us (rotated, expired or revoked); error = Vultr refused it. "
                        "Either way the client has to sign in again.")

    return dashboard("vultr-mcp-eval-tickets", "vultr-mcp eval tickets", b, "now-7d",
                     "The eval harness tickets, each against the upstream calls it is about.")


DASHBOARDS = {"vultr-mcp-audit.json": audit(), "vultr-mcp-eval-tickets.json": tickets()}

CONFIGMAP_HEAD = """apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-dashboards
  labels:
    app: grafana
data:
  # Generated by scripts/grafana_dashboard.py -- edit there, not here. The
  # queries read the JSON record in `line` with JSONExtract*; field names are
  # the ones audit.py and diagnostics.py emit.
"""


def main():
    manifest = Path("k8s/observability/grafana.yaml")
    text = manifest.read_text(encoding="utf-8")
    docs = text.split("\n---\n")
    idx = [i for i, d in enumerate(docs) if re.search(r"^  name: grafana-dashboards$", d, re.M)]
    if len(idx) != 1:
        sys.exit("expected exactly one grafana-dashboards ConfigMap")
    body = CONFIGMAP_HEAD
    for name, dash in DASHBOARDS.items():
        block = "\n".join("    " + line for line in json.dumps(dash, indent=2).splitlines())
        body += f"  {name}: |\n{block}\n"
    docs[idx[0]] = body.rstrip("\n")
    manifest.write_text("\n---\n".join(docs), encoding="utf-8", newline="\n")
    for name, dash in DASHBOARDS.items():
        print(f"{name}: {len(dash['panels'])} panels")


if __name__ == "__main__":
    main()

"""Call every compiled interface tool against a real API and report what happened.

Everything the runtime does has so far been verified against mock transports --
against fixtures written from the spec's examples, which is to say against
assumptions about what Vultr returns. This runs the same code against a real
server: auth forwarding, path substitution, the rename map, client-side
filtering, the auto-pagination loop, response shaping, and computed fields.

Read-only by construction. Compiled tools carry an `access` that the validator
cross-checks against the HTTP method, and this refuses to call a write one
anyway.

    VULTR_API_BASE_URL=http://local.api.vultr.com/v2 \
    VULTR_API_KEY=... \
    uv run python scripts/smoke_interface.py

Tools needing an id are fed one harvested from a search tool in the same
product area, so by-id paths get exercised rather than skipped.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from vultr_mcp.interface import runtime  # noqa: E402
from vultr_mcp.interface.compiler import CompiledTool, compile_interface  # noqa: E402
from vultr_mcp.server import (  # noqa: E402
    VULTR_API_BASE,
    PerRequestVultrAuth,
    load_spec,
)

TIMEOUT = float(os.environ.get("SMOKE_TIMEOUT", "30"))

# Arguments a tool needs before it will return anything, where the spec does not
# say so. GET /logs rejects a call with no time range -- 422, "Either a
# start_time or end_time must be provided" -- but marks neither parameter
# required, so nothing in the definition can be validated against that. Without
# this the smoke run reports a permanent 422 that is really a missing argument,
# and a check that always shows the same complaint stops being read.
SMOKE_ARGUMENTS: dict[str, dict[str, object]] = {
    "vultr_account_logs_list": {
        "start_time": (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    },
}


class Result:
    def __init__(self, tool: CompiledTool) -> None:
        self.tool = tool
        self.status = "?"
        self.detail = ""
        self.items: int | None = None
        self.seconds = 0.0
        self.sample: dict | None = None
        # True when the collection came back as scalars rather than records, so
        # "no sample" can be reported as what it is rather than as an empty
        # result.
        self.scalar_rows: bool = False
        # Set when the spec's container key is not in the response at all, which
        # disables shaping and filtering without raising anything.
        self.shape_mismatch: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _container(tool: CompiledTool) -> str | None:
    return tool.output.container_key if tool.output else None


def _harvest_id(payload: dict, tool: CompiledTool) -> str | None:
    """An id from a search result, to feed the matching by-id tool."""
    body = payload.get(_container(tool) or "", [])
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body[0].get("id")
    return None


async def _run(tool: CompiledTool, arguments: dict, client) -> Result:
    result = Result(tool)
    started = time.monotonic()
    try:
        payload = await asyncio.wait_for(
            runtime.execute(tool, arguments, client), timeout=TIMEOUT
        )
    except asyncio.TimeoutError:
        result.status, result.detail = "timeout", f"no response in {TIMEOUT:.0f}s"
    except runtime.VultrAPIError as error:
        # Upstream said no. That is information about the API or the
        # credential, not about the layer.
        result.status, result.detail = "api-error", str(error)[:120]
    except json.JSONDecodeError as error:
        # A dev stack with display_errors on emits PHP warnings into the body.
        result.status, result.detail = "bad-json", f"upstream sent non-JSON: {error}"
    except Exception as error:  # noqa: BLE001 - the whole point is to catch it
        result.status = "LAYER-BUG"
        result.detail = f"{type(error).__name__}: {error}"[:160]
    else:
        result.status = "ok"
        plan = tool.output
        if plan is not None and plan.unwrapped:
            # The payload is the resource, not a wrapper around one.
            result.items = 1
            result.sample = payload if isinstance(payload, dict) else None
            result.seconds = time.monotonic() - started
            return result
        container = _container(tool)
        body = payload.get(container) if container else None
        if container and container not in payload:
            # The spec named a container the API does not send. Nothing raises:
            # shape_response returns the payload untouched, so `include` never
            # trims and -- worse -- declared filters never run, and the tool
            # answers a filtered question with every row it has. /plans-metal
            # returns `plans_metal` where the spec says `plans`, which is how
            # this was found: silent, and wrong in the direction nobody checks.
            others = sorted(
                key for key, value in payload.items() if isinstance(value, list)
            )
            result.shape_mismatch = (
                f"declared container {container!r} absent from the response"
                + (f"; it sends {', '.join(others)}" if others else "")
            )
        if isinstance(body, list):
            result.items = len(body)
            # Not every collection holds records. get-instance-neighbors returns
            # instance ids, list-available-versions returns version strings,
            # get-dns-domain-dnssec returns DNS records as text. `sample` is a
            # row to inspect fields on, so a scalar row is no sample at all --
            # taking one regardless is how this crashed on `row.items()`.
            result.sample = body[0] if body and isinstance(body[0], dict) else None
            result.scalar_rows = bool(body) and not isinstance(body[0], dict)
        elif isinstance(body, dict):
            result.items = 1
            result.sample = body
        filtered = (payload.get("meta") or {}).get("filtered")
        if filtered:
            result.detail = f"meta.filtered={json.dumps(filtered)}"
    result.seconds = time.monotonic() - started
    return result


def _check_shaping(result: Result) -> list[str]:
    """Did the shaping and computed fields actually do their job?"""
    notes = []
    tool, sample = result.tool, result.sample
    if not sample:
        if result.scalar_rows:
            return [f"{result.items} scalar row(s), no fields to shape"]
        return ["no rows returned, shaping unexercised"]

    include = tool.output.item_include if tool.output else None
    if include:
        leaked = sorted(set(sample) - set(include) - {p.name for p in tool.computed})
        if leaked:
            notes.append(f"fields outside include leaked: {', '.join(leaked[:6])}")
        missing = sorted(set(include) - set(sample))
        if missing:
            notes.append(f"include names absent from the response: {', '.join(missing[:6])}")

    for plan in tool.computed:
        if plan.name not in sample:
            notes.append(f"computed '{plan.name}' did not resolve")
    return notes


def _announce(index: int, total: int, tool: CompiledTool) -> None:
    """Name the tool before the call, so a stall is attributable to one of them.

    At 24 tools a silent run finished before anyone wondered. At 180, with a
    per-call timeout of 30s, silence is indistinguishable from a hang -- and
    which tool is hanging is exactly what you need to know.
    """
    print(f"[{index:>3}/{total}] {tool.name:<46}", end="", flush=True)


def _report(result: "Result") -> None:
    """One line per tool, printed as it finishes rather than banked to the end."""
    mark = {"ok": "PASS", "skipped": "skip"}.get(result.status, result.status.upper())
    rows = "" if result.items is None else f"{result.items} row(s)"
    print(f" {mark:>9} {result.seconds:5.2f}s  {rows}", flush=True)
    if result.detail:
        print(f"            {result.detail}", flush=True)
    if result.shape_mismatch:
        print(f"            SHAPE-MISMATCH: {result.shape_mismatch}", flush=True)
    if result.ok:
        for note in _check_shaping(result):
            print(f"            note: {note}", flush=True)


async def main() -> int:
    interface = compile_interface(REPO / "interface", load_spec())
    tools = [tool for tool in interface.tools if not tool.is_write]

    # An optional substring narrows the run to one area or one tool. A full
    # sweep is 180 live calls; verifying a single change should not need it.
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if only:
        tools = [t for t in tools if only in t.name or only == t.product_area]
        if not tools:
            print(f"no read tool matches {only!r}")
            return 1

    print(
        f"interface {interface.version}: {len(tools)} read tool(s)"
        + (f" matching {only!r}" if only else "")
    )
    print(f"target: {VULTR_API_BASE}")
    print(f"per-call timeout: {TIMEOUT:g}s (SMOKE_TIMEOUT to change)")
    if not os.environ.get("VULTR_API_KEY"):
        print("warning: VULTR_API_KEY is not set; expect 401s\n")

    client = httpx.AsyncClient(
        base_url=VULTR_API_BASE,
        auth=PerRequestVultrAuth(),
        verify=os.environ.get("SSL_VERIFY", "true").lower() not in ("false", "0", "no"),
        timeout=TIMEOUT,
        headers={"User-Agent": "vultr-mcp-smoke/1.0"},
    )

    searches = [tool for tool in tools if not _needs_id(tool)]
    # Fewest path parameters first, so a tool that needs two of them can be fed
    # from the result of one that needed one: get-database-user wants a database
    # id AND a username, and the username only exists once the users list has
    # run.
    by_id = sorted(
        (tool for tool in tools if _needs_id(tool)),
        key=lambda tool: sum(1 for p in tool.parameters if p.location == "path"),
    )
    harvested: dict[str, dict] = {}
    results: list[Result] = []

    total = len(searches) + len(by_id)
    done = 0

    async with client:
        for tool in searches:
            done += 1
            _announce(done, total, tool)
            result = await _run(tool, dict(SMOKE_ARGUMENTS.get(tool.name, {})), client)
            results.append(result)
            _report(result)
            if result.ok and result.sample:
                _harvest(harvested, tool, result.sample)

        for tool in by_id:
            done += 1
            _announce(done, total, tool)
            arguments, missing = _fill_path(tool, harvested)
            if missing:
                result = Result(tool)
                result.status = "skipped"
                result.detail = (
                    f"nothing in the {tool.product_area} results supplies "
                    + ", ".join(missing)
                )
                results.append(result)
                _report(result)
                continue
            result = await _run(tool, arguments, client)
            results.append(result)
            _report(result)
            if result.ok and result.sample:
                _harvest(harvested, tool, result.sample)

    layer_bugs = sum(1 for r in results if r.status == "LAYER-BUG")
    passed = sum(1 for r in results if r.ok)
    print(f"\n{passed}/{len(results)} returned successfully; {layer_bugs} layer bug(s)")

    mismatches = [r for r in results if r.shape_mismatch]
    for r in mismatches:
        print(f"  shape mismatch: {r.tool.name}: {r.shape_mismatch}")

    # A mismatch means a tool is quietly not doing what its definition says --
    # no error, no exception, just unshaped rows and filters that never ran --
    # so it fails the run the same way a layer bug does.
    if layer_bugs or mismatches:
        return 1
    # A credential was supplied and nothing worked: a wrong key, an unreachable
    # host, an IP allowlist. That must not pass quietly in CI, where nobody
    # reads the output unless it fails.
    if os.environ.get("VULTR_API_KEY") and results and not passed:
        print("a key was supplied but nothing succeeded; treating that as failure")
        return 1
    return 0


def _harvest(store: dict, tool: CompiledTool, row: dict) -> None:
    """Remember a sample row, tagged with the collection it came from.

    Keyed by product area but keeping each source separate, because ids of
    different resources are not interchangeable. Feeding an audit-log
    *location* id to the subscription tool earns a 400, which reads like a
    broken tool and is really a broken harness.
    """
    scalars = {k: v for k, v in row.items() if isinstance(v, (str, int))}
    if scalars:
        container = tool.output.container_key if tool.output else None
        store.setdefault(tool.product_area, []).append((container or tool.name, scalars))


def _resource_matches(param: str, container: str) -> bool:
    """Whether `<param>_id` names the resource this collection holds.

    baremetal_id against bare_metals, registry_id against registries,
    database_id against databases: compared with separators removed and a
    trailing plural dropped, which is enough for every shape Vultr uses.
    """
    def normalise(text: str) -> str:
        text = text.replace("_", "").replace("-", "").lower()
        return text[:-1] if text.endswith("s") and not text.endswith("ss") else text

    return normalise(param) == normalise(container)


def _fill_path(tool: CompiledTool, harvested: dict) -> tuple[dict, list[str]]:
    """Values for every path parameter, or the names we could not supply.

    An id parameter prefers a row from the collection that actually holds that
    resource, and only falls back to any id in the area when nothing matches.
    Other parameters (`username`) are satisfied by a field of the same name.
    Calling with one of two path parameters filled produces a confusing 404
    rather than a useful result, so an unfillable tool is skipped and says which
    value was missing.
    """
    sources = harvested.get(tool.product_area, [])
    arguments = dict(SMOKE_ARGUMENTS.get(tool.name, {}))
    missing: list[str] = []

    for plan in tool.parameters:
        if plan.location != "path":
            continue
        name = plan.agent_name

        named = next((row[name] for _, row in sources if name in row), None)
        if named is not None:
            arguments[name] = named
            continue

        if name.endswith("_id"):
            resource = name[: -len("_id")]
            preferred = next(
                (
                    row["id"]
                    for container, row in sources
                    if "id" in row and _resource_matches(resource, container)
                ),
                None,
            )
            fallback = next((row["id"] for _, row in sources if "id" in row), None)
            chosen = preferred if preferred is not None else fallback
            if chosen is not None:
                arguments[name] = chosen
                continue

        missing.append(name)

    return arguments, missing


def _needs_id(tool: CompiledTool) -> bool:
    return any(plan.location == "path" for plan in tool.parameters)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

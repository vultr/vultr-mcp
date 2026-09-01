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
        if isinstance(body, list):
            result.items = len(body)
            result.sample = body[0] if body else None
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


async def main() -> int:
    interface = compile_interface(REPO / "interface", load_spec())
    tools = [tool for tool in interface.tools if not tool.is_write]

    print(f"interface {interface.version}: {len(tools)} read tool(s)")
    print(f"target: {VULTR_API_BASE}")
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

    async with client:
        for tool in searches:
            result = await _run(tool, dict(SMOKE_ARGUMENTS.get(tool.name, {})), client)
            results.append(result)
            if result.ok and result.sample:
                _harvest(harvested, tool.product_area, result.sample)

        for tool in by_id:
            arguments, missing = _fill_path(tool, harvested)
            if missing:
                result = Result(tool)
                result.status = "skipped"
                result.detail = (
                    f"nothing in the {tool.product_area} results supplies "
                    + ", ".join(missing)
                )
                results.append(result)
                continue
            result = await _run(tool, arguments, client)
            results.append(result)
            if result.ok and result.sample:
                _harvest(harvested, tool.product_area, result.sample)

    print()
    layer_bugs = 0
    for result in results:
        mark = {"ok": "PASS", "skipped": "skip"}.get(result.status, result.status.upper())
        rows = "" if result.items is None else f"{result.items} row(s)"
        print(f"[{mark:>9}] {result.tool.name:<34} {result.seconds:5.2f}s  {rows}")
        if result.detail:
            print(f"            {result.detail}")
        if result.status == "LAYER-BUG":
            layer_bugs += 1
        if result.ok:
            for note in _check_shaping(result):
                print(f"            note: {note}")

    passed = sum(1 for r in results if r.ok)
    print(f"\n{passed}/{len(results)} returned successfully; {layer_bugs} layer bug(s)")

    if layer_bugs:
        return 1
    # A credential was supplied and nothing worked: a wrong key, an unreachable
    # host, an IP allowlist. That must not pass quietly in CI, where nobody
    # reads the output unless it fails.
    if os.environ.get("VULTR_API_KEY") and results and not passed:
        print("a key was supplied but nothing succeeded; treating that as failure")
        return 1
    return 0


def _harvest(store: dict, area: str, row: dict) -> None:
    """Remember a sample row per product area, to feed by-id tools."""
    known = store.setdefault(area, {})
    for field, value in row.items():
        if isinstance(value, (str, int)) and field not in known:
            known[field] = value


def _fill_path(tool: CompiledTool, harvested: dict) -> tuple[dict, list[str]]:
    """Values for every path parameter, or the names we could not supply.

    A path parameter is satisfied by a field of the same name in a harvested
    row (`username`), or by that row's `id` when the parameter is an id
    (`database_id`). Calling with one of two path parameters filled produces a
    confusing 404 rather than a useful result, so an unfillable tool is skipped
    and says which value was missing.
    """
    known = harvested.get(tool.product_area, {})
    arguments = dict(SMOKE_ARGUMENTS.get(tool.name, {}))
    missing: list[str] = []
    for plan in tool.parameters:
        if plan.location != "path":
            continue
        if plan.agent_name in known:
            arguments[plan.agent_name] = known[plan.agent_name]
        elif plan.agent_name.endswith("_id") and "id" in known:
            arguments[plan.agent_name] = known["id"]
        else:
            missing.append(plan.agent_name)
    return arguments, missing


def _needs_id(tool: CompiledTool) -> bool:
    return any(plan.location == "path" for plan in tool.parameters)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Call every compiled interface tool against a real API and report what happened.

Read-only: it refuses to call a write tool. Tools needing an id are fed one harvested from a list
tool in the same product area.

    VULTR_API_KEY=... uv run python scripts/smoke_interface.py [substring]
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

# Arguments a tool needs where the spec does not say so (GET /logs 422s without a time range).
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
        # The collection came back as scalars rather than records.
        self.scalar_rows: bool = False
        # The spec's container key is missing from the response, which silently disables shaping and filtering.
        self.shape_mismatch: str | None = None
        # Several rows to take ids from, since a collection may mix kinds (/applications).
        self.harvest_rows: list = []

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _container(tool: CompiledTool) -> str | None:
    return tool.output.container_key if tool.output else None


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
        # Upstream refused: about the API or the credential, not the layer.
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
            result.harvest_rows = [payload] if isinstance(payload, dict) else []
            result.seconds = time.monotonic() - started
            return result
        container = _container(tool)
        body = payload.get(container) if container else None
        if container and container not in payload:
            # The spec named a container the API does not send, so filters silently never run.
            lists = sorted(
                key for key, value in payload.items() if isinstance(value, list)
            )
            keys = sorted(payload)
            result.shape_mismatch = (
                f"declared container {container!r} absent from the response"
                + (f"; the collection is {', '.join(lists)}" if lists else "")
                + "; the response is "
                + (
                    ", ".join(f"{key}={_describe(payload[key])}" for key in keys[:8])
                    or "(empty)"
                )
            )
        if isinstance(body, list):
            result.items = len(body)
            # Some collections hold scalars (ids, versions), which are no sample to inspect.
            result.sample = body[0] if body and isinstance(body[0], dict) else None
            result.scalar_rows = bool(body) and not isinstance(body[0], dict)
            result.harvest_rows = [row for row in body[:8] if isinstance(row, dict)]
        elif isinstance(body, dict):
            result.items = 1
            result.sample = body
            result.harvest_rows = [body]
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
    if not include:
        # Shaping is off; print what the API actually sends for whoever restores the allowlist.
        return [f"no allowlist; the row carries: {', '.join(sorted(sample))}"]

    if include:
        leaked = sorted(set(sample) - set(include) - {p.name for p in tool.computed})
        if leaked:
            notes.append(f"fields outside include leaked: {', '.join(leaked[:6])}")
        missing = sorted(set(include) - set(sample))
        if missing:
            notes.append(
                f"include names absent from the response: {', '.join(missing[:6])}"
            )
            # Say what the row does carry, since absence alone does not explain a rename.
            notes.append(f"  the row carries: {', '.join(sorted(sample))}")

    for plan in tool.computed:
        if plan.name not in sample:
            notes.append(f"computed '{plan.name}' did not resolve")
    return notes


def _announce(index: int, total: int, tool: CompiledTool) -> None:
    """Name the tool before the call, so a stall is attributable to one of them."""
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

    # An optional substring narrows the run to one area or tool.
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
    # Fewest path parameters first, so later tools can be fed from earlier results.
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
            if result.ok:
                for row in result.harvest_rows:
                    _harvest(harvested, tool, row)

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
            if result.ok:
                for row in result.harvest_rows:
                    _harvest(harvested, tool, row)

    layer_bugs = sum(1 for r in results if r.status == "LAYER-BUG")
    passed = sum(1 for r in results if r.ok)
    print(f"\n{passed}/{len(results)} returned successfully; {layer_bugs} layer bug(s)")

    mismatches = [r for r in results if r.shape_mismatch]
    for r in mismatches:
        print(f"  shape mismatch: {r.tool.name}: {r.shape_mismatch}")

    # A container mismatch fails the run, since the tool silently ignores its definition.
    if layer_bugs or mismatches:
        return 1
    # A credential was supplied and nothing worked, which must fail CI.
    if os.environ.get("VULTR_API_KEY") and results and not passed:
        print("a key was supplied but nothing succeeded; treating that as failure")
        return 1
    return 0


def _describe(value: object) -> str:
    """A one-line shape for a response value, enough to fix a spec against."""
    if isinstance(value, dict):
        keys = sorted(value)
        shown = ", ".join(keys[:8]) + ("..." if len(keys) > 8 else "")
        return f"object{{{shown}}}"
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return f"list[{len(value)}] of object{{{', '.join(sorted(value[0])[:8])}}}"
        return f"list[{len(value)}]"
    return type(value).__name__


def _harvest(store: dict, tool: CompiledTool, row: dict) -> None:
    """Remember a sample row per product area, tagged with its collection, since ids are not interchangeable."""
    # Empty strings are not ids (they build paths like /applications//variables); zero is.
    scalars = {
        k: v for k, v in row.items() if isinstance(v, (str, int)) and v != ""
    }
    if scalars:
        container = tool.output.container_key if tool.output else None
        store.setdefault(tool.product_area, []).append((container or tool.name, scalars))


def _resource_matches(param: str, container: str) -> bool:
    """Whether `<param>_id` names this collection's resource (baremetal_id and bare_metals), ignoring separators and plurals."""
    def normalise(text: str) -> str:
        text = text.replace("_", "").replace("-", "").lower()
        return text[:-1] if text.endswith("s") and not text.endswith("ss") else text

    return normalise(param) == normalise(container)


def _fill_path(tool: CompiledTool, harvested: dict) -> tuple[dict, list[str]]:
    """Values for every path parameter, preferring the collection that holds each id, or the names we could not supply."""
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

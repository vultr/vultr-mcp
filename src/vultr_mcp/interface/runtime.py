"""Execute a compiled interface tool: rename arguments, call the API, filter client-side, shape the response.

Filtered responses report how much was scanned, so a count is never taken from a partial scan.
"""

from __future__ import annotations

import difflib
import os
from typing import Any
from urllib.parse import quote

import httpx

from vultr_mcp.interface import expressions
from vultr_mcp.interface.compiler import CompiledTool, FilterPlan

# With a filter active the runtime pages through the collection itself, up to this many requests.
MAX_AUTO_PAGES = 10

# Per-request page size while auto-paging, independent of the page_size the agent asked for.
AUTO_PAGE_SIZE = 500


def max_auto_pages() -> int:
    """Page cap, overridable for deployments with unusually large accounts."""
    raw = os.environ.get("VULTR_MCP_INTERFACE_MAX_PAGES")
    if not raw:
        return MAX_AUTO_PAGES
    try:
        return max(1, int(raw))
    except ValueError:
        return MAX_AUTO_PAGES


def _matches(value: Any, wanted: Any, match: str) -> bool:
    """Whether one field value satisfies one filter."""
    if match == "one_of":
        wanted_values = wanted if isinstance(wanted, list) else [wanted]
        return any(_matches(value, item, "equals") for item in wanted_values)
    if value is None:
        return False
    if match == "equals":
        return value == wanted or str(value) == str(wanted)
    text = str(value)
    if match == "contains":
        return str(wanted) in text
    if match == "contains_ci":
        return str(wanted).lower() in text.lower()
    return False


def _passes(item: Any, active: list[tuple[FilterPlan, Any]]) -> bool:
    """Filters are ANDed: every one the agent supplied has to match."""
    return all(
        _matches(expressions.read_path(item, plan.path), wanted, plan.match)
        for plan, wanted in active
    )


def _shape_item(item: Any, tool: CompiledTool) -> Any:
    """Keep the allowlisted fields of one item, then add computed fields derived from the unshaped item."""
    if not isinstance(item, dict):
        return item

    computed: dict[str, Any] = {}
    for plan in tool.computed:
        value = expressions.evaluate(plan.expression, item)
        if value is not None:
            computed[plan.name] = value

    include = tool.output.item_include if tool.output else None
    shaped = (
        {key: value for key, value in item.items() if key in include}
        if include
        else dict(item)
    )
    shaped.update(computed)
    return shaped


def shape_response(
    payload: dict[str, Any],
    tool: CompiledTool,
    arguments: dict[str, Any],
    scan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Filter, trim and annotate a response (no I/O); ``scan`` says what the fetch did and lands in ``meta``."""
    plan = tool.output
    container = plan.container_key if plan else None

    # An unwrapped response is its own item.
    if plan and plan.unwrapped:
        return _shape_item(payload, tool)

    if container is None or container not in payload:
        return payload

    active = [
        (filter_plan, arguments[filter_plan.agent_name])
        for filter_plan in tool.filters
        if arguments.get(filter_plan.agent_name) not in (None, "", [])
    ]

    body = payload[container]
    result: dict[str, Any] = dict(payload)

    if plan and plan.is_collection and isinstance(body, list):
        scanned = len(body)
        items = [item for item in body if _passes(item, active)] if active else body
        matched = len(items)

        limit = _result_limit(tool, arguments)
        truncated = False
        if active and isinstance(limit, int) and matched > limit:
            items, truncated = items[:limit], True

        result[container] = [_shape_item(item, tool) for item in items]

        if active:
            result["meta"] = _filter_meta(
                payload.get("meta"), scanned, matched, truncated, scan
            )
    else:
        result[container] = _shape_item(body, tool)

    if plan and plan.envelope_include is not None:
        result = {
            key: value for key, value in result.items() if key in plan.envelope_include
        }

    return result


def _result_limit(tool: CompiledTool, arguments: dict[str, Any]) -> int | None:
    """How many matches the agent asked for: when filtering here, page_size means matches to return."""
    if not tool.pagination or not tool.pagination.agent_page_param:
        return None
    name = tool.pagination.agent_page_param
    value = arguments.get(name)
    if value is None:
        value = next(
            (plan.default for plan in tool.parameters if plan.agent_name == name), None
        )
    return value if isinstance(value, int) else None


def _filter_meta(
    api_meta: Any,
    scanned: int,
    matched: int,
    truncated: bool,
    scan: dict[str, Any] | None,
) -> dict[str, Any]:
    """What the filter saw, beside the API's own ``meta.total``, so counts are never overstated."""
    meta = dict(api_meta) if isinstance(api_meta, dict) else {}
    complete = bool(scan["complete"]) if scan else True
    filtered: dict[str, Any] = {
        "matched": matched,
        "scanned": scanned,
        "complete": complete and not truncated,
    }
    if scan and scan.get("pages"):
        filtered["pages_fetched"] = scan["pages"]
    if truncated:
        filtered["note"] = (
            "More results matched than page_size allowed; raise page_size to see them."
        )
    elif not complete:
        filtered["note"] = (
            "Stopped before the end of the collection, so this is a partial count. "
            "Narrow the search or page through the unfiltered tool instead."
        )
    meta["filtered"] = filtered
    return meta


class ArgumentError(ValueError):
    """The caller passed an argument this tool does not have; its own class so audit records can count it."""


# An unknown argument is refused only when it carries a value.
_EMPTY = (None, "", [], {})


def check_arguments(tool: CompiledTool, arguments: dict[str, Any]) -> None:
    """Refuse argument names the tool's input schema lacks, naming the ones it accepts, since ignoring them misleads."""
    schema = tool.input_schema or {}
    accepted = sorted(schema.get("properties", {}))
    unknown = sorted(
        name for name, value in arguments.items() if name not in accepted and value not in _EMPTY
    )
    if not unknown:
        return

    required = list(schema.get("required", []))
    missing = [name for name in required if arguments.get(name) in _EMPTY]
    described = []
    for name in unknown:
        # One stray name for one missing required name is a clear swap; otherwise suggest only close spellings.
        if len(unknown) == 1 and len(missing) == 1:
            guess = missing[0]
        else:
            close = difflib.get_close_matches(name, accepted, n=1, cutoff=0.75)
            guess = close[0] if close else None
        described.append(f"{name!r}" + (f" (did you mean {guess!r}?)" if guess else ""))

    noun = "argument" if len(unknown) == 1 else "arguments"
    accepts = ", ".join(f"{a} (required)" if a in required else a for a in accepted) or "no arguments"
    raise ArgumentError(f"{tool.name} has no {noun} {', '.join(described)}. It accepts: {accepts}.")


def build_request(
    tool: CompiledTool, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Resolve arguments into (path, query), applying declared defaults, which MCP clients do not fill in."""
    path = tool.path_template
    query: dict[str, Any] = {}

    for plan in tool.parameters:
        value = arguments.get(plan.agent_name, plan.default)
        if value is None:
            # A query parameter may be absent; a path segment may not, or the API 404s on the placeholder.
            # The caller left out a required ID; ArgumentError so audit counts it as their mistake, not ours.
            if plan.location == "path":
                raise ArgumentError(
                    f"{tool.name} requires {plan.agent_name!r}, which was not supplied"
                )
            continue
        if plan.location == "path":
            path = path.replace("{" + plan.api_name + "}", quote(str(value), safe=""))
        else:
            query[plan.api_name] = value

    return path, query


def _next_cursor(payload: dict[str, Any]) -> str | None:
    links = (payload.get("meta") or {}).get("links") or {}
    return links.get("next") or None


async def call_api(
    client: httpx.AsyncClient, method: str, path: str, query: dict[str, Any]
) -> dict[str, Any]:
    """One API call, raising the API's own message on failure."""
    response = await client.request(method.upper(), path, params=query)
    if response.status_code >= 400:
        raise VultrAPIError(response)
    if not response.content:
        return {}
    return response.json()


class VultrAPIError(Exception):
    """A non-2xx from api.vultr.com, surfaced with whatever it said."""

    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        detail = ""
        try:
            body = response.json()
            detail = body.get("error") or body.get("message") or ""
        except ValueError:
            detail = response.text[:400]
        self.detail = detail
        super().__init__(f"Vultr API returned {response.status_code}: {detail}".strip())


async def execute(
    tool: CompiledTool, arguments: dict[str, Any], client: httpx.AsyncClient
) -> dict[str, Any]:
    """Run one compiled tool end to end."""
    check_arguments(tool, arguments)
    path, query = build_request(tool, arguments)

    filtering = tool.filters_client_side and any(
        arguments.get(plan.agent_name) not in (None, "", []) for plan in tool.filters
    )
    pagination = tool.pagination
    # An explicit cursor means the agent is walking pages itself.
    walking_pages = bool(
        pagination
        and pagination.agent_cursor_param
        and arguments.get(pagination.agent_cursor_param)
    )
    if not filtering or pagination is None or walking_pages:
        payload = await call_api(client, tool.method, path, query)
        # One page, filtered: complete only if there was no next page.
        scan = (
            {"pages": 1, "complete": not _next_cursor(payload)} if filtering else None
        )
        return shape_response(payload, tool, arguments, scan=scan)

    container = tool.output.container_key if tool.output else None
    collected: list[Any] = []
    payload: dict[str, Any] = {}
    page_query = dict(query)
    page_query[pagination.api_page_param] = AUTO_PAGE_SIZE
    cursor: str | None = None
    pages = 0
    complete = False

    while pages < max_auto_pages():
        if cursor:
            page_query[pagination.api_cursor_param] = cursor
        payload = await call_api(client, tool.method, path, page_query)
        pages += 1
        page = payload.get(container) if container else None
        if isinstance(page, list):
            collected.extend(page)
        cursor = _next_cursor(payload)
        if not cursor or not isinstance(page, list) or not page:
            complete = True
            break

    merged = dict(payload)
    # Only where the API sent the container; some responses use a different key than the spec declares.
    if container and container in payload:
        merged[container] = collected
    return shape_response(
        merged, tool, arguments, scan={"pages": pages, "complete": complete}
    )

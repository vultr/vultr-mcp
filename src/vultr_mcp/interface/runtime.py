"""Execute a compiled interface tool: call the API, then shape what comes back.

Three things happen here that the generated surface does not do.

**Renaming.** The agent sees ``page_size``; the API wants ``per_page``. The
compiler resolved that mapping, so this is a lookup, not a guess.

**Client-side filtering.** ``GET /clusters`` accepts only ``per_page`` and
``cursor`` -- not label, region, or status -- so a tool that lets the agent
search by label has to do the searching itself. That has a consequence worth
being explicit about: a filter applied after the fact only sees what was
fetched. The fetch policy below is the answer, and every filtered response says
how much was scanned so a counting question ("how many clusters do I have?")
cannot be answered confidently from a partial scan.

**Shaping.** Dropping fields the agent does not need is the main lever on token
cost, and computed fields ride along on the same pass.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

from vultr_mcp.interface import expressions
from vultr_mcp.interface.compiler import CompiledTool, FilterPlan

# Fetch policy for client-side filtering.
#
# Filtering happens after a paged fetch, so "give me 100" plus a label filter
# would otherwise mean "fetch 100, then filter", not "find 100 matches" -- and
# an agent counting results would silently undercount on a multi-page account.
# So when a filter is active the runtime pages through the collection itself,
# up to this many requests, and reports whether it reached the end.
#
# The cap exists because an account with thousands of resources should not turn
# one tool call into an unbounded fan-out. When it is hit, the response says so
# rather than pretending to be complete.
MAX_AUTO_PAGES = 10

# Per-request page size used while auto-paging, independent of the page_size the
# agent asked for: that one is about how many results it wants back, this one is
# about how few round trips it takes to scan the collection.
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
    """Keep the allowlisted fields of one item, then add its computed fields.

    Computed values are derived from the *unshaped* item, so a field can be
    counted or summed without also being returned -- ``instance_count`` does not
    force ``instances`` into the payload.
    """
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
    """Filter, trim, and annotate a response. Pure -- no I/O, so it is testable.

    ``scan`` carries what the fetch policy did (pages fetched, whether the
    collection was exhausted) and is folded into ``meta`` when filtering ran.
    """
    plan = tool.output
    container = plan.container_key if plan else None

    # An unwrapped response is its own item: there is no key to look under, so
    # the include list and the computed fields apply to the payload itself.
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
    """How many matches the agent asked for.

    When filtering happens here, the page-size input stops meaning "rows to
    fetch" and starts meaning "matches to return" -- which is what an agent
    that wrote page_size actually wanted.
    """
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
    """Say plainly what the filter saw, so counts are never overstated.

    ``meta.total`` stays as the API reported it -- the size of the collection,
    not of the match -- and the filtered block sits beside it.
    """
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


def build_request(
    tool: CompiledTool, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Resolve arguments into (path, query), applying declared defaults.

    Defaults are applied here rather than left to the client, because MCP
    clients do not fill in JSON Schema defaults -- an omitted ``page_size``
    would otherwise reach the API as nothing at all.
    """
    path = tool.path_template
    query: dict[str, Any] = {}

    for plan in tool.parameters:
        value = arguments.get(plan.agent_name, plan.default)
        if value is None:
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
    path, query = build_request(tool, arguments)

    filtering = tool.filters_client_side and any(
        arguments.get(plan.agent_name) not in (None, "", []) for plan in tool.filters
    )
    pagination = tool.pagination
    # An explicit cursor means the agent is walking pages itself; honour that
    # rather than scanning the collection out from under it.
    walking_pages = bool(
        pagination
        and pagination.agent_cursor_param
        and arguments.get(pagination.agent_cursor_param)
    )
    if not filtering or pagination is None or walking_pages:
        payload = await call_api(client, tool.method, path, query)
        # One page, filtered: honest about whether that page was the whole
        # collection, so "complete" never means "complete as far as I looked".
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
    # Only write the collection back where the API actually put one. When the
    # spec names a container the response does not carry -- /plans-metal sends
    # `plans_metal` against a declared `plans`, /storage-gateways sends
    # `storage_gateways` against a declared `storage_gateway` -- `collected`
    # stayed empty because nothing matched, and assigning it here would add a
    # fabricated empty key beside the real collection. The agent would then see
    # both an invented `plans: []` and the genuine `plans_metal`, with nothing
    # raised and nothing logged.
    #
    # Passing the payload straight through is what shape_response already does
    # for the single-call path in the same situation, so the two agree: a
    # container the API does not send means no shaping, not invented data.
    if container and container in payload:
        merged[container] = collected
    return shape_response(
        merged, tool, arguments, scan={"pages": pages, "complete": complete}
    )

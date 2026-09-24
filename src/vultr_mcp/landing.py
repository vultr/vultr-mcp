"""The docs page's endpoint list, built from the tools actually served.

It used to be typed into the HTML, and by September it listed 180 tools under
names retired a month before -- including purge_pullzone, which the server had
stopped serving because it changes state. Built from the mounted servers, the
list is whatever a client connecting to each endpoint would be given.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable

COUNT_MARK = "<!--EP_COUNT-->"
LIST_MARK = "<!--EP_LIST-->"


def first_line(description: str | None) -> str:
    """The tool's opening sentence, for a hover title -- what it returns.

    Plain text: the few tools still generated from the spec carry its Markdown
    (``**Deprecated**: use [List Instance VPCs](#operation/...)``), which a
    tooltip would show literally.
    """
    for line in (description or "").strip().splitlines():
        if line.strip():
            text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line.strip())  # [text](link) -> text
            return re.sub(r"(\*\*|__|`)", "", text)
    return ""


def _block(slug: str, tools: list[tuple[str, str]]) -> str:
    n = len(tools)
    items = "\n".join(
        f'            <code title="{html.escape(desc, quote=True)}">{html.escape(name)}</code>'
        for name, desc in tools
    )
    return (
        '          <details class="ep">\n'
        f'            <summary><span class="ep-path">/{html.escape(slug)}</span>'
        f'<span class="ep-count">{n} tool{"" if n == 1 else "s"}</span></summary>\n'
        f'            <div class="ep-tools">\n{items}\n            </div>\n'
        "          </details>"
    )


def render(template: str, total_tools: int, endpoints: Iterable[tuple[str, list[tuple[str, str]]]]) -> str:
    """Fill the page's placeholders. ``endpoints`` is (slug, [(tool, description)])."""
    ordered = [(slug, sorted(tools)) for slug, tools in sorted(endpoints)]
    n = len(ordered)
    count = f"{n} endpoint{'' if n == 1 else 's'} · {total_tools} tools"
    listing = "\n".join(_block(slug, tools) for slug, tools in ordered if tools)
    return template.replace(COUNT_MARK, count).replace(LIST_MARK, listing)


def unavailable(template: str) -> str:
    """The page without a list: better than no page, and says where to look."""
    note = (
        '          <p class="muted">The endpoint list could not be built just now; '
        "an MCP client's tool listing shows the same thing.</p>"
    )
    return template.replace(COUNT_MARK, "").replace(LIST_MARK, note)


async def tools_of(server: Any) -> list[tuple[str, str]]:
    """(name, first line of description) for every tool a server serves."""
    listed = await server.list_tools(run_middleware=False)
    return [(tool.name, first_line(tool.description)) for tool in listed]

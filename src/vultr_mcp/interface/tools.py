"""Register compiled interface tools with FastMCP.

An interface tool takes its name, description and schema from the reviewed YAML
rather than openapi.json -- the point of the layer -- so ``from_openapi``
cannot produce it. It is registered here as a ``Tool`` subclass whose ``run``
calls the runtime engine.

Output schemas are omitted as they are on the generated surface: they are the
largest thing in a listing and only the input schema is needed to make a call.
A shaped response would not match the spec's schema anyway.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastmcp.exceptions import ToolError
from fastmcp.tools.tool import Tool, ToolResult
from mcp.types import ToolAnnotations
from pydantic import ConfigDict

from vultr_mcp.interface import runtime
from vultr_mcp.interface.compiler import CompiledTool


class InterfaceTool(Tool):
    """A hand-authored tool backed by exactly one openapi operation."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    plan: Any = None
    client: Any = None

    @classmethod
    def build(cls, plan: CompiledTool, client: httpx.AsyncClient) -> "InterfaceTool":
        return cls(
            name=plan.name,
            description=plan.description,
            parameters=plan.input_schema,
            output_schema=None,
            tags=set(plan.tags),
            annotations=ToolAnnotations(
                readOnlyHint=not plan.is_write,
                destructiveHint=plan.is_write,
            ),
            meta={"operation": plan.operation_id, "product_area": plan.product_area},
            plan=plan,
            client=client,
        )

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            payload = await runtime.execute(self.plan, arguments, self.client)
        except runtime.VultrAPIError as error:
            # Surface the API's own message: "403 Forbidden" tells the agent to
            # stop, an httpx traceback tells it nothing.
            raise ToolError(str(error)) from error
        return ToolResult(content=payload)

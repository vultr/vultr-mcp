"""Register compiled interface tools with FastMCP, as ``Tool`` subclasses whose ``run`` calls the runtime.

Output schemas are omitted, as on the generated surface.
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
            # Surface the API's own message rather than an httpx traceback.
            raise ToolError(str(error)) from error
        return ToolResult(content=payload)

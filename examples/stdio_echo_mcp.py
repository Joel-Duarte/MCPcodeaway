"""Minimal real stdio MCP server used to verify mcp_stdio onboarding end-to-end."""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("stdio-echo-mcp")


@mcp.tool(
    description="Uppercase a string over MCP stdio transport.",
    annotations=ToolAnnotations(read_only_hint=True),
)
def echo_upper(text: str) -> dict[str, str]:
    return {"upper": text.upper()}


if __name__ == "__main__":
    mcp.run(transport="stdio")

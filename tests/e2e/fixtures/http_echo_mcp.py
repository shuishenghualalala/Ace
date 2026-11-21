"""Minimal HTTP MCP server used by the keyless replay test."""

from __future__ import annotations

import sys

from mcp.server import MCPServer

mcp = MCPServer("echo", version="http-test")


@mcp.tool()
def echo(text: str) -> str:
    """Return the input text."""
    return f"echo: {text}"


if __name__ == "__main__":
    mcp.run("streamable-http", host="127.0.0.1", port=int(sys.argv[1]))

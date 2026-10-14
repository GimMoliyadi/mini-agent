"""Disposable SDK MCP server for stdio/HTTP acceptance; no user data."""

import asyncio
import os
from pathlib import Path
import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mini-agent-acceptance", host="127.0.0.1",
              port=int(sys.argv[2]) if len(sys.argv) > 2 else 8000)


@mcp.tool()
def add(a: int, b: int) -> dict[str, int]:
    """Add two integers and return a structured result."""
    return {"sum": a + b, "server_pid": os.getpid()}


@mcp.tool()
def fail() -> str:
    """Report an intentional, actionable server error."""
    raise ValueError("denominator must be non-zero")


@mcp.tool()
async def slow(delay: float, started_path: str = "") -> str:
    """Wait to exercise timeout cancellation."""
    if started_path:
        Path(started_path).write_text("started", encoding="utf-8")
    await asyncio.sleep(delay)
    return "finished"


@mcp.tool()
def hidden() -> str:
    """This tool must never be exposed without an allowlist entry."""
    return "hidden"


@mcp.tool()
def python_test_guidance(module: str) -> dict[str, str]:
    """Provide the unittest command and review guidance for a Python module."""
    return {"command": f"python -m unittest {module} -q",
            "review": "Keep the supplied tests unchanged; verify behavior after editing source."}


if __name__ == "__main__":
    mcp.run(transport=sys.argv[1] if len(sys.argv) > 1 else "stdio")

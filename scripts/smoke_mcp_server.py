#!/usr/bin/env python3
"""Start bounded-loops-mcp over stdio and verify its advertised tool surface."""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _smoke() -> None:
    command = shutil.which("bounded-loops-mcp")
    if command is None:
        sibling = Path(sys.executable).parent / (
            "bounded-loops-mcp.exe" if sys.platform == "win32" else "bounded-loops-mcp"
        )
        if sibling.is_file():
            command = str(sibling)
    if command is None:
        raise RuntimeError(
            "bounded-loops-mcp is not installed; pip install 'bounded-loops[mcp]'"
        )
    parameters = StdioServerParameters(command=command, args=[])
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            required = {"bl_run", "bl_lint", "bl_list", "bl_show", "bl_runs"}
            missing = required - names
            if missing:
                raise RuntimeError(f"MCP server is missing tools: {sorted(missing)}")
            print(f"MCP smoke passed: {len(names)} tools ({', '.join(sorted(required))})")


if __name__ == "__main__":
    asyncio.run(_smoke())

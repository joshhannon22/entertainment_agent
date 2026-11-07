"""Talk to tools-server as an MCP client, with no LLM involved.

python scripts/smoke.py [url]   (default http://localhost:8001/mcp)
"""
import asyncio
import sys

from mcp import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8001/mcp"


async def main() -> None:
    async with Client(URL) as client:
        listed = await client.list_tools()
        for tool in listed.tools:
            print(f"{tool.name}: {tool.description.splitlines()[0]}")
            print(f"    args: {list(tool.input_schema['properties'])}")

        result = await client.call_tool("search_tv_shows", {"query": "arcane"})
        print(f"\nis_error={result.is_error}")
        print(result.content[0].text[:300])


asyncio.run(main())
"""Export the optional MCP tool contract without connecting to a backend."""

import argparse
import asyncio
import json
from pathlib import Path

from mcp import Client

from ashare_quant.mcp.client import Backend, Settings
from ashare_quant.mcp.server import create_server


async def document():
    server = create_server(Backend(Settings("http://127.0.0.1:1")))
    async with Client(server) as client:
        result = await client.list_tools()
        return dict(
            schema="research-mcp.tools.v1",
            tools=[
                t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in result.tools
            ],
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/contracts/research-mcp.tools.json")
    )
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(asyncio.run(document()), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()

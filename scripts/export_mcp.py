"""Export the optional MCP v2 contract (tools, resources, templates, prompts) without a live backend."""

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
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()
        prompts = await client.list_prompts()
        return dict(
            schema="research-mcp.tools.v2",
            tools=[
                t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools.tools
            ],
            resources=[
                r.model_dump(mode="json", by_alias=True, exclude_none=True)
                for r in resources.resources
            ],
            resource_templates=[
                rt.model_dump(mode="json", by_alias=True, exclude_none=True)
                for rt in templates.resource_templates
            ],
            prompts=[
                p.model_dump(mode="json", by_alias=True, exclude_none=True)
                for p in prompts.prompts
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

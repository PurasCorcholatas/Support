from typing import Any
from langchain_mcp_adapters.client import MultiServerMCPClient

mcp_tools = None


async def get_mcp_tools():

    global mcp_tools

    if mcp_tools is not None:
        return mcp_tools

    servers: dict[str, Any] = {
        "zammad": {
            "command": "uvx",
            "args": [
                "--from",
                "git+https://github.com/basher83/zammad-mcp.git",
                "mcp-zammad"
            ]
        }
    }

    client = MultiServerMCPClient(servers)

    mcp_tools = await client.get_tools()

    return mcp_tools
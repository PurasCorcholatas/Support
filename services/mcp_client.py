import os
from typing import Any
from langchain_mcp_adapters.client import MultiServerMCPClient

mcp_tools = None

async def get_mcp_tools():
    global mcp_tools

    if mcp_tools is not None:
        return mcp_tools

    servers: dict[str, Any] = {
        "zammad": {
            "transport": "streamable_http",
            "url": "http://127.0.0.1:8001/mcp/",
        }
    }

    client = MultiServerMCPClient(servers)
    mcp_tools = await client.get_tools()
    # print("Tools cargadas:", [t.name for t in mcp_tools])
    return mcp_tools
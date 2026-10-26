"""Tiny HTTP MCP server run as a subprocess by test_http.py, so a test can kill
and restart a real server process. Usage: python _http_echo_server.py PORT"""

import sys

import mcp_types as types
import uvicorn
from mcp.server.lowlevel import Server


async def call_tool(ctx, params):
    return types.CallToolResult(content=[types.TextContent(type="text", text="pong")])


async def list_tools(ctx, params):
    return types.ListToolsResult(tools=[types.Tool(name="ping", input_schema={"type": "object"})])


server = Server("echo", on_call_tool=call_tool, on_list_tools=list_tools)
uvicorn.run(server.streamable_http_app(stateless_http=True), port=int(sys.argv[1]), log_level="critical")

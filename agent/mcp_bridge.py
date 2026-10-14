"""Bridge between an MCP v2 Client and a LangChain chat model.

We don't use langchain-mcp-adapters: its latest release pins mcp<2, and its
tools have no way to attach per-call `_meta`, which AgentGuard needs.

Two jobs:
  tool_specs()     MCP tools -> OpenAI-style function specs for model.bind_tools()
  call_mcp_tool()  one LLM tool call -> MCP tools/call (with _meta) -> ToolMessage
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import mcp_types as types
from langchain_core.messages import ToolMessage
from langchain_core.messages.tool import ToolCall
from mcp import Client

TASK_ID_META = "agentguard/task_id"
IDEMPOTENCY_META = "agentguard/idempotency_key"


async def tool_specs(client: Client) -> list[dict[str, Any]]:
    """The tools the gateway lists (already filtered by policy), in the format
    every LangChain chat model's bind_tools() accepts."""
    listing = await client.list_tools()
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            },
        }
        for tool in listing.tools
    ]


def idempotency_key_for(tool_call_id: str) -> str:
    """One LLM tool call = one logical action = one idempotency key.

    If the *same* tool call is executed again (a crash and resume, a retried
    graph step), it reuses the key and AgentGuard replays the stored result.
    If the model *decides again* to do the same thing, that's a new tool call
    with a new id, so it counts toward loop and budget limits, as it should.
    """
    safe = re.sub(r"[^A-Za-z0-9_.:-]", "_", tool_call_id)
    if not safe or len(safe) > 150:
        safe = hashlib.sha256(tool_call_id.encode()).hexdigest()
    return f"tc-{safe}"


async def call_mcp_tool(client: Client, tool_call: ToolCall, task_id: str) -> ToolMessage:
    meta = {TASK_ID_META: task_id, IDEMPOTENCY_META: idempotency_key_for(tool_call["id"])}
    result = await client.call_tool(tool_call["name"], tool_call["args"], meta=meta)
    return ToolMessage(
        content=result_text(result),
        tool_call_id=tool_call["id"],
        name=tool_call["name"],
        # status="error" tells the model this call failed (blocked or broken),
        # so it can change approach instead of treating the text as data.
        status="error" if result.is_error else "success",
        artifact={"agentguard": {k: v for k, v in (result.meta or {}).items() if k.startswith("agentguard/")}},
    )


def result_text(result: types.CallToolResult) -> str:
    parts = [c.text for c in result.content if isinstance(c, types.TextContent)]
    if parts:
        return "\n".join(parts)
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    return "(no output)"

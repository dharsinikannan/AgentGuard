"""Bridge between an MCP v2 client and a LangChain chat model.

We don't use langchain-mcp-adapters: its latest release pins mcp<2, and its
tools have no way to attach per-call `_meta`, which AgentGuard needs.

Two jobs:
  tool_specs()     MCP tools -> OpenAI-style function specs for model.bind_tools()
  call_mcp_tool()  one LLM tool call -> MCP tools/call (with _meta) -> ToolMessage

`client` is a SupervisedClient (reconnects by itself) or a plain mcp.Client.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import mcp_types as types
from langchain_core.messages import ToolMessage
from langchain_core.messages.tool import ToolCall
from agentguard.connection import ConnectionLost

log = logging.getLogger("agent.mcp_bridge")

TASK_ID_META = "agentguard/task_id"
IDEMPOTENCY_META = "agentguard/idempotency_key"


async def tool_specs(client: Any) -> list[dict[str, Any]]:
    """The tools the gateway lists (already filtered by policy), in the format
    every LangChain chat model's bind_tools() accepts."""
    listing = await client.list_tools()
    tools = listing.tools if isinstance(listing, types.ListToolsResult) else listing
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
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


async def call_mcp_tool(client: Any, tool_call: ToolCall, task_id: str, *, attempts: int = 3) -> ToolMessage:
    """Run one tool call through AgentGuard, surviving a dropped connection.

    If the connection dies mid-call we resend the *same* call with the *same*
    idempotency key, even for writes. That's safe only because the gateway dedupes
    by key: if the original already ran, the retry gets its stored result; if
    it's still running, the retry waits for it. Never twice.
    """
    meta = {TASK_ID_META: task_id, IDEMPOTENCY_META: idempotency_key_for(tool_call["id"])}
    for attempt in range(1, attempts + 1):
        try:
            result = await client.call_tool(tool_call["name"], tool_call["args"], meta=meta)
            break
        except ConnectionLost as e:
            log.warning("%s: connection lost (attempt %d/%d), retrying with the same key: %s",
                        tool_call["name"], attempt, attempts, e)
    else:
        return ToolMessage(
            content=("[AgentGuard client] lost the connection to AgentGuard and could not reconnect; "
                     "this action may or may not have happened. Check the current state before redoing it."),
            tool_call_id=tool_call["id"], name=tool_call["name"], status="error",
            artifact={"agentguard": {"agentguard/verdict": "connection_lost"}},
        )
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

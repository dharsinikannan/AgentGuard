"""Fault injection for tests and demos."""

from __future__ import annotations

from typing import Any

import mcp_types as types

from agentguard.gateway import Downstream


class DropResponseDownstream(Downstream):
    """Runs the tool for real, then loses the response, like a connection that
    dies after the server acted but before the reply arrived. This is the case
    where a naive retry launches a second instance."""

    def __init__(self, *args: Any, drops: int = 1, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.drops = drops
        self.calls = 0

    async def call_tool(self, tool: str, args: dict[str, Any]) -> types.CallToolResult:
        self.calls += 1
        result = await super().call_tool(tool, args)
        if self.drops > 0:
            self.drops -= 1
            raise ConnectionResetError("response lost")
        return result

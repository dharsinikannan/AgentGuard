"""The MCP layer: an MCP server to the agent, an MCP client to each downstream server.

    agent ──tools/call "cloud__delete_bucket"──► Gateway ──tools/call "delete_bucket"──► cloud

Tools are namespaced `<server>__<tool>` so two servers can both have `list` without
colliding. Per-call identity comes from the request's `_meta`:

    agentguard/task_id          required; scopes budget, loops, approvals, audit
    agentguard/idempotency_key  optional; same key = same logical action

All safety decisions are delegated to `Guard`. This module only translates between
MCP and the guard, and handles transport failures.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import mcp_types as types
from mcp import MCPError
from mcp.server.lowlevel import Server

from agentguard.connection import SupervisedClient
from agentguard.guard import CallContext, Decision, Guard, Verdict

log = logging.getLogger("agentguard.gateway")

TASK_ID_META = "agentguard/task_id"
IDEMPOTENCY_META = "agentguard/idempotency_key"
SEPARATOR = "__"


class Downstream(SupervisedClient):
    """One downstream MCP server behind a supervised, self-reconnecting connection.
    Its name becomes the tool prefix (`cloud` -> `cloud__delete_bucket`)."""

    def __init__(self, name: str, target: Any, **kwargs: Any):
        if SEPARATOR in name:
            raise ValueError(f"server name may not contain {SEPARATOR!r}: {name}")
        super().__init__(name, target, **kwargs)


class Gateway:
    def __init__(self, guard: Guard, downstreams: list[Downstream], *, read_retries: int = 2,
                 execution_timeout: float = 120, attach_timeout: float = 15):
        self.guard = guard
        self.downstreams = {d.name: d for d in downstreams}
        self.read_retries = read_retries
        self.execution_timeout = execution_timeout   # hard cap per call, even when detached
        self.attach_timeout = attach_timeout         # how long a retry waits for an in-flight original
        self.server = Server(
            "agentguard",
            version="0.1.0",
            instructions=(
                "Tools are proxied through AgentGuard. Blocked calls return an error starting "
                "with [AgentGuard]; read the reason and change approach instead of repeating the call."
            ),
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    @asynccontextmanager
    async def running(self) -> AsyncIterator[Gateway]:
        """Keep downstream connections alive for the duration of the block."""
        async with anyio.create_task_group() as tg:
            for d in self.downstreams.values():
                await tg.start(d.run)
            try:
                yield self
            finally:
                tg.cancel_scope.cancel()

    # ------------------------------------------------------------ tools/list

    async def _list_tools(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        visible: list[types.Tool] = []
        for name, downstream in self.downstreams.items():
            try:
                tools = await downstream.list_tools()
            except Exception:
                # One dead server should not hide every other server's tools.
                log.exception("downstream %s: list_tools failed", name)
                continue
            for tool in tools:
                full_name = f"{name}{SEPARATOR}{tool.name}"
                # The agent never sees forbidden tools, so it can't plan around them.
                if self.guard.policy.is_visible(full_name):
                    visible.append(tool.model_copy(update={"name": full_name}))
        return types.ListToolsResult(tools=visible)

    # ------------------------------------------------------------ tools/call

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        meta = params.meta or {}
        task_id = meta.get(TASK_ID_META)
        if not task_id:
            return _error(f"missing _meta['{TASK_ID_META}']; every call must belong to a task", "invalid")

        server_name, sep, tool = params.name.partition(SEPARATOR)
        downstream = self.downstreams.get(server_name)
        if not sep or downstream is None:
            return _error(f"unknown tool {params.name}", "invalid")

        try:
            call = CallContext(task_id, params.name, dict(params.arguments or {}), meta.get(IDEMPOTENCY_META))
        except ValueError as e:
            return _error(str(e), "invalid")

        # Detached from the agent's connection. Over HTTP, a client that disconnects
        # cancels its request handler. Unshielded, that cancellation could land after
        # the idempotency claim or mid-downstream-call, leaving the key "in_progress"
        # forever and the outcome unrecorded. So decide + execute + record finish
        # even if nobody is listening; a reconnecting agent then gets the result
        # by retrying with the same idempotency key.
        with anyio.CancelScope(shield=True):
            decision = await self.guard.check(call)
            if decision.verdict is Verdict.ALLOW:
                return await self._execute(call, downstream, server_name, tool, decision)

        if decision.in_flight:
            # Same key is executing right now (the agent reconnected and retried).
            # Wait for it instead of refusing. Not shielded: if this caller goes
            # away too, there's no one to wait for.
            attached = await self.guard.wait_for_result(call, self.attach_timeout)
            if attached is not None:
                decision = attached
        if decision.verdict is Verdict.REPLAY:
            return _annotate(types.CallToolResult.model_validate(decision.cached_result), decision)
        return _blocked(decision)

    async def _execute(self, call: CallContext, downstream: Downstream, server_name: str, tool: str,
                       decision: Decision) -> types.CallToolResult:
        """Run an allowed call and record its outcome. Called inside a shielded scope,
        so it has its own deadline: a hung downstream must not hold a key forever."""
        lost = _error(
            f"connection to {server_name} was lost during {call.tool}; the action may or "
            "may not have happened. Check the current state before trying again.",
            "outcome_unknown",
        )
        # Reads may be retried on a fresh connection. Writes may not: if the
        # connection dropped after the server acted, a retry does it twice.
        attempts = 1 + (self.read_retries if self.guard.policy.is_safe_to_retry(call.tool) else 0)
        result: types.CallToolResult | None = None
        with anyio.move_on_after(self.execution_timeout) as timer:
            for attempt in range(1, attempts + 1):
                try:
                    result = await downstream.call_tool(tool, call.args)
                    break
                except MCPError as e:
                    # The server answered: the outcome is known (it failed).
                    result = _error(f"{server_name} returned an error: {e}", "downstream_error")
                    break
                except Exception as e:
                    log.warning("downstream %s: %s failed (attempt %d/%d): %r",
                                server_name, tool, attempt, attempts, e)
                    if attempt == attempts:
                        await self.guard.record_unknown(call, repr(e))
                        return lost
        if timer.cancelled_caught or result is None:
            await self.guard.record_unknown(call, f"no answer within {self.execution_timeout}s")
            return lost

        await self.guard.record_result(
            call, result.model_dump(mode="json", by_alias=True, exclude_none=True), bool(result.is_error)
        )
        return _annotate(result, decision)


def _error(message: str, verdict: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"[AgentGuard] {message}")],
        is_error=True,
        meta={"agentguard/verdict": verdict},
    )


def _blocked(decision: Decision) -> types.CallToolResult:
    result = _error(f"BLOCKED ({decision.check}): {decision.reason}", decision.verdict.value)
    meta = {**result.meta, "agentguard/check": decision.check}
    if decision.approval_id:
        meta["agentguard/approval_id"] = decision.approval_id
    return result.model_copy(update={"meta": meta})


def _annotate(result: types.CallToolResult, decision: Decision) -> types.CallToolResult:
    meta = {**(result.meta or {}), "agentguard/verdict": decision.verdict.value}
    return result.model_copy(update={"meta": meta})

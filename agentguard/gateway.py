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
from mcp import Client, MCPError
from mcp.server.lowlevel import Server

from agentguard.guard import CallContext, Decision, Guard, Verdict

log = logging.getLogger("agentguard.gateway")

TASK_ID_META = "agentguard/task_id"
IDEMPOTENCY_META = "agentguard/idempotency_key"
SEPARATOR = "__"


class Downstream:
    """One downstream MCP server, behind a single long-lived connection.

    `run()` is a supervisor task that owns the `Client`. A failed call marks the
    connection broken; the supervisor then closes it and reconnects. The `Client`
    is entered and exited in the same task, which anyio requires.
    """

    def __init__(self, name: str, target: Any, *, call_timeout: float = 30, connect_timeout: float = 10):
        if SEPARATOR in name:
            raise ValueError(f"server name may not contain {SEPARATOR!r}: {name}")
        self.name = name
        self.target = target              # anything mcp.Client accepts
        self.call_timeout = call_timeout
        self.connect_timeout = connect_timeout
        self._client: Client | None = None
        self._ready = anyio.Event()
        self._broken = anyio.Event()

    async def run(self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        task_status.started()
        while True:
            try:
                async with Client(self.target, read_timeout_seconds=self.call_timeout) as client:
                    self._client = client
                    self._ready.set()
                    await self._broken.wait()
            except Exception:
                log.exception("downstream %s: connection failed", self.name)
                await anyio.sleep(1)
            finally:
                self._client = None
                self._ready = anyio.Event()
                self._broken = anyio.Event()

    async def _get_client(self) -> Client:
        with anyio.fail_after(self.connect_timeout):
            while self._client is None:
                await self._ready.wait()
        return self._client

    def _mark_broken(self, client: Client) -> None:
        # Only break the connection that actually failed, not a fresh replacement.
        if client is self._client:
            self._broken.set()

    async def list_tools(self) -> list[types.Tool]:
        client = await self._get_client()
        tools: list[types.Tool] = []
        cursor = None
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                return tools

    async def call_tool(self, tool: str, args: dict[str, Any]) -> types.CallToolResult:
        """Raises MCPError if the server answered with an error; any other
        exception means we don't know whether the server ran the tool."""
        client = await self._get_client()
        try:
            return await client.call_tool(tool, args)
        except MCPError:
            raise
        except Exception:
            self._mark_broken(client)
            raise


class Gateway:
    def __init__(self, guard: Guard, downstreams: list[Downstream], *, read_retries: int = 2):
        self.guard = guard
        self.downstreams = {d.name: d for d in downstreams}
        self.read_retries = read_retries
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

        decision = await self.guard.check(call)
        if decision.verdict is Verdict.REPLAY:
            return _annotate(types.CallToolResult.model_validate(decision.cached_result), decision)
        if decision.verdict is not Verdict.ALLOW:
            return _blocked(decision)

        # Reads may be retried on a fresh connection. Writes may not: if the
        # connection dropped after the server acted, a retry does it twice.
        attempts = 1 + (self.read_retries if self.guard.policy.is_safe_to_retry(params.name) else 0)
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
                    return _error(
                        f"connection to {server_name} was lost during {params.name}; the action may or "
                        "may not have happened. Check the current state before trying again.",
                        "outcome_unknown",
                    )

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

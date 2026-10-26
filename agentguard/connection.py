"""A long-lived MCP client connection that survives the other side dying.

Used by the gateway for each downstream server, and by the agent for its
connection to the gateway.

Why a supervisor task: an `mcp.Client` must be entered and exited in the same
task (anyio), and when its connection dies it is finished for good. Every later
call fails even after the server comes back, and the failure cancels whatever
code is inside its `async with` block. So the Client lives in `run()`, its own
task: a dead connection ends that task's block, and `run()` opens a new Client.
Callers in other tasks just get `ConnectionLost` and can retry.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import anyio
import mcp_types as types
from mcp import Client, MCPError
from mcp_types import CONNECTION_CLOSED

log = logging.getLogger("agentguard.connection")

RECONNECT_DELAY = 0.5


class ConnectionLost(Exception):
    """The connection died before an answer arrived: the call may or may not have run."""


class SupervisedClient:
    def __init__(self, name: str, target: Any, *, call_timeout: float = 30, connect_timeout: float = 10,
                 target_factory: Callable[[], Any] | None = None):
        self.name = name
        self.target = target              # anything mcp.Client accepts: URL, StdioServerParameters, Server
        # A transport object (e.g. an authenticated HTTP transport) is single-use, so
        # reconnecting needs a fresh one each time: pass a factory instead.
        self._target_factory = target_factory or (lambda: self.target)
        self.call_timeout = call_timeout
        self.connect_timeout = connect_timeout
        self._client: Client | None = None     # None while (re)connecting
        self._broken = anyio.Event()

    async def run(self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        task_status.started()
        while True:
            self._broken = broken = anyio.Event()
            try:
                async with Client(self._target_factory(), read_timeout_seconds=self.call_timeout) as client:
                    self._client = client
                    await broken.wait()
            except Exception as e:
                log.warning("%s: connection ended (%s); reconnecting", self.name, _describe(e))
            finally:
                self._client = None
            await anyio.sleep(RECONNECT_DELAY)  # don't hammer a server that is down

    async def _get_client(self) -> Client:
        # Polling, not an Event: the client can be swapped several times while a
        # caller waits, and a 50 ms poll can't miss a wake-up the way a replaced Event can.
        try:
            with anyio.fail_after(self.connect_timeout):
                while self._client is None:
                    await anyio.sleep(0.05)
        except TimeoutError:
            raise ConnectionLost(f"{self.name}: not connected after {self.connect_timeout}s") from None
        return self._client

    def restart(self) -> None:
        """Drop the current connection; the supervisor opens a fresh one."""
        if self._client is not None:
            self._mark_broken(self._client)

    def _mark_broken(self, client: Client) -> None:
        # Only break the connection that actually failed, not a fresh replacement.
        # Unpublish it at once: closing it can take a while, and every caller in
        # that window would otherwise be handed the same dead client.
        if client is self._client:
            self._client = None
            self._broken.set()

    async def list_tools(self, *, meta: dict[str, Any] | None = None) -> list[types.Tool]:
        client = await self._get_client()
        tools: list[types.Tool] = []
        cursor = None
        try:
            while True:
                page = await client.list_tools(cursor=cursor, meta=meta)
                tools.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    return tools
        except MCPError as e:
            if e.code != CONNECTION_CLOSED:
                raise
            raise self._lost(client, e) from e
        except Exception as e:
            raise self._lost(client, e) from e

    async def call_tool(self, tool: str, args: dict[str, Any], *,
                        meta: dict[str, Any] | None = None) -> types.CallToolResult:
        """Raises MCPError if the server answered with an error, ConnectionLost if
        the connection died, in which case we don't know whether the tool ran."""
        client = await self._get_client()
        try:
            return await client.call_tool(tool, args, meta=meta)
        except MCPError as e:
            # The SDK reports a dead connection as MCPError(CONNECTION_CLOSED), synthesized
            # locally: no server sent it. Treating it as "the server answered" would record
            # a write that may have happened as a known failure, and the agent would redo it.
            # A server could choose -32000 for its own errors too; we still treat it as lost.
            # That errs toward "unknown" (a human checks), never toward a duplicate.
            if e.code != CONNECTION_CLOSED:
                raise
            raise self._lost(client, e) from e
        except Exception as e:
            raise self._lost(client, e) from e

    def _lost(self, client: Client, error: Exception) -> ConnectionLost:
        self._mark_broken(client)
        return ConnectionLost(f"{self.name}: {_describe(error)}")


def _describe(error: BaseException) -> str:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}"

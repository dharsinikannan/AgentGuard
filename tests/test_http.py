"""Milestone 4: Streamable HTTP and surviving dropped connections.

Over HTTP (protocol 2026-07-28, stateless) a client that disconnects cancels its
request on the server. These tests recreate each failure found while building it,
with a real HTTP gateway (uvicorn) and, for restarts, a real server process.
"""

import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import mcp_types as types
import pytest
import uvicorn
from mcp import Client, MCPError
from mcp.server.mcpserver import MCPServer
from mcp_types import CONNECTION_CLOSED, INTERNAL_ERROR

from agent.mcp_bridge import call_mcp_tool
from agentguard.connection import ConnectionLost, SupervisedClient
from agentguard.gateway import Downstream, Gateway
from agentguard.guard import Guard
from agentguard.policy import Policy
from servers import cloud_server

ALLOW_ALL = Policy.from_dict({"name": "t", "allow": ["*"]})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def http_gateway(gateway: Gateway):
    port = free_port()
    app = gateway.server.streamable_http_app(stateless_http=True)
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="critical"))
    async with gateway.running(), anyio.create_task_group() as tg:
        tg.start_soon(server.serve)
        while not server.started:
            await anyio.sleep(0.02)
        try:
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            server.should_exit = True


def slow_cloud(delay: float):
    """A downstream whose create_instance takes `delay` seconds and counts executions."""
    mcp = MCPServer("cloud")
    launched: list[str] = []

    @mcp.tool()
    async def create_instance(name: str) -> str:
        await anyio.sleep(delay)
        launched.append(name)
        return f"launched {name}"

    @mcp.tool()
    async def hang() -> str:
        await anyio.sleep(3600)
        return "never"

    return mcp, launched


META = {"agentguard/task_id": "t1", "agentguard/idempotency_key": "k1"}


async def test_gateway_over_http_end_to_end(guard):
    cloud_server.reset_state()
    gateway = Gateway(guard, [Downstream("cloud", cloud_server.mcp)])
    async with http_gateway(gateway) as url, Client(url) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        r = await client.call_tool("cloud__get_logs", {"service": "api"}, meta={"agentguard/task_id": "t1"})
        assert client.protocol_version == "2026-07-28"   # stateless: no session, no handshake
    assert "cloud__delete_iam_role" not in names
    assert "pool exhausted" in r.content[0].text


async def test_dropped_client_does_not_abort_the_write(redis):
    # Regression: the disconnect cancelled the gateway handler mid-call. The
    # downstream call died, the key stayed "in_progress" for 24 h, every retry was refused.
    cloud, launched = slow_cloud(delay=1.0)
    gateway = Gateway(Guard(redis, ALLOW_ALL), [Downstream("cloud", cloud)])
    async with http_gateway(gateway) as url:
        async with Client(url) as client:
            with anyio.move_on_after(0.3):                  # the agent's connection dies
                await client.call_tool("cloud__create_instance", {"name": "vm1"}, meta=META)
        await anyio.sleep(1.2)
        assert launched == ["vm1"]                         # finished although nobody listened
        async with Client(url) as client:                  # the agent reconnects and retries
            r = await client.call_tool("cloud__create_instance", {"name": "vm1"}, meta=META)
    assert r.meta["agentguard/verdict"] == "replay"
    assert r.content[0].text == "launched vm1"
    assert launched == ["vm1"]                             # still exactly once


async def test_retry_during_original_attaches_to_it(redis):
    cloud, launched = slow_cloud(delay=1.0)
    gateway = Gateway(Guard(redis, ALLOW_ALL), [Downstream("cloud", cloud)])
    async with http_gateway(gateway) as url:
        async with Client(url) as client:
            with anyio.move_on_after(0.2):
                await client.call_tool("cloud__create_instance", {"name": "vm1"}, meta=META)
        async with Client(url) as client:                  # retry while the original still runs
            r = await client.call_tool("cloud__create_instance", {"name": "vm1"}, meta=META)
    assert r.meta["agentguard/verdict"] == "replay"        # waited, didn't refuse "in flight"
    assert r.content[0].text == "launched vm1"
    assert launched == ["vm1"]
    events = [e["event"] for _, e in await redis.xrange("ag:audit:t1")]
    assert "attached" in events


async def test_hung_downstream_is_recorded_unknown_not_stuck(redis):
    cloud, _ = slow_cloud(delay=0)
    gateway = Gateway(Guard(redis, ALLOW_ALL), [Downstream("cloud", cloud)], execution_timeout=0.5)
    async with http_gateway(gateway) as url, Client(url) as client:
        r1 = await client.call_tool("cloud__hang", {}, meta=META)
        r2 = await client.call_tool("cloud__hang", {}, meta=META)
    assert r1.meta["agentguard/verdict"] == "outcome_unknown"
    assert r2.meta["agentguard/check"] == "idempotency" and "unknown" in r2.content[0].text


class FakeClient:
    def __init__(self, *errors):
        self.errors = list(errors)

    async def call_tool(self, *args, **kwargs):
        raise self.errors.pop(0)


async def test_lost_connection_is_never_reported_as_a_known_failure():
    # Regression: the SDK reports a dead connection as MCPError(CONNECTION_CLOSED).
    # The gateway took that as "the server answered", so a write that may have run
    # was recorded as failed, and the agent would redo it.
    sc = SupervisedClient("x", target=None)
    sc._client = FakeClient(MCPError(CONNECTION_CLOSED, "Connection closed"))
    with pytest.raises(ConnectionLost):
        await sc.call_tool("t", {})
    assert sc._client is None                              # dead client unpublished at once

    sc._client = FakeClient(MCPError(INTERNAL_ERROR, "boom"))
    with pytest.raises(MCPError):                          # a real server answer passes through
        await sc.call_tool("t", {})


async def test_supervisor_reconnects_after_server_restart():
    # Regression: after a crash the dead client was never replaced, so every later
    # call failed even once the server was back.
    port = free_port()
    script = str(Path(__file__).with_name("_http_echo_server.py"))

    async def start():
        proc = subprocess.Popen([sys.executable, script, str(port)])
        await anyio.sleep(2.5)
        return proc

    proc = await start()
    sc = SupervisedClient("echo", f"http://127.0.0.1:{port}/mcp", connect_timeout=10)
    try:
        async with anyio.create_task_group() as tg:
            await tg.start(sc.run)
            assert (await sc.call_tool("ping", {})).content[0].text == "pong"
            proc.kill()
            proc.wait()
            with pytest.raises(ConnectionLost):
                await sc.call_tool("ping", {})
            proc = await start()
            assert (await sc.call_tool("ping", {})).content[0].text == "pong"
            tg.cancel_scope.cancel()
    finally:
        proc.kill()


async def test_bridge_resends_with_the_same_key_after_connection_loss():
    seen = []

    class Flaky:
        async def call_tool(self, name, args, *, meta):
            seen.append(meta["agentguard/idempotency_key"])
            if len(seen) == 1:
                raise ConnectionLost("dropped")
            return types.CallToolResult(content=[types.TextContent(type="text", text="ok")],
                                        meta={"agentguard/verdict": "replay"})

    tc = {"name": "cloud__create_instance", "args": {"name": "vm1"}, "id": "call_9", "type": "tool_call"}
    msg = await call_mcp_tool(Flaky(), tc, "t1")
    assert seen == ["tc-call_9", "tc-call_9"]               # same key both times
    assert msg.status == "success" and msg.content == "ok"

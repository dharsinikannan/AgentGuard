"""End-to-end through real MCP: agent Client -> Gateway (lowlevel Server) -> cloud MCPServer.

Everything runs in-process. Each test opens the stack with `async with stack(...)`
rather than a fixture, because anyio task groups must be entered and exited
in the same task.
"""

from contextlib import asynccontextmanager

from mcp import Client

from agentguard.chaos import DropResponseDownstream
from agentguard.gateway import Downstream, Gateway
from agentguard.guard import Guard, decide
from servers import cloud_server


@asynccontextmanager
async def stack(guard: Guard, downstream: Downstream | None = None):
    cloud_server.reset_state()
    gateway = Gateway(guard, [downstream or Downstream("cloud", cloud_server.mcp)])
    async with gateway.running():
        async with Client(gateway.server) as client:
            yield client


def meta(task="t1", key=None):
    m = {"agentguard/task_id": task}
    if key:
        m["agentguard/idempotency_key"] = key
    return m


def text(result) -> str:
    return result.content[0].text


async def test_list_tools_is_namespaced_and_filtered(guard):
    async with stack(guard) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert "cloud__get_logs" in names
    assert "cloud__delete_bucket" in names        # visible, but needs approval
    assert "cloud__delete_iam_role" not in names  # denied -> invisible


async def test_allowed_call_is_proxied(guard):
    async with stack(guard) as client:
        r = await client.call_tool("cloud__get_logs", {"service": "api"}, meta=meta())
    assert not r.is_error
    assert "pool exhausted" in text(r)
    assert r.meta["agentguard/verdict"] == "allow"


async def test_missing_task_id_is_refused(guard):
    async with stack(guard) as client:
        r = await client.call_tool("cloud__get_logs", {"service": "api"})
    assert r.is_error and "task_id" in text(r)


async def test_denied_tool_never_reaches_server(guard):
    async with stack(guard) as client:
        r = await client.call_tool("cloud__delete_iam_role", {"role": "admin"}, meta=meta())
    assert r.is_error and r.meta["agentguard/check"] == "policy"


async def test_loop_blocked_through_mcp(guard):
    async with stack(guard) as client:
        results = [await client.call_tool("cloud__get_logs", {"service": "api"}, meta=meta())
                   for _ in range(4)]
    assert [r.is_error for r in results] == [False, False, False, True]
    assert "loop detected" in text(results[-1])


async def test_approval_end_to_end(guard, redis):
    async with stack(guard) as client:
        args = {"bucket": "old-logs-2023"}
        r1 = await client.call_tool("cloud__delete_bucket", args, meta=meta())
        assert r1.meta["agentguard/verdict"] == "pending_approval"
        assert "old-logs-2023" in cloud_server.STATE["buckets"]

        await decide(redis, r1.meta["agentguard/approval_id"], approve=True)
        r2 = await client.call_tool("cloud__delete_bucket", args, meta=meta())
    assert not r2.is_error
    assert "old-logs-2023" not in cloud_server.STATE["buckets"]


async def test_duplicate_create_replays_instead_of_launching_twice(guard):
    async with stack(guard) as client:
        args = {"name": "api-2"}
        r1 = await client.call_tool("cloud__create_instance", args, meta=meta(key="launch-api-2"))
        r2 = await client.call_tool("cloud__create_instance", args, meta=meta(key="launch-api-2"))
    assert text(r1) == text(r2)
    assert r2.meta["agentguard/verdict"] == "replay"
    assert len(cloud_server.STATE["instances"]) == 2  # i-1000 + exactly one new


async def test_dropped_write_is_not_retried_and_marked_unknown(guard):
    chaos = DropResponseDownstream("cloud", cloud_server.mcp)
    async with stack(guard, chaos) as client:
        args = {"name": "api-2"}
        r1 = await client.call_tool("cloud__create_instance", args, meta=meta(key="k1"))
        r2 = await client.call_tool("cloud__create_instance", args, meta=meta(key="k1"))
    assert r1.meta["agentguard/verdict"] == "outcome_unknown"
    assert chaos.calls == 1                      # no automatic retry of a write
    assert r2.meta["agentguard/check"] == "idempotency"
    assert "unknown" in text(r2)
    assert len(cloud_server.STATE["instances"]) == 2  # launched once, never twice


async def test_dropped_read_is_retried(guard):
    chaos = DropResponseDownstream("cloud", cloud_server.mcp)
    async with stack(guard, chaos) as client:
        r = await client.call_tool("cloud__list_instances", {}, meta=meta())
    assert not r.is_error
    assert chaos.calls == 2

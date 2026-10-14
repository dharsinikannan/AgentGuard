"""The LangGraph agent end to end, with a scripted model instead of an LLM.

Real graph, real MCP, real gateway; only the model's decisions are scripted,
so these tests are deterministic and need no API key.
"""

from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from mcp import Client

from agent.graph import build_agent
from agent.mcp_bridge import call_mcp_tool, idempotency_key_for, tool_specs
from agentguard.gateway import Downstream, Gateway
from servers import cloud_server


class ScriptedModel:
    """Stands in for a chat model. `script(turn, messages)` returns the next AIMessage."""

    def __init__(self, script):
        self.script = script
        self.turn = 0
        self.bound_specs = None

    def bind_tools(self, specs):
        self.bound_specs = specs
        return self

    async def ainvoke(self, messages):
        self.turn += 1
        return self.script(self.turn, messages)


def call(name, args, id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id}])


@asynccontextmanager
async def agent_stack(guard):
    cloud_server.reset_state()
    gateway = Gateway(guard, [Downstream("cloud", cloud_server.mcp)])
    async with gateway.running(), Client(gateway.server) as client:
        yield client


async def run(client, model, task_id="t1", max_steps=25):
    graph = build_agent(model, client, await tool_specs(client))
    config = {"configurable": {"task_id": task_id}, "recursion_limit": max_steps}
    state = await graph.ainvoke({"messages": [HumanMessage("api latency high")]}, config)
    return state["messages"]


def tool_messages(messages):
    return [m for m in messages if isinstance(m, ToolMessage)]


def test_idempotency_key_is_safe_for_redis_keys():
    assert idempotency_key_for("call_abc-123") == "tc-call_abc-123"
    assert idempotency_key_for("weird id/with spaces") == "tc-weird_id_with_spaces"
    assert len(idempotency_key_for("x" * 500)) <= 200


async def test_model_only_sees_policy_visible_tools(guard):
    model = ScriptedModel(lambda turn, msgs: AIMessage("nothing to do"))
    async with agent_stack(guard) as client:
        await run(client, model)
    names = {s["function"]["name"] for s in model.bound_specs}
    assert "cloud__scale_service" in names
    assert "cloud__delete_iam_role" not in names


async def test_investigate_then_fix(guard, redis):
    def script(turn, msgs):
        return {
            1: call("cloud__get_logs", {"service": "api"}, "call_1"),
            2: call("cloud__scale_service", {"service": "api", "replicas": 6}, "call_2"),
        }.get(turn, AIMessage("Cause: pool exhaustion. Scaled api 2 -> 6."))

    async with agent_stack(guard) as client:
        messages = await run(client, ScriptedModel(script), task_id="inc-1")

    results = tool_messages(messages)
    assert [m.status for m in results] == ["success", "success"]
    assert "pool exhausted" in results[0].content
    assert cloud_server.STATE["services"]["api"]["replicas"] == 6
    assert messages[-1].content.startswith("Cause")

    # task id and per-tool-call idempotency key reached the gateway
    decisions = [e for _, e in await redis.xrange("ag:audit:inc-1") if e["event"] == "decision"]
    assert [d["idempotency_key"] for d in decisions] == ["tc-call_1", "tc-call_2"]


async def test_blocked_call_reaches_model_as_error(guard):
    seen = {}

    def script(turn, msgs):
        if turn == 1:
            return call("cloud__delete_bucket", {"bucket": "old-logs-2023"}, "call_1")
        seen["last"] = msgs[-1]  # what the model observes after the block
        return AIMessage("Deletion is waiting for human approval.")

    async with agent_stack(guard) as client:
        await run(client, ScriptedModel(script))

    observed = seen["last"]
    assert isinstance(observed, ToolMessage) and observed.status == "error"
    assert observed.content.startswith("[AgentGuard]")
    assert observed.artifact["agentguard"]["agentguard/verdict"] == "pending_approval"
    assert "old-logs-2023" in cloud_server.STATE["buckets"]


async def test_reexecuting_the_same_tool_call_replays(guard):
    # Simulates a crash after the tool ran but before the graph saved its state:
    # on resume the same tool call (same id) executes again.
    tc = {"name": "cloud__create_instance", "args": {"name": "api-2"}, "id": "call_7", "type": "tool_call"}
    async with agent_stack(guard) as client:
        first = await call_mcp_tool(client, tc, "t1")
        second = await call_mcp_tool(client, tc, "t1")
    assert second.artifact["agentguard"]["agentguard/verdict"] == "replay"
    assert first.content == second.content
    assert len(cloud_server.STATE["instances"]) == 2  # i-1000 + one launch


async def test_looping_model_is_blocked_by_gateway(guard):
    # A stuck model: same call every turn, each with a fresh tool_call id.
    model = ScriptedModel(lambda turn, msgs: call("cloud__get_logs", {"service": "api"}, f"call_{turn}"))
    async with agent_stack(guard) as client:
        graph = build_agent(model, client, await tool_specs(client))
        config = {"configurable": {"task_id": "t1"}, "recursion_limit": 12}
        statuses = []
        with pytest.raises(GraphRecursionError):
            async for update in graph.astream({"messages": [HumanMessage("go")]}, config, stream_mode="updates"):
                for m in update.get("tools", {}).get("messages", []):
                    statuses.append((m.status, "loop detected" in m.content))

    assert statuses[:3] == [("success", False)] * 3
    assert statuses[3:] and all(s == ("error", True) for s in statuses[3:])

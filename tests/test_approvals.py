"""Milestone 5: the agent pauses for human approval and resumes, even in a new process.

Graph tests use the real graph + MCP + gateway with a scripted model and an
in-memory checkpointer. The last tests use the real Redis checkpointer and the
Streamlit UI, and skip themselves when Redis 8 isn't running.
"""

import asyncio
import os
import urllib.parse

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent.graph import build_agent
from agent.mcp_bridge import tool_specs
from agentguard.guard import CallContext, Guard, approval_status, decide, list_pending
from servers import cloud_server
from test_agent import ScriptedModel, agent_stack, call

DELETE = ("cloud__delete_bucket", {"bucket": "old-logs-2023"})


def config(task="t1"):
    return {"configurable": {"thread_id": task, "task_id": task}, "recursion_limit": 25}


def script_delete_then_finish(turn, msgs):
    if turn == 1:
        return call(*DELETE, "call_del")
    return AIMessage("done")


async def start(client, model, saver, task="t1"):
    graph = build_agent(model, client, await tool_specs(client), checkpointer=saver)
    await graph.ainvoke({"messages": [HumanMessage("clean up")]}, config(task))
    return graph


def tool_results(state):
    return [m for m in state.values["messages"] if isinstance(m, ToolMessage)]


# ------------------------------------------------------------------- guard

async def test_approval_status_lifecycle(guard, redis):
    ctx = CallContext("t1", *DELETE)
    aid = (await guard.check(ctx)).approval_id
    assert await approval_status(redis, aid) == "pending"
    await decide(redis, aid, approve=True)
    assert await approval_status(redis, aid) == "approved"
    assert (await guard.check(ctx)).verdict.value == "allow"  # uses the grant up
    assert await approval_status(redis, aid) == "unknown"     # nothing pending, nothing granted
    await guard.check(ctx)                                    # doing it again...
    assert await approval_status(redis, aid) == "pending"     # ...needs a new decision


async def test_simultaneous_approve_and_reject_only_one_wins(guard, redis):
    # Regression: decide() read, then wrote. Two people clicking at once could
    # leave both a grant and a rejection, and the guard checks the grant first.
    aid = (await guard.check(CallContext("t1", *DELETE))).approval_id
    results = await asyncio.gather(decide(redis, aid, True), decide(redis, aid, False),
                                   return_exceptions=True)
    assert sum(isinstance(r, KeyError) for r in results) == 1
    granted = await redis.exists(f"ag:approval:grant:{aid}")
    rejected = await redis.exists(f"ag:approval:rejected:{aid}")
    assert granted + rejected == 1


# ------------------------------------------------------------------- graph

async def test_run_pauses_until_approved_then_resumes(guard, redis):
    saver = InMemorySaver()
    async with agent_stack(guard) as client:
        graph = await start(client, ScriptedModel(script_delete_then_finish), saver)
        state = await graph.aget_state(config())
        assert state.next == ("tools",)                          # paused inside the tools node
        request = state.interrupts[0].value
        assert request["tool"] == "cloud__delete_bucket" and request["args"] == DELETE[1]
        assert "old-logs-2023" in cloud_server.STATE["buckets"]  # nothing ran yet

        await decide(redis, request["approval_id"], approve=True)
        final = await graph.ainvoke(Command(resume={"decision": "approved"}), config())

    assert "old-logs-2023" not in cloud_server.STATE["buckets"]
    assert final["messages"][-1].content == "done"
    assert [m.status for m in final["messages"] if isinstance(m, ToolMessage)] == ["success"]


async def test_rejected_action_reaches_the_model_as_an_error(guard, redis):
    saver = InMemorySaver()
    async with agent_stack(guard) as client:
        graph = await start(client, ScriptedModel(script_delete_then_finish), saver)
        aid = (await graph.aget_state(config())).interrupts[0].value["approval_id"]
        await decide(redis, aid, approve=False)
        await graph.ainvoke(Command(resume={"decision": "rejected"}), config())
        (result,) = tool_results(await graph.aget_state(config()))
    assert result.status == "error" and "rejected" in result.content
    assert "old-logs-2023" in cloud_server.STATE["buckets"]


async def test_earlier_call_in_the_same_turn_is_replayed_not_rerun(guard, redis):
    # On resume LangGraph re-runs the paused node from the top. The launch that ran
    # before the pause must come back as a replay, not a second instance.
    def script(turn, msgs):
        if turn == 1:
            return AIMessage(content="", tool_calls=[
                {"name": "cloud__create_instance", "args": {"name": "api-2"}, "id": "call_launch"},
                {"name": DELETE[0], "args": DELETE[1], "id": "call_del"},
            ])
        return AIMessage("done")

    saver = InMemorySaver()
    async with agent_stack(guard) as client:
        graph = await start(client, ScriptedModel(script), saver)
        assert len(cloud_server.STATE["instances"]) == 2         # i-1000 + api-2, before the pause
        aid = (await graph.aget_state(config())).interrupts[0].value["approval_id"]
        await decide(redis, aid, approve=True)
        await graph.ainvoke(Command(resume={"decision": "approved"}), config())
        launch, delete = tool_results(await graph.aget_state(config()))

    assert len(cloud_server.STATE["instances"]) == 2             # still one launch
    assert launch.artifact["agentguard"]["agentguard/verdict"] == "replay"
    assert delete.status == "success"


async def test_resume_from_a_fresh_graph_like_a_new_process(guard, redis):
    saver = InMemorySaver()
    async with agent_stack(guard) as client:
        await start(client, ScriptedModel(script_delete_then_finish), saver)
    aid = (await list_pending(redis)).popitem()[0]
    await decide(redis, aid, approve=True)

    # New model object, new MCP connection, new graph: only the checkpoint is shared.
    async with agent_stack(guard, reset=False) as client:
        model = ScriptedModel(lambda turn, msgs: AIMessage("done"))
        graph = build_agent(model, client, await tool_specs(client), checkpointer=saver)
        final = await graph.ainvoke(Command(resume={"decision": "approved"}), config())
    assert "old-logs-2023" not in cloud_server.STATE["buckets"]
    assert final["messages"][-1].content == "done"


# ------------------------------------------------------- live: Redis 8 + UI

LIVE_REDIS = "redis://localhost:6379/3"   # separate db for the UI test; flushed by it
CHECKPOINT_REDIS = "redis://localhost:6379/0"  # RediSearch indexes only work in db 0


def _redis8_with_search() -> bool:
    try:
        import redis
        r = redis.Redis.from_url(LIVE_REDIS, socket_connect_timeout=1)
        r.execute_command("FT._LIST")
        return True
    except Exception:
        return False


live = pytest.mark.skipif(not _redis8_with_search(), reason="needs Redis 8 (docker compose up -d)")


@pytest.mark.live
@live
async def test_redis_checkpointer_survives_a_new_saver(guard, redis):
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    task = f"live-{os.getpid()}"
    async with AsyncRedisSaver.from_conn_string(CHECKPOINT_REDIS) as saver:
        await saver.asetup()
        async with agent_stack(guard) as client:
            await start(client, ScriptedModel(script_delete_then_finish), saver, task=task)
    aid = (await list_pending(redis)).popitem()[0]
    await decide(redis, aid, approve=True)

    async with AsyncRedisSaver.from_conn_string(CHECKPOINT_REDIS) as saver:   # "new process"
        async with agent_stack(guard, reset=False) as client:
            graph = build_agent(ScriptedModel(lambda t, m: AIMessage("done")), client,
                                await tool_specs(client), checkpointer=saver)
            final = await graph.ainvoke(Command(resume={"decision": "approved"}), config(task))
        await saver.adelete_thread(task)
    assert final["messages"][-1].content == "done"
    assert "old-logs-2023" not in cloud_server.STATE["buckets"]


@pytest.mark.live
@live
def test_ui_lists_pending_and_approves(monkeypatch):
    import redis as redis_sync
    from streamlit.testing.v1 import AppTest

    from agentguard.policy import Policy
    from redis.asyncio import Redis

    async def make_pending():
        r = Redis.from_url(LIVE_REDIS, decode_responses=True)
        await r.flushdb()
        policy = Policy.from_dict({"name": "t", "require_approval": ["cloud__delete_*"]})
        aid = (await Guard(r, policy).check(CallContext("ui-task", *DELETE))).approval_id
        await r.aclose()
        return aid

    aid = asyncio.run(make_pending())
    monkeypatch.setenv("REDIS_URL", LIVE_REDIS)
    app = AppTest.from_file(os.path.join(os.path.dirname(__file__), "..", "ui", "approvals.py"))
    app.run(timeout=15)
    assert not app.exception
    assert any(aid in m.value for m in app.markdown)
    app.button(key=f"approve-{aid}").click().run(timeout=15)

    r = redis_sync.Redis.from_url(LIVE_REDIS, decode_responses=True)
    try:
        assert r.get(f"ag:approval:grant:{aid}") == f"ui:{app.sidebar.text_input[0].value}"
        assert not r.hexists("ag:approvals:pending", aid)
    finally:
        r.flushdb()

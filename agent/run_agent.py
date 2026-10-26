"""Run the on-call agent against an alert, with every tool call going through AgentGuard.

    python -m agent.run_agent                                    # in-process gateway, fakeredis
    python -m agent.run_agent --redis-url redis://localhost:6379 # gateway subprocess (stdio) + Redis
    python -m agent.run_agent --gateway-url http://127.0.0.1:8765/mcp
                                       # a running HTTP gateway: python -m agentguard --transport http

Keys come from .env (GROQ_API_KEY). In every mode the connection to AgentGuard is a
SupervisedClient: if it drops, it reconnects, and the interrupted tool call is
resent with the same idempotency key.

Human approval: the run pauses (LangGraph interrupt) and is checkpointed, in Redis
when the gateway uses Redis, otherwise in memory. In-process mode asks you in this
terminal; otherwise approve in the UI (streamlit run ui/approvals.py) or with
scripts/approve.py, and the run resumes by itself. With --on-approval exit the
process stops instead; continue later, even after a reboot, with --resume.

Options: --alert "...", --policy path, --model name, --max-steps N, --task-id id
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.parse
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
from langgraph.types import Command
from redis.asyncio import Redis
from mcp import StdioServerParameters

from agent.graph import build_agent
from agent.mcp_bridge import tool_specs
from agentguard.connection import SupervisedClient
from agentguard.env import child_env

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALERT = "ALERT [P2] api: p99 latency 2400ms for 10 minutes (threshold 500ms). Investigate and mitigate."
# Groq retires models often; list yours with: python -c "from groq import Groq; print([m.id for m in Groq().models.list().data])"
DEFAULT_MODEL = "openai/gpt-oss-120b"
# Longer than the gateway's execution_timeout (120 s) + attach_timeout (15 s): the gateway
# must be the one to give up and record "unknown", not the agent hanging up first.
AGENT_CALL_TIMEOUT = 180


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="agent.run_agent")
    p.add_argument("--alert", default=DEFAULT_ALERT)
    p.add_argument("--policy", default=str(ROOT / "skills/policies/prod-safe.yaml"))
    p.add_argument("--model", default=os.environ.get("GROQ_MODEL", DEFAULT_MODEL))
    p.add_argument("--max-steps", type=int, default=30, help="LangGraph recursion_limit")
    p.add_argument("--task-id", default=f"incident-{int(time.time())}")
    p.add_argument("--redis-url", help="spawn the real gateway (stdio) with this Redis, instead of in-process")
    p.add_argument("--gateway-url", help="connect to a running HTTP gateway, e.g. http://127.0.0.1:8765/mcp")
    p.add_argument("--cloud", choices=["mock", "aws"], default="mock",
                   help="aws: boto3 servers on LocalStack, seeded, with simulated traffic")
    p.add_argument("--no-seed", action="store_true", help="aws: keep current LocalStack state")
    p.add_argument("--resume", action="store_true", help="continue the paused/stopped run of --task-id")
    p.add_argument("--on-approval", choices=["wait", "exit"], default="wait",
                   help="when the run pauses for approval: wait and resume by itself, or exit")
    p.add_argument("--checkpoint-url", help="Redis for agent checkpoints (default: the gateway's Redis; "
                                            "in-process mode keeps them in memory)")
    return p.parse_args()


async def supervised(stack: AsyncExitStack, target) -> SupervisedClient:
    client = SupervisedClient("agentguard", target, call_timeout=AGENT_CALL_TIMEOUT, connect_timeout=60)
    tg = await stack.enter_async_context(anyio.create_task_group())
    stack.callback(tg.cancel_scope.cancel)  # runs before the task group exits (LIFO)
    await tg.start(client.run)
    return client


async def open_gateway(stack: AsyncExitStack, args: argparse.Namespace):
    """Returns (client connected to AgentGuard, the gateway's Redis, in_process).

    The Redis handle is for the human side only (waiting on approvals, the audit
    report); the agent itself reaches tools through MCP alone."""
    if args.gateway_url:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        return await supervised(stack, args.gateway_url), await open_redis(stack, redis_url), False
    if args.redis_url:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agentguard", "--policy", args.policy, "--redis-url", args.redis_url,
                  "--cloud", args.cloud],
            cwd=str(ROOT),
            env=child_env(),
        )
        return await supervised(stack, params), await open_redis(stack, args.redis_url), False

    # In-process: same gateway code, fakeredis instead of a Redis server.
    from fakeredis import FakeAsyncRedis

    from agentguard.gateway import Downstream, Gateway
    from agentguard.guard import Guard
    from agentguard.policy import Policy

    if args.cloud == "aws":
        from servers import aws_cloud_server, observability_server
        downstreams = [Downstream("cloud", aws_cloud_server.mcp), Downstream("obs", observability_server.mcp)]
    else:
        from servers import cloud_server
        downstreams = [Downstream("cloud", cloud_server.mcp)]

    redis = FakeAsyncRedis(decode_responses=True)
    gateway = Gateway(Guard(redis, Policy.from_yaml(args.policy)), downstreams)
    await stack.enter_async_context(gateway.running())
    return await supervised(stack, gateway.server), redis, True


async def open_redis(stack: AsyncExitStack, url: str) -> Redis:
    redis = Redis.from_url(url, decode_responses=True)
    stack.push_async_callback(redis.aclose)
    return redis


async def open_checkpointer(stack: AsyncExitStack, args: argparse.Namespace, in_process: bool):
    url = args.checkpoint_url or (None if in_process else args.redis_url or os.environ.get(
        "REDIS_URL", "redis://localhost:6379/0"))
    if url is None:
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver(), "memory"
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver
    if (db := urllib.parse.urlparse(url).path.strip("/")) not in ("", "0"):
        # The checkpointer indexes with RediSearch, which only works in database 0.
        sys.exit(f"checkpoints need Redis database 0 (RediSearch limit), got /{db}; pass --checkpoint-url")
    # Same 24 h lifetime as the guard's per-task state; refreshed while the task is in use.
    saver = await stack.enter_async_context(
        AsyncRedisSaver.from_conn_string(url, ttl={"default_ttl": 24 * 60, "refresh_on_read": True}))
    await saver.asetup()
    return saver, url


async def wait_for_decision(request: dict, redis: Redis, in_process: bool, args: argparse.Namespace):
    """Block until a human decides. Returns the resume value, or None to stop the process."""
    from agentguard.guard import approval_status, decide

    aid = request["approval_id"]
    print(f"\n\033[1;33m⏸  paused: {request['tool']}({json.dumps(request['args'])}) needs human approval\033[0m")
    print(f"   approval id: {aid}   task: {request['task_id']}")
    if in_process:
        # Only reachable with fakeredis in this process, so the human answers here.
        # Still a human channel: the model has no way to reach this prompt.
        answer = await anyio.to_thread.run_sync(input, "   approve? [y/N] ")
        approved = answer.strip().lower() in ("y", "yes")
        await decide(redis, aid, approved, who=f"terminal:{getpass.getuser()}")
        return {"decision": "approved" if approved else "rejected"}
    if args.on_approval == "exit":
        print("   checkpointed. Decide in the UI (streamlit run ui/approvals.py) or")
        print(f"   python scripts/approve.py approve {aid}, then continue with:")
        print(f"   python -m agent.run_agent --resume --task-id {args.task_id} " + _mode_flags(args))
        return None
    print(f"   waiting... approve in the UI (streamlit run ui/approvals.py) or: python scripts/approve.py approve {aid}")
    while (status := await approval_status(redis, aid)) == "pending":
        await anyio.sleep(2)
    print(f"   human decision: {status}")
    return {"decision": status}


def _mode_flags(args: argparse.Namespace) -> str:
    if args.gateway_url:
        return f"--gateway-url {args.gateway_url}"
    return f"--redis-url {args.redis_url} --cloud {args.cloud}"


async def start_world(stack: AsyncExitStack, args: argparse.Namespace, interval: float = 3) -> None:
    """aws mode: reset LocalStack to the incident start, then emit traffic in the background."""
    from servers import world

    if not args.no_seed:
        print("seeding LocalStack ...")
        await anyio.to_thread.run_sync(world.seed)

    async def traffic() -> None:
        while True:
            await anyio.to_thread.run_sync(world.tick)
            await anyio.sleep(interval)

    tg = await stack.enter_async_context(anyio.create_task_group())
    stack.callback(tg.cancel_scope.cancel)  # runs before the task group exits (LIFO)
    tg.start_soon(traffic)


def show(message) -> None:
    if isinstance(message, AIMessage):
        if message.content:
            print(f"\n\033[1magent:\033[0m {message.content}")
        for tc in message.tool_calls:
            print(f"  \033[36m→ {tc['name']}({json.dumps(tc['args'])})\033[0m")
    elif isinstance(message, ToolMessage):
        verdict = (message.artifact or {}).get("agentguard", {}).get("agentguard/verdict", "?")
        colour = "\033[32m" if verdict in ("allow", "replay") else "\033[33m"
        body = " ".join(str(message.content).split())
        print(f"  {colour}← [{verdict}]\033[0m {body[:200]}")


async def main() -> None:
    # utf-8-sig: Windows Notepad may save .env with a BOM. Real env vars win (override=False).
    load_dotenv(ROOT / ".env", encoding="utf-8-sig")
    args = parse_args()
    if not os.environ.get("GROQ_API_KEY"):
        sys.exit("Set GROQ_API_KEY first (free key: https://console.groq.com/keys)")
    from groq import RateLimitError
    from langchain_groq import ChatGroq

    print(f"task={args.task_id} model={args.model} policy={Path(args.policy).name} cloud={args.cloud}")
    if not args.resume:
        print(args.alert)
    async with AsyncExitStack() as stack:
        if args.cloud == "aws":
            await start_world(stack, args)
        client, redis, in_process = await open_gateway(stack, args)
        checkpointer, where = await open_checkpointer(stack, args, in_process)
        specs = await tool_specs(client)
        print("tools:", ", ".join(s["function"]["name"] for s in specs))
        print(f"checkpoints: {where} (thread {args.task_id})")

        # Free tier: 8k tokens/min. The SDK backs off and honours retry-after; 2 retries (default) isn't enough.
        graph = build_agent(ChatGroq(model=args.model, temperature=0, max_retries=8), client, specs,
                            checkpointer=checkpointer)
        # thread_id: the checkpoint thread. One task = one thread, so --resume finds it.
        config = {"configurable": {"thread_id": args.task_id, "task_id": args.task_id},
                  "recursion_limit": args.max_steps}
        state = await graph.aget_state(config)
        if args.resume and not state.values:
            sys.exit(f"no saved run for task {args.task_id} in {where}")
        if state.values and not args.resume:
            sys.exit(f"task {args.task_id} already has a saved run; add --resume, or use a new --task-id")
        run_input = {"messages": [HumanMessage(args.alert)]}
        if args.resume:
            run_input = Command(resume={"decision": "resumed"}) if state.interrupts else None
            print(state.values["messages"][0].content)  # the task's own alert, from the checkpoint
            print(f"resuming task {args.task_id}" + (" (was paused for approval)" if state.interrupts else ""))
        try:
            while True:
                request = None
                async for update in graph.astream(run_input, config, stream_mode="updates"):
                    if "__interrupt__" in update:
                        request = update["__interrupt__"][0].value
                        continue
                    for node_output in update.values():
                        for message in (node_output or {}).get("messages", []):
                            show(message)
                if request is None:
                    break
                decision = await wait_for_decision(request, redis, in_process, args)
                if decision is None:
                    return
                run_input = Command(resume=decision)
        except GraphRecursionError:
            print(f"\n\033[33mstopped: agent used all {args.max_steps} graph steps\033[0m")
        except RateLimitError as e:
            print(f"\n\033[33mstopped: Groq rate limit still hit after retries ({e.message[:120]}). "
                  "Wait a minute, or try --model openai/gpt-oss-20b.\033[0m")

        from agentguard.guard import audit_lines
        print(f"\n== audit trail ag:audit:{args.task_id}")
        for line in await audit_lines(redis, args.task_id):
            print("  " + line[:160])


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")
        sys.stdout.reconfigure(encoding="utf-8")
    anyio.run(main)

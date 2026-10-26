"""Run the on-call agent against an alert, with every tool call going through AgentGuard.

    python -m agent.run_agent                                    # in-process gateway, fakeredis
    python -m agent.run_agent --redis-url redis://localhost:6379 # gateway subprocess (stdio) + Redis
    python -m agent.run_agent --gateway-url http://127.0.0.1:8765/mcp
                                       # a running HTTP gateway: python -m agentguard --transport http

Keys come from .env (GROQ_API_KEY). In every mode the connection to AgentGuard is a
SupervisedClient: if it drops, it reconnects, and the interrupted tool call is
resent with the same idempotency key.

Options: --alert "...", --policy path, --model name, --max-steps N, --task-id id
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

import anyio
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError
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
    return p.parse_args()


async def supervised(stack: AsyncExitStack, target) -> SupervisedClient:
    client = SupervisedClient("agentguard", target, call_timeout=AGENT_CALL_TIMEOUT, connect_timeout=60)
    tg = await stack.enter_async_context(anyio.create_task_group())
    stack.callback(tg.cancel_scope.cancel)  # runs before the task group exits (LIFO)
    await tg.start(client.run)
    return client


async def open_gateway(stack: AsyncExitStack, args: argparse.Namespace):
    """Returns (client connected to AgentGuard, redis-or-None for the final report)."""
    if args.gateway_url:
        return await supervised(stack, args.gateway_url), None
    if args.redis_url:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "agentguard", "--policy", args.policy, "--redis-url", args.redis_url,
                  "--cloud", args.cloud],
            cwd=str(ROOT),
            env=child_env(),
        )
        return await supervised(stack, params), None

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
    return await supervised(stack, gateway.server), redis


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
    print(args.alert)
    async with AsyncExitStack() as stack:
        if args.cloud == "aws":
            await start_world(stack, args)
        client, redis = await open_gateway(stack, args)
        specs = await tool_specs(client)
        print("tools:", ", ".join(s["function"]["name"] for s in specs))

        # Free tier: 8k tokens/min. The SDK backs off and honours retry-after; 2 retries (default) isn't enough.
        graph = build_agent(ChatGroq(model=args.model, temperature=0, max_retries=8), client, specs)
        config = {"configurable": {"task_id": args.task_id}, "recursion_limit": args.max_steps}
        try:
            async for update in graph.astream({"messages": [HumanMessage(args.alert)]}, config,
                                              stream_mode="updates"):
                for node_output in update.values():
                    for message in node_output["messages"]:
                        show(message)
        except GraphRecursionError:
            print(f"\n\033[33mstopped: agent used all {args.max_steps} graph steps\033[0m")
        except RateLimitError as e:
            print(f"\n\033[33mstopped: Groq rate limit still hit after retries ({e.message[:120]}). "
                  "Wait a minute, or try --model openai/gpt-oss-20b.\033[0m")

        if redis is not None:
            from agentguard.guard import audit_lines
            print(f"\n== audit trail ag:audit:{args.task_id}")
            for line in await audit_lines(redis, args.task_id):
                print("  " + line[:160])
        else:
            print(f"\naudit trail: python scripts/audit.py {args.task_id}")


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")
        sys.stdout.reconfigure(encoding="utf-8")
    anyio.run(main)

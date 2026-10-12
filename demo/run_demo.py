"""Scripted on-call incident: every AgentGuard protection, failing safely, in ~2 seconds.

    python demo/run_demo.py

Runs fully in-process (fakeredis, in-memory cloud server), so it needs no Redis.
The "agent" here is a script; milestone 2 replaces it with a LangGraph agent.
"""

from __future__ import annotations

import json
import os
import sys

import anyio
from fakeredis import FakeAsyncRedis
from mcp import Client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentguard.chaos import DropResponseDownstream  # noqa: E402
from agentguard.gateway import Gateway  # noqa: E402
from agentguard.guard import Guard, audit_lines, decide  # noqa: E402
from agentguard.policy import Policy  # noqa: E402
from servers import cloud_server  # noqa: E402

TASK = "incident-4711"
POLICY = os.path.join(os.path.dirname(__file__), "..", "skills", "policies", "prod-safe.yaml")


def step(title: str) -> None:
    print(f"\n\033[1m== {title}\033[0m")


async def call(client: Client, tool: str, args: dict, key: str | None = None):
    meta = {"agentguard/task_id": TASK}
    if key:
        meta["agentguard/idempotency_key"] = key
    r = await client.call_tool(tool, args, meta=meta)
    verdict = (r.meta or {}).get("agentguard/verdict", "?")
    colour = "\033[32m" if verdict in ("allow", "replay") else "\033[33m"
    body = r.content[0].text if r.content else json.dumps(r.structured_content)
    print(f"  {tool}({json.dumps(args)}) -> {colour}{verdict}\033[0m: {' '.join(body.split())[:150]}")
    return r


async def main() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    guard = Guard(redis, Policy.from_yaml(POLICY))
    # The first create_instance response is "lost" to show the unknown-outcome path.
    cloud = DropResponseDownstream("cloud", cloud_server.mcp, drops=0)
    gateway = Gateway(guard, [cloud])

    print("ALERT: api p99 latency 2400ms (threshold 500ms)")
    async with gateway.running(), Client(gateway.server) as agent:
        step("1. Agent lists tools (policy prod-safe)")
        names = [t.name for t in (await agent.list_tools()).tools]
        print("  visible:", ", ".join(names))
        print("  hidden : cloud__delete_iam_role (denied tools are never listed)")

        step("2. Investigate, then get stuck re-reading the same logs")
        for _ in range(4):
            await call(agent, "cloud__get_logs", {"service": "api"})

        step("3. Fix: scale the api service (₹50)")
        await call(agent, "cloud__scale_service", {"service": "api", "replicas": 6})

        step("4. Cleanup idea: delete an old bucket -> needs a human")
        r = await call(agent, "cloud__delete_bucket", {"bucket": "old-logs-2023"})
        aid = r.meta["agentguard/approval_id"]
        print(f"  (human runs: python scripts/approve.py approve {aid})")
        await decide(redis, aid, approve=True, who="oncall-human")
        await call(agent, "cloud__delete_bucket", {"bucket": "old-logs-2023"})
        print("  the approval was single-use; repeating it asks again:")
        await call(agent, "cloud__delete_bucket", {"bucket": "old-logs-2023"})

        step("5. Launch an instance; the connection drops after the server acted")
        cloud.drops = 1
        await call(agent, "cloud__create_instance", {"name": "api-3"}, key="launch-api-3")
        print("  agent reconnects and retries with the same idempotency key:")
        await call(agent, "cloud__create_instance", {"name": "api-3"}, key="launch-api-3")
        print("  so it checks reality instead:")
        await call(agent, "cloud__list_instances", {})

        step("6. Retry after a clean response -> replayed, not relaunched")
        await call(agent, "cloud__create_instance", {"name": "api-4"}, key="launch-api-4")
        await call(agent, "cloud__create_instance", {"name": "api-4"}, key="launch-api-4")

        step("7. Agent keeps launching instances -> ₹1000 budget stops it")
        for i in range(5, 8):
            await call(agent, "cloud__create_instance", {"name": f"api-{i}"}, key=f"launch-api-{i}")

    instances = [v["name"] for v in cloud_server.STATE["instances"].values()]
    step("Result")
    print(f"  instances: {instances}")
    print(f"  spent: ₹{await guard.spent_paise(TASK) / 100:,.2f} of ₹{guard.policy.budget_paise / 100:,.2f}")
    step(f"Audit trail ag:audit:{TASK}")
    for line in await audit_lines(redis, TASK):
        print("  " + line[:160])


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console
        sys.stdout.reconfigure(encoding="utf-8")
    anyio.run(main)

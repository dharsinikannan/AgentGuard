"""The "Try it" incident for the dashboard: a scripted on-call agent, through the real
gateway, writing to whatever Redis the dashboard reads (in-memory in the public demo).

It leaves real approval requests behind, so a visitor can approve or reject them on
the Approvals page and then press "Retry approved actions" to see the outcome.
"""

from __future__ import annotations

import json
import logging

from mcp import Client
from redis.asyncio import Redis

from agentguard.gateway import Downstream, Gateway
from agentguard.guard import Guard
from evals.harness import PROD_SAFE
from servers import cloud_server

INCIDENT = [
    ("investigate", "cloud__list_services", {}, None),
    ("read the logs", "cloud__get_logs", {"service": "api"}, None),
    ("…again", "cloud__get_logs", {"service": "api"}, None),
    ("…and again", "cloud__get_logs", {"service": "api"}, None),
    ("…stuck in a loop", "cloud__get_logs", {"service": "api"}, None),
    ("reasonable fix", "cloud__scale_service", {"service": "api", "replicas": 4}, None),
    ("overreaction", "cloud__scale_service", {"service": "api", "replicas": 12}, None),
    ("touches a healthy service", "cloud__scale_service", {"service": "db", "replicas": 3}, None),
    ("wants a GPU box", "cloud__create_instance", {"name": "gpu", "instance_type": "p4d.24xlarge"}, None),
    ("launches a helper", "cloud__create_instance", {"name": "api-3", "instance_type": "t3.micro"}, "tc-launch"),
    ("network blip: same request again", "cloud__create_instance", {"name": "api-3", "instance_type": "t3.micro"}, "tc-launch"),
    ("tidies up storage", "cloud__delete_bucket", {"bucket": "old-logs-2023"}, None),
    ("touches IAM", "cloud__delete_iam_role", {"role": "admin"}, None),
]
NEEDS_HUMAN = [step for step in INCIDENT if step[0] in ("overreaction", "touches a healthy service", "tidies up storage")]


async def _run(redis: Redis, task_id: str, steps) -> list[dict[str, str]]:
    logging.getLogger("agentguard").setLevel(logging.ERROR)
    gateway = Gateway(Guard(redis, PROD_SAFE), [Downstream("cloud", cloud_server.mcp)])
    rows = []
    async with gateway.running(), Client(gateway.server) as client:
        for why, tool, args, key in steps:
            meta = {"agentguard/task_id": task_id, "agentguard/user": "visitor"}
            if key:
                meta["agentguard/idempotency_key"] = key
            r = await client.call_tool(tool, args, meta=meta)
            rows.append({"agent": why, "call": f"{tool}({json.dumps(args)})",
                         "verdict": (r.meta or {}).get("agentguard/verdict", "?"),
                         "result": " ".join(r.content[0].text.split())[:140]})
    return rows


async def run_incident(redis: Redis, task_id: str) -> list[dict[str, str]]:
    cloud_server.reset_state()
    return await _run(redis, task_id, INCIDENT)


async def retry_approved(redis: Redis, task_id: str) -> list[dict[str, str]]:
    """Re-send the calls that needed a human; the gateway decides what happens now."""
    return await _run(redis, task_id, NEEDS_HUMAN)

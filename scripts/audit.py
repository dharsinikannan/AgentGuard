"""Print the audit trail of a task (or list known tasks).

    python scripts/audit.py            # list task ids
    python scripts/audit.py <task_id>  # every decision and outcome, in order
"""

from __future__ import annotations

import argparse
import os
import sys

import anyio
from redis.asyncio import Redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentguard.guard import audit_lines  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    args = parser.parse_args()

    redis = Redis.from_url(args.redis_url, decode_responses=True)
    try:
        if not args.task_id:
            for task in sorted(await redis.smembers("ag:tasks")):
                print(task)
            return
        for line in await audit_lines(redis, args.task_id):
            print(line)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    anyio.run(main)

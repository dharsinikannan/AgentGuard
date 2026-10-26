"""Human approval CLI. Runs outside the agent's reach: it talks to Redis directly
and is never exposed as an MCP tool.

    python scripts/approve.py list
    python scripts/approve.py approve <approval_id>
    python scripts/approve.py reject  <approval_id>
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime

import anyio
from redis.asyncio import Redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agentguard.guard import decide, list_pending  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["list", "approve", "reject"])
    parser.add_argument("approval_id", nargs="?")
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    args = parser.parse_args()

    redis = Redis.from_url(args.redis_url, decode_responses=True)
    try:
        if args.action == "list":
            pending = await list_pending(redis)
            if not pending:
                print("no pending approvals")
            for aid, req in pending.items():
                when = datetime.fromtimestamp(req["requested_at"]).strftime("%H:%M:%S")
                print(f"{aid}  {when}  task={req['task_id']}  {req['tool']} {json.dumps(req['args'])}")
            return

        if not args.approval_id:
            parser.error("approval_id is required")
        try:
            req = await decide(redis, args.approval_id, approve=args.action == "approve", who=getpass.getuser())
        except KeyError as e:
            sys.exit(str(e))
        print(f"{'approved' if args.action == 'approve' else 'rejected'} {req['tool']} {json.dumps(req['args'])} for task {req['task_id']}")
    finally:
        await redis.aclose()


if __name__ == "__main__":
    anyio.run(main)

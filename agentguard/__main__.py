"""Run AgentGuard as a stdio MCP server backed by real Redis.

    python -m agentguard --policy skills/policies/prod-safe.yaml

Point any MCP client (Claude Desktop, LangGraph, `mcp` CLI) at this command
instead of at the cloud server directly.

stdout is the MCP channel, so all logging goes to stderr. One stray print()
to stdout corrupts the JSON-RPC stream.
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import sys
from pathlib import Path

import anyio
from mcp import StdioServerParameters
from mcp.server.stdio import stdio_server
from redis.asyncio import Redis

from agentguard.gateway import Downstream, Gateway
from agentguard.guard import Guard
from agentguard.policy import Policy

ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agentguard")
    parser.add_argument("--policy", default=str(ROOT / "skills/policies/prod-safe.yaml"))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    parser.add_argument(
        "--server", action="append", metavar="NAME=COMMAND",
        help='downstream stdio server, e.g. cloud="python -m servers.cloud_server" (repeatable)',
    )
    return parser.parse_args()


def downstreams_from(specs: list[str] | None) -> list[Downstream]:
    if not specs:
        default = StdioServerParameters(command=sys.executable, args=["-m", "servers.cloud_server"], cwd=str(ROOT))
        return [Downstream("cloud", default)]
    result = []
    for spec in specs:
        name, _, command = spec.partition("=")
        argv = shlex.split(command, posix=os.name != "nt")
        result.append(Downstream(name, StdioServerParameters(command=argv[0], args=argv[1:], cwd=str(ROOT))))
    return result


async def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    redis = Redis.from_url(args.redis_url, decode_responses=True)
    try:
        await redis.ping()
    except Exception as e:
        sys.exit(f"agentguard: cannot reach Redis at {args.redis_url}: {e}")

    policy = Policy.from_yaml(args.policy)
    gateway = Gateway(Guard(redis, policy), downstreams_from(args.server))
    logging.info("policy=%s downstreams=%s", policy.name, list(gateway.downstreams))

    async with gateway.running(), stdio_server() as (read, write):
        await gateway.server.run(read, write, gateway.server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)

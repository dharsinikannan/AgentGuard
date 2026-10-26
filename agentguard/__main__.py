"""Run AgentGuard as an MCP server backed by real Redis.

    python -m agentguard                                  # stdio: a client launches us
    python -m agentguard --transport http --port 8765     # Streamable HTTP at /mcp

Point any MCP client (Claude Desktop, LangGraph, `mcp` CLI) at this instead of at
the cloud server directly.

HTTP is stateless (protocol 2026-07-28: one request in, one response out, no
session). Everything AgentGuard remembers lives in Redis, keyed by task id and
idempotency key, so an agent can reconnect, even to another gateway process,
without losing its place.

In stdio mode stdout is the MCP channel, so all logging goes to stderr. One stray
print() to stdout corrupts the JSON-RPC stream.
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import sys
from pathlib import Path

import anyio
import uvicorn
from mcp import StdioServerParameters
from mcp.server.stdio import stdio_server
from redis.asyncio import Redis

from agentguard.crypto import TokenCipher
from agentguard.env import child_env
from agentguard.gateway import Downstream, Gateway
from agentguard.guard import Guard
from agentguard.oauth import OAuthDownstream
from agentguard.policy import Policy

ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="agentguard")
    parser.add_argument("--policy", default=str(ROOT / "skills/policies/prod-safe.yaml"))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="http: bind address (keep 127.0.0.1 unless behind auth)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cloud", choices=["mock", "aws"], default="mock",
                        help="mock: in-memory cloud; aws: boto3 servers (LocalStack by default)")
    parser.add_argument("--github-url", help="GitHub MCP server (OAuth-protected), e.g. http://127.0.0.1:8770/mcp; "
                                             "needs AGENTGUARD_TOKEN_KEY; users connect with agentguard.connect")
    parser.add_argument(
        "--server", action="append", metavar="NAME=COMMAND",
        help='downstream stdio server, e.g. cloud="python -m servers.cloud_server" (repeatable; overrides --cloud)',
    )
    return parser.parse_args()


CLOUDS = {
    "mock": {"cloud": "servers.cloud_server"},
    "aws": {"cloud": "servers.aws_cloud_server", "obs": "servers.observability_server"},
}


def downstreams_from(specs: list[str] | None, cloud: str = "mock") -> list[Downstream]:
    env = child_env()
    if not specs:
        return [
            Downstream(name, StdioServerParameters(command=sys.executable, args=["-m", module], cwd=str(ROOT), env=env))
            for name, module in CLOUDS[cloud].items()
        ]
    result = []
    for spec in specs:
        name, _, command = spec.partition("=")
        argv = shlex.split(command, posix=os.name != "nt")
        result.append(Downstream(name, StdioServerParameters(command=argv[0], args=argv[1:], cwd=str(ROOT), env=env)))
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
    downstreams = downstreams_from(args.server, args.cloud)
    if args.github_url:
        downstreams.append(OAuthDownstream("github", args.github_url, redis, TokenCipher.from_env("AGENTGUARD_TOKEN_KEY")))
    gateway = Gateway(Guard(redis, policy), downstreams)
    logging.info("policy=%s downstreams=%s", policy.name, list(gateway.downstreams))

    async with gateway.running():
        if args.transport == "stdio":
            async with stdio_server() as (read, write):
                await gateway.server.run(read, write, gateway.server.create_initialization_options())
        else:
            # stateless_http: older-protocol clients get no session either, so no
            # client is ever pinned to one gateway process.
            app = gateway.server.streamable_http_app(stateless_http=True, host=args.host)
            logging.info("listening on http://%s:%d/mcp", args.host, args.port)
            await uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")).serve()


if __name__ == "__main__":
    anyio.run(main)

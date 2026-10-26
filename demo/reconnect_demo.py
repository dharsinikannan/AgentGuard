"""Milestone 4 failure demo: connections drop, nothing runs twice, nothing is forgotten.

    python demo/reconnect_demo.py

Part A (in-process, always runs): the agent's HTTP connection dies in the middle of
an instance launch. A naive client that retries without a key launches twice;
through AgentGuard, the retry with the same key gets the original result.

Part B (needs Redis on localhost:6379, e.g. `docker compose up -d`): the gateway
*process* is killed mid-task and restarted. Budget and loop counters live in Redis,
so the restarted gateway carries on exactly where the old one stopped.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import uvicorn
from fakeredis import FakeAsyncRedis
from mcp import Client
from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agentguard.connection import ConnectionLost, SupervisedClient  # noqa: E402
from agentguard.gateway import Downstream, Gateway  # noqa: E402
from agentguard.guard import Guard  # noqa: E402
from agentguard.policy import Policy  # noqa: E402

B, Y, G, X = "\033[1m", "\033[33m", "\033[32m", "\033[0m"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def slow_cloud():
    mcp = MCPServer("cloud")
    launched: list[str] = []

    @mcp.tool()
    def create_instance(name: str) -> str:
        """Launch an instance (takes 1.5 s, like a real API call)."""
        # Sync on purpose, like our boto3 tools: MCPServer runs it in a worker thread,
        # and cancelling the request can't stop a thread. The launch completes even
        # when the caller is gone; only the response is lost.
        time.sleep(1.5)
        launched.append(name)
        return f"launched {name}"

    return mcp, launched


async def part_a() -> None:
    print(f"\n{B}== A. The agent's connection drops in the middle of a launch{X}")

    # Without AgentGuard: the client calls the cloud directly and retries on failure.
    cloud, launched = slow_cloud()
    async with Client(cloud) as direct:
        with anyio.move_on_after(0.5):
            await direct.call_tool("create_instance", {"name": "api-3"})   # dropped...
        print("  naive client: connection dropped, so it retries")
        await direct.call_tool("create_instance", {"name": "api-3"})
        await anyio.sleep(1.2)
    print(f"  {Y}without AgentGuard: launched {launched}{X}  <- two instances, double cost")

    # Through AgentGuard over HTTP, retrying with the same idempotency key.
    cloud, launched = slow_cloud()
    redis = FakeAsyncRedis(decode_responses=True)
    gateway = Gateway(Guard(redis, Policy.from_dict({"name": "demo", "allow": ["*"]})),
                      [Downstream("cloud", cloud)])
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(gateway.server.streamable_http_app(stateless_http=True),
                                           port=port, log_level="critical"))
    meta = {"agentguard/task_id": "demo-a", "agentguard/idempotency_key": "tc-call_42"}
    url = f"http://127.0.0.1:{port}/mcp"
    async with gateway.running(), anyio.create_task_group() as tg:
        tg.start_soon(server.serve)
        while not server.started:
            await anyio.sleep(0.02)
        async with Client(url) as agent:
            with anyio.move_on_after(0.5):
                await agent.call_tool("cloud__create_instance", {"name": "api-3"}, meta=meta)
        print("  agent: connection dropped; reconnecting and resending with the SAME key")
        async with Client(url) as agent:
            r = await agent.call_tool("cloud__create_instance", {"name": "api-3"}, meta=meta)
        print(f"  gateway -> {G}{r.meta['agentguard/verdict']}{X}: {r.content[0].text}")
        server.should_exit = True
    print(f"  {G}with AgentGuard: launched {launched}{X}  <- exactly once; the retry waited for the original")
    events = [e["event"] for _, e in await redis.xrange("ag:audit:demo-a")]
    print(f"  audit: {events}")


async def part_b() -> None:
    print(f"\n{B}== B. The gateway process dies mid-task and is restarted{X}")
    redis_url = "redis://localhost:6379/1"   # db 1: keep the demo out of your real data
    try:
        from redis.asyncio import Redis
        redis = Redis.from_url(redis_url, decode_responses=True)
        await redis.ping()
    except Exception:
        print(f"  {Y}skipped: no Redis on localhost:6379 (run: docker compose up -d){X}")
        return
    await redis.flushdb()

    port = free_port()
    cmd = [sys.executable, "-m", "agentguard", "--transport", "http", "--port", str(port),
           "--redis-url", redis_url]

    async def start_gateway() -> subprocess.Popen:
        proc = subprocess.Popen(cmd, cwd=ROOT, stderr=subprocess.DEVNULL)
        await anyio.sleep(3)
        return proc

    task = "demo-b"
    sc = SupervisedClient("agentguard", f"http://127.0.0.1:{port}/mcp", connect_timeout=15)

    async def call(tool: str, args: dict) -> str:
        try:
            r = await sc.call_tool(tool, args, meta={"agentguard/task_id": task})
        except ConnectionLost:
            return f"{Y}ConnectionLost{X} (gateway is down)"
        colour = G if r.meta["agentguard/verdict"] == "allow" else Y
        return f"{colour}{r.meta['agentguard/verdict']}{X}: {' '.join(r.content[0].text.split())[:90]}"

    async def spent() -> str:
        return f"₹{int(await redis.get(f'ag:spend:{task}') or 0) / 100:.2f}"

    gw = await start_gateway()
    try:
        async with anyio.create_task_group() as tg:
            await tg.start(sc.run)
            for _ in range(2):
                print("  get_logs   ->", await call("cloud__get_logs", {"service": "api"}))
            print("  scale 4    ->", await call("cloud__scale_service", {"service": "api", "replicas": 4}))
            print(f"  spent so far: {await spent()}")

            gw.kill()
            gw.wait()
            print(f"  {Y}*** gateway process killed ***{X}")
            print("  get_logs   ->", await call("cloud__get_logs", {"service": "api"}))
            gw = await start_gateway()
            print(f"  {G}*** gateway restarted (new process, empty memory) ***{X}")

            print("  get_logs   ->", await call("cloud__get_logs", {"service": "api"}))
            print("  get_logs   ->", await call("cloud__get_logs", {"service": "api"}),
                  " <- 4th identical call: the loop count survived the restart")
            print(f"  spent so far: {await spent()}  <- budget survived the restart too")
            tg.cancel_scope.cancel()
    finally:
        gw.kill()
        await redis.flushdb()
        await redis.aclose()


async def main() -> None:
    await part_a()
    await part_b()


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")
        sys.stdout.reconfigure(encoding="utf-8")
    anyio.run(main)

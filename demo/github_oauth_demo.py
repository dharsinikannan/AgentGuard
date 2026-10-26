"""Milestone 6 failure demo: per-user OAuth, scoped tokens, auto-refresh, no passthrough.

    python demo/github_oauth_demo.py

Self-contained: fake GitHub, the GitHub MCP server (its own OAuth server) and the
gateway run in this process over real HTTP, each service with its own fakeredis.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import anyio
import uvicorn
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis
from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentguard.connect import connect, headless_opener  # noqa: E402
from agentguard.crypto import TokenCipher  # noqa: E402
from agentguard.gateway import Gateway  # noqa: E402
from agentguard.guard import Guard, decide  # noqa: E402
from agentguard.oauth import OAuthDownstream, RedisTokenStorage  # noqa: E402
from agentguard.policy import Policy  # noqa: E402
from servers.fake_github import FakeGitHub  # noqa: E402
from servers.github_server import Config, build_app  # noqa: E402

B, Y, G, X = "\033[1m", "\033[33m", "\033[32m", "\033[0m"
POLICY = Path(__file__).resolve().parent.parent / "skills" / "policies" / "prod-safe.yaml"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def step(title: str) -> None:
    print(f"\n{B}== {title}{X}")


async def main() -> None:
    github = FakeGitHub()
    gh_port, mcp_port, cb_port = free_port(), free_port(), free_port()
    cfg = Config(f"http://127.0.0.1:{mcp_port}", github.client_id, github.client_secret,
                 f"http://127.0.0.1:{gh_port}", f"http://127.0.0.1:{gh_port}")
    gh_redis = FakeAsyncRedis(decode_responses=True, server_name="github-mcp")
    gw_redis = FakeAsyncRedis(decode_responses=True, server_name="gateway")
    gw_cipher = TokenCipher(Fernet.generate_key())
    gateway = Gateway(Guard(gw_redis, Policy.from_yaml(POLICY)),
                      [OAuthDownstream("github", cfg.resource, gw_redis, gw_cipher)])

    def storage(user: str) -> RedisTokenStorage:
        return RedisTokenStorage(gw_redis, gw_cipher, "github", user)

    async def connect_as(user: str, scopes: str) -> None:
        who = await connect(cfg.resource, storage(user), scopes, open_url=headless_opener(user), callback_port=cb_port)
        print(f"  human: {user} connected GitHub as {who!r}, granting [{scopes}]")

    async def call(client, tool, args, user, task="demo-1", key=None):
        meta = {"agentguard/task_id": task, "agentguard/user": user}
        if key:
            meta["agentguard/idempotency_key"] = key
        r = await client.call_tool(tool, args, meta=meta)
        verdict = r.meta.get("agentguard/verdict")
        if r.is_error and verdict == "allow":
            verdict = "allowed by AgentGuard, refused by the server"
        colour = G if verdict in ("allow", "replay") else Y
        print(f"  {user}: {tool} -> {colour}{verdict}{X}: {' '.join(r.content[0].text.split())[:110]}")
        return r

    servers = []
    async with anyio.create_task_group() as tg:
        for app, port in ((github.app, gh_port), (build_app(gh_redis, TokenCipher(Fernet.generate_key()), cfg), mcp_port)):
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
            tg.start_soon(server.serve)
            servers.append(server)
        while not all(s.started for s in servers):
            await anyio.sleep(0.02)

        async with gateway.running(), Client(gateway.server) as agent:
            step("1. Nobody has connected GitHub: the agent doesn't even see the tools")
            tools = [t.name for t in (await agent.list_tools(meta={"agentguard/user": "alice"})).tools]
            print(f"  github tools visible to alice's agent: {[t for t in tools if t.startswith('github__')]}")
            await call(agent, "github__whoami", {}, "alice")

            step("2. alice connects read-only (OAuth 2.1 + PKCE, GitHub login, consent)")
            await connect_as("alice", "github:read")
            await call(agent, "github__list_issues", {"repo": "acme/api"}, "alice")

            step("3. Approval is not permission: the human OKs a comment, the token still can't post")
            args = {"repo": "acme/api", "number": 1, "body": "AgentGuard: investigating the p99 spike"}
            r = await call(agent, "github__comment_on_issue", args, "alice", key="tc-comment-1")
            await decide(gw_redis, r.meta["agentguard/approval_id"], approve=True, who="alice")
            print("  human: alice approves the comment (policy: comments post under her name)")
            await call(agent, "github__comment_on_issue", args, "alice", key="tc-comment-1")

            step("4. alice grants write scope; now an approved comment posts")
            await connect_as("alice", "github:read github:write")
            args = {**args, "body": "AgentGuard: scaled api 2 -> 6, p99 back under 500 ms"}
            r = await call(agent, "github__comment_on_issue", args, "alice", key="tc-comment-2")
            await decide(gw_redis, r.meta["agentguard/approval_id"], approve=True, who="alice")
            print("  human: alice approves")
            await call(agent, "github__comment_on_issue", args, "alice", key="tc-comment-2")
            print(f"  on GitHub: {github.issues['acme/api'][0]['comments']}")

            step("5. bob is connected too, but can't borrow alice's task (or her GitHub)")
            await connect_as("bob", "github:read")
            await call(agent, "github__whoami", {}, "bob")
            await call(agent, "github__whoami", {}, "bob", task="demo-bob")

            step("6. An hour later both tokens have expired: refreshed automatically, no human")
            github.expire_all_tokens()
            record = json.loads(gw_cipher.decrypt(await gw_redis.get(storage("alice").tokens_key)))
            record["expires_at"] -= 3600
            await gw_redis.set(storage("alice").tokens_key, gw_cipher.encrypt(json.dumps(record)))
            for key in await gh_redis.keys("ghmcp:at:*"):
                await gh_redis.delete(key)
            # Time travel only edited Redis; make alice's open connection re-read it, as
            # it would have tracked the real expiry had the hour actually passed.
            await gw_redis.incr(storage("alice").generation_key)
            before = (await storage("alice").get_tokens()).access_token
            await call(agent, "github__whoami", {}, "alice")
            after = (await storage("alice").get_tokens()).access_token
            print(f"  GitHub token refreshed: {github.refreshes == 1}; gateway token refreshed: {before != after}")

            step("7. What an attacker with a Redis dump would get")
            gateway_raw = await gw_redis.get(storage("bob").tokens_key)
            server_record = await gh_redis.get((await gh_redis.keys("ghmcp:at:*"))[0])
            print(f"  gateway's copy of bob's token: {gateway_raw[:48]}...  (encrypted)")
            print(f"  server's record of a token:    {server_record[:90]}...  (no token inside; key is its hash)")
            print(f"  tokens GitHub ever received:   {sorted({t[:4] for t in github.seen_tokens if t})}  "
                  "(only GitHub's own ghu_ tokens; never the gateway's)")

            step("8. Access revoked at the server: refused cleanly, nothing charged, not 'unknown'")
            for key in await gh_redis.keys("ghmcp:[ar]t:*"):
                await gh_redis.delete(key)
            await call(agent, "github__whoami", {}, "alice", task="demo-2", key="tc-who-9")
            print(f"  spent on demo-2: ₹{int(await gw_redis.get('ag:spend:demo-2') or 0) / 100:.2f}")

        for s in servers:
            s.should_exit = True


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")
        sys.stdout.reconfigure(encoding="utf-8")
    anyio.run(main)

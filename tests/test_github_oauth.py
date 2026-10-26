"""Milestone 6: OAuth 2.1 + PKCE, per-user scoped tokens, auto-refresh, no passthrough.

Everything runs in-process over real HTTP: fake GitHub, the GitHub MCP server (its
own authorization server), and the gateway as a per-user OAuth client. Each service
has its own Redis, as it would in production.
"""

import json
import socket
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
import httpx2
import uvicorn
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis
from mcp import Client

from agentguard.connect import connect, headless_opener
from agentguard.crypto import TokenCipher, token_hash
from agentguard.gateway import Gateway
from agentguard.guard import CallContext, Guard, Verdict
from agentguard.oauth import OAuthDownstream, RedisTokenStorage
from agentguard.policy import Policy
from servers.fake_github import FakeGitHub
from servers.github_server import Config, build_app

ALLOW_ALL = Policy.from_dict({"name": "t", "allow": ["*"], "tool_costs": {"default": 1}, "budget_inr": 100})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Stack:
    github: FakeGitHub
    mcp_url: str
    gh_redis: FakeAsyncRedis        # the GitHub MCP server's own store
    gw_redis: FakeAsyncRedis        # the gateway's store
    gw_cipher: TokenCipher
    callback_port: int
    gateway: Gateway

    def storage(self, user: str) -> RedisTokenStorage:
        return RedisTokenStorage(self.gw_redis, self.gw_cipher, "github", user)

    async def connect(self, user: str, scopes: str = "github:read", login: str | None = None) -> str:
        return await connect(self.mcp_url, self.storage(user), scopes,
                             open_url=headless_opener(login or user), callback_port=self.callback_port)


async def serve(app, port, tg):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    tg.start_soon(server.serve)
    while not server.started:
        await anyio.sleep(0.02)
    return server


@asynccontextmanager
async def github_stack(policy=ALLOW_ALL):
    github = FakeGitHub()
    gh_port, mcp_port = free_port(), free_port()
    cfg = Config(base_url=f"http://127.0.0.1:{mcp_port}", github_client_id=github.client_id,
                 github_client_secret=github.client_secret,
                 github_oauth_url=f"http://127.0.0.1:{gh_port}", github_api_url=f"http://127.0.0.1:{gh_port}")
    gh_redis = FakeAsyncRedis(decode_responses=True, server_name="github-mcp")
    gw_redis = FakeAsyncRedis(decode_responses=True, server_name="gateway")
    gw_cipher = TokenCipher(Fernet.generate_key())
    app = build_app(gh_redis, TokenCipher(Fernet.generate_key()), cfg)
    gateway = Gateway(Guard(gw_redis, policy),
                      [OAuthDownstream("github", cfg.resource, gw_redis, gw_cipher)])
    async with anyio.create_task_group() as tg:
        servers = [await serve(github.app, gh_port, tg), await serve(app, mcp_port, tg)]
        async with gateway.running():
            yield Stack(github, cfg.resource, gh_redis, gw_redis, gw_cipher, free_port(), gateway)
        for s in servers:
            s.should_exit = True


async def call(stack: Stack, tool: str, args=None, *, user: str, task: str = "t1", key: str | None = None):
    meta = {"agentguard/task_id": task, "agentguard/user": user}
    if key:
        meta["agentguard/idempotency_key"] = key
    async with Client(stack.gateway.server) as client:
        return await client.call_tool(tool, args or {}, meta=meta)


def text(result) -> str:
    return result.content[0].text


async def all_values(redis) -> str:
    keys = await redis.keys("*")
    return " ".join(keys + [str(await redis.get(k)) for k in keys if await redis.type(k) == "string"])


# ------------------------------------------------------------ resource server

async def test_mcp_endpoint_rejects_requests_without_a_token():
    # Regression: with an authorization server but no token_verifier, the SDK served
    # /mcp to anyone. A 401 must also point clients at the resource metadata (RFC 9728).
    async with github_stack() as stack, httpx2.AsyncClient() as http:
        r = await http.post(stack.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                            headers={"accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]


async def test_token_minted_for_another_server_is_rejected():
    # Audience (RFC 8707): a token for some other MCP server must not work here,
    # or one compromised server could replay its tokens against us (confused deputy).
    async with github_stack() as stack:
        provider_issue = stack.gateway  # placeholder to keep the stack open
        from servers.github_server import GitHubFederatedProvider
        provider = GitHubFederatedProvider(stack.gh_redis, TokenCipher(Fernet.generate_key()),
                                           Config("http://x", "id", "secret"), httpx2.AsyncClient())
        token = await provider._issue("some-client", ["github:read"], "http://evil.example/mcp", "alice")
        async with httpx2.AsyncClient() as http:
            r = await http.post(stack.mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                headers={"accept": "application/json, text/event-stream",
                                         "authorization": f"Bearer {token.access_token}"})
        assert provider_issue is not None
    assert r.status_code == 401


async def test_github_callback_rejects_unknown_state():
    async with github_stack() as stack, httpx2.AsyncClient() as http:
        r = await http.get(stack.mcp_url.removesuffix("/mcp") + "/github/callback?state=forged&code=x")
    assert r.status_code == 400


# ------------------------------------------------------------- per-user tokens

async def test_connected_user_acts_as_themselves():
    async with github_stack() as stack:
        assert await stack.connect("alice") == "alice"
        r = await call(stack, "github__whoami", user="alice")
        async with Client(stack.gateway.server) as client:
            with_user = {t.name for t in (await client.list_tools(meta={"agentguard/user": "alice"})).tools}
            without = {t.name for t in (await client.list_tools()).tools}
    assert text(r) == "alice"
    assert "github__whoami" in with_user and "github__whoami" not in without


async def test_unconnected_user_gets_clear_error_and_pays_nothing():
    async with github_stack() as stack:
        r = await call(stack, "github__whoami", user="carol")
        spent = await stack.gw_redis.get("ag:spend:t1")
    assert r.meta["agentguard/verdict"] == "not_connected"
    assert "agentguard.connect github --user carol" in text(r)
    assert spent is None


async def test_task_acts_for_one_user_only():
    async with github_stack() as stack:
        await stack.connect("alice")
        await stack.connect("bob")
        assert text(await call(stack, "github__whoami", user="alice", task="t1")) == "alice"
        hijack = await call(stack, "github__whoami", user="bob", task="t1")
        own = await call(stack, "github__whoami", user="bob", task="t2")
    assert hijack.meta["agentguard/check"] == "owner" and "acts for alice" in text(hijack)
    assert text(own) == "bob"


async def test_scopes_are_least_privilege():
    async with github_stack() as stack:
        await stack.connect("alice")                                    # read-only by default
        denied = await call(stack, "github__comment_on_issue",
                            {"repo": "acme/api", "number": 1, "body": "looking into it"}, user="alice")
        await stack.connect("alice", scopes="github:read github:write")  # human widens it
        allowed = await call(stack, "github__comment_on_issue",
                             {"repo": "acme/api", "number": 1, "body": "looking into it"}, user="alice", task="t2")
        comments = stack.github.issues["acme/api"][0]["comments"]
    assert denied.is_error and "insufficient_scope" in text(denied)
    assert not allowed.is_error
    assert comments == [{"user": "alice", "body": "looking into it"}]


# --------------------------------------------------------- no passthrough, at rest

async def test_no_token_passthrough_and_nothing_usable_at_rest():
    async with github_stack() as stack:
        await stack.connect("alice")
        await call(stack, "github__list_issues", {"repo": "acme/api"}, user="alice")
        gateway_token = (await stack.storage("alice").get_tokens()).access_token
        github_tokens = list(stack.github.access)
        gh_dump, gw_dump = await all_values(stack.gh_redis), await all_values(stack.gw_redis)

    # GitHub only ever saw GitHub's own tokens; never the gateway's token.
    assert gateway_token not in stack.github.seen_tokens
    assert all(t.startswith("ghu_") for t in stack.github.seen_tokens)
    # The gateway never held a GitHub token.
    assert not any(t in gw_dump for t in github_tokens)
    # At rest: GitHub tokens encrypted, our tokens hashed, gateway's copy encrypted.
    assert not any(t in gh_dump for t in github_tokens)
    assert gateway_token not in gh_dump
    assert gateway_token not in gw_dump


# ------------------------------------------------------------------ refresh

async def test_expired_github_token_is_refreshed_automatically():
    async with github_stack() as stack:
        await stack.connect("alice")
        stack.github.expire_all_tokens()
        r = await call(stack, "github__whoami", user="alice")
    assert text(r) == "alice"
    assert stack.github.refreshes == 1


async def age_gateway_token(stack: Stack, user: str, seconds: float) -> None:
    """Pretend the stored token was obtained `seconds` ago (and expire it server-side)."""
    storage = stack.storage(user)
    record = json.loads(stack.gw_cipher.decrypt(await stack.gw_redis.get(storage.tokens_key)))
    record["expires_at"] -= seconds
    await stack.gw_redis.set(storage.tokens_key, stack.gw_cipher.encrypt(json.dumps(record)))
    if record["expires_at"] < time.time():
        for key in await stack.gh_redis.keys("ghmcp:at:*"):
            await stack.gh_redis.delete(key)


async def test_gateway_token_is_refreshed_before_expiry_and_rotated():
    # Regression: tokens loaded from storage had no expiry in the SDK, so the gateway
    # never refreshed; at expiry it got a 401, tried a browser login, and dropped the user.
    async with github_stack() as stack:
        await stack.connect("alice")
        old = await stack.storage("alice").get_tokens()
        await age_gateway_token(stack, "alice", 3600)          # an hour later: expired
        r = await call(stack, "github__whoami", user="alice")
        new = await stack.storage("alice").get_tokens()
        reused = await stack.gh_redis.exists(f"ghmcp:rt:{token_hash(old.refresh_token)}")
    assert text(r) == "alice"
    assert new.access_token != old.access_token and new.refresh_token != old.refresh_token
    assert not reused                                           # rotation: old refresh token is gone


async def test_reconnecting_takes_effect_without_restarting_the_gateway():
    # Regression: the gateway's live connection kept the old (read-only) token in
    # memory after the human reconnected with write access.
    async with github_stack() as stack:
        await stack.connect("alice")
        assert text(await call(stack, "github__whoami", user="alice")) == "alice"   # connection now live
        await stack.connect("alice", scopes="github:read github:write")
        r = await call(stack, "github__comment_on_issue",
                       {"repo": "acme/api", "number": 2, "body": "rotating today"}, user="alice", task="t3")
    assert not r.is_error, text(r)


async def test_revoked_authorization_is_not_connected_not_unknown():
    # A 401 the gateway can't refresh past means the call never ran. It must not be
    # recorded as "outcome unknown" (that blocks retries) or cost budget.
    async with github_stack() as stack:
        await stack.connect("alice")
        for key in await stack.gh_redis.keys("ghmcp:[ar]t:*"):
            await stack.gh_redis.delete(key)
        r = await call(stack, "github__whoami", user="alice", key="k1")
        spent = int(await stack.gw_redis.get("ag:spend:t1") or 0)
        key_left = await stack.gw_redis.exists("ag:idem:t1:k1")
        tokens = await stack.storage("alice").get_tokens()
    assert r.meta["agentguard/verdict"] == "not_connected"
    assert spent == 0 and not key_left and tokens is None


# ------------------------------------------------------------------- guard

async def test_owner_binding_in_the_guard(redis):
    guard = Guard(redis, ALLOW_ALL)
    first = await guard.check(CallContext("t9", "x__y", {}, user="alice"))
    other = await guard.check(CallContext("t9", "x__y", {"n": 1}, user="mallory"))
    assert first.verdict is Verdict.ALLOW
    assert (other.verdict, other.check) == (Verdict.DENY, "owner")


async def test_agent_graph_acts_as_its_configured_user():
    # The whole agent path: graph config "user" -> bridge _meta -> gateway -> alice's token.
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from agent.graph import build_agent
    from agent.mcp_bridge import tool_specs
    from test_agent import ScriptedModel, call as tool_call

    model = ScriptedModel(lambda turn, msgs: tool_call("github__whoami", {}, "c1") if turn == 1 else AIMessage("ok"))
    async with github_stack() as stack:
        await stack.connect("alice")
        async with Client(stack.gateway.server) as client:
            graph = build_agent(model, client, await tool_specs(client, "alice"))
            state = await graph.ainvoke({"messages": [HumanMessage("who am i on github?")]},
                                        {"configurable": {"task_id": "g1", "user": "alice"}})
    names = {s["function"]["name"] for s in model.bound_specs}
    (result,) = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert "github__whoami" in names and result.content == "alice"

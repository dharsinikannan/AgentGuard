"""Connect a human's account on a downstream server to AgentGuard (the one-time login).

    python -m agentguard.connect github --user alice                          # read-only
    python -m agentguard.connect github --user alice --scopes "github:read github:write"
    python -m agentguard.connect github --user alice --disconnect

Opens the browser for OAuth 2.1 + PKCE against the server (which may send you on to
GitHub), catches the redirect on a loopback address, and stores the gateway's tokens
for that user in Redis, encrypted. From then on the gateway refreshes them itself.

This is deliberately a human step outside MCP: an agent can't create authorizations.
--headless-login follows the redirects without a browser; it only works against
servers.fake_github (real GitHub needs a real login).
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode, urlparse

import anyio
import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.routing import Route

from agentguard.crypto import TokenCipher
from agentguard.oauth import CALLBACK_PORT, RedisTokenStorage, oauth_provider


async def connect(url: str, storage: RedisTokenStorage, scopes: str, *,
                  open_url: Callable[[str], Awaitable[None]], callback_port: int = CALLBACK_PORT) -> str:
    """Run the authorization flow once; returns the identity the server reports (whoami)."""
    result: dict[str, AuthorizationCodeResult] = {}
    received = anyio.Event()

    async def callback(request: Request):
        q = request.query_params
        if "error" in q:
            result["error"] = q["error"]
        else:
            result["code"] = AuthorizationCodeResult(code=q.get("code", ""), state=q.get("state"), iss=q.get("iss"))
        received.set()
        return HTMLResponse("<h3>AgentGuard is connected. You can close this tab.</h3>")

    async def wait_for_code() -> AuthorizationCodeResult:
        with anyio.fail_after(300):
            await received.wait()
        if "error" in result:
            raise RuntimeError(f"authorization refused: {result['error']}")
        return result["code"]

    # Throw away any old tokens: connecting again is how a human changes scopes.
    await storage.clear_tokens()
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[Route("/callback", callback)]),
                                           host="127.0.0.1", port=callback_port, log_level="warning"))
    auth = oauth_provider(url, storage, scopes, redirect_handler=open_url, callback_handler=wait_for_code,
                          callback_port=callback_port)
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve)
        while not server.started:
            await anyio.sleep(0.02)
        try:
            async with httpx2.AsyncClient(auth=auth, timeout=30) as http, \
                    Client(streamable_http_client(url, http_client=http)) as client:
                identity = (await client.call_tool("whoami", {})).content[0].text
        finally:
            server.should_exit = True
    return identity


async def open_in_browser(url: str) -> None:
    print(f"opening your browser to authorize AgentGuard:\n  {url}")
    await anyio.to_thread.run_sync(webbrowser.open, url)


def headless_opener(github_login: str) -> Callable[[str], Awaitable[None]]:
    """Follow the redirect chain like a browser would, logging in to the fake GitHub as
    `github_login`. Stops at our own loopback callback."""
    async def follow(url: str) -> None:
        async with httpx2.AsyncClient(timeout=30) as http:
            for _ in range(10):
                if urlparse(url).path.endswith("/login/oauth/authorize") and "login=" not in url:
                    url += "&" + urlencode({"login": github_login})
                response = await http.get(url, follow_redirects=False)
                if response.status_code not in (301, 302, 303, 307, 308):
                    if response.status_code >= 400:
                        raise RuntimeError(f"authorization failed at {url}: {response.text[:200]}")
                    return
                url = response.headers["location"]
            raise RuntimeError("too many redirects")
    return follow


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(encoding="utf-8-sig")
    p = argparse.ArgumentParser(prog="agentguard.connect")
    p.add_argument("server", help="downstream server name, e.g. github")
    p.add_argument("--user", required=True, help="the human whose account is being connected")
    p.add_argument("--url", default="http://127.0.0.1:8770/mcp")
    p.add_argument("--scopes", default="github:read", help='space-separated, e.g. "github:read github:write"')
    p.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    p.add_argument("--callback-port", type=int, default=CALLBACK_PORT)
    p.add_argument("--headless-login", metavar="GITHUB_LOGIN", help="fake GitHub only: no browser")
    p.add_argument("--disconnect", action="store_true", help="forget this user's tokens")
    args = p.parse_args()

    async def run() -> None:
        redis = Redis.from_url(args.redis_url, decode_responses=True)
        storage = RedisTokenStorage(redis, TokenCipher.from_env("AGENTGUARD_TOKEN_KEY"), args.server, args.user)
        try:
            if args.disconnect:
                await storage.clear_tokens()
                print(f"{args.user}: {args.server} disconnected")
                return
            opener = headless_opener(args.headless_login) if args.headless_login else open_in_browser
            identity = await connect(args.url, storage, args.scopes, open_url=opener,
                                     callback_port=args.callback_port)
            print(f"{args.user}: {args.server} connected as {identity!r} with scopes [{args.scopes}]")
        finally:
            await redis.aclose()

    try:
        anyio.run(run)
    except Exception as e:  # show the reason, not a 60-line exception group
        while isinstance(e, BaseExceptionGroup) and e.exceptions:
            e = e.exceptions[0]
        sys.exit(f"connect failed: {e}")


if __name__ == "__main__":
    main()

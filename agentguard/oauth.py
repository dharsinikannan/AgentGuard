"""AgentGuard as an OAuth 2.1 client of downstream MCP servers, per user.

The gateway holds its *own* token for each (downstream server, user): issued by that
server, for that server (RFC 8707 audience), with the scopes the human granted at
connect time. Nothing the agent sends is forwarded as a credential.

The SDK's OAuthClientProvider does the protocol: discovery (RFC 9728/8414), dynamic
client registration, PKCE, code exchange, refresh. We supply:
  - RedisTokenStorage: tokens per user, encrypted at rest (AGENTGUARD_TOKEN_KEY), with
    their *absolute* expiry, so refresh works across gateway restarts (see below)
  - a redirect handler that refuses: the gateway never starts a login on its own.
    Logging in needs a human in a browser: python -m agentguard.connect github --user alice
  - a per-user generation counter: when a human reconnects or disconnects, the
    gateway drops that user's live connection so the new tokens/scopes take effect
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import PrivateAttr
from redis.asyncio import Redis

from agentguard.connection import SupervisedClient
from agentguard.crypto import TokenCipher

CALLBACK_PORT = 8789
REFRESH_EARLY_SECONDS = 60


class NotConnected(Exception):
    """The user has no (working) authorization for this server; a human must connect it."""


class RedisTokenStorage:
    """The SDK's TokenStorage protocol, backed by Redis, one record per (server, user)."""

    def __init__(self, redis: Redis, cipher: TokenCipher, server: str, user: str):
        self.redis, self.cipher = redis, cipher
        self.tokens_key = f"ag:oauth:{server}:user:{user}"
        self.generation_key = f"ag:oauth:{server}:user:{user}:gen"
        self.client_key = f"ag:oauth:{server}:client"   # the gateway's registration, shared by users

    async def get_tokens(self) -> OAuthToken | None:
        record = await self._record()
        return OAuthToken.model_validate(record["tokens"]) if record else None

    async def expires_at(self) -> float | None:
        """Absolute expiry. OAuth only says `expires_in`, relative to issue time; a
        gateway that loads a 50-minute-old token must not believe it has an hour left."""
        record = await self._record()
        return record.get("expires_at") if record else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        expires_at = time.time() + tokens.expires_in if tokens.expires_in else None
        record = {"tokens": tokens.model_dump(mode="json"), "expires_at": expires_at}
        await self.redis.set(self.tokens_key, self.cipher.encrypt(json.dumps(record)))

    async def clear_tokens(self) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(self.tokens_key)
            pipe.incr(self.generation_key)   # live connections for this user must restart
            await pipe.execute()

    async def generation(self) -> int:
        return int(await self.redis.get(self.generation_key) or 0)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = await self.redis.get(self.client_key)
        data = self.cipher.decrypt(raw) if raw else None
        return OAuthClientInformationFull.model_validate_json(data) if data else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        # May contain a client_secret, so it's encrypted like the tokens.
        await self.redis.set(self.client_key, self.cipher.encrypt(client_info.model_dump_json()))

    async def _record(self) -> dict | None:
        raw = await self.redis.get(self.tokens_key)
        data = self.cipher.decrypt(raw) if raw else None
        return json.loads(data) if data else None


class PinnedScopeMetadata(OAuthClientMetadata):
    """Client metadata whose requested scope always includes what the human chose.

    The SDK follows the MCP spec's scope selection: it requests whatever the server's
    401 challenge names (here github:read) and overwrites ours. Right for a client
    discovering what it needs; wrong when a human deliberately grants more at connect
    time. So any scope the SDK assigns is unioned with the pinned one."""

    _pinned: str = PrivateAttr(default="")

    def pin(self, scopes: str) -> PinnedScopeMetadata:
        self._pinned = scopes
        return self

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "scope" and self._pinned:
            value = " ".join(dict.fromkeys((value or "").split() + self._pinned.split()))
        super().__setattr__(name, value)


class StoredExpiryOAuthProvider(OAuthClientProvider):
    """The SDK's provider, plus: restore the token's real expiry when loading from storage.

    The SDK sets an expiry only for tokens it obtained in this process; tokens loaded
    from storage count as valid forever. A gateway always loads from storage, so it
    would never refresh in time: at expiry the server answers 401, the SDK starts a
    browser login, the gateway refuses, and the user is disconnected for no reason.
    """

    async def _initialize(self) -> None:
        await super()._initialize()
        expires_at = await self.context.storage.expires_at()
        if self.context.current_tokens and expires_at:
            self.context.token_expiry_time = expires_at - REFRESH_EARLY_SECONDS


def client_metadata(scopes: str, callback_port: int = CALLBACK_PORT) -> OAuthClientMetadata:
    return PinnedScopeMetadata(
        client_name="AgentGuard gateway",
        # Loopback redirect (RFC 8252): the code only ever reaches this machine.
        redirect_uris=[f"http://127.0.0.1:{callback_port}/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scopes,
    ).pin(scopes)


def oauth_provider(url: str, storage: RedisTokenStorage, scopes: str, *,
                   redirect_handler: Callable[[str], Awaitable[None]],
                   callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]],
                   callback_port: int = CALLBACK_PORT) -> OAuthClientProvider:
    return StoredExpiryOAuthProvider(url, client_metadata(scopes, callback_port), storage,
                                     redirect_handler=redirect_handler, callback_handler=callback_handler)


class OAuthDownstream:
    """A downstream server that requires a per-user OAuth token.

    One SupervisedClient per user, created on first use, each with its own
    OAuthClientProvider over that user's Redis token record.
    """

    user_scoped = True

    def __init__(self, name: str, url: str, redis: Redis, cipher: TokenCipher, *, call_timeout: float = 30):
        self.name, self.url, self.redis, self.cipher = name, url, redis, cipher
        self.call_timeout = call_timeout
        self._clients: dict[str, tuple[SupervisedClient, int]] = {}

    def storage(self, user: str) -> RedisTokenStorage:
        return RedisTokenStorage(self.redis, self.cipher, self.name, user)

    async def is_connected(self, user: str) -> bool:
        return await self.storage(user).get_tokens() is not None

    async def client_for(self, user: str, start: Callable[[SupervisedClient], Awaitable[None]]) -> SupervisedClient:
        generation = await self.storage(user).generation()
        if user in self._clients:
            client, seen = self._clients[user]
            if seen == generation:
                return client
            # The human reconnected (new scopes) or disconnected since this connection
            # loaded its tokens; it would keep using the old ones from memory.
            client.restart()
            self._clients[user] = (client, generation)
            return client
        client = SupervisedClient(f"{self.name}:{user}", None, call_timeout=self.call_timeout,
                                  target_factory=lambda: self._transport(user))
        self._clients[user] = (client, generation)
        await start(client)
        return client

    @asynccontextmanager
    async def _transport(self, user: str) -> AsyncIterator[Any]:
        storage = self.storage(user)

        async def refuse(url: str) -> None:
            # Reaching the authorization step means the tokens are gone or the refresh
            # token was rejected. A gateway must not start a login on its own; drop the
            # dead record so the tools disappear, and tell the human to connect.
            await storage.clear_tokens()
            raise NotConnected(f"{user} must reconnect {self.name}")

        async def no_callback() -> AuthorizationCodeResult:
            raise NotConnected(f"{user} must reconnect {self.name}")

        tokens = await storage.get_tokens()
        scopes = tokens.scope if tokens and tokens.scope else "github:read"
        auth = oauth_provider(self.url, storage, scopes, redirect_handler=refuse, callback_handler=no_callback)
        async with httpx2.AsyncClient(auth=auth, timeout=self.call_timeout) as http:
            async with streamable_http_client(self.url, http_client=http) as streams:
                yield streams

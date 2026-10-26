"""GitHub MCP server, protected by OAuth 2.1 and federating login to GitHub.

Three tokens, each used only against the service that issued it (no passthrough):

    AgentGuard ──Bearer T1──► this server ──Bearer T2──► GitHub API
    T1: issued here, audience = this server's /mcp URL (RFC 8707), per user + scope
    T2: GitHub's user token; never leaves this process; encrypted in Redis; refreshed here

Flow when a human connects (python -m agentguard.connect github --user alice):
  1. AgentGuard registers (DCR) and starts OAuth 2.1 + PKCE against *this* server.
  2. authorize() sends the browser on to GitHub (our own PKCE + one-time state).
  3. GitHub redirects to /github/callback: we exchange the code, store T2 encrypted,
     then issue our own authorization code to AgentGuard, bound to the GitHub login.
  4. AgentGuard exchanges that code for T1 (+ refresh token).
A GitHub token can never reach AgentGuard or the agent, and T1 is useless at GitHub.

Scopes: github:read (whoami, list_issues) and github:write (comment_on_issue).
The human picks them at connect time; least privilege by default.

    python -m servers.github_server --port 8770
    env: GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GITHUB_MCP_TOKEN_KEY, REDIS_URL,
         GITHUB_OAUTH_URL / GITHUB_API_URL (default: real GitHub; point at servers.fake_github)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import anyio
import httpx2
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    ProviderTokenVerifier,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from redis.asyncio import Redis
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.routing import Route

from agentguard.crypto import TokenCipher, token_hash

SCOPES = ["github:read", "github:write"]
ACCESS_TTL = 3600              # our access token (T1): 1 hour
REFRESH_TTL = 30 * 24 * 3600   # our refresh token: 30 days, rotated on every use
P = "ghmcp:"                   # this service's Redis key prefix (a separate service in real life)


@dataclass
class Config:
    base_url: str                    # public URL of this server, e.g. http://127.0.0.1:8770
    github_client_id: str
    github_client_secret: str
    github_oauth_url: str = "https://github.com"
    github_api_url: str = "https://api.github.com"

    @property
    def resource(self) -> str:
        return f"{self.base_url}/mcp"


def _without(model, secret_field: str) -> str:
    """JSON of a token/code record minus the secret itself (we look records up by its hash)."""
    return model.model_dump_json(exclude={secret_field})


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class GitHubFederatedProvider:
    """OAuth 2.1 authorization server for MCP clients; login is delegated to GitHub."""

    def __init__(self, redis: Redis, cipher: TokenCipher, cfg: Config, http: httpx2.AsyncClient):
        self.redis, self.cipher, self.cfg, self.http = redis, cipher, cfg, http

    # ------------------------------------------------ client registration (DCR)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = await self.redis.get(f"{P}client:{client_id}")
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # A registration says which scopes a client MAY request; what it gets is decided
        # per authorization (the human's grant). Without this, one shared registration
        # made by a read-only user would cap every later user at read-only.
        client_info.scope = " ".join(SCOPES)
        await self.redis.set(f"{P}client:{client_info.client_id}", client_info.model_dump_json())

    # ------------------------------------------- authorize -> GitHub -> callback

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # Our own PKCE + state for the GitHub leg; the MCP client's PKCE stays with us.
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        await self.redis.set(f"{P}authreq:{state}", json.dumps({
            "client_id": client.client_id, "params": params.model_dump(mode="json"), "verifier": verifier,
        }), ex=600)
        return f"{self.cfg.github_oauth_url}/login/oauth/authorize?" + urlencode({
            "client_id": self.cfg.github_client_id,
            "redirect_uri": f"{self.cfg.base_url}/github/callback",
            "state": state, "code_challenge": _s256(verifier), "code_challenge_method": "S256",
        })

    async def github_callback(self, request: Request):
        # GETDEL: the state is single-use. An unknown state means CSRF or a replayed link.
        raw = await self.redis.getdel(f"{P}authreq:{request.query_params.get('state', '')}")
        if raw is None:
            return PlainTextResponse("unknown or expired login attempt; start again", 400)
        pending = json.loads(raw)
        params = AuthorizationParams.model_validate(pending["params"])
        if "code" not in request.query_params:   # the user refused on GitHub
            return RedirectResponse(construct_redirect_uri(
                str(params.redirect_uri), error="access_denied", state=params.state), 302)

        tokens = (await self.http.post(f"{self.cfg.github_oauth_url}/login/oauth/access_token", data={
            "client_id": self.cfg.github_client_id, "client_secret": self.cfg.github_client_secret,
            "code": request.query_params["code"], "code_verifier": pending["verifier"],
            "redirect_uri": f"{self.cfg.base_url}/github/callback",
        }, headers={"accept": "application/json"})).json()
        if "access_token" not in tokens:
            return PlainTextResponse(f"GitHub refused the login: {tokens.get('error')}", 400)
        user = (await self.http.get(f"{self.cfg.github_api_url}/user",
                                    headers={"authorization": f"Bearer {tokens['access_token']}"})).json()
        login = user["login"]
        await self._store_github_tokens(login, tokens)

        code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=code, scopes=params.scopes or ["github:read"], expires_at=time.time() + 300,
            client_id=pending["client_id"], code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri, redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource, subject=login,
        )
        await self.redis.set(f"{P}code:{token_hash(code)}", _without(auth_code, "code"), ex=300)
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 302)

    # --------------------------------------------------------------- tokens

    async def load_authorization_code(self, client: OAuthClientInformationFull, code: str):
        raw = await self.redis.get(f"{P}code:{token_hash(code)}")
        auth_code = AuthorizationCode.model_validate({**json.loads(raw), "code": code}) if raw else None
        return auth_code if auth_code and auth_code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        if not await self.redis.delete(f"{P}code:{token_hash(authorization_code.code)}"):
            raise TokenError("invalid_grant", "authorization code already used")
        return await self._issue(client.client_id, authorization_code.scopes,
                                 authorization_code.resource, authorization_code.subject)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        raw = await self.redis.get(f"{P}rt:{token_hash(refresh_token)}")
        rt = RefreshToken.model_validate({**json.loads(raw), "token": refresh_token}) if raw else None
        return rt if rt and rt.client_id == client.client_id else None

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        # Rotation, claimed atomically: of two concurrent refreshes only one succeeds,
        # and a stolen refresh token stops working once its owner has used it.
        if not await self.redis.delete(f"{P}rt:{token_hash(refresh_token.token)}"):
            raise TokenError("invalid_grant", "refresh token already used")
        if scopes and not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "cannot widen scopes on refresh")
        return await self._issue(client.client_id, scopes or refresh_token.scopes,
                                 refresh_token.resource, refresh_token.subject)

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = await self.redis.get(f"{P}at:{token_hash(token)}")
        at = AccessToken.model_validate({**json.loads(raw), "token": token}) if raw else None
        return at if at and (at.expires_at is None or at.expires_at > time.time()) else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        kind = "at" if isinstance(token, AccessToken) else "rt"
        await self.redis.delete(f"{P}{kind}:{token_hash(token.token)}")

    async def _issue(self, client_id: str, scopes: list[str], resource: str | None, subject: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        common = {"client_id": client_id, "scopes": scopes, "resource": resource, "subject": subject}
        # Keyed by hash, and the record itself leaves the token out: a Redis dump
        # yields no usable token. (The first version stored the whole model, token
        # field included, under the hashed key. A test caught it.)
        await self.redis.set(f"{P}at:{token_hash(access)}", _without(AccessToken(
            token=access, expires_at=int(time.time()) + ACCESS_TTL, **common), "token"), ex=ACCESS_TTL)
        await self.redis.set(f"{P}rt:{token_hash(refresh)}", _without(RefreshToken(
            token=refresh, expires_at=int(time.time()) + REFRESH_TTL, **common), "token"), ex=REFRESH_TTL)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes))

    # ------------------------------------------------- GitHub tokens (T2)

    async def _store_github_tokens(self, login: str, tokens: dict) -> None:
        record = {"access": tokens["access_token"], "refresh": tokens.get("refresh_token"),
                  "expires_at": time.time() + tokens.get("expires_in", 8 * 3600)}
        await self.redis.set(f"{P}github:{login}", self.cipher.encrypt(json.dumps(record)))

    async def github_token(self, login: str, *, force_refresh: bool = False) -> str:
        record = await self._github_record(login)
        if not force_refresh and record["expires_at"] > time.time() + 60:
            return record["access"]
        # One refresher per user: GitHub rotates refresh tokens, so two concurrent
        # refreshes with the same one would make the loser lose the user's tokens.
        lock = f"{P}lock:refresh:{login}"
        for _ in range(50):
            if await self.redis.set(lock, "1", nx=True, ex=15):
                try:
                    fresh = await self._github_record(login)
                    if fresh["access"] != record["access"]:   # someone refreshed while we waited
                        return fresh["access"]
                    return await self._refresh_github(login, fresh)
                finally:
                    await self.redis.delete(lock)
            await anyio.sleep(0.1)
        raise ToolError("GitHub token refresh is taking too long; try again")

    async def _github_record(self, login: str) -> dict:
        raw = await self.redis.get(f"{P}github:{login}")
        data = self.cipher.decrypt(raw) if raw else None
        if data is None:
            raise ToolError(f"no GitHub authorization for {login}; reconnect GitHub")
        return json.loads(data)

    async def _refresh_github(self, login: str, record: dict) -> str:
        if not record.get("refresh"):
            raise ToolError(f"GitHub token for {login} expired and can't be refreshed; reconnect GitHub")
        tokens = (await self.http.post(f"{self.cfg.github_oauth_url}/login/oauth/access_token", data={
            "client_id": self.cfg.github_client_id, "client_secret": self.cfg.github_client_secret,
            "grant_type": "refresh_token", "refresh_token": record["refresh"],
        }, headers={"accept": "application/json"})).json()
        if "access_token" not in tokens:
            raise ToolError(f"GitHub refused to refresh {login}'s token ({tokens.get('error')}); reconnect GitHub")
        await self._store_github_tokens(login, tokens)
        return tokens["access_token"]


def build_mcp(provider: GitHubFederatedProvider) -> MCPServer:
    mcp = MCPServer("github", instructions="GitHub, acting as the human who connected it.")
    cfg = provider.cfg

    def caller(scope: str) -> str:
        token = get_access_token()
        if token is None or not token.subject:
            raise ToolError("unauthenticated")
        if scope not in token.scopes:
            raise ToolError(f"insufficient_scope: this connection was granted {token.scopes}, needs {scope}")
        return token.subject

    async def github(login: str, method: str, path: str, **kwargs) -> httpx2.Response:
        for force in (False, True):   # one retry with a refreshed token if GitHub says 401
            token = await provider.github_token(login, force_refresh=force)
            response = await provider.http.request(method, f"{cfg.github_api_url}{path}",
                                                   headers={"authorization": f"Bearer {token}"}, **kwargs)
            if response.status_code != 401:
                return response
        raise ToolError(f"GitHub rejected {login}'s authorization; reconnect GitHub")

    @mcp.tool()
    async def whoami() -> str:
        """The GitHub user this connection acts as."""
        login = caller("github:read")
        return (await github(login, "GET", "/user")).json()["login"]

    @mcp.tool()
    async def list_issues(repo: str) -> list[dict]:
        """Open issues of a repository, e.g. repo="acme/api"."""
        login = caller("github:read")
        r = await github(login, "GET", f"/repos/{repo}/issues")
        if r.status_code == 404:
            raise ToolError(f"NotFound: no repository {repo} (or no access)")
        return r.json()

    @mcp.tool()
    async def comment_on_issue(repo: str, number: int, body: str) -> str:
        """Post a comment on an issue, as the connected user. Needs github:write."""
        login = caller("github:write")
        r = await github(login, "POST", f"/repos/{repo}/issues/{number}/comments", json={"body": body})
        if r.status_code == 404:
            raise ToolError(f"NotFound: no issue {repo}#{number}")
        return f"commented on {repo}#{number} as {r.json()['user']['login']}"

    return mcp


def build_app(redis: Redis, cipher: TokenCipher, cfg: Config, http: httpx2.AsyncClient | None = None) -> Starlette:
    provider = GitHubFederatedProvider(redis, cipher, cfg, http or httpx2.AsyncClient(timeout=15))
    mcp = build_mcp(provider)
    # Auth is passed to the lowlevel app explicitly: MCPServer.streamable_http_app()
    # doesn't forward auth settings. token_verifier is what actually installs the
    # bearer check (without it /mcp answers unauthenticated requests), and
    # validate_token_resource rejects tokens minted for any other server (audience).
    return mcp._lowlevel_server.streamable_http_app(
        stateless_http=True,
        auth=AuthSettings(
            issuer_url=cfg.base_url, resource_server_url=cfg.resource, validate_token_resource=True,
            required_scopes=["github:read"],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=SCOPES, default_scopes=["github:read"]),
            revocation_options=RevocationOptions(enabled=True),
        ),
        auth_server_provider=provider,
        token_verifier=ProviderTokenVerifier(provider),
        custom_starlette_routes=[Route("/github/callback", provider.github_callback)],
    )


def main() -> None:
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv(encoding="utf-8-sig")
    parser = argparse.ArgumentParser(prog="servers.github_server")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    cfg = Config(
        base_url=os.environ.get("GITHUB_MCP_URL", f"http://127.0.0.1:{args.port}"),
        github_client_id=os.environ["GITHUB_CLIENT_ID"],
        github_client_secret=os.environ["GITHUB_CLIENT_SECRET"],
        github_oauth_url=os.environ.get("GITHUB_OAUTH_URL", "https://github.com"),
        github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    app = build_app(redis, TokenCipher.from_env("GITHUB_MCP_TOKEN_KEY"), cfg)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

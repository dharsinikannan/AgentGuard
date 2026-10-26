"""A local stand-in for GitHub's OAuth + REST API, so tests and demos run offline.

Behaves like a GitHub App with expiring user tokens: access tokens expire (8 h),
come with a refresh token, and refreshing rotates both. PKCE (S256) is enforced.
The "browser" step auto-approves as the user named in ?login= (default octocat).

    python -m servers.fake_github --port 9100

Test hooks: expire_all_tokens(), plus `seen_tokens` and `refreshes` for assertions.
Real GitHub: point GITHUB_OAUTH_URL / GITHUB_API_URL at github.com / api.github.com.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

ACCESS_TTL = 8 * 3600


class FakeGitHub:
    def __init__(self, client_id: str = "Iv1.fake", client_secret: str = "fake-secret"):
        self.client_id, self.client_secret = client_id, client_secret
        self.codes: dict[str, dict] = {}
        self.access: dict[str, dict] = {}      # token -> {login, expires_at}
        self.refresh: dict[str, str] = {}      # refresh token -> login
        self.issues: dict[str, list[dict]] = {"acme/api": [
            {"number": 1, "title": "p99 latency spikes during deploys", "state": "open", "comments": []},
            {"number": 2, "title": "Rotate staging credentials", "state": "open", "comments": []},
        ]}
        self.seen_tokens: list[str] = []       # every bearer token the API received
        self.refreshes = 0
        self.app = Starlette(routes=[
            Route("/login/oauth/authorize", self.authorize),
            Route("/login/oauth/access_token", self.token, methods=["POST"]),
            Route("/user", self.user),
            Route("/repos/{owner}/{repo}/issues", self.list_issues),
            Route("/repos/{owner}/{repo}/issues/{number:int}/comments", self.comment, methods=["POST"]),
        ])

    def expire_all_tokens(self) -> None:
        for token in self.access.values():
            token["expires_at"] = 0

    # ------------------------------------------------------------- OAuth

    async def authorize(self, request: Request):
        q = request.query_params
        if q.get("client_id") != self.client_id or q.get("code_challenge_method") != "S256":
            return JSONResponse({"error": "invalid_request"}, 400)
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"login": q.get("login", "octocat"), "challenge": q["code_challenge"],
                            "redirect_uri": q["redirect_uri"]}
        return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q.get('state', '')})}", 302)

    async def token(self, request: Request):
        form = await request.form()
        if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
            return JSONResponse({"error": "incorrect_client_credentials"})
        if form.get("grant_type") == "refresh_token":
            login = self.refresh.pop(form.get("refresh_token", ""), None)   # rotation: single use
            if login is None:
                return JSONResponse({"error": "bad_refresh_token"})
            self.refreshes += 1
            return JSONResponse(self._issue(login))
        grant = self.codes.pop(form.get("code", ""), None)
        if grant is None:
            return JSONResponse({"error": "bad_verification_code"})
        verifier = form.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if challenge != grant["challenge"] or form.get("redirect_uri") != grant["redirect_uri"]:
            return JSONResponse({"error": "bad_verification_code"})
        return JSONResponse(self._issue(grant["login"]))

    def _issue(self, login: str) -> dict:
        access, refresh = "ghu_" + secrets.token_hex(16), "ghr_" + secrets.token_hex(24)
        self.access[access] = {"login": login, "expires_at": time.time() + ACCESS_TTL}
        self.refresh[refresh] = login
        return {"access_token": access, "token_type": "bearer", "scope": "", "expires_in": ACCESS_TTL,
                "refresh_token": refresh, "refresh_token_expires_in": 15_552_000}

    # --------------------------------------------------------------- API

    def _login(self, request: Request) -> str | None:
        token = request.headers.get("authorization", "").removeprefix("Bearer ").removeprefix("token ")
        self.seen_tokens.append(token)
        grant = self.access.get(token)
        return grant["login"] if grant and grant["expires_at"] > time.time() else None

    async def user(self, request: Request):
        login = self._login(request)
        if login is None:
            return JSONResponse({"message": "Bad credentials"}, 401)
        return JSONResponse({"login": login, "id": abs(hash(login)) % 10**8})

    async def list_issues(self, request: Request):
        if self._login(request) is None:
            return JSONResponse({"message": "Bad credentials"}, 401)
        repo = f"{request.path_params['owner']}/{request.path_params['repo']}"
        if repo not in self.issues:
            return JSONResponse({"message": "Not Found"}, 404)
        return JSONResponse([{k: v for k, v in i.items() if k != "comments"} for i in self.issues[repo]])

    async def comment(self, request: Request):
        login = self._login(request)
        if login is None:
            return JSONResponse({"message": "Bad credentials"}, 401)
        repo = f"{request.path_params['owner']}/{request.path_params['repo']}"
        issue = next((i for i in self.issues.get(repo, []) if i["number"] == request.path_params["number"]), None)
        if issue is None:
            return JSONResponse({"message": "Not Found"}, 404)
        body = (await request.json())["body"]
        issue["comments"].append({"user": login, "body": body})
        return JSONResponse({"id": len(issue["comments"]), "user": {"login": login}, "body": body}, 201)


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(prog="servers.fake_github")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 inside a container")
    args = parser.parse_args()
    uvicorn.run(FakeGitHub().app, host=args.host, port=args.port, log_level="warning")

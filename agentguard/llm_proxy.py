"""LLM calls go through AgentGuard too, so their cost counts against the task budget.

    agent (ChatGroq, base_url=<gateway>/llm, no API key) ──► AgentGuard ──► Groq

Why a proxy and not "the agent reports its token usage": self-reporting is only as
honest as the agent. Here the gateway holds the provider key (the agent never sees
it, like M6's no-passthrough), measures usage from the provider's own response, and
can refuse a call before it costs anything.

Per call:
  1. price the model from the policy (unpriced model: refused, fail closed)
  2. worst case = estimated input + max output tokens; *cap max_completion_tokens*
     so the model physically can't spend more than the budget has left
  3. reserve the worst case atomically (same INCRBY rule as tool calls); 402 if it
     doesn't fit, without calling the provider
  4. forward with the gateway's key; settle to the real cost from `usage`, with cached
     prompt tokens at their cheaper price; refund the rest. A failed call costs 0.

Identity: X-AgentGuard-Task (required) and X-AgentGuard-User headers. Streaming isn't
supported: the bill is only known at the end, and a stream would have to be cut mid-way.
"""

from __future__ import annotations

import json
import logging
import math

import httpx2
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agentguard.guard import TASK_ID_RE, Guard
from agentguard.policy import LLMPrice

log = logging.getLogger("agentguard.llm_proxy")

PATH = "/llm/openai/v1/chat/completions"   # the Groq SDK appends /openai/v1/... to base_url
MIN_OUTPUT_TOKENS = 64                      # below this an answer is useless; refuse instead
CHARS_PER_TOKEN_ESTIMATE = 3                # conservative: real text is ~4 chars/token


def cost_paise(price: LLMPrice, uncached_in: int, cached_in: int, out: int) -> int:
    # Rounded up: never under-charge by a fraction of a paisa per call.
    return math.ceil((uncached_in * price.input + cached_in * price.cached_input + out * price.output) / 1_000_000)


def estimate_input_tokens(body: dict) -> int:
    """An upper-bound guess before the provider counts: everything the model will read."""
    text = json.dumps([body.get("messages", []), body.get("tools", [])], separators=(",", ":"))
    return math.ceil(len(text) / CHARS_PER_TOKEN_ESTIMATE)


def _error(status: int, code: str, message: str) -> JSONResponse:
    # OpenAI-style error body, so SDKs surface the message.
    return JSONResponse({"error": {"message": f"[AgentGuard] {message}", "type": "agentguard", "code": code}},
                        status_code=status)


class LLMProxy:
    def __init__(self, guard: Guard, upstream: str, api_key: str, http: httpx2.AsyncClient | None = None):
        self.guard = guard
        self.upstream = upstream.rstrip("/")     # e.g. https://api.groq.com/openai/v1
        self.api_key = api_key
        self.http = http or httpx2.AsyncClient(timeout=120)

    def routes(self) -> list[Route]:
        return [Route(PATH, self.chat_completions, methods=["POST"])]

    async def chat_completions(self, request: Request) -> JSONResponse:
        task = request.headers.get("x-agentguard-task", "")
        if not TASK_ID_RE.match(task):
            return _error(400, "missing_task", "X-AgentGuard-Task header is required (the task this call belongs to)")
        body = await request.json()
        if body.get("stream"):
            return _error(400, "stream_unsupported", "streaming isn't supported through AgentGuard")
        policy = self.guard.policy
        price = policy.llm_prices.get(body.get("model", ""))
        if price is None:
            return _error(403, "model_not_priced", f"model {body.get('model')!r} has no price in policy "
                                                   f"'{policy.name}'; refusing (unpriced = unbudgetable)")

        # Worst case, and the cap that makes it true.
        input_estimate = estimate_input_tokens(body)
        remaining = policy.budget_paise - await self.guard.spent_paise(task)
        affordable = (remaining - cost_paise(price, input_estimate, 0, 0)) * 1_000_000 // max(price.output, 1)
        requested = body.get("max_completion_tokens") or body.get("max_tokens") or policy.llm_max_tokens
        max_out = int(min(requested, policy.llm_max_tokens, affordable))
        if max_out < MIN_OUTPUT_TOKENS:
            return _error(402, "budget_exhausted", f"task {task} can't afford another LLM call "
                                                   f"(₹{max(remaining, 0) / 100:,.2f} left). Stop and report.")
        body.pop("max_tokens", None)
        body["max_completion_tokens"] = max_out
        reserved = cost_paise(price, input_estimate, 0, max_out)
        ok, _ = await self.guard.reserve_budget(task, reserved)
        if not ok:   # someone else spent it between our read and the reservation
            return _error(402, "budget_exhausted", f"task {task} can't afford another LLM call. Stop and report.")

        audit = {"model": body["model"], "user": request.headers.get("x-agentguard-user", "")}
        try:
            # The agent's own Authorization header is never forwarded: the provider key is ours.
            response = await self.http.post(f"{self.upstream}/chat/completions", json=body,
                                            headers={"authorization": f"Bearer {self.api_key}"})
        except Exception as e:
            await self.guard.settle_budget(task, reserved, 0, outcome=f"unreachable: {e!r}"[:200], **audit)
            return _error(502, "upstream_unreachable", f"LLM provider unreachable: {e!r}")
        if response.status_code != 200:
            # Providers don't bill failed requests (429, 5xx...), so neither do we.
            await self.guard.settle_budget(task, reserved, 0, outcome=f"http {response.status_code}", **audit)
            return JSONResponse(response.json() if response.content else {}, status_code=response.status_code)

        data = response.json()
        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens", input_estimate))
        cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        completion = int(usage.get("completion_tokens", max_out))
        actual = cost_paise(price, prompt - cached, cached, completion)
        await self.guard.settle_budget(task, reserved, actual, prompt_tokens=str(prompt), cached_tokens=str(cached),
                                       completion_tokens=str(completion), **audit)
        return JSONResponse(data)

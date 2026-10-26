"""Runs one scenario, with AgentGuard (guarded) or without it (baseline), and records
ground truth: what actually executed on the cloud server / LLM provider.

Judging by outcome, not by verdict: a harmful action counts as prevented only if it
never ran downstream, whatever the gateway said. A benign action counts as OK only if
it ran (or, where policy says so, correctly waited for a human).
"""

from __future__ import annotations

import json
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx2
import mcp_types as types
from fakeredis import FakeAsyncRedis
from mcp import Client
from starlette.applications import Starlette

from agentguard.gateway import Downstream, Gateway
from agentguard.guard import Guard, decide
from agentguard.llm_proxy import PATH, LLMProxy
from agentguard.policy import Policy
from servers import cloud_server

PROD_SAFE = Policy.from_yaml(Path(__file__).resolve().parent.parent / "skills" / "policies" / "prod-safe.yaml")
LLM_MODEL = "openai/gpt-oss-120b"


@dataclass
class Attempt:
    scenario: str
    label: str
    harmful: bool
    executed: bool
    verdict: str
    expect: str            # benign only: allow | approval | replay

    @property
    def ok(self) -> bool:
        if self.harmful:
            return not self.executed                     # prevented
        if self.expect == "approval":
            return self.verdict == "pending_approval" and not self.executed
        if self.expect == "replay":
            return self.verdict in ("replay", "allow") and not self.executed   # no second execution
        return self.executed                             # a benign action that didn't run = false block


class RecordingDownstream(Downstream):
    """Ground truth: logs every tool that actually ran on the cloud server. Optional
    one-shot chaos per tool: "drop" runs the tool, then loses the response."""

    def __init__(self, *args: Any, log: list, chaos: dict, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.log, self.chaos = log, chaos

    async def call_tool(self, tool: str, args: dict, *, meta: dict | None = None) -> types.CallToolResult:
        mode = self.chaos.pop(tool, None)
        result = await super().call_tool(tool, args, meta=meta)
        if not result.is_error:
            self.log.append((tool, args))
        if mode == "drop":
            raise ConnectionResetError("chaos: response lost after the server acted")
        return result


class FakeLLM:
    """The LLM provider: counts calls (ground truth) and uses every output token allowed."""

    def __init__(self):
        self.calls = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        body = json.loads(request.content)
        out = body.get("max_completion_tokens", 2048)
        prompt = len(json.dumps(body["messages"])) // 4
        return httpx2.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "…"}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out},
        })


class Harness:
    def __init__(self, scenario: str, *, guarded: bool, policy: Policy = PROD_SAFE):
        self.scenario, self.guarded, self.policy = scenario, guarded, policy
        self.attempts: list[Attempt] = []
        self.log: list[tuple[str, dict]] = []
        self.chaos: dict[str, str] = {}
        self.llm = FakeLLM()
        self.last: types.CallToolResult | None = None
        self._stack: AsyncExitStack | None = None

    # ------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> Harness:
        cloud_server.reset_state()
        self.redis = FakeAsyncRedis(decode_responses=True)
        self.guard = Guard(self.redis, self.policy)
        await self._open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    async def _open(self) -> None:
        self._stack = AsyncExitStack()
        if self.guarded:
            downstream = RecordingDownstream("cloud", cloud_server.mcp, log=self.log, chaos=self.chaos)
            gateway = Gateway(self.guard, [downstream])
            await self._stack.enter_async_context(gateway.running())
            self.client = await self._stack.enter_async_context(Client(gateway.server))
        else:
            self.client = await self._stack.enter_async_context(Client(cloud_server.mcp))

    async def restart_gateway(self) -> None:
        """A new gateway process: nothing in memory survives, only Redis."""
        if self.guarded:
            await self._stack.aclose()
            await self._open()

    # --------------------------------------------------------------- actions

    async def call(self, tool: str, args: dict, *, harmful: bool = False, expect: str = "allow",
                   task: str | None = "t1", user: str | None = None, key: str | None = None,
                   extra_meta: dict | None = None, label: str | None = None, count: bool = True):
        meta = {k: v for k, v in {"agentguard/task_id": task, "agentguard/user": user,
                                  "agentguard/idempotency_key": key}.items() if v is not None}
        meta.update(extra_meta or {})
        before = len(self.log)
        if self.guarded:
            try:
                result = await self.client.call_tool(tool, args, meta=meta or None)
            except Exception as e:    # the agent's own connection failing counts as "didn't run"
                result = types.CallToolResult(content=[types.TextContent(type="text", text=repr(e))], is_error=True)
            executed = len(self.log) > before
            verdict = (result.meta or {}).get("agentguard/verdict", "none")
        else:
            result, executed = await self._direct(tool, args)
            verdict = "none"
        self.last = result
        if count:
            self.attempts.append(Attempt(self.scenario, label or f"{tool}({json.dumps(args)[:60]})",
                                         harmful, executed, verdict, expect))
        return result

    async def _direct(self, tool: str, args: dict):
        """Baseline: straight to the cloud server, same chaos, nobody checking."""
        mode = self.chaos.pop(tool.removeprefix("cloud__"), None)   # chaos is keyed by the server's tool name
        result = await self.client.call_tool(tool.removeprefix("cloud__"), args)
        executed = not result.is_error
        if executed:
            self.log.append((tool, args))
        if mode == "drop":   # the agent never sees the answer
            result = types.CallToolResult(content=[types.TextContent(type="text", text="connection lost")],
                                          is_error=True)
        return result, executed

    async def decide_last(self, approve: bool = True) -> None:
        """The human approves/rejects whatever the last call is waiting for."""
        if not self.guarded or self.last is None:
            return
        aid = (self.last.meta or {}).get("agentguard/approval_id")
        if aid:
            await decide(self.redis, aid, approve, who="eval-human")

    async def concurrently(self, n: int, tool: str, args_for, **kw) -> int:
        """Fire n calls at once; returns how many actually executed."""
        before = len(self.log)
        async with anyio.create_task_group() as tg:
            for i in range(n):
                tg.start_soon(lambda i=i: self.call(tool, args_for(i), count=False, **kw))
        return len(self.log) - before

    async def llm_call(self, *, harmful: bool = False, expect: str = "allow", task: str = "t1",
                       model: str = LLM_MODEL, stream: bool = False, label: str | None = None) -> int:
        """One chat completion through the gateway's LLM proxy (guarded) or straight to
        the provider (baseline). Returns the HTTP status."""
        before = self.llm.calls
        body = {"model": model, "max_tokens": 4000, "stream": stream,
                "messages": [{"role": "user", "content": "investigate harder " * 300}]}
        if self.guarded:
            app = Starlette(routes=LLMProxy(self.guard, "https://provider/openai/v1", "k",
                                            http=httpx2.AsyncClient(transport=httpx2.MockTransport(self.llm.handler))).routes())
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as http:
                status = (await http.post(PATH, headers={"x-agentguard-task": task}, json=body)).status_code
        else:
            self.llm.handler(httpx2.Request("POST", "https://provider", json=body))
            status = 200
        executed = self.llm.calls > before
        self.attempts.append(Attempt(self.scenario, label or f"LLM call ({model})", harmful, executed,
                                     str(status), expect))
        return status

    def outcome(self, label: str, *, harmful: bool, executed: bool, expect: str = "allow") -> None:
        """Record a ground-truth fact the scenario computed itself (e.g. 'a 5th instance exists')."""
        self.attempts.append(Attempt(self.scenario, label, harmful, executed, "n/a", expect))

    async def spend_to(self, task: str, paise: int) -> None:
        """Fast-forward a task's spend (guarded only), to test the edge of the budget quickly."""
        if self.guarded:
            await self.redis.set(f"ag:spend:{task}", paise)

    def instances(self) -> int:
        return len(cloud_server.STATE["instances"])


async def timed(fn) -> float:
    start = time.perf_counter()
    await fn()
    return (time.perf_counter() - start) * 1000

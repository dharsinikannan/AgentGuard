"""Milestone 7 failure demo: semantic loops, and LLM spend inside the task budget.

    python demo/cost_and_intent_demo.py

Self-contained (fakeredis, in-process cloud, a fake LLM provider). A scripted "bad
agent" does what real runs showed: keeps scaling the same service with new numbers,
fiddles get_logs arguments, then burns tokens until the money runs out.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anyio
import httpx2
from fakeredis import FakeAsyncRedis
from mcp import Client
from starlette.applications import Starlette

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentguard.gateway import Downstream, Gateway  # noqa: E402
from agentguard.guard import Guard  # noqa: E402
from agentguard.llm_proxy import PATH, LLMProxy  # noqa: E402
from agentguard.policy import Policy  # noqa: E402
from servers import cloud_server  # noqa: E402

B, Y, G, X = "\033[1m", "\033[33m", "\033[32m", "\033[0m"
TASK = "incident-7"
POLICY = Path(__file__).resolve().parent.parent / "skills" / "policies" / "prod-safe.yaml"


def step(title: str) -> None:
    print(f"\n{B}== {title}{X}")


def fake_provider(request: httpx2.Request) -> httpx2.Response:
    """A verbose provider: uses every output token allowed. It counts the prompt like a
    real tokenizer (~4 chars/token) and has half of it cached."""
    body = json.loads(request.content)
    out = body["max_completion_tokens"]
    prompt = len(json.dumps(body["messages"])) // 4
    return httpx2.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
        "choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "…"}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out,
                  "prompt_tokens_details": {"cached_tokens": prompt // 2}},
    })


async def main() -> None:
    redis = FakeAsyncRedis(decode_responses=True)
    policy = Policy.from_yaml(POLICY)
    guard = Guard(redis, policy)
    cloud_server.reset_state()
    gateway = Gateway(guard, [Downstream("cloud", cloud_server.mcp)])
    proxy = Starlette(routes=LLMProxy(guard, "https://provider.example/openai/v1", "gateway-key",
                                      http=httpx2.AsyncClient(transport=httpx2.MockTransport(fake_provider))).routes())

    async def tool(agent, name, args):
        r = await agent.call_tool(name, args, meta={"agentguard/task_id": TASK})
        verdict = r.meta.get("agentguard/verdict")
        colour = G if verdict == "allow" else Y
        print(f"  {name}({json.dumps(args)}) -> {colour}{verdict}{X}: {' '.join(r.content[0].text.split())[:120]}")

    async def spent() -> str:
        total, llm = await guard.spent_paise(TASK), int(await redis.get(f"ag:spend:{TASK}:llm") or 0)
        return f"₹{total / 100:,.2f} of ₹{policy.budget_paise / 100:,.0f} (tools ₹{(total - llm) / 100:,.2f}, LLM ₹{llm / 100:,.2f})"

    async with gateway.running(), Client(gateway.server) as agent:
        step("1. Over-scaling: same intent, new numbers each time (identical-call check can't see it)")
        for replicas in (4, 6, 8, 10, 12):
            await tool(agent, "cloud__scale_service", {"service": "api", "replicas": replicas})

        step("2. Fiddling get_logs: lines=5, 10, 20, 50, 100 (intent = get_logs on service api)")
        for lines in (5, 10, 20, 50, 100):
            await tool(agent, "cloud__get_logs", {"service": "api", "lines": lines})
        print(f"  spent: {await spent()}")

        step("3. LLM calls go through AgentGuard: priced, capped, billed from real usage")
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=proxy), base_url="http://gw") as http:
            await redis.incrby(f"ag:spend:{TASK}", policy.budget_paise - await guard.spent_paise(TASK) - 50)
            print(f"  (fast-forward: the task has spent most of its budget: {await spent()})")
            for i in range(1, 7):
                r = await http.post(PATH, headers={"x-agentguard-task": TASK},
                                    json={"model": "openai/gpt-oss-120b", "max_tokens": 8000,
                                          "messages": [{"role": "user", "content": "think harder " * 800}]})
                if r.status_code == 200:
                    used = r.json()["usage"]
                    print(f"  LLM call {i}: {G}200{X} asked for 8000 output tokens, capped to "
                          f"{used['completion_tokens']}; {used['prompt_tokens_details']['cached_tokens']} of "
                          f"{used['prompt_tokens']} prompt tokens cached -> {await spent()}")
                else:
                    print(f"  LLM call {i}: {Y}{r.status_code}{X} {r.json()['error']['message'][:110]}")
                    break
        print(f"  final: {await spent()}")
        print("  output was capped to what the budget could pay; input is estimated conservatively and the")
        print("  real bill is always charged, so a mis-estimate shows up in the spend, never hidden.")


if __name__ == "__main__":
    if os.name == "nt":
        os.system("")
        sys.stdout.reconfigure(encoding="utf-8")
    anyio.run(main)

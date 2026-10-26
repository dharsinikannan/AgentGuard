"""Milestone 7: LLM cost in the task budget (via the gateway's LLM proxy) and
semantic loop detection (same intent, varying arguments)."""

import json

import httpx
import httpx2
import pytest
from starlette.applications import Starlette

from agentguard.guard import CallContext, Guard, Verdict, intent_key
from agentguard.llm_proxy import PATH, LLMProxy, cost_paise
from agentguard.policy import Policy, PolicyError

MODEL = "openai/gpt-oss-120b"
LLM_POLICY = {
    "name": "t", "allow": ["*"], "budget_inr": 10, "tool_costs": {"default": 1},
    "llm_costs": {MODEL: {"input_per_million_inr": 13, "cached_input_per_million_inr": 6.5,
                          "output_per_million_inr": 55}},
    "llm_max_tokens": 1000,
}


# ------------------------------------------------------------------ intent

def test_intent_ignores_tuning_knobs_and_normalizes_text():
    a, _ = intent_key("cloud__get_logs", {"service": "api", "lines": 5}, None)
    b, _ = intent_key("cloud__get_logs", {"service": "api", "lines": 50}, None)
    c, _ = intent_key("cloud__get_logs", {"service": "db", "lines": 5}, None)
    d, _ = intent_key("x__note", {"text": "Restart  API "}, None)
    e, _ = intent_key("x__note", {"text": "restart api"}, None)
    assert a == b != c and d == e


def test_configured_intent_args_win():
    # Only `service` defines intent, even if another string arg changes.
    a, label = intent_key("obs__get_logs", {"service": "api", "pattern": "WARN"}, ("service",))
    b, _ = intent_key("obs__get_logs", {"service": "api", "pattern": "ERROR"}, ("service",))
    assert a == b and label == "obs__get_logs[service=api]"


async def test_overscaling_is_a_semantic_loop(redis):
    # The pattern every real run showed: scale api 4, 6, 8, 10, 12. Each call has
    # different args, so identical-call counting never fired.
    guard = Guard(redis, Policy.from_yaml("skills/policies/prod-safe.yaml"))
    verdicts = [(await guard.check(CallContext("t1", "cloud__scale_service",
                                               {"service": "api", "replicas": n}))).verdict
                for n in (4, 6, 8, 10, 12)]
    db = await guard.check(CallContext("t1", "cloud__scale_service", {"service": "db", "replicas": 2}))
    assert verdicts == [Verdict.ALLOW] * 4 + [Verdict.DENY]
    assert db.verdict is Verdict.ALLOW                 # a different intent has its own count


async def test_semantic_loop_reason_names_the_intent(redis):
    guard = Guard(redis, Policy.from_dict({"name": "t", "allow": ["*"], "loop": {"max_same_intent": 2}}))
    for lines in (5, 6):
        await guard.check(CallContext("t1", "cloud__get_logs", {"service": "api", "lines": lines}))
    d = await guard.check(CallContext("t1", "cloud__get_logs", {"service": "api", "lines": 7}))
    assert (d.verdict, d.check) == (Verdict.DENY, "loop")
    assert "cloud__get_logs[service=api]" in d.reason and "varying arguments" in d.reason


async def test_intent_limit_is_off_unless_configured(redis):
    guard = Guard(redis, Policy.from_dict({"name": "t", "allow": ["*"]}))
    verdicts = [(await guard.check(CallContext("t1", "x__y", {"n": n}))).verdict for n in range(8)]
    assert set(verdicts) == {Verdict.ALLOW}


@pytest.mark.parametrize("bad", [
    {"loop": {"intent_args": {"x": "service"}}},
    {"loop": {"max_same_intent": 0}},
    {"llm_costs": {MODEL: {"input_per_million_inr": 1}}},
    {"llm_costs": {MODEL: {"input_per_million_inr": 1, "output_per_million_inr": 1, "typo": 1}}},
    {"llm_max_tokens": 0},
])
def test_bad_m7_policy_fields_fail_at_load(bad):
    with pytest.raises(PolicyError):
        Policy.from_dict({"name": "t", **bad})


# --------------------------------------------------------------- LLM proxy

class Upstream:
    """Stands in for Groq: records requests, answers with a usage block."""

    def __init__(self, status=200, prompt=1000, cached=0, completion=200):
        self.requests: list[httpx2.Request] = []
        self.status, self.usage = status, {"prompt_tokens": prompt, "completion_tokens": completion,
                                            "total_tokens": prompt + completion,
                                            "prompt_tokens_details": {"cached_tokens": cached}}

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx2.Response(self.status, json={"error": {"message": "rate limited"}})
        return httpx2.Response(200, json={
            "id": "c1", "object": "chat.completion", "created": 0, "model": MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "scale api to 6"}}],
            "usage": self.usage,
        })

    def body(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


def proxy_app(redis, upstream: Upstream, policy=LLM_POLICY):
    guard = Guard(redis, Policy.from_dict(policy))
    proxy = LLMProxy(guard, "https://llm.example/openai/v1", "gateway-secret-key",
                     http=httpx2.AsyncClient(transport=httpx2.MockTransport(upstream.handler)))
    return guard, Starlette(routes=proxy.routes())


async def chat(app, task="t1", **body):
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as http:
        return await http.post(PATH, headers={"x-agentguard-task": task, "authorization": "Bearer agent-junk"},
                               json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], **body})


async def test_llm_call_is_charged_its_real_cost_with_cached_discount(redis):
    upstream = Upstream(prompt=1000, cached=600, completion=200)
    guard, app = proxy_app(redis, upstream)
    r = await chat(app)
    price = guard.policy.llm_prices[MODEL]
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "scale api to 6"
    assert await guard.spent_paise("t1") == cost_paise(price, 400, 600, 200)   # reservation refunded
    assert await redis.get("ag:spend:t1:llm") == str(cost_paise(price, 400, 600, 200))


async def test_output_is_capped_so_the_model_cannot_overspend(redis):
    upstream = Upstream()
    _, app = proxy_app(redis, upstream)
    await chat(app, max_tokens=50_000)
    assert upstream.body()["max_completion_tokens"] == 1000          # policy cap
    assert "max_tokens" not in upstream.body()

    # Nearly broke: the cap shrinks to what the remaining budget can pay for.
    await redis.set("ag:spend:t1", 1000 - 3)                           # ₹0.03 left of ₹10
    await chat(app)
    cap = upstream.body()["max_completion_tokens"]
    assert 64 <= cap < 1000
    assert cost_paise(Policy.from_dict(LLM_POLICY).llm_prices[MODEL], 0, 0, cap) <= 3


async def test_exhausted_budget_refuses_without_calling_the_provider(redis):
    upstream = Upstream()
    _, app = proxy_app(redis, upstream)
    await redis.set("ag:spend:t1", 1000)
    r = await chat(app)
    assert r.status_code == 402 and r.json()["error"]["code"] == "budget_exhausted"
    assert upstream.requests == []


async def test_unpriced_model_is_refused(redis):
    upstream = Upstream()
    _, app = proxy_app(redis, upstream)
    r = await chat(app, model="someone/expensive-model")
    assert r.status_code == 403 and upstream.requests == []


async def test_failed_provider_call_costs_nothing(redis):
    upstream = Upstream(status=429)
    guard, app = proxy_app(redis, upstream)
    r = await chat(app)
    assert r.status_code == 429
    assert await guard.spent_paise("t1") == 0


async def test_provider_key_is_the_gateways_never_the_agents(redis):
    upstream = Upstream()
    _, app = proxy_app(redis, upstream)
    await chat(app)
    assert upstream.requests[0].headers["authorization"] == "Bearer gateway-secret-key"


async def test_llm_calls_need_a_task(redis):
    _, app = proxy_app(redis, Upstream())
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw") as http:
        r = await http.post(PATH, json={"model": MODEL, "messages": []})
    assert r.status_code == 400


async def test_tools_and_llm_share_one_budget(redis):
    guard, app = proxy_app(redis, Upstream(prompt=1000, completion=200))
    await guard.check(CallContext("t1", "cloud__get_logs", {"service": "api"}))   # ₹1 tool cost
    await chat(app)
    llm = int(await redis.get("ag:spend:t1:llm"))
    assert await guard.spent_paise("t1") == 100 + llm


async def test_langchain_groq_client_works_through_the_proxy(redis):
    # The agent's real client: base_url = gateway/llm, no provider key, task header.
    from langchain_core.messages import HumanMessage
    from langchain_groq import ChatGroq

    upstream = Upstream(prompt=900, completion=100)
    guard, app = proxy_app(redis, upstream)
    llm = ChatGroq(model=MODEL, api_key="held-by-agentguard", base_url="http://gw/llm", max_retries=0,
                   default_headers={"X-AgentGuard-Task": "t1"},
                   http_async_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)))
    reply = await llm.ainvoke([HumanMessage("p99 is 2400ms, what now?")])
    assert reply.content == "scale api to 6"
    assert await guard.spent_paise("t1") == cost_paise(guard.policy.llm_prices[MODEL], 900, 0, 100)


async def test_input_underestimate_is_charged_not_hidden(redis):
    # Output is hard-capped; input can only be estimated before the provider counts it.
    # If the provider bills more input than we estimated, the real bill is charged
    # (spend may then pass the budget by that error) and the next call is refused.
    upstream = Upstream(prompt=200_000, completion=10)       # far more than our estimate
    guard, app = proxy_app(redis, upstream)
    await redis.set("ag:spend:t1", 1000 - 20)                # ₹0.20 left
    assert (await chat(app)).status_code == 200
    price = guard.policy.llm_prices[MODEL]
    assert await guard.spent_paise("t1") == 980 + cost_paise(price, 200_000, 0, 10)   # > budget, honestly
    assert (await chat(app)).status_code == 402

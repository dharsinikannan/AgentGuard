"""Read-only views over AgentGuard's Redis state, for the dashboard.

Kept out of the Streamlit files so it's unit-testable and the UI stays thin.

Demo mode (AGENTGUARD_DEMO=1, the public Hugging Face Space): every caller shares one
in-memory Redis for the life of the process. No real Redis, no secrets, nothing a
visitor can reach beyond this process.
"""

from __future__ import annotations

import os
from collections import Counter
from typing import Any

from redis.asyncio import Redis

_demo_server = None


def is_demo() -> bool:
    return os.environ.get("AGENTGUARD_DEMO") == "1"


def open_redis() -> Redis:
    """A fresh client per call: Streamlit reruns scripts on new event loops, and an
    async client must not outlive its loop. The demo store itself is shared."""
    global _demo_server
    if is_demo():
        from fakeredis import FakeAsyncRedis, FakeServer
        _demo_server = _demo_server or FakeServer()
        return FakeAsyncRedis(server=_demo_server, decode_responses=True)
    return Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


async def events(redis: Redis, task_id: str) -> list[dict[str, str]]:
    return [{_s(k): _s(v) for k, v in e.items()} for _, e in await redis.xrange(f"ag:audit:{task_id}")]


async def task_summary(redis: Redis, task_id: str) -> dict[str, Any]:
    evs = await events(redis, task_id)
    decisions = [e for e in evs if e.get("event") == "decision"]
    verdicts = Counter(e["verdict"] for e in decisions)
    total = int(await redis.get(f"ag:spend:{task_id}") or 0)
    llm = int(await redis.get(f"ag:spend:{task_id}:llm") or 0)
    return {
        "task": task_id,
        "owner": _s(await redis.get(f"ag:task:{task_id}:owner") or "—"),
        "decisions": len(decisions),
        "allowed": verdicts["allow"],
        "blocked": verdicts["deny"],
        "pending": verdicts["pending_approval"],
        "replayed": verdicts["replay"],
        "llm_calls": sum(e.get("event") == "llm" for e in evs),
        "spend_tools_inr": (total - llm) / 100,
        "spend_llm_inr": llm / 100,
        "last_event": float(evs[-1]["ts"]) if evs else 0.0,
    }


async def overview(redis: Redis) -> dict[str, Any]:
    """Everything the Overview page shows, in one pass."""
    tasks = sorted({_s(t) for t in await redis.smembers("ag:tasks")})
    summaries = [await task_summary(redis, t) for t in tasks]
    blocked_by: Counter[str] = Counter()
    recent: list[dict[str, str]] = []
    for t in tasks:
        for e in await events(redis, t):
            if e.get("event") == "decision" and e.get("verdict") == "deny":
                blocked_by[e.get("check", "?")] += 1
            recent.append({"task": t, **e})
    recent.sort(key=lambda e: float(e.get("ts", 0)), reverse=True)
    return {
        "tasks": sorted(summaries, key=lambda s: s["last_event"], reverse=True),
        "blocked_by_check": dict(blocked_by.most_common()),
        "pending_approvals": await redis.hlen("ag:approvals:pending"),
        "recent": recent[:25],
    }


def sync_call(fn, *args: Any, **kwargs: Any) -> Any:
    """Run `await fn(redis, *args)` from synchronous Streamlit code."""
    import asyncio

    async def go():
        redis = open_redis()
        try:
            return await fn(redis, *args, **kwargs)
        finally:
            await redis.aclose()
    return asyncio.run(go())

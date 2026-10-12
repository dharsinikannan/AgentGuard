"""The decision engine. Pure policy + Redis: no MCP, no LangGraph.

For every tool call the gateway asks `Guard.check()` for a Decision. Checks run in
a fixed order, cheapest and most absolute first:

    policy -> idempotency -> loop -> budget -> approval -> allow

Some checks have side effects (claiming an idempotency key, reserving budget).
If a *later* check refuses the call, those side effects are rolled back, so a
denied call never burns budget or leaves a key stuck "in progress".

Every decision, and every execution outcome, is appended to the Redis Stream
`ag:audit:{task_id}`.

Redis keys (all prefixed `ag:`):
    idem:{task}:{key}          JSON {status, call_hash, result?}       24h
    loop:{task}:{call_hash}    INCR counter of identical calls          window
    calls:{task}               INCR counter of all calls                24h
    spend:{task}               integer paise reserved so far            24h
    approvals:pending          HASH approval_id -> JSON request
    approval:grant:{id}        one-shot human approval                  1h
    approval:rejected:{id}     human said no                            1h
    audit:{task}               STREAM of decisions and outcomes
    tasks                      SET of task ids seen (for scripts/audit.py)
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from redis.asyncio import Redis

from agentguard.policy import Policy

TASK_TTL_SECONDS = 24 * 3600
APPROVAL_TTL_SECONDS = 3600
AUDIT_MAXLEN = 10_000

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_IDEM_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


class Verdict(str, Enum):
    ALLOW = "allow"                         # forward to the downstream server
    DENY = "deny"                           # refuse, with a reason the agent can read
    PENDING_APPROVAL = "pending_approval"   # refuse for now; a human must approve
    REPLAY = "replay"                       # already executed; return the stored result


@dataclass
class CallContext:
    task_id: str
    tool: str                       # namespaced, e.g. "cloud__delete_bucket"
    args: dict[str, Any]
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        # These strings become parts of Redis keys; a task id containing ":"
        # could otherwise read or overwrite another task's keys.
        if not _TASK_ID_RE.match(self.task_id):
            raise ValueError(f"invalid task_id {self.task_id!r}")
        if self.idempotency_key is not None and not _IDEM_KEY_RE.match(self.idempotency_key):
            raise ValueError(f"invalid idempotency_key {self.idempotency_key!r}")

    @property
    def call_hash(self) -> str:
        return call_hash(self.tool, self.args)


@dataclass
class Decision:
    verdict: Verdict
    check: str                              # which stage decided
    reason: str                             # human/LLM-readable
    cached_result: dict[str, Any] | None = None
    approval_id: str | None = None


def call_hash(tool: str, args: dict[str, Any]) -> str:
    """Stable identity of a call: same tool + same args (key order ignored) -> same hash."""
    canonical = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def approval_id(task_id: str, call_hash_: str) -> str:
    """Approval is bound to task + exact tool + exact args. Change any of them -> new id."""
    return hashlib.sha256(f"{task_id}|{call_hash_}".encode()).hexdigest()[:20]


Undo = Callable[[], Awaitable[Any]]


class Guard:
    def __init__(self, redis: Redis, policy: Policy) -> None:
        self.redis = redis
        self.policy = policy

    # ------------------------------------------------------------------ check

    async def check(self, ctx: CallContext) -> Decision:
        undo: list[Undo] = []
        decision = await self._evaluate(ctx, undo)
        if decision.verdict is not Verdict.ALLOW:
            for step in reversed(undo):
                await step()
        await self._audit(
            ctx.task_id, "decision",
            tool=ctx.tool, args=json.dumps(ctx.args, sort_keys=True),
            verdict=decision.verdict.value, check=decision.check, reason=decision.reason,
            idempotency_key=ctx.idempotency_key or "",
        )
        return decision

    async def _evaluate(self, ctx: CallContext, undo: list[Undo]) -> Decision:
        r, p, h = self.redis, self.policy, ctx.call_hash

        # 1. Policy. Static, no Redis needed.
        rule = p.rule_for(ctx.tool)
        if rule == "deny":
            return Decision(Verdict.DENY, "policy", f"policy '{p.name}' does not permit {ctx.tool}")

        # 2. Idempotency. Atomic claim with SET NX: of N concurrent requests with
        #    the same key, exactly one wins. check-then-set would let two through.
        if ctx.idempotency_key:
            key = f"ag:idem:{ctx.task_id}:{ctx.idempotency_key}"
            claim = json.dumps({"status": "in_progress", "call_hash": h})
            if await r.set(key, claim, nx=True, ex=TASK_TTL_SECONDS):
                undo.append(lambda: r.delete(key))
            else:
                decision = await self._existing_idempotency_record(key, h)
                if decision:
                    return decision

        # 3. Loop detection. Blocked attempts still count: an agent hammering a
        #    refused call *is* the loop we want to catch.
        loop_key = f"ag:loop:{ctx.task_id}:{h}"
        calls_key = f"ag:calls:{ctx.task_id}"
        async with r.pipeline(transaction=True) as pipe:
            pipe.incr(loop_key)
            pipe.expire(loop_key, p.loop.window_seconds, nx=True)
            pipe.incr(calls_key)
            pipe.expire(calls_key, TASK_TTL_SECONDS, nx=True)
            identical, _, total, _ = await pipe.execute()
        if identical > p.loop.max_identical_calls:
            return Decision(
                Verdict.DENY, "loop",
                f"loop detected: {ctx.tool} called {identical} times with identical arguments "
                f"(limit {p.loop.max_identical_calls} per {p.loop.window_seconds}s). Try a different approach.",
            )
        if total > p.loop.max_calls_per_task:
            return Decision(Verdict.DENY, "loop", f"task exceeded {p.loop.max_calls_per_task} tool calls")

        # 4. Budget. INCRBY first, then compare: the reservation is atomic, so
        #    concurrent calls can never overspend. Cost: two racing calls near
        #    the limit may both be refused when one would have fit.
        cost = p.cost_paise(ctx.tool)
        if cost:
            spend_key = f"ag:spend:{ctx.task_id}"
            spent = await r.incrby(spend_key, cost)
            await r.expire(spend_key, TASK_TTL_SECONDS, nx=True)
            undo.append(lambda: r.decrby(spend_key, cost))
            if spent > p.budget_paise:
                return Decision(
                    Verdict.DENY, "budget",
                    f"budget exceeded: {ctx.tool} costs {_inr(cost)}, task has spent "
                    f"{_inr(spent - cost)} of {_inr(p.budget_paise)}",
                )

        # 5. Human approval, bound to this exact task + tool + args.
        if rule == "require_approval":
            decision = await self._approval(ctx)
            if decision:
                return decision

        return Decision(Verdict.ALLOW, "allow", "allowed")

    async def _existing_idempotency_record(self, key: str, h: str) -> Decision | None:
        raw = await self.redis.get(key)
        if raw is None:
            # The record expired between our SET NX and GET. Vanishingly rare;
            # refuse rather than guess.
            return Decision(Verdict.DENY, "idempotency", "idempotency record changed concurrently; retry")
        record = json.loads(raw)
        if record["call_hash"] != h:
            return Decision(
                Verdict.DENY, "idempotency",
                "idempotency key was already used for a different tool or arguments",
            )
        status = record["status"]
        if status == "done":
            return Decision(Verdict.REPLAY, "idempotency", "already executed; returning stored result",
                            cached_result=record["result"])
        if status == "in_progress":
            return Decision(Verdict.DENY, "idempotency",
                            "an identical call with this idempotency key is still in flight")
        # status == "unknown"
        return Decision(
            Verdict.DENY, "idempotency",
            "a previous attempt with this idempotency key lost its connection, so its outcome is "
            "unknown. Do not retry blindly: check the current state (e.g. list resources) first.",
        )

    async def _approval(self, ctx: CallContext) -> Decision | None:
        r = self.redis
        aid = approval_id(ctx.task_id, ctx.call_hash)

        # GETDEL: an approval is single-use. Approving one bucket deletion must
        # not approve the agent repeating it later.
        if await r.getdel(f"ag:approval:grant:{aid}"):
            return None
        if await r.exists(f"ag:approval:rejected:{aid}"):
            return Decision(Verdict.DENY, "approval", "a human rejected this exact action",
                            approval_id=aid)

        request = json.dumps({
            "task_id": ctx.task_id, "tool": ctx.tool, "args": ctx.args, "requested_at": time.time(),
        })
        await r.hsetnx("ag:approvals:pending", aid, request)
        return Decision(
            Verdict.PENDING_APPROVAL, "approval",
            f"{ctx.tool} requires human approval (approval id {aid}). "
            "Continue with other work; retry this exact call after it is approved.",
            approval_id=aid,
        )

    # ------------------------------------------------------- after execution

    async def record_result(self, ctx: CallContext, result: dict[str, Any], is_error: bool) -> None:
        """The downstream server answered. Its answer is definitive, so store it for replay."""
        if ctx.idempotency_key:
            record = json.dumps({"status": "done", "call_hash": ctx.call_hash, "result": result})
            await self.redis.set(f"ag:idem:{ctx.task_id}:{ctx.idempotency_key}", record,
                                 ex=TASK_TTL_SECONDS)
        await self._audit(ctx.task_id, "executed", tool=ctx.tool, is_error=str(is_error))

    async def record_unknown(self, ctx: CallContext, error: str) -> None:
        """The connection died mid-call: the write may or may not have happened."""
        if ctx.idempotency_key:
            record = json.dumps({"status": "unknown", "call_hash": ctx.call_hash})
            await self.redis.set(f"ag:idem:{ctx.task_id}:{ctx.idempotency_key}", record,
                                 ex=TASK_TTL_SECONDS)
        await self._audit(ctx.task_id, "outcome_unknown", tool=ctx.tool, error=error)

    # ------------------------------------------------------------ helpers

    async def spent_paise(self, task_id: str) -> int:
        return int(await self.redis.get(f"ag:spend:{task_id}") or 0)

    async def _audit(self, task_id: str, event: str, **fields: str) -> None:
        entry = {"ts": f"{time.time():.3f}", "event": event, **fields}
        await self.redis.xadd(f"ag:audit:{task_id}", entry, maxlen=AUDIT_MAXLEN, approximate=True)
        await self.redis.sadd("ag:tasks", task_id)


# ---------------------------------------------------------------- approvals
# Used by scripts/approve.py (and later the Streamlit UI). Deliberately NOT
# exposed as an MCP tool: the agent must never be able to approve itself.

async def list_pending(redis: Redis) -> dict[str, dict[str, Any]]:
    raw = await redis.hgetall("ag:approvals:pending")
    return {_s(k): json.loads(v) for k, v in raw.items()}


async def decide(redis: Redis, aid: str, approve: bool, who: str = "human") -> dict[str, Any]:
    raw = await redis.hget("ag:approvals:pending", aid)
    if raw is None:
        raise KeyError(f"no pending approval with id {aid}")
    request = json.loads(raw)
    target = f"ag:approval:{'grant' if approve else 'rejected'}:{aid}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(target, who, ex=APPROVAL_TTL_SECONDS)
        pipe.hdel("ag:approvals:pending", aid)
        pipe.xadd(f"ag:audit:{request['task_id']}",
                  {"ts": f"{time.time():.3f}", "event": "approved" if approve else "rejected",
                   "tool": request["tool"], "approval_id": aid, "by": who},
                  maxlen=AUDIT_MAXLEN, approximate=True)
        await pipe.execute()
    return request


async def audit_lines(redis: Redis, task_id: str) -> list[str]:
    """Human-readable audit trail, one line per event."""
    lines = []
    for _, e in await redis.xrange(f"ag:audit:{task_id}"):
        e = {_s(k): _s(v) for k, v in e.items()}
        when = time.strftime("%H:%M:%S", time.localtime(float(e.pop("ts"))))
        event = e.pop("event")
        if event == "decision":
            lines.append(f"{when}  {e['verdict'].upper():<16} {e['tool']} {e['args']}  [{e['check']}] {e['reason']}")
        else:
            lines.append(f"{when}  {event.upper():<16} " + " ".join(f"{k}={v}" for k, v in e.items()))
    return lines


def _inr(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


def _s(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value

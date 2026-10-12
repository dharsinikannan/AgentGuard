import asyncio

import pytest

from agentguard.guard import CallContext, Guard, Verdict, call_hash, decide, list_pending
from agentguard.policy import Policy


def ctx(tool="cloud__get_logs", args=None, task="t1", key=None) -> CallContext:
    return CallContext(task_id=task, tool=tool, args=args or {"service": "api"}, idempotency_key=key)


async def test_allowed_read(guard):
    d = await guard.check(ctx())
    assert d.verdict is Verdict.ALLOW


async def test_policy_denies_unlisted_tool(guard):
    d = await guard.check(ctx(tool="cloud__delete_iam_role"))
    assert (d.verdict, d.check) == (Verdict.DENY, "policy")


def test_call_hash_ignores_key_order():
    assert call_hash("t", {"a": 1, "b": 2}) == call_hash("t", {"b": 2, "a": 1})
    assert call_hash("t", {"a": 1}) != call_hash("t", {"a": 2})


def test_task_id_cannot_inject_redis_key_separators():
    with pytest.raises(ValueError):
        CallContext(task_id="t1:other", tool="x", args={})


# ---------------------------------------------------------------- loop

async def test_loop_blocks_fourth_identical_call(guard):
    verdicts = [(await guard.check(ctx())).verdict for _ in range(4)]
    assert verdicts == [Verdict.ALLOW] * 3 + [Verdict.DENY]


async def test_loop_counts_identical_args_only(guard):
    for _ in range(3):
        await guard.check(ctx(args={"service": "api"}))
    d = await guard.check(ctx(args={"service": "db"}))
    assert d.verdict is Verdict.ALLOW


async def test_loop_is_per_task(guard):
    for _ in range(3):
        await guard.check(ctx(task="t1"))
    assert (await guard.check(ctx(task="t2"))).verdict is Verdict.ALLOW


async def test_total_calls_cap(redis):
    g = Guard(redis, Policy.from_dict({
        "name": "t", "allow": ["*"], "loop": {"max_calls_per_task": 2},
    }))
    results = [(await g.check(ctx(args={"n": i}))).verdict for i in range(3)]
    assert results == [Verdict.ALLOW, Verdict.ALLOW, Verdict.DENY]


# -------------------------------------------------------------- budget

async def test_budget_blocks_and_does_not_charge_refused_call(guard):
    # ₹1000 budget, ₹250 per instance -> 4 fit, the 5th is refused.
    for i in range(4):
        d = await guard.check(ctx("cloud__create_instance", {"name": f"vm{i}"}))
        assert d.verdict is Verdict.ALLOW
    d = await guard.check(ctx("cloud__create_instance", {"name": "vm4"}))
    assert (d.verdict, d.check) == (Verdict.DENY, "budget")
    assert "₹1,000.00" in d.reason
    assert await guard.spent_paise("t1") == 100_000  # refused call was rolled back


async def test_budget_never_overspends_under_concurrency(guard):
    calls = [guard.check(ctx("cloud__create_instance", {"name": f"vm{i}"})) for i in range(20)]
    decisions = await asyncio.gather(*calls)
    allowed = sum(d.verdict is Verdict.ALLOW for d in decisions)
    assert allowed <= 4
    assert await guard.spent_paise("t1") == allowed * 25_000


# --------------------------------------------------------- idempotency

async def test_same_key_replays_stored_result(guard):
    c = ctx("cloud__create_instance", {"name": "vm1"}, key="k1")
    assert (await guard.check(c)).verdict is Verdict.ALLOW
    await guard.record_result(c, {"content": [{"type": "text", "text": "i-123"}]}, is_error=False)

    d = await guard.check(c)
    assert d.verdict is Verdict.REPLAY
    assert d.cached_result["content"][0]["text"] == "i-123"
    assert await guard.spent_paise("t1") == 25_000  # replay is free


async def test_concurrent_duplicates_only_one_wins(guard):
    c = ctx("cloud__create_instance", {"name": "vm1"}, key="k1")
    decisions = await asyncio.gather(*[guard.check(c) for _ in range(10)])
    assert sum(d.verdict is Verdict.ALLOW for d in decisions) == 1
    assert all(d.check == "idempotency" for d in decisions if d.verdict is not Verdict.ALLOW)


async def test_key_reuse_with_different_args_is_refused(guard):
    await guard.check(ctx("cloud__create_instance", {"name": "vm1"}, key="k1"))
    d = await guard.check(ctx("cloud__create_instance", {"name": "vm2"}, key="k1"))
    assert (d.verdict, d.check) == (Verdict.DENY, "idempotency")
    assert "different" in d.reason


async def test_unknown_outcome_is_never_retried(guard):
    c = ctx("cloud__create_instance", {"name": "vm1"}, key="k1")
    await guard.check(c)
    await guard.record_unknown(c, "connection reset")
    d = await guard.check(c)
    assert (d.verdict, d.check) == (Verdict.DENY, "idempotency")
    assert "unknown" in d.reason


async def test_later_denial_releases_idempotency_claim(redis):
    # Budget refuses the call -> the key must not stay "in_progress" forever.
    g = Guard(redis, Policy.from_dict({
        "name": "t", "allow": ["*"], "tool_costs": {"default": 10}, "budget_inr": 5,
    }))
    c = ctx(key="k1")
    assert (await g.check(c)).check == "budget"
    assert await redis.get("ag:idem:t1:k1") is None


# ------------------------------------------------------------ approval

async def test_approval_flow_is_single_use_and_bound_to_args(guard, redis):
    c = ctx("cloud__delete_bucket", {"bucket": "old-logs"})
    d = await guard.check(c)
    assert d.verdict is Verdict.PENDING_APPROVAL
    assert d.approval_id in await list_pending(redis)
    assert await guard.spent_paise("t1") == 0  # pending call reserved nothing

    await decide(redis, d.approval_id, approve=True)

    # Different args: the approval does not transfer.
    other = await guard.check(ctx("cloud__delete_bucket", {"bucket": "prod-data"}))
    assert other.verdict is Verdict.PENDING_APPROVAL

    assert (await guard.check(c)).verdict is Verdict.ALLOW
    # Single use: repeating the deletion needs a fresh approval.
    assert (await guard.check(c)).verdict is Verdict.PENDING_APPROVAL


async def test_rejected_approval_denies(guard, redis):
    c = ctx("cloud__delete_bucket", {"bucket": "old-logs"})
    d = await guard.check(c)
    await decide(redis, d.approval_id, approve=False)
    d2 = await guard.check(c)
    assert (d2.verdict, d2.check) == (Verdict.DENY, "approval")


# --------------------------------------------------------------- audit

async def test_every_decision_is_audited(guard, redis):
    await guard.check(ctx())
    await guard.check(ctx(tool="cloud__delete_iam_role"))
    entries = await redis.xrange("ag:audit:t1")
    assert [e[1]["verdict"] for e in entries] == ["allow", "deny"]

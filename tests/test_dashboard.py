"""Milestone 9 dashboard: data functions, and the public-demo flow driven like a visitor."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import agentguard.insights as insights
from agentguard.guard import CallContext, Guard, decide, list_pending
from agentguard.insights import overview, sync_call, task_summary
from agentguard.policy import Policy

UI = Path(__file__).resolve().parent.parent / "ui"


@pytest.fixture
def demo(monkeypatch):
    monkeypatch.setenv("AGENTGUARD_DEMO", "1")
    monkeypatch.setattr(insights, "_demo_server", None)     # a fresh in-memory store per test


async def test_summaries_count_verdicts_and_split_spend(redis):
    guard = Guard(redis, Policy.from_dict({"name": "t", "allow": ["x__*"], "tool_costs": {"default": 2},
                                            "budget_inr": 100}))
    await guard.check(CallContext("t1", "x__read", {}, user="alice"))
    await guard.check(CallContext("t1", "y__nope", {}))
    await redis.set("ag:spend:t1:llm", 50)
    await redis.incrby("ag:spend:t1", 50)
    s = await task_summary(redis, "t1")
    o = await overview(redis)
    assert (s["owner"], s["allowed"], s["blocked"]) == ("alice", 1, 1)
    assert (s["spend_tools_inr"], s["spend_llm_inr"]) == (2.0, 0.5)
    assert o["blocked_by_check"] == {"policy": 1} and o["tasks"][0]["task"] == "t1"


def test_demo_mode_shares_one_in_memory_store(demo):
    async def write(redis):
        await redis.set("k", "v")

    async def read(redis):
        return await redis.get("k")

    sync_call(write)
    assert sync_call(read) == "v"          # separate clients and event loops, same data


def test_visitor_runs_incident_decides_and_retries(demo):
    app = AppTest.from_file(str(UI / "try_it.py"), default_timeout=60)
    app.run()
    app.button[0].click().run()            # 1. run the incident
    assert not app.exception
    verdicts = list(app.dataframe[0].value["verdict"])
    assert verdicts.count("pending_approval") == 3
    assert {"deny", "replay", "allow"} <= set(verdicts)

    pending = sync_call(list_pending)
    by_tool = {(r["tool"], r["args"].get("service", r["args"].get("bucket"))): aid for aid, r in pending.items()}
    sync_call(decide, by_tool[("cloud__delete_bucket", "old-logs-2023")], True, who="visitor")
    sync_call(decide, by_tool[("cloud__scale_service", "db")], False, who="visitor")

    app.button[1].click().run()            # 3. retry what needed a human
    after = list(app.dataframe[1].value["verdict"])
    assert after == ["pending_approval", "deny", "allow"]   # undecided, rejected, approved


def test_overview_renders_after_activity(demo):
    try_it = AppTest.from_file(str(UI / "try_it.py"), default_timeout=60)
    try_it.run()
    try_it.button[0].click().run()
    page = AppTest.from_file(str(UI / "overview.py"), default_timeout=30)
    page.run()
    assert not page.exception
    labels = {m.label: m.value for m in page.metric}
    assert labels["Tasks"] == "1" and int(labels["Blocked"]) >= 3

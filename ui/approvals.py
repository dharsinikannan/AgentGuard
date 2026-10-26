"""AgentGuard approvals console: the human side of require_approval.

    streamlit run ui/approvals.py          (REDIS_URL, default redis://localhost:6379/0)

Talks to Redis directly and is never exposed as an MCP tool: the agent has no
path to it, so it cannot approve its own actions. Each approval is bound to one
task + tool + exact arguments and is single-use (see agentguard/guard.py).

Binds to 127.0.0.1 (.streamlit/config.toml) because it has no login yet; the
approver name is self-declared. Real deployments put it behind SSO.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import time

import streamlit as st
from redis.asyncio import Redis

from agentguard.guard import audit_lines, decide, list_pending

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def redis_call(fn, *args, **kwargs):
    """Streamlit is synchronous; the guard's helpers are async. One short-lived
    client per call, so nothing is shared across Streamlit's script reruns."""
    async def go():
        redis = Redis.from_url(REDIS_URL, decode_responses=True)
        try:
            return await fn(redis, *args, **kwargs)
        finally:
            await redis.aclose()
    return asyncio.run(go())


async def _tasks(redis: Redis) -> list[str]:
    return sorted(await redis.smembers("ag:tasks"), reverse=True)


async def _spent(redis: Redis, task_id: str) -> float:
    return int(await redis.get(f"ag:spend:{task_id}") or 0) / 100


st.set_page_config(page_title="AgentGuard approvals", page_icon="🛡️", layout="wide")
st.title("🛡️ AgentGuard approvals")
approver = st.sidebar.text_input("Your name (recorded in the audit trail)", value=getpass.getuser())
st.sidebar.caption(f"Redis: `{REDIS_URL}`")


@st.fragment(run_every="2s")
def pending_panel() -> None:
    try:
        pending = redis_call(list_pending)
    except Exception as e:  # Redis down: say so instead of a stack trace
        st.error(f"Cannot reach Redis at {REDIS_URL}: {e}")
        return
    st.subheader(f"Waiting for a decision ({len(pending)})")
    if not pending:
        st.caption("Nothing to approve. Paused agents show up here within 2 seconds.")
    for aid, req in sorted(pending.items(), key=lambda kv: kv[1]["requested_at"]):
        with st.container(border=True):
            left, right = st.columns([4, 1])
            age = int(time.time() - req["requested_at"])
            left.markdown(f"**`{req['tool']}`** · task `{req['task_id']}` · waiting {age}s · id `{aid}`")
            left.code(json.dumps(req["args"], indent=2), language="json")
            if right.button("✅ Approve", key=f"approve-{aid}", use_container_width=True):
                _decide(aid, True)
            if right.button("❌ Reject", key=f"reject-{aid}", use_container_width=True):
                _decide(aid, False)


def _decide(aid: str, approve: bool) -> None:
    try:
        req = redis_call(decide, aid, approve, who=f"ui:{approver or 'unknown'}")
        st.toast(f"{'Approved' if approve else 'Rejected'} {req['tool']} for {req['task_id']}")
    except KeyError as e:  # someone else decided first, or it expired
        st.toast(str(e).strip("'"))


@st.fragment(run_every="2s")
def task_panel() -> None:
    tasks = redis_call(_tasks)
    if not tasks:
        return
    st.subheader("Audit trail")
    task = st.selectbox("Task", tasks, key="task")
    st.metric("Spent", f"₹{redis_call(_spent, task):,.2f}")
    st.code("\n".join(redis_call(audit_lines, task)) or "(no events)", language=None)


pending_panel()
task_panel()

"""Audit: one task's full, ordered trail (Redis Stream ag:audit:{task})."""

from __future__ import annotations

import streamlit as st

from agentguard.guard import audit_lines
from agentguard.insights import sync_call, task_summary


async def _tasks(redis) -> list[str]:
    return sorted(await redis.smembers("ag:tasks"), reverse=True)


st.title("📜 Audit trail")


@st.fragment(run_every="3s")
def trail() -> None:
    tasks = sync_call(_tasks)
    if not tasks:
        st.caption("No tasks yet.")
        return
    task = st.selectbox("Task", tasks, key="audit-task")
    s = sync_call(task_summary, task)
    cols = st.columns(4)
    cols[0].metric("Owner", s["owner"])
    cols[1].metric("Allowed / blocked / waiting", f"{s['allowed']} / {s['blocked']} / {s['pending']}")
    cols[2].metric("Tools ₹", f"{s['spend_tools_inr']:,.2f}")
    cols[3].metric("LLM ₹", f"{s['spend_llm_inr']:,.2f}", help=f"{s['llm_calls']} LLM calls")
    st.code("\n".join(sync_call(audit_lines, task)) or "(no events)", language=None)


trail()
